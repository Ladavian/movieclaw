"""应用设置接口测试：读写配置、校验与重启调度。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from movieclaw_api.core.config import get_settings
from movieclaw_api.services import app_config
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'test.db'}")
    monkeypatch.setenv("SECRET_KEY_FILE", str(tmp_path / ".secret_key"))
    monkeypatch.setenv("SCHEDULER_ENABLED", "false")
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
    assert resp.json()["data"]["external_url"] == ""


def test_save_external_url_strips_trailing_slash(client):
    resp = client.put(
        "/api/v1/app/config",
        json={"external_url": "http://192.168.1.10:3000/"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["external_url"] == "http://192.168.1.10:3000"
    # 重新读取还原一致
    assert (
        client.get("/api/v1/app/config").json()["data"]["external_url"]
        == "http://192.168.1.10:3000"
    )


def test_save_rejects_bad_external_url(client):
    resp = client.put(
        "/api/v1/app/config",
        json={"external_url": "192.168.1.10:3000"},
    )
    assert resp.status_code == 400
    assert "http(s)" in resp.json()["message"]


def test_save_tolerates_legacy_port_field(client):
    """历史版本落库过 port 字段；带旧字段的请求/存量记录都不应报错。"""
    resp = client.put(
        "/api/v1/app/config",
        json={"port": 12345, "external_url": "http://192.168.1.10:3000"},
    )
    assert resp.status_code == 200
    assert resp.json()["data"]["external_url"] == "http://192.168.1.10:3000"
    assert "port" not in resp.json()["data"]


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
    monkeypatch.setattr(app_config, "_restart_exit_code", None)
    signals: list[int] = []
    monkeypatch.setattr(app_config.os, "kill", lambda pid, sig: signals.append(sig))

    app_config._request_graceful_exit(app_config.RESTART_EXIT_CODE)
    assert fake.should_exit is True
    assert signals == []
    # main.run 停机后据此改用重启约定退出码（42）
    assert app_config.restart_exit_code() == app_config.RESTART_EXIT_CODE


def test_graceful_exit_falls_back_to_sigterm(monkeypatch):
    """未注册 Server 实例（开发热重载）时：退回向自身发 SIGTERM。"""
    monkeypatch.setattr(app_config, "_uvicorn_server", None)
    monkeypatch.setattr(app_config, "_restart_exit_code", None)
    signals: list[int] = []
    monkeypatch.setattr(app_config.os, "kill", lambda pid, sig: signals.append(sig))

    app_config._request_graceful_exit(app_config.RESTART_EXIT_CODE)
    assert signals == [app_config.signal.SIGTERM]
