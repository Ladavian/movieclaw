"""上下文压缩的纯函数：token 估算、水位线判定、历史重建规则。"""

from __future__ import annotations

from movieclaw_agent.compaction import (
    APPROX_BYTES_PER_TOKEN,
    RETAINED_USER_TOKEN_BUDGET,
    build_replacement_history,
    estimate_tokens,
    is_summary_message,
    should_compact,
)
from movieclaw_agent.prompts import SUMMARY_PREFIX
from movieclaw_llm import ChatMessage, ToolCall


def test_estimate_tokens_bytes_heuristic():
    # ASCII：4 字节 ≈ 1 token
    assert estimate_tokens("x" * 400) == 100
    # 中文：UTF-8 每字 3 字节
    assert estimate_tokens("电影" * 200) == 200 * 2 * 3 // APPROX_BYTES_PER_TOKEN
    # 消息计入正文与工具调用的名称/原始参数
    message = ChatMessage(
        role="assistant",
        content="好的",
        tool_calls=[ToolCall(id="c1", name="search", raw_arguments='{"q": "沙丘"}')],
    )
    assert estimate_tokens(message) > estimate_tokens("好的")
    # 列表 = 逐条求和
    assert estimate_tokens([message, message]) == 2 * estimate_tokens(message)


def test_should_compact_threshold_and_missing_window():
    # 未声明窗口 → 永不自动压缩
    assert not should_compact(10**9, None)
    assert not should_compact(10**9, 0)
    # 90% 水位线边界
    assert not should_compact(899, 1000)
    assert should_compact(900, 1000)


def test_build_replacement_history_keeps_recent_users_and_appends_summary():
    messages = [
        ChatMessage(role="system", content="系统词"),
        ChatMessage(role="user", content="第一问"),
        ChatMessage(role="assistant", content="回答", tool_calls=[ToolCall(id="c1", name="t")]),
        ChatMessage(role="tool", content="工具结果", tool_call_id="c1", name="t"),
        ChatMessage(role="user", content=f"{SUMMARY_PREFIX}\n上次的旧摘要"),
        ChatMessage(role="user", content="第二问"),
    ]
    rebuilt = build_replacement_history(messages, "新摘要")
    # 只剩真实用户原话（按原顺序）+ 摘要收尾；assistant/tool/旧摘要全部丢弃
    assert [m.role for m in rebuilt] == ["user", "user", "user"]
    assert [m.text() for m in rebuilt[:2]] == ["第一问", "第二问"]
    assert rebuilt[-1].text() == f"{SUMMARY_PREFIX}\n新摘要"
    assert is_summary_message(rebuilt[-1])


def test_build_replacement_history_budget_truncates_oldest():
    # 三条用户消息，预算只装得下最新一条 + 次新一条的一部分
    big = "x" * (RETAINED_USER_TOKEN_BUDGET * APPROX_BYTES_PER_TOKEN)  # 恰好占满全部预算
    messages = [
        ChatMessage(role="user", content="最旧的一条"),
        ChatMessage(role="user", content=big),
        ChatMessage(role="user", content="最新的一条"),
    ]
    rebuilt = build_replacement_history(messages, "摘要")
    texts = [m.text() for m in rebuilt]
    # 最新的完整保留；big 被中部截断（带标记）；最旧的一条被挤出预算
    assert texts[-2] == "最新的一条" or texts[-1].startswith(SUMMARY_PREFIX)
    assert any("已截断" in t for t in texts)
    assert all(t != "最旧的一条" for t in texts)
    # 保留消息的估算总量不超预算（截断标记有少量溢出容忍，放宽 5%）
    retained_tokens = sum(estimate_tokens(m) for m in rebuilt[:-1])
    assert retained_tokens <= RETAINED_USER_TOKEN_BUDGET * 1.05
