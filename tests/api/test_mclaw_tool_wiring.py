"""mclaw 工具在 API 层的装配测试：服务目录同步、描述快照、递归服务端硬闸。"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from movieclaw_agent.tools.mclaw import build_description
from movieclaw_api.core.config import get_settings
from movieclaw_api.services.auth import reset_auth_state
from movieclaw_api.services.mclaw_tool import (
    _DOMAIN_LINES,
    render_service_map,
    spec_domains,
)
from movieclaw_api.settings.store import reset_setting_store
from movieclaw_db.crypto import reset_secret_box

_SNAPSHOT = Path(__file__).parent / "mclaw_tool_description_snapshot.txt"


def test_service_map_covers_every_spec_domain() -> None:
    """目录同步守护：spec 的每个开放域都必须出现在服务目录里。

    新增业务域忘了在 _DOMAIN_LINES 润色时，这里也不会漏——渲染会回落
    DOMAIN_HELP 短标签；本测试保证「出现」，润色质量靠快照测试评审。
    """
    rendered = render_service_map()
    for domain in spec_domains():
        assert any(line.lstrip("- ").startswith(domain) for line in rendered.splitlines()), (
            f"服务目录缺少域：{domain}"
        )


def test_curated_lines_have_no_orphans() -> None:
    """润色行不能指向不存在的域（防止改版后目录里残留幽灵条目）。"""
    orphans = set(_DOMAIN_LINES) - spec_domains()
    assert not orphans, f"_DOMAIN_LINES 含 spec 中不存在（或已被排除）的域：{orphans}"


def test_agent_domain_is_excluded() -> None:
    """agent 域被工具硬闸禁止，目录里绝不能出现（自相矛盾会误导模型）。"""
    rendered = render_service_map()
    assert not any(line.lstrip("- ").startswith("agent ") for line in rendered.splitlines()), (
        "目录不应包含被硬闸禁止的 agent 域"
    )


def test_full_description_matches_snapshot() -> None:
    """工具描述是模型行为的一部分：任何改动必须显式过快照评审。"""
    actual = build_description(render_service_map())
    if not _SNAPSHOT.exists():
        _SNAPSHOT.write_text(actual, encoding="utf-8")
    expected = _SNAPSHOT.read_text(encoding="utf-8")
    assert actual == expected, (
        f"mclaw 工具描述与快照不一致。确认属预期变更后删除快照文件重新生成：\n  rm {_SNAPSHOT}"
    )


# ---------------------------------------------------------------------------
# 服务端递归硬闸
# ---------------------------------------------------------------------------

_ADMIN = {"username": "admin", "password": "s3cret-pass"}


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

    with TestClient(create_app()) as c:
        c.post("/api/v1/auth/bootstrap", json=_ADMIN)
        yield c

    reset_setting_store()
    reset_secret_box()
    reset_auth_state()
    get_settings.cache_clear()


def test_agent_token_cannot_start_new_run(client: TestClient) -> None:
    """持 agent 工作区令牌调 /agent/start → 400 禁止递归（先于一切校验）。"""
    import asyncio

    from movieclaw_api.services import auth as auth_service

    token = asyncio.run(auth_service.issue_agent_token("sess-x"))

    # 必须用不带 Cookie 的干净客户端——服务端鉴权 Cookie 优先，带着
    # bootstrap 的管理员 Cookie 会把身份判成 admin。真实 Agent 工作区
    # 也只有 Bearer 令牌，这正是它的调用形态。
    fresh = TestClient(client.app)
    resp = fresh.post(
        "/api/v1/agent/start",
        json={"input": "递归测试"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 400
    assert "禁止递归" in resp.json()["message"]
