"""应用设置接口测试：读写配置、校验、重启调度与启动端口解析。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_api.services import app_config
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.settings.app_server import resolve_boot_port
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
    # 保证「端口是否被环境变量钉死」的判定可控
    monkeypatch.delenv("APP_PORT", raising=False)
    monkeypatch.delenv("MOVIECLAW_RUNTIME_PORT", raising=False)
    get_settings.cache_clear()
    reset_setting_store()
    reset_secret_box()
    reset_auth_state()

    from movieclaw_api.app import create_app

    app = create_app()
    with TestClient(app) as c:
        c.post(
            "/api/v1/auth/bootstrap",
            json={"username": "admin", "password": "s3cret-pass"},
        )
        yield c

    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()


def test_get_config_returns_defaults(client):
    resp = client.get("/api/v1/app/config")
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["port"] == 0
    assert data["external_url"] == ""
    assert data["default_port"] == 8000
    assert data["port_env_locked"] is False
    assert data["restart_required"] is False


def test_save_port_flags_restart_required(client):
    resp = client.put(
        "/api/v1/app/config",
        json={"port": 12345, "external_url": ""},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["port"] == 12345
    # 保存的端口 ≠ 当前监听端口 → 需要重启
    assert data["restart_required"] is True
    # 重新读取还原一致
    assert client.get("/api/v1/app/config").json()["data"]["port"] == 12345


def test_save_external_url_strips_trailing_slash(client):
    resp = client.put(
        "/api/v1/app/config",
        json={"port": 0, "external_url": "http://192.168.1.10:3000/"},
    )
    assert resp.status_code == 200
    data = resp.json()["data"]
    assert data["external_url"] == "http://192.168.1.10:3000"
    # 端口未设置 → 无需重启
    assert data["restart_required"] is False


def test_save_rejects_reserved_port(client):
    resp = client.put("/api/v1/app/config", json={"port": 80, "external_url": ""})
    assert resp.status_code == 400
    assert "1024~65535" in resp.json()["message"]


def test_save_rejects_bad_external_url(client):
    resp = client.put(
        "/api/v1/app/config",
        json={"port": 0, "external_url": "192.168.1.10:3000"},
    )
    assert resp.status_code == 400
    assert "http(s)" in resp.json()["message"]


def test_env_locked_port_ignores_db_setting(client, monkeypatch):
    """APP_PORT 环境变量钉死端口时（Docker 部署），设置页端口不再触发重启提示。"""
    client.put("/api/v1/app/config", json={"port": 12345, "external_url": ""})
    monkeypatch.setenv("APP_PORT", "8000")
    data = client.get("/api/v1/app/config").json()["data"]
    assert data["port_env_locked"] is True
    assert data["restart_required"] is False


def test_restart_schedules_graceful_exit(client, monkeypatch):
    """重启接口只调度、不阻塞响应；此处替换调度函数以免真把测试进程杀掉。"""
    calls: list[bool] = []
    monkeypatch.setattr(app_config, "schedule_restart", lambda: calls.append(True))
    resp = client.post("/api/v1/app/restart")
    assert resp.status_code == 200
    assert calls == [True]


def test_graceful_exit_prefers_should_exit_over_signal(monkeypatch):
    """已注册 Server 实例时：置 should_exit（signal-free）而不发 SIGTERM。"""

    class FakeServer:
        should_exit = False

    fake = FakeServer()
    monkeypatch.setattr(app_config, "_uvicorn_server", fake)
    monkeypatch.setattr(app_config, "_restart_requested", False)
    signals: list[int] = []
    monkeypatch.setattr(app_config.os, "kill", lambda pid, sig: signals.append(sig))

    app_config._request_graceful_exit()
    assert fake.should_exit is True
    assert signals == []
    # main.run 停机后据此改用重启约定退出码（42）
    assert app_config.restart_requested() is True


def test_graceful_exit_falls_back_to_sigterm(monkeypatch):
    """未注册 Server 实例（开发热重载）时：退回向自身发 SIGTERM。"""
    monkeypatch.setattr(app_config, "_uvicorn_server", None)
    monkeypatch.setattr(app_config, "_restart_requested", False)
    signals: list[int] = []
    monkeypatch.setattr(app_config.os, "kill", lambda pid, sig: signals.append(sig))

    app_config._request_graceful_exit()
    assert signals == [app_config.signal.SIGTERM]


def test_resolve_boot_port_reads_db_setting(client, monkeypatch, tmp_path):
    """启动端口三级解析：APP_PORT 环境变量 > 落库设置 > 默认值。"""
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'test.db'}"
    # 未落库：回落默认
    assert resolve_boot_port(database_url, 8000) == 8000
    # 落库后：读到设置页配置的端口
    client.put("/api/v1/app/config", json={"port": 12345, "external_url": ""})
    assert resolve_boot_port(database_url, 8000) == 12345
    # 环境变量显式钉死：库里的设置不生效
    monkeypatch.setenv("APP_PORT", "8000")
    assert resolve_boot_port(database_url, 8000) == 8000


def test_resolve_boot_port_survives_missing_database(tmp_path):
    """首次启动数据库/表还不存在时，端口解析必须静默回落默认值、不挡启动。"""
    database_url = f"sqlite+aiosqlite:///{tmp_path / 'no-such.db'}"
    assert resolve_boot_port(database_url, 8000) == 8000
