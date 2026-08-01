"""StepReplyPusher:按「步」收敛 Agent 事件流为离散消息。"""

from __future__ import annotations

from movieclaw_agent.events import AgentDone, AgentEvent
from movieclaw_channel.pusher import StepReplyPusher
from movieclaw_llm import ToolCall


def _collector() -> tuple[list[str], StepReplyPusher]:
    emitted: list[str] = []

    async def emit(text: str) -> None:
        emitted.append(text)

    return emitted, StepReplyPusher(emit)


def _ev(type_: str, **kw) -> AgentEvent:
    return AgentEvent(type=type_, run_id="r1", **kw)


async def test_single_step_text_flushed_on_done():
    emitted, pusher = _collector()
    await pusher.feed(_ev("agent_start"))
    await pusher.feed(_ev("text_delta", delta="你好,"))
    await pusher.feed(_ev("text_delta", delta="我来帮你。"))
    await pusher.feed(_ev("agent_done", result=AgentDone(text="你好,我来帮你。")))
    assert emitted == ["你好,我来帮你。"]
    assert pusher.final_text == "你好,我来帮你。"


async def test_step_boundary_at_tool_call():
    """每一步的正文 = 一条消息:工具调用前的正文先发,终答再发一条。"""
    emitted, pusher = _collector()
    await pusher.feed(_ev("text_delta", delta="我先搜一下。"))
    await pusher.feed(_ev("tool_call", tool_call=ToolCall(id="t1", name="mclaw")))
    # 同一步连发第二个工具调用:缓冲已空,不产生空消息
    await pusher.feed(_ev("tool_call", tool_call=ToolCall(id="t2", name="mclaw")))
    await pusher.feed(_ev("tool_result"))
    await pusher.feed(_ev("text_delta", delta="找到了这些结果。"))
    await pusher.feed(_ev("agent_done", result=AgentDone(text="找到了这些结果。")))
    assert emitted == ["我先搜一下。", "找到了这些结果。"]


async def test_thinking_and_deltas_are_dropped():
    emitted, pusher = _collector()
    await pusher.feed(_ev("thinking_delta", delta="内心戏"))
    await pusher.feed(_ev("tool_call_delta", delta='{"a":1}', tool_call_id="t1"))
    await pusher.feed(_ev("agent_done", result=AgentDone(text=None)))
    assert emitted == []


async def test_error_flushes_then_notifies():
    emitted, pusher = _collector()
    await pusher.feed(_ev("text_delta", delta="进行到一半"))
    await pusher.feed(_ev("agent_error", error="模型超时"))
    assert emitted == ["进行到一半", "⚠️ 处理失败:模型超时"]
    assert pusher.errored is True


async def test_cancelled_notice():
    emitted, pusher = _collector()
    await pusher.feed(_ev("agent_cancelled"))
    assert emitted == ["已取消本次处理。"]
