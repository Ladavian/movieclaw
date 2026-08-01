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
- sha256 校验是强制项：用户可能走第三方加速镜像下载，被篡改的产物过不了
  与 GitHub 官方 manifest 的比对；
- 解包用 tarfile 的 data 过滤器，杜绝路径穿越；
- 切换前自动备份 SQLite（迁移是单向的，回退跨版本时可从备份恢复）。

只有 Docker entrypoint 环境才支持应用内更新（据 MOVIECLAW_RUNTIME_VERSION
环境变量判定）：源码部署的用户直接 git pull 即可，无需这套机制。
"""

from __future__ import annotations

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

from movieclaw_api import __version__
from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import BadRequestException
from movieclaw_api.schemas.app_update import (
    UpdateCheckView,
    UpdateProgressView,
    UpdateStatusView,
)
from movieclaw_api.services.app_config import FULL_RESTART_EXIT_CODE, schedule_restart

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


def _version_key(version: str) -> tuple:
    """版本比较键：数字段按整数比，非数字段按字符串比（足以覆盖 x.y.z）。"""
    return tuple(int(p) if p.isdigit() else p for p in re.split(r"[.\-+]", version))


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


def build_status() -> UpdateStatusView:
    updates = _updates_dir()
    state_dir = updates / "state"
    bad_versions = sorted(
        p.name.removeprefix("bad-") for p in state_dir.glob("bad-*")
    ) if state_dir.is_dir() else []
    previous = updates / "previous"
    return UpdateStatusView(
        current_version=__version__,
        code_source=os.environ.get("MOVIECLAW_CODE_SOURCE", "dev"),
        overlay_version=os.environ.get("MOVIECLAW_OVERLAY_VERSION") or None,
        runtime_version=_runtime_version(),
        can_update=_runtime_version() is not None,
        has_previous=previous.exists() and (previous / _MANIFEST_NAME).is_file(),
        bad_versions=list(bad_versions),
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


async def _fetch_latest_release() -> tuple[dict, dict]:
    """取最新 Release 与其 manifest。返回 (release_json, manifest_dict)。"""
    settings = get_settings()
    api_url = f"{settings.update_api_base_url}/repos/{settings.update_repo}/releases/latest"
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "movieclaw-updater"}
    async with httpx.AsyncClient(
        timeout=_HTTP_TIMEOUT, follow_redirects=True, headers=headers
    ) as client:
        try:
            resp = await client.get(api_url)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise BadRequestException(
                "无法连接 GitHub 检查更新，请确认网络可达（可配置代理或 UPDATE_API_BASE_URL 反代）"
            ) from exc
        release = resp.json()
        manifest_asset = next(
            (a for a in release.get("assets", []) if a.get("name") == _MANIFEST_NAME), None
        )
        if manifest_asset is None:
            raise BadRequestException(
                f"最新 Release（{release.get('tag_name', '?')}）没有携带更新清单，"
                "可能是旧版发布或发布流程未完成，暂无法应用内更新"
            )
        try:
            resp = await client.get(_mirror_url(manifest_asset["browser_download_url"]))
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise BadRequestException("下载更新清单失败，请稍后重试") from exc
        return release, _parse_manifest(resp.content)


async def check_update() -> UpdateCheckView:
    release, manifest = await _fetch_latest_release()
    latest = manifest["version"]
    runtime = _runtime_version()
    return UpdateCheckView(
        current_version=__version__,
        latest_version=latest,
        update_available=_version_key(latest) > _version_key(__version__),
        compatible=runtime is not None and manifest["requires_runtime"] == runtime,
        requires_runtime=manifest["requires_runtime"],
        changelog=release.get("body") or "",
        published_at=release.get("published_at") or "",
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
    版本时恢复数据的唯一通道。"""
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
    shutil.copy2(db_path, target)
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

    # 落定版本目录后原子切换指针：previous ← 现 current，current ← 新版本
    shutil.rmtree(final_dir, ignore_errors=True)
    os.replace(partial_dir, final_dir)
    current = updates / "current"
    if current.is_symlink():
        _atomic_symlink(Path(os.readlink(current)), updates / "previous")
    _atomic_symlink(final_dir, current)

    # 只保留 current/previous 引用的版本目录，更早的清掉（磁盘不无限增长）
    referenced = set()
    for link in (current, updates / "previous"):
        if link.is_symlink():
            referenced.add(Path(os.readlink(link)).name)
    for vdir in versions_dir.iterdir():
        if vdir.is_dir() and vdir.name not in referenced:
            shutil.rmtree(vdir, ignore_errors=True)
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
        if _version_key(manifest["version"]) <= _version_key(__version__):
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
    if previous.is_symlink() and (previous / _MANIFEST_NAME).is_file():
        prev_target = Path(os.readlink(previous))
        if current.is_symlink():
            cur_target = Path(os.readlink(current))
            _atomic_symlink(prev_target, current)
            _atomic_symlink(cur_target, previous)
        else:
            _atomic_symlink(prev_target, current)
            previous.unlink(missing_ok=True)
        message = "已切换到上一版本，应用正在重启……如需撤销可再次回退切回"
    elif current.is_symlink():
        current.unlink()
        message = "没有更早的更新版本，已回落到镜像内置版本，应用正在重启……"
    else:
        raise BadRequestException("当前运行的就是镜像内置版本，没有可回退的目标")
    logger.info("应用回退：%s", message)
    schedule_restart(FULL_RESTART_EXIT_CODE)
    return message
