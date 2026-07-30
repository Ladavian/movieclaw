"""精选命令：mclaw agent run / attach——AI 助手运行的实时流渲染。

start 是普通 POST，价值在流：SSE 逐事件渲染（正文打 stdout、工具调用
过程打 stderr），断线用 Last-Event-ID 精确续传 + 指数退避重连，终态事件
决定退出码（agent_error → 1）。--detach 只拿编号就返回，配合
mclaw agent attach 随时接回。
"""

from __future__ import annotations

import json
import sys
import time

import click
import httpx

from movieclaw_cli.core.errors import CliError
from movieclaw_cli.core.http import Api
from movieclaw_cli.core.output import emit
from movieclaw_cli.overlay.groups import output_option

_TERMINAL_EVENTS = {"agent_done", "agent_error", "agent_cancelled"}
_MAX_RECONNECTS = 5


def _render_event(event: str, payload: dict) -> None:
    """把一帧事件画到终端：正文进 stdout，过程信息进 stderr。"""
    if event == "agent_start":
        print(
            f"[运行开始] {payload.get('provider')}/{payload.get('model')}",
            file=sys.stderr,
        )
    elif event == "text_delta":
        print(payload.get("delta") or "", end="", flush=True)
    elif event == "tool_call":
        call = payload.get("tool_call") or {}
        args = json.dumps(call.get("arguments") or {}, ensure_ascii=False)
        if len(args) > 120:
            args = args[:117] + "..."
        print(f"\n→ 工具 {call.get('name')}：{args}", file=sys.stderr)
    elif event == "tool_result":
        result = payload.get("tool_result") or {}
        mark = "✗" if result.get("is_error") else "✓"
        print(f"  {mark} 完成（{result.get('elapsed_ms')}ms）", file=sys.stderr)


def _stream_run(api: Api, run_id: str, from_id: int | None) -> tuple[str, dict]:
    """消费运行事件流直到终态；断流自动用 Last-Event-ID 续传。

    返回 (终态事件名, 终态载荷)。
    """
    cursor = from_id
    failures = 0
    while True:
        try:
            for sse in api.stream_sse(f"/agent/runs/{run_id}/stream", last_event_id=cursor):
                failures = 0
                if sse.id is not None:
                    cursor = sse.id
                payload = json.loads(sse.data) if sse.data else {}
                if sse.event in _TERMINAL_EVENTS:
                    return sse.event, payload
                _render_event(sse.event, payload)
            # 流正常闭合但没见到终态（服务重启等）：按断线处理重连
            raise httpx.ReadError("事件流在终态前闭合")
        except (httpx.HTTPError, httpx.StreamError):
            failures += 1
            if failures > _MAX_RECONNECTS:
                raise CliError(
                    f"事件流连续断开 {failures} 次，停止重连（运行可能仍在后台）",
                    hint=f"稍后用 mclaw agent attach {run_id} 接回，"
                    f"或 mclaw agent sessions show 查看结果",
                ) from None
            delay = min(0.5 * (2 ** (failures - 1)), 5.0)
            print(f"（连接断开，{delay:.1f}s 后续传…）", file=sys.stderr)
            time.sleep(delay)


def _finish(event: str, payload: dict, settings) -> None:
    """终态渲染与退出码结算。"""
    if event == "agent_done":
        result = payload.get("result") or {}
        print("", flush=True)
        usage = result.get("usage") or {}
        print(
            f"[完成] {result.get('steps')} 步，{result.get('elapsed_ms')}ms，"
            f"tokens in/out={usage.get('input_tokens')}/{usage.get('output_tokens')}",
            file=sys.stderr,
        )
        return
    if event == "agent_cancelled":
        print("\n[已取消] 运行被主动停止", file=sys.stderr)
        return
    raise CliError(
        payload.get("error") or "Agent 运行失败",
        hint="完整过程可用 mclaw agent sessions show <session_id> 回看",
    )


@click.command(name="run", short_help="发起一次 AI 助手运行并实时渲染（支持断线续传）")
@click.argument("prompt")
@click.option("--session", "session_id", help="续聊：接在已有会话之后")
@click.option("--model", help="指定模型（缺省用默认供应商的默认模型）")
@click.option("--detach", is_flag=True, help="只创建运行并输出编号，不等待（后接 agent attach）")
@output_option
@click.pass_obj
def agent_run(
    settings,
    output_override: str | None,
    prompt: str,
    session_id: str | None,
    model: str | None,
    detach: bool,
):
    """让产品内 AI 助手执行一个任务。

    示例：

        mclaw agent run "把待识别清单里能认出来的都认领掉"

        mclaw agent run --detach "整理媒体库" && mclaw agent attach <run_id>

    实时输出：助手正文在 stdout，工具调用过程在 stderr；Ctrl-C 只断开
    观看、不取消运行（取消用 mclaw agent runs cancel <run_id>）。
    """
    body: dict = {"input": prompt}
    if session_id:
        body["session_id"] = session_id
    if model:
        body["model"] = model
    api = settings.make_api()
    try:
        started = api.request("POST", "/agent/start", json_body=body) or {}
        run_id = started.get("run_id")
        print(
            f"运行已创建：run_id={run_id} session_id={started.get('session_id')}",
            file=sys.stderr,
        )
        if detach:
            emit(started, output=output_override or settings.output, quiet=settings.quiet)
            return
        event, payload = _stream_run(api, run_id, from_id=None)
    finally:
        api.close()
    _finish(event, payload, settings)


@click.command(name="attach", short_help="接回一个运行的实时事件流（断点续传）")
@click.argument("run_id")
@click.option("--from-id", type=int, default=None, help="从指定事件序号之后续传（缺省回放全部）")
@click.pass_obj
def agent_attach(settings, run_id: str, from_id: int | None):
    """订阅指定运行的事件流；已结束的运行会回放并立即给出终态。"""
    api = settings.make_api()
    try:
        event, payload = _stream_run(api, run_id, from_id)
    finally:
        api.close()
    _finish(event, payload, settings)
