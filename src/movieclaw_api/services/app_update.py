"""应用内更新服务（docs/design/in-app-update.md M3）。

职责与流程：
- 检查：查 GitHub 最新 Release → 下载 manifest.json → 与当前版本、镜像
  runtime 版本比对，告诉用户「可更新 / 已最新 / 需升级镜像」；
- 执行：后台线程完成 下载 → sha256 逐文件校验 → 解包到 versions/<ver> →
  备份 SQLite → 原子切换 current/previous 符号链接 → 以约定码 43 触发
  前后端全量重启（entrypoint 重新解析代码来源后从新版本拉起）；
- 回退：current 与 previous 互换（可再切回来），无 previous 时删除 current
  回落镜像基线；同样以 43 重启。

安全设计：
- 更新从不覆盖镜像内文件，只在 data 卷的 updates/ 目录落盘并切换指针，
  任何一步失败都不影响正在运行的版本（详见 entrypoint 的解析与兜底）；
- sha256 校验是强制项，其信任锚点是 manifest：未配置签名公钥时 manifest
  固定直连 GitHub（只有大文件走加速镜像），恶意镜像无法自洽伪造；
  配置 UPDATE_MANIFEST_PUBKEY 后 manifest 强制验签，此时可整体走镜像；
- 解包用 tarfile 的 data 过滤器，杜绝路径穿越；
- 切换前用 sqlite3 backup API 在线备份数据库（WAL 下直接拷文件会丢
  未 checkpoint 的事务；迁移是单向的，回退跨版本时靠备份恢复数据）。

只有 Docker entrypoint 环境才支持应用内更新（据 MOVIECLAW_RUNTIME_VERSION
环境变量判定）：源码部署的用户直接 git pull 即可，无需这套机制。
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import tarfile
import threading
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

import httpx
from sqlmodel import select

from movieclaw_api import __version__
from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import BadRequestException
from movieclaw_api.schemas.app_update import (
    ModelUpdateCheckView,
    UpdateCheckView,
    UpdateProgressView,
    UpdateStatusView,
)
from movieclaw_api.services.app_config import FULL_RESTART_EXIT_CODE, schedule_restart
from movieclaw_api.services.system_notice import resolve_notices, upsert_notice
from movieclaw_db.engine import get_database
from movieclaw_db.models import NoticeSeverity, NoticeStatus, SystemNotice
from movieclaw_db.models.scheduled_task import TriggerType
from movieclaw_scheduler import register_task

logger = logging.getLogger("movieclaw_api.app_update")

# Release 产物文件名（与 scripts/build-release-artifacts.sh 的产出一致）
_ARTIFACT_NAMES = ("app-backend.tar.gz", "app-web.tar.gz")
_MANIFEST_NAME = "manifest.json"
# 数据库备份保留份数（SQLite 单文件，成本低，多留几份）
_BACKUP_KEEP = 5
_HTTP_TIMEOUT = httpx.Timeout(30.0, connect=15.0)


# ---------------------------------------------------------------------------
# 路径与环境
# ---------------------------------------------------------------------------


def _updates_dir() -> Path:
    return Path(get_settings().updates_dir).resolve()


def _runtime_version() -> int | None:
    """镜像的运行时版本；非 Docker entrypoint 环境（源码部署/开发）为 None。"""
    raw = os.environ.get("MOVIECLAW_RUNTIME_VERSION", "")
    return int(raw) if raw.isdigit() else None


def _sanitize(version: str) -> str:
    """与 entrypoint 的 sanitize 同款：版本号转文件名安全形式。"""
    return re.sub(r"[^A-Za-z0-9._-]", "_", version)


_VERSION_RE = re.compile(r"^(\d+(?:\.\d+)*)(?:-([0-9A-Za-z.\-]+))?(?:\+.*)?$")


def _is_newer(candidate: str, current: str) -> bool:
    """candidate 是否比 current 新（semver 语义的够用子集）。

    规则：数字主段逐段比较；带预发布段（-beta.1 等）的版本比同主段的正式版
    旧；预发布段按点分段、数字段按数值比（beta.10 > beta.9，semver 语义）。
    任何一侧无法解析时退化为「不相等即视为更新」——绝不抛异常（版本命名
    不规范不能把检查更新整个打挂）。
    """

    def parse(text: str) -> tuple[tuple[int, ...], tuple] | None:
        match = _VERSION_RE.match(text.strip())
        if match is None:
            return None
        nums = tuple(int(p) for p in match.group(1).split("."))
        pre = match.group(2)
        if not pre:
            # 正式版排在同主段的任何预发布之后：(1,) > (0, …)
            return nums, (1,)
        # 数字段标 (0, 数值)、非数字段标 (1, 字符串)：同位仅同类比较，不混型
        pre_key = tuple(
            (0, int(seg)) if seg.isdigit() else (1, seg) for seg in pre.split(".")
        )
        return nums, (0, pre_key)

    a, b = parse(candidate), parse(current)
    if a is None or b is None:
        return candidate.strip() != current.strip()
    # 主段长度对齐（1.2 与 1.2.0 等价）
    width = max(len(a[0]), len(b[0]))
    a_nums = a[0] + (0,) * (width - len(a[0]))
    b_nums = b[0] + (0,) * (width - len(b[0]))
    return (a_nums, a[1]) > (b_nums, b[1])


# ---------------------------------------------------------------------------
# 进度状态（模块级单例，后台线程写、接口读）
# ---------------------------------------------------------------------------


@dataclass
class _Progress:
    phase: str = "idle"
    detail: str = ""
    percent: float | None = None
    error: str | None = None
    target_version: str | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def set(
        self,
        phase: str,
        detail: str = "",
        *,
        percent: float | None = None,
        error: str | None = None,
        target_version: str | None = ...,  # type: ignore[assignment]
    ) -> None:
        with self.lock:
            self.phase = phase
            self.detail = detail
            self.percent = percent
            self.error = error
            if target_version is not ...:
                self.target_version = target_version

    def view(self) -> UpdateProgressView:
        with self.lock:
            return UpdateProgressView(
                phase=self.phase,
                detail=self.detail,
                percent=self.percent,
                error=self.error,
                target_version=self.target_version,
            )

    def busy(self) -> bool:
        with self.lock:
            return self.phase in ("checking", "downloading", "verifying", "applying", "restarting")


_progress = _Progress()


def get_progress() -> UpdateProgressView:
    return _progress.view()


def reset_progress_for_tests() -> None:
    """测试隔离用：把进度状态复位为 idle。"""
    _progress.set("idle", target_version=None)


# ---------------------------------------------------------------------------
# 状态
# ---------------------------------------------------------------------------


def _is_marked_bad(version: str) -> bool:
    return (_updates_dir() / "state" / f"bad-{_sanitize(version)}").is_file()


def _overlay_state(vdir: Path) -> tuple[str, bool, str]:
    """判定一个 overlay 版本目录能否被 entrypoint 采用（与其校验口径一致）。

    返回 (版本号, 是否可用, 不可用原因的中文说明)。status 的 has_previous、
    rollback 的目标校验、previous 指针的候选校验都用它——「能回退/会生效」
    的承诺必须以 entrypoint 真实会接受为准，否则用户会经历一次什么都没变的
    全量重启。
    """
    manifest_path = vdir / _MANIFEST_NAME
    if not vdir.is_dir() or not manifest_path.is_file():
        return "", False, "缺少更新清单"
    try:
        manifest = json.loads(manifest_path.read_bytes())
        version = str(manifest["version"])
        requires = int(manifest["requires_runtime"])
    except Exception:
        return "", False, "更新清单损坏"
    runtime = _runtime_version()
    if runtime is not None and requires != runtime:
        return version, False, f"需要 runtime {requires}（当前镜像为 {runtime}），需升级镜像"
    if _is_marked_bad(version):
        return version, False, "曾连续启动失败，已被自动回落保护"
    return version, True, ""


def build_status() -> UpdateStatusView:
    updates = _updates_dir()
    state_dir = updates / "state"
    bad_versions = sorted(
        p.name.removeprefix("bad-") for p in state_dir.glob("bad-*")
    ) if state_dir.is_dir() else []
    previous = updates / "previous"
    has_previous = previous.is_symlink() and _overlay_state(Path(previous))[1]

    # 盘上 current 指向的版本与实际运行版本不一致时，把「为什么没生效」外显：
    # 否则 runtime 不匹配 / bad 回落场景下，用户昨天还在的版本会凭空消失
    running_overlay = os.environ.get("MOVIECLAW_OVERLAY_VERSION") or None
    inactive_version: str | None = None
    inactive_reason: str | None = None
    current = updates / "current"
    if current.is_symlink():
        version, usable, reason = _overlay_state(Path(current))
        if version and version != running_overlay:
            inactive_version = version
            inactive_reason = reason if not usable else "尚未生效（等待应用重启）"

    return UpdateStatusView(
        current_version=__version__,
        code_source=os.environ.get("MOVIECLAW_CODE_SOURCE", "dev"),
        overlay_version=running_overlay,
        runtime_version=_runtime_version(),
        can_update=_runtime_version() is not None,
        has_previous=has_previous,
        bad_versions=list(bad_versions),
        model_tag=_current_model_tag(),
        inactive_overlay_version=inactive_version,
        inactive_overlay_reason=inactive_reason,
    )


# ---------------------------------------------------------------------------
# 检查更新
# ---------------------------------------------------------------------------


def _mirror_url(url: str) -> str:
    """按配置给下载地址套上加速镜像前缀（ghproxy 风格）。"""
    mirror = get_settings().update_download_mirror.strip()
    if not mirror:
        return url
    return mirror.rstrip("/") + "/" + url


_SIGNATURE_NAME = "manifest.json.sig"


def _verify_manifest_signature(raw: bytes, sig_text: bytes) -> None:
    """用配置的 Ed25519 公钥校验更新清单签名（base64 签名，签的是清单原始字节）。

    仅在 UPDATE_MANIFEST_PUBKEY 配置时被调用；校验失败抛 BadRequest（中文直达用户）。
    """
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

    try:
        pubkey = Ed25519PublicKey.from_public_bytes(
            base64.b64decode(get_settings().update_manifest_pubkey.strip())
        )
    except (binascii.Error, ValueError) as exc:
        raise BadRequestException(
            "UPDATE_MANIFEST_PUBKEY 配置无效（应为 base64 的 32 字节 Ed25519 公钥），"
            "签名校验无法进行"
        ) from exc
    try:
        pubkey.verify(base64.b64decode(sig_text.strip()), raw)
    except (InvalidSignature, binascii.Error, ValueError) as exc:
        raise BadRequestException(
            "更新清单的签名校验失败，发布内容可能被篡改，已放弃本次更新。"
            "请改为直连 GitHub 重试，并向项目反馈"
        ) from exc


async def _download_manifest_bytes(client: httpx.AsyncClient, release: dict) -> bytes:
    """下载 Release 的更新清单；配置了签名公钥时强制验签后才返回。

    信任模型：清单是产物 sha256 的信任锚点，若也走第三方加速镜像下载，
    恶意镜像可同时伪造清单与产物、sha256 自洽通过。因此**未配置签名公钥时
    清单固定直连 GitHub**（体积小，直连通常可行），只有大文件走镜像；
    配置了公钥后签名接管防篡改，清单也允许走镜像加速。
    """
    signed = bool(get_settings().update_manifest_pubkey.strip())
    manifest_asset = next(
        (a for a in release.get("assets", []) if a.get("name") == _MANIFEST_NAME), None
    )
    if manifest_asset is None:
        raise BadRequestException(
            f"发布（{release.get('tag_name', '?')}）没有携带更新清单，"
            "可能是旧版发布或发布流程未完成，暂无法应用内更新"
        )

    async def fetch(url: str) -> bytes:
        resp = await client.get(_mirror_url(url) if signed else url)
        resp.raise_for_status()
        return resp.content

    try:
        raw = await fetch(manifest_asset["browser_download_url"])
    except httpx.HTTPError as exc:
        raise BadRequestException(
            "下载更新清单失败。为防篡改，未配置签名公钥时清单固定直连 GitHub 获取——"
            "直连不可达时可配置代理，或配置 UPDATE_MANIFEST_PUBKEY 签名校验后走加速镜像"
        ) from exc
    if signed:
        sig_asset = next(
            (a for a in release.get("assets", []) if a.get("name") == _SIGNATURE_NAME), None
        )
        if sig_asset is None:
            raise BadRequestException(
                f"本实例要求更新清单携带签名，但发布（{release.get('tag_name', '?')}）"
                f"缺少 {_SIGNATURE_NAME}，已放弃本次更新"
            )
        try:
            sig_raw = await fetch(sig_asset["browser_download_url"])
        except httpx.HTTPError as exc:
            raise BadRequestException("下载更新清单签名失败，请稍后重试") from exc
        _verify_manifest_signature(raw, sig_raw)
    return raw


def _parse_manifest(raw: bytes) -> dict:
    try:
        manifest = json.loads(raw)
        version = str(manifest["version"])
        requires = int(manifest["requires_runtime"])
        files = manifest["files"]
        for name in _ARTIFACT_NAMES:
            if not files[name]["sha256"]:
                raise KeyError(name)
    except Exception as exc:
        raise BadRequestException(
            "更新清单（manifest.json）格式异常，可能是发布产物不完整，请稍后重试或反馈问题"
        ) from exc
    return {"version": version, "requires_runtime": requires, "files": files, "raw": raw}


# GitHub 条件请求缓存（ETag → 上次的 Release 列表）：命中 304 时 GitHub
# **不计入**未认证配额（60 次/小时/IP），这是每小时轮询不挤兑同出口 IP
# 用户配额的关键——绝大多数轮询都是 304，只有真发了新版才付一次完整请求。
_releases_etag: str | None = None
_releases_cached: list[dict] | None = None


def reset_release_cache_for_tests() -> None:
    global _releases_etag, _releases_cached
    _releases_etag = None
    _releases_cached = None


async def _fetch_releases(what: str) -> list[dict]:
    """拉全部 Release 列表（应用与模型的 Release 混在同一个仓库里，
    必须列表过滤，绝不能用 /releases/latest——模型发布会把 latest 顶掉）。"""
    global _releases_etag, _releases_cached
    settings = get_settings()
    api_url = f"{settings.update_api_base_url}/repos/{settings.update_repo}/releases?per_page=100"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "movieclaw-updater"}
    if _releases_etag and _releases_cached is not None:
        headers["If-None-Match"] = _releases_etag
    async with httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT, follow_redirects=True, headers=headers
    ) as client:
        try:
            resp = await client.get(api_url)
            if resp.status_code == 304 and _releases_cached is not None:
                return _releases_cached
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise BadRequestException(
                f"无法连接 GitHub 检查{what}，"
                "请确认网络可达（可配置代理或 UPDATE_API_BASE_URL 反代）"
            ) from exc
        releases = resp.json()
        etag = resp.headers.get("etag")
        if etag:
            _releases_etag = etag
            _releases_cached = releases
        return releases


_APP_TAG_RE = re.compile(r"^v\d+(?:\.\d+)*(?:[-+].*)?$")


def _latest_app_release(releases: list[dict]) -> dict | None:
    """从 Release 列表挑出版本号最大的应用发布（tag 形如 vX.Y.Z；
    跳过草稿/预发布，也天然跳过模型发布的 torrent-ner-vN）。"""
    latest: dict | None = None
    for release in releases:
        tag = release.get("tag_name") or ""
        if release.get("draft") or release.get("prerelease"):
            continue
        if not _APP_TAG_RE.match(tag):
            continue
        if latest is None or _is_newer(tag[1:], (latest.get("tag_name") or "v")[1:]):
            latest = release
    return latest


async def _fetch_latest_release() -> tuple[dict, dict]:
    """取最新应用 Release 与其 manifest。返回 (release_json, manifest_dict)。"""
    releases = await _fetch_releases("更新")
    release = _latest_app_release(releases)
    if release is None:
        raise BadRequestException("未找到任何应用发布（vX.Y.Z 的 Release），暂无可用更新")
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "movieclaw-updater"}
    async with httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT, follow_redirects=True, headers=headers
    ) as client:
        raw = await _download_manifest_bytes(client, release)
    manifest = _parse_manifest(raw)
    # 清单与 Release tag 强一致（与模型侧对称）：防「旧签名清单挂到新 tag」
    # 的重放，也防手工发布时张冠李戴——下载 URL 是按清单 version 拼的
    tag = release.get("tag_name") or ""
    if tag != f"v{manifest['version']}":
        raise BadRequestException(
            f"发布（{tag}）的更新清单声明的是 v{manifest['version']}，内容不匹配，已放弃"
        )
    return release, manifest


async def check_update() -> UpdateCheckView:
    release, manifest = await _fetch_latest_release()
    latest = manifest["version"]
    runtime = _runtime_version()
    return UpdateCheckView(
        current_version=__version__,
        latest_version=latest,
        update_available=_is_newer(latest, __version__),
        compatible=runtime is not None and manifest["requires_runtime"] == runtime,
        requires_runtime=manifest["requires_runtime"],
        changelog=release.get("body") or "",
        published_at=release.get("published_at") or "",
        latest_known_bad=_is_marked_bad(latest),
    )


# ---------------------------------------------------------------------------
# 执行更新（后台线程）
# ---------------------------------------------------------------------------


def _atomic_symlink(target: Path, link: Path) -> None:
    """原子地把 link 指向 target：先建临时链接再 rename 替换。"""
    tmp = link.with_name(link.name + ".tmp")
    if tmp.is_symlink() or tmp.exists():
        tmp.unlink()
    tmp.symlink_to(target)
    os.replace(tmp, link)


def _verify_sha256(path: Path, expected: str) -> None:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    if digest.hexdigest() != expected:
        raise RuntimeError(
            f"{path.name} 的校验和不匹配（下载可能被篡改或不完整）。"
            "若使用了加速镜像，请更换镜像或改为直连后重试"
        )


def _extract(tar_path: Path, dest: Path) -> None:
    """解包 tar.gz；data 过滤器拒绝路径穿越/设备文件等危险成员。"""
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path, "r:gz") as tar:
        tar.extractall(dest, filter="data")


def _validate_layout(version_dir: Path) -> None:
    """与 entrypoint 的完整性校验同款：关键入口文件缺一不可。"""
    for rel in (
        "backend/src/movieclaw_api/main.py",
        "backend/alembic.ini",
        "backend/src/movieclaw_cli/data/spec.json",
        "web/apps/web/server.js",
    ):
        if not (version_dir / rel).is_file():
            raise RuntimeError(f"更新产物解包后布局异常（缺少 {rel}），已放弃本次更新")


def _backup_database(updates: Path) -> None:
    """切换版本前备份 SQLite（非 SQLite 部署跳过）。迁移单向，备份是回退跨
    版本时恢复数据的唯一通道。

    必须用 sqlite3 的 backup API 而不是拷文件：数据库开着 WAL，已提交但
    未 checkpoint 的事务都在 -wal 文件里，直接拷主文件必然丢这部分数据，
    撞上 checkpoint 还可能拷出损坏文件。备份失败视为致命，中止本次更新。
    """
    import sqlite3

    url = get_settings().database_url
    if "sqlite" not in url or ":///" not in url:
        logger.info("当前数据库不是 SQLite，跳过更新前自动备份")
        return
    db_path = Path(url.split(":///", 1)[1])
    if not db_path.is_file():
        return
    backup_dir = updates / "backup"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    target = backup_dir / f"movieclaw-v{_sanitize(__version__)}-{stamp}.db"
    try:
        src = sqlite3.connect(db_path)
        try:
            dst = sqlite3.connect(target)
            try:
                src.backup(dst)
            finally:
                dst.close()
        finally:
            src.close()
    except sqlite3.Error as exc:
        target.unlink(missing_ok=True)
        raise RuntimeError(
            f"更新前的数据库自动备份失败（{exc}），为保护数据已中止本次更新"
        ) from exc
    logger.info("已在更新前备份数据库：%s", target)
    backups = sorted(backup_dir.glob("movieclaw-*.db"), key=lambda p: p.stat().st_mtime)
    for old in backups[:-_BACKUP_KEEP]:
        old.unlink(missing_ok=True)


def _apply_downloaded(manifest: dict, download_dir: Path) -> None:
    """校验、解包并切换到已下载的版本（下载完成后的纯本地流程，可独立测试）。

    download_dir 内应有 manifest["files"] 声明的全部产物文件。
    """
    version = manifest["version"]
    updates = _updates_dir()
    versions_dir = updates / "versions"
    final_dir = versions_dir / f"v{_sanitize(version)}"
    partial_dir = versions_dir / f"v{_sanitize(version)}.partial"

    _progress.set("verifying", "正在校验下载文件的完整性……", target_version=version)
    for name in _ARTIFACT_NAMES:
        _verify_sha256(download_dir / name, manifest["files"][name]["sha256"])

    _progress.set("applying", "正在解包并安装新版本……", target_version=version)
    shutil.rmtree(partial_dir, ignore_errors=True)
    _extract(download_dir / "app-backend.tar.gz", partial_dir / "backend")
    _extract(download_dir / "app-web.tar.gz", partial_dir / "web")
    (partial_dir / _MANIFEST_NAME).write_bytes(manifest["raw"])
    _validate_layout(partial_dir)

    # 重新安装同一版本时清掉历史的坏标记/失败计数（用户显式重试即既往不咎）
    marker = _sanitize(version)
    (updates / "state" / f"bad-{marker}").unlink(missing_ok=True)
    (updates / "state" / f"failures-{marker}").unlink(missing_ok=True)

    _backup_database(updates)

    # 落定版本目录后原子切换指针：previous ← **实际运行中的版本**，current ← 新版本。
    # 不能简单取 readlink(current)：current 可能指向一个已被标 bad、实际并未在
    # 运行的版本（entrypoint 回落了 previous），把它记成 previous 会让「回退」
    # 名存实亡，还会把真正在运行的目录当垃圾清掉。
    shutil.rmtree(final_dir, ignore_errors=True)
    os.replace(partial_dir, final_dir)
    current = updates / "current"
    prev_target: Path | None = None
    running = os.environ.get("MOVIECLAW_OVERLAY_VERSION")
    if running:
        running_dir = versions_dir / f"v{_sanitize(running)}"
        if running_dir.is_dir() and running_dir.name != final_dir.name:
            prev_target = running_dir
    if prev_target is None and current.is_symlink():
        cur_target = Path(os.readlink(current))
        # 旧 current 只有在 entrypoint 真会采用（runtime 匹配且未标坏）时才配
        # 当 previous——把不可用的旧版本记成「可回退目标」只会制造无效回退
        if cur_target.name != final_dir.name and _overlay_state(cur_target)[1]:
            prev_target = cur_target
    if prev_target is not None:
        _atomic_symlink(prev_target, updates / "previous")
    _atomic_symlink(final_dir, current)

    # 只保留 current/previous 引用与运行中的版本目录，更早的清掉（磁盘不无限增长）
    referenced = set()
    for link in (current, updates / "previous"):
        if link.is_symlink():
            referenced.add(Path(os.readlink(link)).name)
    if running:
        referenced.add(f"v{_sanitize(running)}")
    for vdir in versions_dir.iterdir():
        if vdir.is_dir() and vdir.name not in referenced:
            shutil.rmtree(vdir, ignore_errors=True)
    # 状态标记跟着版本目录走：目录已清理的版本，其 bad/failures 标记一并清掉
    state_dir = updates / "state"
    if state_dir.is_dir():
        remaining = {v.name for v in versions_dir.iterdir() if v.is_dir()}
        for marker in list(state_dir.glob("bad-*")) + list(state_dir.glob("failures-*")):
            marked_version = marker.name.split("-", 1)[1]
            if f"v{marked_version}" not in remaining:
                marker.unlink(missing_ok=True)
    logger.info("应用内更新已就绪：v%s，即将全量重启生效", version)


def _download_and_apply(manifest: dict) -> None:
    """后台线程主体：下载产物 → _apply_downloaded → 触发全量重启。"""
    version = manifest["version"]
    settings = get_settings()
    updates = _updates_dir()
    tmp_dir = updates / "tmp"
    try:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        base = (
            f"https://github.com/{settings.update_repo}/releases/download/v{version}"
        )
        total = sum(int(manifest["files"][n].get("size") or 0) for n in _ARTIFACT_NAMES)
        done = 0
        with httpx.Client(
            timeout=_HTTP_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "movieclaw-updater"},
        ) as client:
            for name in _ARTIFACT_NAMES:
                _progress.set(
                    "downloading",
                    f"正在下载 {name}……",
                    percent=(done / total * 100) if total else None,
                    target_version=version,
                )
                url = _mirror_url(f"{base}/{name}")
                with client.stream("GET", url) as resp:
                    resp.raise_for_status()
                    with (tmp_dir / name).open("wb") as fh:
                        for chunk in resp.iter_bytes(1024 * 256):
                            fh.write(chunk)
                            done += len(chunk)
                            if total:
                                _progress.set(
                                    "downloading",
                                    f"正在下载 {name}……",
                                    percent=min(done / total * 100, 100.0),
                                    target_version=version,
                                )
        _apply_downloaded(manifest, tmp_dir)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        _progress.set(
            "restarting",
            f"新版本 v{version} 安装完成，正在重启应用……",
            target_version=version,
        )
        schedule_restart(FULL_RESTART_EXIT_CODE)
    except httpx.HTTPError as exc:
        logger.warning("应用内更新下载失败：%s", exc)
        _progress.set(
            "failed",
            "",
            error="下载更新产物失败，请检查网络（可配置加速镜像 UPDATE_DOWNLOAD_MIRROR）后重试",
            target_version=version,
        )
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception as exc:
        logger.exception("应用内更新失败")
        _progress.set("failed", "", error=str(exc), target_version=version)
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def start_update() -> UpdateProgressView:
    """发起一次更新：前置校验后交给后台线程执行，立即返回当前进度。"""
    if _runtime_version() is None:
        raise BadRequestException(
            "当前部署形态不支持应用内更新（仅 Docker 镜像部署支持；源码部署请用 git pull 更新）"
        )
    if _progress.busy():
        raise BadRequestException("已有更新正在进行中，请等待其完成")
    _progress.set("checking", "正在获取最新版本信息……", target_version=None)
    try:
        _release, manifest = await _fetch_latest_release()
        if not _is_newer(manifest["version"], __version__):
            raise BadRequestException(f"当前已是最新版本（v{__version__}），无需更新")
        if manifest["requires_runtime"] != _runtime_version():
            raise BadRequestException(
                f"新版本 v{manifest['version']} 包含运行时依赖变化"
                f"（需要 runtime {manifest['requires_runtime']}，"
                f"当前镜像为 {_runtime_version()}），请升级 Docker 镜像完成本次更新"
            )
    except BaseException:
        _progress.set("idle", target_version=None)
        raise
    thread = threading.Thread(
        target=_download_and_apply, args=(manifest,), name="app-update", daemon=True
    )
    thread.start()
    return _progress.view()


# ---------------------------------------------------------------------------
# NER 模型更新（独立于代码更新：模型 Release 单独发布，tag 形如 torrent-ner-vN；
# 更新只切 data 卷上的 current 指针，之后走 43 全量重启让 entrypoint
# 重新解析模型指针——42 不重解析 MOVIECLAW_NER_DIR，新模型不会生效）
# ---------------------------------------------------------------------------

_MODEL_TAG_RE = re.compile(r"^torrent-ner-v(\d+)$")
_MODEL_FILES = ("model.int8.onnx", "tokenizer.json", "labels.json")


def _models_dir() -> Path:
    return Path(get_settings().models_dir).resolve()


def _current_model_tag() -> str | None:
    """当前生效模型的 Release tag：读模型目录里的 .release-tag 记录。

    镜像构建与应用内安装都会写这个文件；老镜像没有该记录时返回 None
    （UI 显示「无法识别」，检查更新按「可更新」处理）。
    """
    ner_dir = os.environ.get("MOVIECLAW_NER_DIR", "")
    tag_file = Path(ner_dir) / ".release-tag" if ner_dir else None
    if tag_file and tag_file.is_file():
        tag = tag_file.read_text(encoding="utf-8").strip()
        return tag or None
    return None


def _model_tag_num(tag: str | None) -> int:
    match = _MODEL_TAG_RE.match(tag or "")
    return int(match.group(1)) if match else 0


def _latest_model_release(releases: list[dict]) -> dict | None:
    """从 Release 列表挑出 tag 版本号最大的模型发布。"""
    candidates = [r for r in releases if _MODEL_TAG_RE.match(r.get("tag_name") or "")]
    if not candidates:
        return None
    return max(candidates, key=lambda r: _model_tag_num(r["tag_name"]))


def _parse_model_manifest(raw: bytes) -> dict:
    try:
        manifest = json.loads(raw)
        files = manifest["files"]
        for name in _MODEL_FILES:
            if not files[name]["sha256"]:
                raise KeyError(name)
    except Exception as exc:
        raise BadRequestException(
            "模型更新清单（manifest.json）格式异常，请稍后重试或反馈问题"
        ) from exc
    return {"files": files, "raw": raw, "tag": manifest.get("tag") or ""}


async def _fetch_latest_model_release() -> dict:
    release = _latest_model_release(await _fetch_releases("模型更新"))
    if release is None:
        raise BadRequestException("未找到任何模型发布（torrent-ner-v* 的 Release）")
    return release


def _model_manifest_asset(release: dict) -> dict | None:
    return next(
        (a for a in release.get("assets", []) if a.get("name") == _MANIFEST_NAME), None
    )


async def check_model_update() -> ModelUpdateCheckView:
    release = await _fetch_latest_model_release()
    latest_tag = release["tag_name"]
    current = _current_model_tag()
    return ModelUpdateCheckView(
        current_tag=current,
        latest_tag=latest_tag,
        update_available=_model_tag_num(latest_tag) > _model_tag_num(current),
        installable=_model_manifest_asset(release) is not None,
        published_at=release.get("published_at") or "",
    )


def _install_model_files(tag: str, manifest: dict, download_dir: Path) -> None:
    """校验并安装已下载的模型文件（纯本地流程，可独立测试）：
    落盘到 models_dir/<tag>/ → 写 .release-tag → 原子切 current 指针 → 清旧目录。"""
    for name in _MODEL_FILES:
        _verify_sha256(download_dir / name, manifest["files"][name]["sha256"])
    models = _models_dir()
    final_dir = models / _sanitize(tag)
    partial_dir = models / f"{_sanitize(tag)}.partial"
    shutil.rmtree(partial_dir, ignore_errors=True)
    partial_dir.mkdir(parents=True)
    for name in _MODEL_FILES:
        shutil.move(str(download_dir / name), partial_dir / name)
    (partial_dir / ".release-tag").write_text(tag + "\n", encoding="utf-8")
    shutil.rmtree(final_dir, ignore_errors=True)
    os.replace(partial_dir, final_dir)
    _atomic_symlink(final_dir, models / "current")
    # 清掉旧模型目录，但保护**当前进程正在用**的那个（MOVIECLAW_NER_DIR 指向
    # 的目录）：距离重启生效还有约 11 秒窗口，删了它会让窗口内的 NER 推理
    # 因文件缺失而失败；留下的这一个目录会在下次模型更新时被清理
    active = os.environ.get("MOVIECLAW_NER_DIR", "")
    active_dir = Path(active).resolve() if active else None
    for entry in models.iterdir():
        if entry.is_dir() and not entry.is_symlink() and entry.name != final_dir.name:
            if active_dir is not None and entry.resolve() == active_dir:
                continue
            shutil.rmtree(entry, ignore_errors=True)
    logger.info("NER 模型已安装：%s，即将重启后端生效", tag)


def _download_and_apply_model(release: dict, manifest: dict) -> None:
    """后台线程主体：下载模型文件 → 安装 → 全量重启生效（43，见下方注释）。"""
    tag = release["tag_name"]
    assets = {a.get("name"): a for a in release.get("assets", [])}
    tmp_dir = _models_dir() / "tmp"
    try:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        tmp_dir.mkdir(parents=True, exist_ok=True)
        total = sum(int(manifest["files"][n].get("size") or 0) for n in _MODEL_FILES)
        done = 0
        with httpx.Client(
            timeout=_HTTP_TIMEOUT,
            follow_redirects=True,
            headers={"User-Agent": "movieclaw-updater"},
        ) as client:
            for name in _MODEL_FILES:
                asset = assets.get(name)
                if asset is None:
                    raise RuntimeError(f"模型发布 {tag} 缺少文件 {name}，无法安装")
                _progress.set(
                    "downloading",
                    f"正在下载模型文件 {name}……",
                    percent=(done / total * 100) if total else None,
                    target_version=tag,
                )
                with client.stream("GET", _mirror_url(asset["browser_download_url"])) as resp:
                    resp.raise_for_status()
                    with (tmp_dir / name).open("wb") as fh:
                        for chunk in resp.iter_bytes(1024 * 256):
                            fh.write(chunk)
                            done += len(chunk)
                            if total:
                                _progress.set(
                                    "downloading",
                                    f"正在下载模型文件 {name}……",
                                    percent=min(done / total * 100, 100.0),
                                    target_version=tag,
                                )
        _progress.set("verifying", "正在校验模型文件的完整性……", target_version=tag)
        _install_model_files(tag, manifest, tmp_dir)
        shutil.rmtree(tmp_dir, ignore_errors=True)
        _progress.set(
            "restarting",
            f"模型 {tag} 安装完成，正在重启应用生效……",
            target_version=tag,
        )
        # 必须走 43 全量重启：MOVIECLAW_NER_DIR 是 entrypoint 解析模型指针后
        # 导出的**具体版本目录**路径，只有重新走一遍 resolve 才会指向新模型；
        # 42 只拉起后端、不重解析，新模型不会生效（旧目录还已被清理）。
        schedule_restart(FULL_RESTART_EXIT_CODE)
    except httpx.HTTPError as exc:
        logger.warning("模型更新下载失败：%s", exc)
        _progress.set(
            "failed",
            "",
            error="下载模型文件失败，请检查网络（可配置加速镜像 UPDATE_DOWNLOAD_MIRROR）后重试",
            target_version=tag,
        )
        shutil.rmtree(tmp_dir, ignore_errors=True)
    except Exception as exc:
        logger.exception("模型更新失败")
        _progress.set("failed", "", error=str(exc), target_version=tag)
        shutil.rmtree(tmp_dir, ignore_errors=True)


async def start_model_update() -> UpdateProgressView:
    """发起一次模型更新：前置校验后交给后台线程执行，立即返回当前进度。"""
    if _runtime_version() is None:
        raise BadRequestException(
            "当前部署形态不支持应用内更新模型（仅 Docker 镜像部署支持）"
        )
    if _progress.busy():
        raise BadRequestException("已有更新正在进行中，请等待其完成")
    _progress.set("checking", "正在获取最新模型信息……", target_version=None)
    try:
        release = await _fetch_latest_model_release()
        tag = release["tag_name"]
        if _model_tag_num(tag) <= _model_tag_num(_current_model_tag()):
            raise BadRequestException(f"当前模型已是最新（{_current_model_tag()}），无需更新")
        if _model_manifest_asset(release) is None:
            raise BadRequestException(
                f"模型发布 {tag} 未携带更新清单（manifest.json），暂无法应用内安装"
            )
        headers = {"User-Agent": "movieclaw-updater"}
        async with httpx.AsyncClient(
            timeout=_HTTP_TIMEOUT, follow_redirects=True, headers=headers
        ) as client:
            raw = await _download_manifest_bytes(client, release)
        manifest = _parse_model_manifest(raw)
        # 清单声明了 tag 时必须与 Release tag 一致：开启验签后这一致性把
        # 「用旧签名清单重放成新版本」的路径也堵死（清单未声明 tag 时跳过，
        # 兼容早期模型发布）
        if manifest.get("tag") and manifest["tag"] != tag:
            raise BadRequestException(
                f"模型发布 {tag} 的更新清单声明的是 {manifest['tag']}，内容不匹配，已放弃安装"
            )
    except BaseException:
        _progress.set("idle", target_version=None)
        raise
    thread = threading.Thread(
        target=_download_and_apply_model,
        args=(release, manifest),
        name="model-update",
        daemon=True,
    )
    thread.start()
    return _progress.view()


# ---------------------------------------------------------------------------
# 定时自动检查（只提醒、绝不自动安装：更新会重启服务，须由用户主动触发）
# ---------------------------------------------------------------------------

_NOTICE_KEY_APP = "app_update:new-version"
_NOTICE_KEY_MODEL = "app_update:new-model"


async def _relight_dismissed_on_change(session, dedupe_key: str, field: str, value: str) -> None:
    """用户 dismiss 的是「某个具体新版本」的提醒，不是「永远别提醒我更新」：
    再有更新的版本发布时，把沉默的旧提醒清场，让 upsert 以新内容重新点亮。"""
    row = (
        await session.execute(select(SystemNotice).where(SystemNotice.dedupe_key == dedupe_key))
    ).scalar_one_or_none()
    if (
        row is not None
        and row.status == NoticeStatus.DISMISSED.value
        and (row.payload or {}).get(field) != value
    ):
        await resolve_notices(session, dedupe_key=dedupe_key)


@register_task(
    "check_app_update",
    title="检查应用更新",
    trigger_type=TriggerType.INTERVAL,
    interval_seconds=3600,
    description=(
        "每小时检查 GitHub 是否发布了新版本与新的 NER 模型；发现后在待处理事项中"
        "提醒（不会自动安装，更新入口在「设置 → 关于与更新」）。检查用 ETag 条件"
        "请求，无新版时不消耗 GitHub API 配额。仅 Docker 部署生效。"
    ),
)
async def check_app_update_task() -> None:
    if _runtime_version() is None:
        return  # 源码部署/开发环境不适用应用内更新
    db = get_database()
    # 应用与模型两条检查相互独立：一条网络失败不拖累另一条；
    # 网络不可达按「下轮再试」处理，不产生告警（避免离线环境天天报错）
    try:
        view = await check_update()
    except Exception as exc:
        # 兜住一切异常（含 GitHub 返回异常结构导致的解析错误）：
        # 定时检查失败只记日志、下轮再试，绝不让任务本身报故障打扰用户
        logger.info("定时检查应用更新未完成：%s", exc)
    else:
        async with db.session() as session:
            # 曾在本机连续启动失败被回落的版本不再提醒（提醒用户安装刚坑过
            # 自己的版本是误导）；等更新的版本发布后自然恢复提醒
            if view.update_available and not view.latest_known_bad:
                await _relight_dismissed_on_change(
                    session, _NOTICE_KEY_APP, "latest_version", view.latest_version
                )
                await upsert_notice(
                    session,
                    dedupe_key=_NOTICE_KEY_APP,
                    severity=NoticeSeverity.WARNING,
                    source="app",
                    title=f"发现新版本 v{view.latest_version}",
                    message=(
                        f"movieclaw v{view.latest_version} 已发布（当前 v{view.current_version}）。"
                        + (
                            "到「设置 → 关于与更新」一键更新即可，无需重拉镜像。"
                            if view.compatible
                            else "本次更新包含运行时依赖变化，需要拉取新的 Docker 镜像升级。"
                        )
                    ),
                    payload={"latest_version": view.latest_version, "compatible": view.compatible},
                )
            else:
                await resolve_notices(session, dedupe_key=_NOTICE_KEY_APP)
    try:
        model = await check_model_update()
    except Exception as exc:
        logger.info("定时检查模型更新未完成：%s", exc)
    else:
        async with db.session() as session:
            if model.update_available and model.installable:
                await _relight_dismissed_on_change(
                    session, _NOTICE_KEY_MODEL, "latest_tag", model.latest_tag
                )
                await upsert_notice(
                    session,
                    dedupe_key=_NOTICE_KEY_MODEL,
                    severity=NoticeSeverity.WARNING,
                    source="app",
                    title=f"发现新的识别模型 {model.latest_tag}",
                    message=(
                        f"种子名识别（NER）模型 {model.latest_tag} 已发布。"
                        "到「设置 → 关于与更新」一键更新即可（更新后应用会自动重启）。"
                    ),
                    payload={"latest_tag": model.latest_tag},
                )
            else:
                await resolve_notices(session, dedupe_key=_NOTICE_KEY_MODEL)


# ---------------------------------------------------------------------------
# 启动首查（lifespan 拉起）：NAS 用户重启容器/设备很常见，启动后几分钟就
# 检查一次，不用等下一个整点周期才第一次感知到新版
# ---------------------------------------------------------------------------

_STARTUP_CHECK_DELAY_SECONDS = 180
_startup_check_task: asyncio.Task | None = None


def start_startup_check() -> None:
    """排定启动后的更新首查（由 lifespan 在调度器启动后调用）。

    延迟几分钟再查：把启动窗口让给迁移、扫库等更要紧的初始化，也避免
    容器被 restart 策略反复拉起时高频打 GitHub。非 Docker 部署不排定。
    """
    global _startup_check_task
    if _runtime_version() is None:
        return
    _startup_check_task = asyncio.get_running_loop().create_task(_startup_check())


async def _startup_check() -> None:
    try:
        await asyncio.sleep(_STARTUP_CHECK_DELAY_SECONDS)
        await check_app_update_task()
    except asyncio.CancelledError:
        raise
    except Exception:
        # 首查失败无所谓：每小时的定时任务会继续兜着
        logger.info("启动后的更新首查未完成（定时任务会继续检查）", exc_info=True)


async def close_startup_check() -> None:
    """停机时取消未执行完的首查任务（lifespan 关闭阶段调用）。"""
    global _startup_check_task
    if _startup_check_task is not None:
        _startup_check_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await _startup_check_task
        _startup_check_task = None


# ---------------------------------------------------------------------------
# 回退
# ---------------------------------------------------------------------------


def rollback() -> str:
    """回退到上一版本（current/previous 互换，可再切回）；无上一版本时
    删除 current 回落镜像基线。返回给用户的中文说明。"""
    if _runtime_version() is None:
        raise BadRequestException("当前部署形态不支持应用内回退（仅 Docker 镜像部署支持）")
    if _progress.busy():
        raise BadRequestException("已有更新正在进行中，无法回退")
    updates = _updates_dir()
    current = updates / "current"
    previous = updates / "previous"
    if current.exists() and not current.is_symlink():
        raise BadRequestException(
            "updates/current 不是符号链接（可能被手工改动过），"
            "请删除 data/updates/current 后重启容器回到镜像内置版本"
        )
    # 回退目标必须是 entrypoint 真实会采用的版本（runtime 匹配且未标坏）：
    # 否则用户会经历一次什么都没变的全量重启。不可用的 previous 视同不存在。
    if previous.is_symlink() and _overlay_state(Path(previous))[1]:
        prev_target = Path(os.readlink(previous))
        if current.is_symlink():
            cur_target = Path(os.readlink(current))
            _atomic_symlink(prev_target, current)
            _atomic_symlink(cur_target, previous)
        else:
            _atomic_symlink(prev_target, current)
            previous.unlink(missing_ok=True)
        message = "已切换到上一版本，应用正在重启……如需撤销可再次回退切回"
    elif current.is_symlink() and os.environ.get("MOVIECLAW_OVERLAY_VERSION"):
        current.unlink()
        message = "没有可用的更早版本，已回落到镜像内置版本，应用正在重启……"
    else:
        raise BadRequestException("当前运行的就是镜像内置版本，没有可回退的目标")
    logger.info("应用回退：%s", message)
    # 占住 busy 态：重启前的窗口（响应缓冲 + 优雅停机最长约 11 秒）内
    # 拒绝并发的更新/再回退请求，与更新线程置 restarting 的做法对称
    _progress.set("restarting", "正在回退并重启应用……", target_version=None)
    schedule_restart(FULL_RESTART_EXIT_CODE)
    return message
