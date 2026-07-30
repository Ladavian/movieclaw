"""SSE 分帧解析的单元测试。"""

from __future__ import annotations

from movieclaw_cli.core.sse import parse_sse_lines


def test_basic_frames() -> None:
    lines = [
        "event: start",
        'data: {"a": 1}',
        "",
        "event: done",
        'data: {"b": 2}',
        "",
    ]
    events = list(parse_sse_lines(lines))
    assert [(e.event, e.data) for e in events] == [
        ("start", '{"a": 1}'),
        ("done", '{"b": 2}'),
    ]


def test_id_and_heartbeat_and_multiline_data() -> None:
    lines = [
        ": heartbeat",
        "id: 3",
        "event: text_delta",
        "data: 第一行",
        "data: 第二行",
        "",
        ": heartbeat",
    ]
    events = list(parse_sse_lines(lines))
    assert len(events) == 1
    assert events[0].id == 3
    assert events[0].data == "第一行\n第二行"


def test_frame_without_trailing_blank_line_still_emitted() -> None:
    events = list(parse_sse_lines(["event: done", "data: {}"]))
    assert [(e.event, e.data) for e in events] == [("done", "{}")]


def test_event_defaults_to_message() -> None:
    events = list(parse_sse_lines(["data: x", ""]))
    assert events[0].event == "message"
