"""测试共享常量与操作。"""

from __future__ import annotations

from fastapi.testclient import TestClient

ADMIN = {"username": "admin", "password": "s3cret-pass"}
AUTH_HEADER = (
    'MediaBrowser Client="Infuse", Device="Apple TV", '
    'DeviceId="test-device-1", Version="8.2"'
)


def jf_login(client: TestClient) -> str:
    """Jellyfin 登录，返回 AccessToken。"""
    resp = client.post(
        "/Users/AuthenticateByName",
        json={"Username": ADMIN["username"], "Pw": ADMIN["password"]},
        headers={"Authorization": AUTH_HEADER},
    )
    assert resp.status_code == 200, resp.text
    return resp.json()["AccessToken"]
