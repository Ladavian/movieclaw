"""应用内更新服务测试（docs/design/in-app-update.md M3）。

重点覆盖安全性质：sha256 不匹配/路径穿越/布局异常时绝不切换版本指针，
切换与回退的符号链接操作正确且可互换，非 Docker 部署形态拒绝更新。
网络部分（GitHub API）不在此测——那是薄封装，错误路径已转成中文提示。
"""

from __future__ import annotations

import hashlib
import io
import json
import tarfile
from pathlib import Path

import pytest

from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import BadRequestException
from movieclaw_api.services import app_update


@pytest.fixture
def updates_dir(tmp_path, monkeypatch):
    """把更新目录隔离到临时目录，并模拟 Docker entrypoint 环境。"""
    updates = tmp_path / "updates"
    monkeypatch.setenv("MOVIECLAW_UPDATES_DIR", str(updates))
    monkeypatch.setenv("MOVIECLAW_RUNTIME_VERSION", "1")
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'app.db'}")
    get_settings.cache_clear()
    app_update.reset_progress_for_tests()
    yield updates
    get_settings.cache_clear()


@pytest.fixture
def no_restart(monkeypatch):
    """拦截重启调度（否则测试进程会被优雅停机干掉），并记录调用。"""
    calls: list[int] = []
    monkeypatch.setattr(app_update, "schedule_restart", lambda code=42: calls.append(code))
    return calls


def _tar_bytes(members: dict[str, bytes]) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, content in members.items():
            info = tarfile.TarInfo(name)
            info.size = len(content)
            tar.addfile(info, io.BytesIO(content))
    return buf.getvalue()


def _make_manifest(download_dir: Path, version: str = "0.2.0") -> dict:
    """按 build-release-artifacts.sh 的产物布局伪造一套下载完成的文件。"""
    backend = _tar_bytes(
        {
            "src/movieclaw_api/main.py": b"# main",
            "src/movieclaw_cli/data/spec.json": b"{}",
            "alembic.ini": b"[alembic]",
            "alembic/env.py": b"# env",
        }
    )
    web = _tar_bytes({"apps/web/server.js": b"// server"})
    download_dir.mkdir(parents=True, exist_ok=True)
    (download_dir / "app-backend.tar.gz").write_bytes(backend)
    (download_dir / "app-web.tar.gz").write_bytes(web)
    files = {
        name: {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        for name, data in (("app-backend.tar.gz", backend), ("app-web.tar.gz", web))
    }
    raw = json.dumps(
        {"schema": 1, "version": version, "requires_runtime": 1, "files": files}
    ).encode()
    return {"version": version, "requires_runtime": 1, "files": files, "raw": raw}


# ---------------------------------------------------------------------------
# 版本比较
# ---------------------------------------------------------------------------


def test_is_newer_semver_ordering():
    assert app_update._is_newer("0.10.0", "0.9.9")
    assert app_update._is_newer("1.0.0", "0.99.0")
    assert not app_update._is_newer("0.2.0", "0.2.0")
    # 主段长度对齐：1.2 与 1.2.0 等价
    assert not app_update._is_newer("1.2", "1.2.0")
    # 预发布比同主段正式版旧（semver 语义）
    assert app_update._is_newer("0.2.0", "0.2.0-beta.1")
    assert not app_update._is_newer("0.2.0-beta.1", "0.2.0")
    assert app_update._is_newer("0.2.1-beta", "0.2.0")


def test_is_newer_never_raises_on_weird_versions():
    # 混合数字/非数字段、完全不规范的版本号：退化为「不相等即更新」，绝不抛异常
    assert app_update._is_newer("0.2.x", "0.2.0")
    assert app_update._is_newer("0.2.0-beta.1", "0.2.0-1") in (True, False)
    assert not app_update._is_newer("garbage", "garbage")


def test_latest_app_release_filters_model_and_prerelease():
    releases = [
        {"tag_name": "torrent-ner-v2"},  # 模型发布：绝不能被当成应用 latest
        {"tag_name": "v0.3.0-rc1", "prerelease": True},
        {"tag_name": "v0.2.5", "draft": True},
        {"tag_name": "v0.2.0"},
        {"tag_name": "v0.10.0"},
        {"tag_name": "v0.3.0"},
    ]
    assert app_update._latest_app_release(releases)["tag_name"] == "v0.10.0"
    assert app_update._latest_app_release([{"tag_name": "torrent-ner-v9"}]) is None


# ---------------------------------------------------------------------------
# 应用（下载后的本地流程）
# ---------------------------------------------------------------------------


def test_apply_switches_current_and_prunes(updates_dir, tmp_path):
    manifest = _make_manifest(tmp_path / "dl", "0.2.0")
    app_update._apply_downloaded(manifest, tmp_path / "dl")

    current = updates_dir / "current"
    assert current.is_symlink()
    assert Path(current).resolve().name == "v0.2.0"
    assert (current / "manifest.json").is_file()
    assert (current / "backend" / "src" / "movieclaw_api" / "main.py").is_file()
    assert (current / "web" / "apps" / "web" / "server.js").is_file()
    # 首次更新没有 previous
    assert not (updates_dir / "previous").exists()

    # 第二次更新：current → 新版本，previous → 旧版本，更早的版本被清理
    manifest2 = _make_manifest(tmp_path / "dl2", "0.3.0")
    app_update._apply_downloaded(manifest2, tmp_path / "dl2")
    manifest3 = _make_manifest(tmp_path / "dl3", "0.4.0")
    app_update._apply_downloaded(manifest3, tmp_path / "dl3")
    assert Path(updates_dir / "current").resolve().name == "v0.4.0"
    assert Path(updates_dir / "previous").resolve().name == "v0.3.0"
    assert not (updates_dir / "versions" / "v0.2.0").exists()


def test_apply_clears_bad_markers_and_backs_up_db(updates_dir, tmp_path):
    import sqlite3

    # 真实的 SQLite 库（备份走 sqlite3 backup API，假文件会被正确拒绝）
    db = Path(get_settings().database_url.split(":///", 1)[1])
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE t (v TEXT)")
    conn.execute("INSERT INTO t VALUES ('data-1')")
    conn.commit()
    conn.close()
    state = updates_dir / "state"
    state.mkdir(parents=True)
    (state / "bad-0.2.0").touch()
    (state / "failures-0.2.0").write_text("1\n")

    manifest = _make_manifest(tmp_path / "dl", "0.2.0")
    app_update._apply_downloaded(manifest, tmp_path / "dl")

    assert not (state / "bad-0.2.0").exists()
    assert not (state / "failures-0.2.0").exists()
    backups = list((updates_dir / "backup").glob("movieclaw-*.db"))
    assert len(backups) == 1
    # 备份是可打开的完整数据库，数据在
    check = sqlite3.connect(backups[0])
    assert check.execute("SELECT v FROM t").fetchall() == [("data-1",)]
    check.close()


def test_apply_aborts_when_db_backup_fails(updates_dir, tmp_path):
    # 数据库文件损坏（非 SQLite 格式）→ 备份失败必须中止更新，不切换版本
    db = Path(get_settings().database_url.split(":///", 1)[1])
    db.write_bytes(b"not-a-sqlite-file")
    manifest = _make_manifest(tmp_path / "dl", "0.2.0")
    with pytest.raises(RuntimeError, match="备份失败"):
        app_update._apply_downloaded(manifest, tmp_path / "dl")
    assert not (updates_dir / "current").exists()


def test_apply_prune_keeps_running_version(updates_dir, tmp_path, monkeypatch):
    """current 指向已标 bad 的 vB、实际运行 previous 的 vA 时更新 vC：
    previous 必须指向真正在运行的 vA（而非 bad 的 vB），vA 不能被清理。"""
    app_update._apply_downloaded(_make_manifest(tmp_path / "d1", "0.2.0"), tmp_path / "d1")
    app_update._apply_downloaded(_make_manifest(tmp_path / "d2", "0.3.0"), tmp_path / "d2")
    # 模拟 entrypoint 因 v0.3.0 坏而回落运行 v0.2.0
    monkeypatch.setenv("MOVIECLAW_OVERLAY_VERSION", "0.2.0")

    app_update._apply_downloaded(_make_manifest(tmp_path / "d3", "0.4.0"), tmp_path / "d3")
    assert Path(updates_dir / "current").resolve().name == "v0.4.0"
    assert Path(updates_dir / "previous").resolve().name == "v0.2.0"
    assert (updates_dir / "versions" / "v0.2.0").exists()


def test_apply_rejects_bad_checksum(updates_dir, tmp_path):
    manifest = _make_manifest(tmp_path / "dl", "0.2.0")
    manifest["files"]["app-web.tar.gz"]["sha256"] = "0" * 64
    with pytest.raises(RuntimeError, match="校验和不匹配"):
        app_update._apply_downloaded(manifest, tmp_path / "dl")
    assert not (updates_dir / "current").exists()


def test_apply_rejects_path_traversal(updates_dir, tmp_path):
    manifest = _make_manifest(tmp_path / "dl", "0.2.0")
    evil = _tar_bytes({"../evil.py": b"boom"})
    (tmp_path / "dl" / "app-backend.tar.gz").write_bytes(evil)
    manifest["files"]["app-backend.tar.gz"] = {
        "sha256": hashlib.sha256(evil).hexdigest(),
        "size": len(evil),
    }
    with pytest.raises(tarfile.TarError):
        app_update._apply_downloaded(manifest, tmp_path / "dl")
    assert not (updates_dir / "current").exists()
    assert not (tmp_path.parent / "evil.py").exists()


def test_apply_rejects_incomplete_layout(updates_dir, tmp_path):
    manifest = _make_manifest(tmp_path / "dl", "0.2.0")
    incomplete = _tar_bytes({"src/movieclaw_api/main.py": b"# main"})  # 缺 alembic/spec
    (tmp_path / "dl" / "app-backend.tar.gz").write_bytes(incomplete)
    manifest["files"]["app-backend.tar.gz"] = {
        "sha256": hashlib.sha256(incomplete).hexdigest(),
        "size": len(incomplete),
    }
    with pytest.raises(RuntimeError, match="布局异常"):
        app_update._apply_downloaded(manifest, tmp_path / "dl")
    assert not (updates_dir / "current").exists()


# ---------------------------------------------------------------------------
# 回退
# ---------------------------------------------------------------------------


def test_rollback_swaps_current_and_previous(updates_dir, tmp_path, no_restart):
    app_update._apply_downloaded(_make_manifest(tmp_path / "d1", "0.2.0"), tmp_path / "d1")
    app_update._apply_downloaded(_make_manifest(tmp_path / "d2", "0.3.0"), tmp_path / "d2")
    app_update.reset_progress_for_tests()  # 直调 _apply_downloaded 会停在 applying 态

    app_update.rollback()
    assert Path(updates_dir / "current").resolve().name == "v0.2.0"
    assert Path(updates_dir / "previous").resolve().name == "v0.3.0"
    assert no_restart == [app_update.FULL_RESTART_EXIT_CODE]
    # 回退后处于 restarting 占位（重启窗口内拒绝并发操作）
    with pytest.raises(BadRequestException, match="正在进行中"):
        app_update.rollback()

    # 再次回退 = 撤销回退，切回新版（真实场景中重启后进度自然复位）
    app_update.reset_progress_for_tests()
    app_update.rollback()
    assert Path(updates_dir / "current").resolve().name == "v0.3.0"


def test_rollback_without_previous_falls_back_to_baseline(updates_dir, tmp_path, no_restart):
    app_update._apply_downloaded(_make_manifest(tmp_path / "d1", "0.2.0"), tmp_path / "d1")
    app_update.reset_progress_for_tests()
    app_update.rollback()
    assert not (updates_dir / "current").exists()
    assert no_restart == [app_update.FULL_RESTART_EXIT_CODE]


def test_rollback_on_baseline_rejected(updates_dir, no_restart):
    with pytest.raises(BadRequestException, match="没有可回退"):
        app_update.rollback()
    assert no_restart == []


def test_rollback_rejected_outside_docker(updates_dir, monkeypatch, no_restart):
    monkeypatch.delenv("MOVIECLAW_RUNTIME_VERSION")
    with pytest.raises(BadRequestException, match="不支持"):
        app_update.rollback()


# ---------------------------------------------------------------------------
# 状态与更新前置校验
# ---------------------------------------------------------------------------


def test_build_status_reflects_env(updates_dir, monkeypatch):
    monkeypatch.setenv("MOVIECLAW_CODE_SOURCE", "overlay")
    monkeypatch.setenv("MOVIECLAW_OVERLAY_VERSION", "0.2.0")
    state = updates_dir / "state"
    state.mkdir(parents=True)
    (state / "bad-0.1.5").touch()
    status = app_update.build_status()
    assert status.code_source == "overlay"
    assert status.overlay_version == "0.2.0"
    assert status.runtime_version == 1
    assert status.can_update is True
    assert status.bad_versions == ["0.1.5"]


def test_build_status_dev_mode(updates_dir, monkeypatch):
    monkeypatch.delenv("MOVIECLAW_RUNTIME_VERSION")
    monkeypatch.delenv("MOVIECLAW_CODE_SOURCE", raising=False)
    status = app_update.build_status()
    assert status.code_source == "dev"
    assert status.can_update is False


@pytest.mark.asyncio
async def test_start_update_rejected_outside_docker(updates_dir, monkeypatch):
    monkeypatch.delenv("MOVIECLAW_RUNTIME_VERSION")
    with pytest.raises(BadRequestException, match="不支持应用内更新"):
        await app_update.start_update()


# ---------------------------------------------------------------------------
# NER 模型更新
# ---------------------------------------------------------------------------


@pytest.fixture
def models_dir(tmp_path, monkeypatch, updates_dir):
    models = tmp_path / "models"
    monkeypatch.setenv("MOVIECLAW_MODELS_DIR", str(models))
    get_settings.cache_clear()
    return models


def test_model_tag_ordering_and_latest_release():
    releases = [
        {"tag_name": "v0.2.0"},
        {"tag_name": "torrent-ner-v2"},
        {"tag_name": "torrent-ner-v10"},
        {"tag_name": "torrent-ner-v1"},
    ]
    latest = app_update._latest_model_release(releases)
    assert latest["tag_name"] == "torrent-ner-v10"
    assert app_update._latest_model_release([{"tag_name": "v1.0.0"}]) is None
    # 无法识别当前版本（老镜像无 tag 记录）时按 0 处理 → 一律视为可更新
    assert app_update._model_tag_num(None) == 0


def test_current_model_tag_reads_release_tag_file(tmp_path, monkeypatch):
    model = tmp_path / "ner"
    model.mkdir()
    monkeypatch.setenv("MOVIECLAW_NER_DIR", str(model))
    assert app_update._current_model_tag() is None
    (model / ".release-tag").write_text("torrent-ner-v1\n")
    assert app_update._current_model_tag() == "torrent-ner-v1"


def test_install_model_files_switches_pointer_and_prunes(models_dir, tmp_path):
    def make_download(tag: str) -> tuple[dict, Path]:
        dl = tmp_path / f"dl-{tag}"
        dl.mkdir()
        files = {}
        for name in app_update._MODEL_FILES:
            data = f"{tag}:{name}".encode()
            (dl / name).write_bytes(data)
            files[name] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
        return {"files": files}, dl

    manifest, dl = make_download("torrent-ner-v2")
    app_update._install_model_files("torrent-ner-v2", manifest, dl)
    current = models_dir / "current"
    assert current.is_symlink()
    assert Path(current).resolve().name == "torrent-ner-v2"
    assert (current / ".release-tag").read_text().strip() == "torrent-ner-v2"
    assert (current / "model.int8.onnx").is_file()

    # 安装更新版本后：指针切换、旧模型目录被清理
    manifest3, dl3 = make_download("torrent-ner-v3")
    app_update._install_model_files("torrent-ner-v3", manifest3, dl3)
    assert Path(current).resolve().name == "torrent-ner-v3"
    assert not (models_dir / "torrent-ner-v2").exists()


def test_install_model_files_rejects_bad_checksum(models_dir, tmp_path):
    dl = tmp_path / "dl"
    dl.mkdir()
    for name in app_update._MODEL_FILES:
        (dl / name).write_bytes(b"data")
    manifest = {
        "files": {name: {"sha256": "0" * 64, "size": 4} for name in app_update._MODEL_FILES}
    }
    with pytest.raises(RuntimeError, match="校验和不匹配"):
        app_update._install_model_files("torrent-ner-v2", manifest, dl)
    assert not (models_dir / "current").exists()


@pytest.mark.asyncio
async def test_start_model_update_rejected_outside_docker(models_dir, monkeypatch):
    monkeypatch.delenv("MOVIECLAW_RUNTIME_VERSION")
    with pytest.raises(BadRequestException, match="不支持应用内更新模型"):
        await app_update.start_model_update()


# ---------------------------------------------------------------------------
# 更新清单签名（可选启用：配置 UPDATE_MANIFEST_PUBKEY 后强制验签）
# ---------------------------------------------------------------------------


def _signing_pair() -> tuple[bytes, str]:
    """生成测试密钥对：返回（私钥对象可签名的 raw 清单签名函数用密钥, 公钥 base64）。"""
    import base64

    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
    from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat

    key = Ed25519PrivateKey.generate()
    pub_b64 = base64.b64encode(
        key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    ).decode()
    raw = b'{"schema":1}'
    sig_b64 = base64.b64encode(key.sign(raw))
    return raw, pub_b64, sig_b64  # type: ignore[return-value]


def test_manifest_signature_roundtrip(monkeypatch):

    raw, pub_b64, sig_b64 = _signing_pair()
    monkeypatch.setenv("UPDATE_MANIFEST_PUBKEY", pub_b64)
    get_settings.cache_clear()
    try:
        # 正确签名通过；尾随换行（脚本产物带 \n）也通过
        app_update._verify_manifest_signature(raw, sig_b64)
        app_update._verify_manifest_signature(raw, sig_b64 + b"\n")
        # 内容被篡改 → 拒绝
        with pytest.raises(BadRequestException, match="签名校验失败"):
            app_update._verify_manifest_signature(raw + b" ", sig_b64)
        # 签名本身是垃圾 → 拒绝
        with pytest.raises(BadRequestException, match="签名校验失败"):
            app_update._verify_manifest_signature(raw, b"not-base64!!")
        # 公钥配置无效 → 明确报配置错误
        monkeypatch.setenv("UPDATE_MANIFEST_PUBKEY", "bad key")
        get_settings.cache_clear()
        with pytest.raises(BadRequestException, match="配置无效"):
            app_update._verify_manifest_signature(raw, sig_b64)
    finally:
        get_settings.cache_clear()


# ---------------------------------------------------------------------------
# 更新提醒提速：ETag 条件请求 / 启动首查 / dismiss 后版本变化重点亮
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_fetch_releases_uses_etag_cache(updates_dir, monkeypatch):
    """第二次请求带 If-None-Match，304 时直接用缓存（不计 GitHub 配额）。"""
    import httpx

    calls: list[dict] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(dict(request.headers))
        if request.headers.get("if-none-match") == 'W/"abc"':
            return httpx.Response(304)
        return httpx.Response(
            200, json=[{"tag_name": "v0.2.0"}], headers={"ETag": 'W/"abc"'}
        )

    transport = httpx.MockTransport(handler)
    real_client = httpx.AsyncClient

    def patched_client(**kwargs):
        kwargs["transport"] = transport
        return real_client(**kwargs)

    monkeypatch.setattr(app_update.httpx, "AsyncClient", patched_client)
    app_update.reset_release_cache_for_tests()
    try:
        first = await app_update._fetch_releases("更新")
        second = await app_update._fetch_releases("更新")
    finally:
        app_update.reset_release_cache_for_tests()
    assert first == second == [{"tag_name": "v0.2.0"}]
    assert "if-none-match" not in calls[0]
    assert calls[1].get("if-none-match") == 'W/"abc"'


@pytest.mark.asyncio
async def test_startup_check_runs_after_delay(updates_dir, monkeypatch):
    ran = []

    async def fake_task():
        ran.append(True)

    monkeypatch.setattr(app_update, "_STARTUP_CHECK_DELAY_SECONDS", 0)
    monkeypatch.setattr(app_update, "check_app_update_task", fake_task)
    app_update.start_startup_check()
    assert app_update._startup_check_task is not None
    await app_update._startup_check_task
    await app_update.close_startup_check()
    assert ran == [True]


@pytest.mark.asyncio
async def test_startup_check_skipped_outside_docker(updates_dir, monkeypatch):
    monkeypatch.delenv("MOVIECLAW_RUNTIME_VERSION")
    app_update.start_startup_check()
    assert app_update._startup_check_task is None


class _StubResult:
    def __init__(self, row):
        self._row = row

    def scalar_one_or_none(self):
        return self._row


class _StubSession:
    def __init__(self, row):
        self._row = row

    async def execute(self, _query):
        return _StubResult(self._row)


class _StubNotice:
    def __init__(self, status: str, payload: dict):
        self.status = status
        self.payload = payload


@pytest.mark.asyncio
async def test_relight_dismissed_only_on_version_change(monkeypatch):
    from movieclaw_db.models import NoticeStatus

    resolved: list[str] = []

    async def fake_resolve(_session, *, dedupe_key=None, prefix=None):
        resolved.append(dedupe_key or prefix)
        return 1

    monkeypatch.setattr(app_update, "resolve_notices", fake_resolve)

    # 没有历史提醒 / dismiss 的就是当前版本：不动
    await app_update._relight_dismissed_on_change(_StubSession(None), "k", "v", "0.3.0")
    row_same = _StubNotice(NoticeStatus.DISMISSED.value, {"v": "0.3.0"})
    await app_update._relight_dismissed_on_change(_StubSession(row_same), "k", "v", "0.3.0")
    assert resolved == []

    # dismiss 的是旧版本、新版本到来：清场以便重新点亮
    row_old = _StubNotice(NoticeStatus.DISMISSED.value, {"v": "0.3.0"})
    await app_update._relight_dismissed_on_change(_StubSession(row_old), "k", "v", "0.4.0")
    assert resolved == ["k"]

    # 活跃（未 dismiss）的提醒版本变化：upsert 自己会刷新内容，不清场
    row_active = _StubNotice("active", {"v": "0.3.0"})
    await app_update._relight_dismissed_on_change(_StubSession(row_active), "k", "v", "0.4.0")
    assert resolved == ["k"]
