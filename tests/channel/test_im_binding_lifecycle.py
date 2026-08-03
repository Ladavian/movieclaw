"""IM 配对绑定生命周期测试:尝试上限、超时清理与原账号恢复、异常响应容错。

用假的通道规格(FakeClient/FakeAdapter)驱动真实的 ImChannelService +
ChannelManager + dispatcher 链路,验证评审中发现的三类问题已修复:
- 配对码错够次数后绑定作废、临时账号停止;
- 配对超时后临时账号停止,且该 bot 原有的正式绑定被恢复;
- Telegram API 返回非 JSON(如代理错误页)时归入可重试错误而不是裸抛。
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable

import httpx
import pytest
from sqlmodel import SQLModel

from movieclaw_channel.telegram.client import TelegramApiError, TelegramClient
from movieclaw_channel.types import InboundMessage, ReplyContext
from movieclaw_db.engine import dispose_db, init_db


@pytest.fixture
async def db(tmp_path):
    """临时 SQLite 库 + 临时密钥的加密器(通道凭据落库前会加密)。"""
    from movieclaw_db.crypto import init_secret_box, reset_secret_box

    reset_secret_box()
    init_secret_box(None, tmp_path / ".secret_key")
    database = init_db(f"sqlite+aiosqlite:///{tmp_path}/test.db")
    async with database.engine.begin() as conn:
        await conn.run_sync(SQLModel.metadata.create_all)
    yield database
    await dispose_db()
    reset_secret_box()


class FakeClient:
    def __init__(self, token: str) -> None:
        self.token = token
        self.closed = False

    async def aclose(self) -> None:
        self.closed = True


class FakeAdapter:
    """最小适配器:收消息循环只等 stop,发送直接记到列表。"""

    channel_id = "telegram"
    max_text_len = 1000

    def __init__(self, client: FakeClient, account_id: str) -> None:
        self.client = client
        self.account_id = account_id
        self.sent: list[str] = []

    async def run(self, ctx) -> None:
        await ctx.stop.wait()

    async def send_text(self, reply: ReplyContext, text: str) -> None:
        self.sent.append(text)


def _install_fake_spec(monkeypatch, adapters: list[FakeAdapter]) -> None:
    """把 telegram 的通道规格换成假实现(bot_id 固定为 bot1)。"""
    from movieclaw_api.services import im_channel

    async def validate(client: FakeClient) -> tuple[str, str]:
        return "bot1", "testbot"

    def make_adapter(client: FakeClient, account_id: str) -> FakeAdapter:
        adapter = FakeAdapter(client, account_id)
        adapters.append(adapter)
        return adapter

    monkeypatch.setitem(
        im_channel._SPECS,
        "telegram",
        im_channel._ChannelSpec(
            channel_id="telegram",
            display_name="Telegram",
            make_client=FakeClient,
            make_adapter=make_adapter,
            validate=validate,
        ),
    )


def _inbound(text: str, seq: int) -> InboundMessage:
    reply = ReplyContext(channel_id="telegram", account_id="bot1", user_id="u1")
    return InboundMessage(
        channel_id="telegram",
        account_id="bot1",
        user_id="u1",
        text=text,
        reply=reply,
        provider_message_id=f"m{seq}",
    )


async def _wait_for(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError("等待条件超时")


async def test_wrong_pair_code_attempt_cap(db, monkeypatch) -> None:
    """错满次数后绑定作废,临时账号被停止(防 6 位码暴力猜解)。"""
    from movieclaw_api.services.im_channel import _PAIR_MAX_ATTEMPTS, ImChannelService

    adapters: list[FakeAdapter] = []
    _install_fake_spec(monkeypatch, adapters)
    service = ImChannelService()
    try:
        challenge = await service.begin_binding("telegram", "tok-123456")
        dispatcher = service.manager.get_dispatcher("telegram", challenge.bot_id)
        assert dispatcher is not None
        for i in range(_PAIR_MAX_ATTEMPTS):
            await dispatcher.submit_inbound(_inbound("wrong", i))
        await _wait_for(lambda: challenge.status == "failed")
        await _wait_for(lambda: any("作废" in text for text in adapters[0].sent))
        await _wait_for(lambda: not service.manager.is_running("telegram", challenge.bot_id))
        assert adapters[0].client.closed
    finally:
        await service.stop()


async def test_expired_challenge_stops_temp_account_and_restores(db, monkeypatch) -> None:
    """超时守护:停临时账号、恢复该 bot 原有的正式绑定、清理内存 challenge。"""
    from movieclaw_api.services import im_channel
    from movieclaw_db.repositories.channel_account_repo import ChannelAccountRepository

    # 预置:该 bot 已有正式绑定(重绑中途放弃的场景)
    async with db.session() as session:
        await ChannelAccountRepository(session).upsert(
            channel_id="telegram",
            account_id="bot1",
            token="old-token",
            base_url="",
            bound_user_id="u1",
        )

    monkeypatch.setattr(im_channel, "_PAIR_TTL_S", 0.1)
    monkeypatch.setattr(im_channel, "_CHALLENGE_LINGER_S", 0.05)

    adapters: list[FakeAdapter] = []
    _install_fake_spec(monkeypatch, adapters)
    service = im_channel.ImChannelService()
    restored_tokens: list[str] = []
    original_start = service._start_account

    async def record_start(row, token: str) -> None:
        restored_tokens.append(token)
        await original_start(row, token)

    monkeypatch.setattr(service, "_start_account", record_start)
    try:
        challenge = await service.begin_binding("telegram", "new-token")
        await _wait_for(lambda: challenge.status == "expired")
        # 恢复用的是库里的旧凭据,而不是这次未完成配对的新 token
        await _wait_for(lambda: restored_tokens == ["old-token"])
        await _wait_for(lambda: service.manager.is_running("telegram", "bot1"))
        # 留存期过后 challenge 从内存清掉(不再只增不减)
        await _wait_for(lambda: challenge.challenge_id not in service._challenges)
    finally:
        await service.stop()


async def test_telegram_client_non_json_response() -> None:
    """5xx/代理错误页返回 HTML:应抛可重试的 TelegramApiError,而非 JSON 解码异常。"""
    client = TelegramClient("dummy")
    await client._http.aclose()
    client._http = httpx.AsyncClient(
        transport=httpx.MockTransport(
            lambda request: httpx.Response(502, text="<html>Bad Gateway</html>")
        )
    )
    try:
        with pytest.raises(TelegramApiError) as exc_info:
            await client.get_me()
        assert not exc_info.value.auth_failed
    finally:
        await client.aclose()
