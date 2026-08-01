"""ChannelDispatcher:分片、去重、鉴权、同会话串行、/stop 与 /reset。"""

from __future__ import annotations

import asyncio

from movieclaw_channel.dispatcher import make_dispatcher, split_message
from movieclaw_channel.types import InboundMessage, ReplyContext

# ---------------------------------------------------------------------------
# split_message(纯函数)
# ---------------------------------------------------------------------------


def test_split_short_text_untouched():
    assert split_message("你好", 100) == ["你好"]


def test_split_empty_text():
    assert split_message("  \n ", 100) == []


def test_split_prefers_paragraph_boundary():
    text = "第一段。\n\n第二段。\n\n第三段。"
    chunks = split_message(text, 12)
    assert all(len(c) <= 12 for c in chunks)
    assert "".join(chunks).replace("\n\n", "") == text.replace("\n\n", "")


def test_split_hard_cuts_oversized_paragraph():
    text = "字" * 25
    chunks = split_message(text, 10)
    assert chunks == ["字" * 10, "字" * 10, "字" * 5]


# ---------------------------------------------------------------------------
# dispatcher 行为(用假 adapter 驱动)
# ---------------------------------------------------------------------------


class FakeAdapter:
    channel_id = "weixin"
    max_text_len = 50

    def __init__(self) -> None:
        self.sent: list[tuple[str, str]] = []

    async def run(self, ctx) -> None:  # pragma: no cover - dispatcher 测试不走收循环
        raise NotImplementedError

    async def send_text(self, reply: ReplyContext, text: str) -> None:
        self.sent.append((reply.user_id, text))


def _msg(text: str, *, msg_id: str, user: str = "u1@im.wechat") -> InboundMessage:
    reply = ReplyContext(channel_id="weixin", account_id="bot1", user_id=user)
    return InboundMessage(
        channel_id="weixin",
        account_id="bot1",
        user_id=user,
        text=text,
        reply=reply,
        provider_message_id=msg_id,
    )


async def _drain(dispatcher, adapter, *, expect: int, timeout: float = 2.0) -> None:
    """等发送泵把 expect 条消息发完(轮询,避免依赖内部实现)。"""
    deadline = asyncio.get_event_loop().time() + timeout
    while len(adapter.sent) < expect:
        if asyncio.get_event_loop().time() > deadline:
            raise AssertionError(f"等待发送超时:已发 {len(adapter.sent)}/{expect}")
        await asyncio.sleep(0.01)


async def test_serial_processing_and_reply():
    """同会话消息按序处理,回复经出站泵送达。"""
    adapter = FakeAdapter()
    order: list[str] = []

    async def run_agent(msg, emit):
        order.append(msg.text)
        await emit(f"回复:{msg.text}")

    d = make_dispatcher(adapter, is_allowed=lambda _u: True, run_agent=run_agent)
    d.start()
    try:
        await d.submit_inbound(_msg("一", msg_id="m1"))
        await d.submit_inbound(_msg("二", msg_id="m2"))
        await _drain(d, adapter, expect=2)
        assert order == ["一", "二"]
        assert [t for _, t in adapter.sent] == ["回复:一", "回复:二"]
    finally:
        await d.close()


async def test_dedup_and_auth():
    adapter = FakeAdapter()
    handled: list[str] = []

    async def run_agent(msg, emit):
        handled.append(msg.provider_message_id)

    d = make_dispatcher(
        adapter,
        is_allowed=lambda u: u == "owner@im.wechat",
        run_agent=run_agent,
    )
    d.start()
    try:
        await d.submit_inbound(_msg("hi", msg_id="dup", user="owner@im.wechat"))
        await d.submit_inbound(_msg("hi", msg_id="dup", user="owner@im.wechat"))  # 重复:丢
        await d.submit_inbound(_msg("hi", msg_id="other", user="stranger@im.wechat"))  # 未授权:丢
        await asyncio.sleep(0.1)
        assert handled == ["dup"]
    finally:
        await d.close()


async def test_reset_command():
    adapter = FakeAdapter()
    resets: list[str] = []

    async def run_agent(msg, emit):  # pragma: no cover - 命令不进 Agent
        raise AssertionError("命令消息不应进入 Agent")

    async def reset_session(key: str) -> None:
        resets.append(key)

    d = make_dispatcher(
        adapter,
        is_allowed=lambda _u: True,
        run_agent=run_agent,
        reset_session=reset_session,
    )
    d.start()
    try:
        await d.submit_inbound(_msg("/reset", msg_id="c1"))
        await _drain(d, adapter, expect=1)
        assert resets == ["weixin:bot1:u1@im.wechat"]
        assert "会话已重置" in adapter.sent[0][1]
    finally:
        await d.close()


async def test_stop_cancels_running_agent():
    adapter = FakeAdapter()
    started = asyncio.Event()

    async def run_agent(msg, emit):
        started.set()
        await asyncio.sleep(30)  # 模拟长任务,等着被 /stop 取消

    d = make_dispatcher(adapter, is_allowed=lambda _u: True, run_agent=run_agent)
    d.start()
    try:
        await d.submit_inbound(_msg("跑个长任务", msg_id="m1"))
        await asyncio.wait_for(started.wait(), timeout=2)
        await d.submit_inbound(_msg("/stop", msg_id="m2"))
        # /stop 只回执一条,worker 侧不再重复发「已取消」
        await _drain(d, adapter, expect=1)
        await asyncio.sleep(0.05)
        texts = [t for _, t in adapter.sent]
        assert texts == ["已取消当前处理。"]
    finally:
        await d.close()


async def test_dead_worker_is_revived():
    """worker 意外死亡后,下一条消息触发自动复活,会话不永久卡死。"""
    adapter = FakeAdapter()
    calls: list[str] = []

    async def run_agent(msg, emit):
        calls.append(msg.text)
        await emit(f"回复:{msg.text}")

    d = make_dispatcher(adapter, is_allowed=lambda _u: True, run_agent=run_agent)
    d.start()
    try:
        await d.submit_inbound(_msg("一", msg_id="m1"))
        await _drain(d, adapter, expect=1)
        # 模拟 worker 被意外终止(等价于内部未预期崩溃后任务结束)
        session = d._sessions["weixin:bot1:u1@im.wechat"]  # noqa: SLF001 -- 测试内部状态
        assert session.worker is not None
        session.worker.cancel()
        await asyncio.sleep(0.05)
        assert session.worker.done()

        await d.submit_inbound(_msg("还活着吗", msg_id="m2"))
        await _drain(d, adapter, expect=2)
        assert calls == ["一", "还活着吗"]
        assert adapter.sent[-1][1] == "回复:还活着吗"
    finally:
        await d.close()


async def test_long_reply_is_chunked():
    adapter = FakeAdapter()

    async def run_agent(msg, emit):
        await emit("段落甲" * 10 + "\n\n" + "段落乙" * 10)  # 60 字,上限 50

    d = make_dispatcher(adapter, is_allowed=lambda _u: True, run_agent=run_agent)
    d.start()
    try:
        await d.submit_inbound(_msg("hi", msg_id="m1"))
        await _drain(d, adapter, expect=2)
        assert all(len(t) <= 50 for _, t in adapter.sent)
    finally:
        await d.close()
