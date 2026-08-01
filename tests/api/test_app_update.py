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


def test_version_key_ordering():
    assert app_update._version_key("0.10.0") > app_update._version_key("0.9.9")
    assert app_update._version_key("1.0.0") > app_update._version_key("0.99.0")
    assert app_update._version_key("0.2.0") == app_update._version_key("0.2.0")


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
    # 数据库文件存在 → 切换前自动备份
    db = Path(get_settings().database_url.split(":///", 1)[1])
    db.write_bytes(b"sqlite-data")
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
    assert backups[0].read_bytes() == b"sqlite-data"


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

    # 再次回退 = 撤销回退，切回新版
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
