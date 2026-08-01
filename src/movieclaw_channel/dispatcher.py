"""ChannelDispatcher —— 收发与 Agent 运行的事件驱动解耦层(每账号一个实例)。

三段式结构(与用户确认的架构):

    收泵(adapter.run)          会话 worker(每会话一个)         发送泵(本类)
    归一化→submit_inbound ──▶ 串行取件→run_agent→emit ──▶ 出站队列→分片→send_text

关键语义:
- **同会话串行**:每个 session_key 一条队列一个 worker,同一用户的消息
  按序处理,Agent 不会并发跑同一会话;
- **跨会话并发**:不同用户并行,全局信号量封顶,防止把 LLM 配额打爆;
- **出站统一走队列**:Agent 回复、命令回执、未来的主动推送都经发送泵,
  同账号出站顺序有保证,重试/分片集中一处;
- **收泵永不阻塞在 Agent 上**:submit_inbound 只做去重/鉴权/入队即返回,
  长任务运行期间新消息照常接收。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import OrderedDict
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from movieclaw_channel.adapter import ChannelAdapter
from movieclaw_channel.types import InboundMessage, OutboundEnvelope, ReplyContext

logger = logging.getLogger("movieclaw_channel.dispatcher")

#: Agent 执行回调:由 API 服务层注入(装配 LLM 路由/工具/历史后驱动 pusher)。
#: 第二个参数是 emit——每产出一条完整消息调用一次。
RunAgentFn = Callable[[InboundMessage, Callable[[str], Awaitable[None]]], Awaitable[None]]

#: 幂等去重窗口(最近 N 条 provider_message_id)
_DEDUP_WINDOW = 512
#: 单会话入站队列上限:满了直接拒收新消息并提示,而不是无限排队跑 N 轮 Agent
_SESSION_QUEUE_SIZE = 8
#: 出站队列上限(仅作背压兜底,正常远达不到)
_OUTBOUND_QUEUE_SIZE = 256
#: 全局 Agent 并发上限(跨会话)
_MAX_CONCURRENT_RUNS = 2
#: 出站发送失败的重试次数与间隔
_SEND_RETRIES = 1
_SEND_RETRY_DELAY_S = 2.0


def split_message(text: str, limit: int) -> list[str]:
    """把超长文本按段落边界切成若干条,每条不超过 limit 字符。

    优先在空行(段落)处断开;单段仍超限时按行断,再不行硬切。
    """
    text = text.strip()
    if not text:
        return []
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    current = ""

    def push_current() -> None:
        nonlocal current
        if current.strip():
            chunks.append(current.strip())
        current = ""

    for para in text.split("\n\n"):
        candidate = f"{current}\n\n{para}" if current else para
        if len(candidate) <= limit:
            current = candidate
            continue
        push_current()
        if len(para) <= limit:
            current = para
            continue
        # 单段超限:硬切(按 limit 步进,保证终止)
        for i in range(0, len(para), limit):
            chunks.append(para[i : i + limit])
    push_current()
    return chunks


@dataclass(slots=True)
class _Session:
    """一条会话的运行时状态(队列 + worker + 当前运行)。"""

    queue: asyncio.Queue[InboundMessage]
    worker: asyncio.Task[None] | None = None
    #: 正在执行的 Agent 运行任务(/stop 的取消目标)
    current_run: asyncio.Task[None] | None = None


@dataclass(slots=True)
class _DispatcherDeps:
    """服务层注入的回调集合。"""

    #: 鉴权:该 user_id 是否允许使用本账号(白名单/配对)
    is_allowed: Callable[[str], bool]
    #: Agent 执行(见 RunAgentFn)
    run_agent: RunAgentFn
    #: /reset 命令:清空该会话的跨轮历史
    reset_session: Callable[[str], None] = field(default=lambda _key: None)


class ChannelDispatcher:
    """单账号的入站分发 + 出站发送泵。"""

    def __init__(self, adapter: ChannelAdapter, deps: _DispatcherDeps) -> None:
        self._adapter = adapter
        self._deps = deps
        self._sessions: dict[str, _Session] = {}
        self._seen: OrderedDict[str, None] = OrderedDict()
        self._outbound: asyncio.Queue[OutboundEnvelope] = asyncio.Queue(
            maxsize=_OUTBOUND_QUEUE_SIZE
        )
        self._run_sema = asyncio.Semaphore(_MAX_CONCURRENT_RUNS)
        self._pump_task: asyncio.Task[None] | None = None
        self._closing = False

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    def start(self) -> None:
        self._pump_task = asyncio.create_task(
            self._outbound_pump(), name=f"channel-pump-{self._adapter.channel_id}"
        )

    async def close(self) -> None:
        """停止全部 worker 与发送泵(不排空队列:进程要退了,快停优先)。"""
        self._closing = True
        tasks: list[asyncio.Task[None]] = []
        for session in self._sessions.values():
            if session.current_run and not session.current_run.done():
                session.current_run.cancel()
            if session.worker and not session.worker.done():
                session.worker.cancel()
                tasks.append(session.worker)
        if self._pump_task and not self._pump_task.done():
            self._pump_task.cancel()
            tasks.append(self._pump_task)
        for t in tasks:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await t

    # ------------------------------------------------------------------
    # 入站
    # ------------------------------------------------------------------
    async def submit_inbound(self, msg: InboundMessage) -> None:
        """收泵入口:去重 → 鉴权 → 命令 → 入会话队列。必须快速返回。"""
        if self._closing:
            return
        if msg.provider_message_id:
            if msg.provider_message_id in self._seen:
                logger.debug("重复消息已忽略 id=%s", msg.provider_message_id)
                return
            self._seen[msg.provider_message_id] = None
            while len(self._seen) > _DEDUP_WINDOW:
                self._seen.popitem(last=False)

        if not self._deps.is_allowed(msg.user_id):
            # 与 openclaw 同款策略:未授权静默丢弃,不给探测者任何反馈
            logger.info("未授权用户消息已丢弃 user=%s", msg.user_id)
            return

        if await self._handle_command(msg):
            return

        if not msg.text.strip():
            await self.push_outbound(
                OutboundEnvelope(reply=msg.reply, text="暂时只支持文字消息哦。", origin="system")
            )
            return

        session = self._sessions.get(msg.session_key)
        if session is None:
            session = _Session(queue=asyncio.Queue(maxsize=_SESSION_QUEUE_SIZE))
            self._sessions[msg.session_key] = session
        if session.worker is None or session.worker.done():
            # 首次创建,或 worker 因未预期异常死亡后自动复活——否则该会话的
            # 队列将无人消费,用户消息从此石沉大海
            if session.worker is not None:
                logger.warning("会话 worker 已退出,自动重建 session=%s", msg.session_key)
            session.worker = asyncio.create_task(
                self._session_worker(msg.session_key, session),
                name=f"channel-worker-{msg.session_key}",
            )
        try:
            session.queue.put_nowait(msg)
        except asyncio.QueueFull:
            await self.push_outbound(
                OutboundEnvelope(
                    reply=msg.reply,
                    text="排队的消息太多啦,请等我处理完手头的再发。",
                    origin="system",
                )
            )

    async def _handle_command(self, msg: InboundMessage) -> bool:
        """处理斜杠命令,返回是否已消费该消息。

        /stop 是唯一的「插队」路径:绕过会话队列直接取消当前运行。
        """
        cmd = msg.text.strip().lower()
        if cmd == "/stop":
            session = self._sessions.get(msg.session_key)
            run = session.current_run if session else None
            if run and not run.done():
                run.cancel()
                text = "已取消当前处理。"
            else:
                text = "当前没有正在进行的处理。"
            await self.push_outbound(OutboundEnvelope(reply=msg.reply, text=text, origin="system"))
            return True
        if cmd == "/reset":
            self._deps.reset_session(msg.session_key)
            await self.push_outbound(
                OutboundEnvelope(
                    reply=msg.reply, text="会话已重置,我们重新开始吧。", origin="system"
                )
            )
            return True
        return False

    async def _session_worker(self, session_key: str, session: _Session) -> None:
        """会话 worker:串行消费本会话队列,每条消息驱动一次 Agent 运行。"""
        while not self._closing:
            msg = await session.queue.get()

            async def emit(text: str, *, _reply: ReplyContext = msg.reply) -> None:
                await self.push_outbound(OutboundEnvelope(reply=_reply, text=text))

            async with self._run_sema:
                run_task = asyncio.create_task(
                    self._deps.run_agent(msg, emit),
                    name=f"channel-run-{session_key}",
                )
                session.current_run = run_task
                try:
                    await run_task
                except asyncio.CancelledError:
                    if run_task.cancelled() and not self._closing:
                        # /stop 取消的是运行而非 worker(命令处理已回执过,
                        # 这里不再重复发消息),继续消费下一条
                        continue
                    raise
                except Exception:
                    logger.exception("Agent 运行异常 session=%s", session_key)
                    await self.push_outbound(
                        OutboundEnvelope(
                            reply=msg.reply,
                            text="⚠️ 处理出错了,请稍后再试。",
                            origin="system",
                        )
                    )
                finally:
                    session.current_run = None

    # ------------------------------------------------------------------
    # 出站
    # ------------------------------------------------------------------
    async def push_outbound(self, env: OutboundEnvelope) -> None:
        """出站统一入口(Agent 回复 / 命令回执 / 未来的主动推送)。"""
        await self._outbound.put(env)

    async def _outbound_pump(self) -> None:
        """发送泵:串行取信封 → 分片 → 调 adapter 发送(带一次重试)。"""
        while True:
            env = await self._outbound.get()
            for chunk in split_message(env.text, self._adapter.max_text_len):
                await self._send_with_retry(env.reply, chunk, env.origin)

    async def _send_with_retry(self, reply: ReplyContext, text: str, origin: str) -> None:
        for attempt in range(_SEND_RETRIES + 1):
            try:
                await self._adapter.send_text(reply, text)
                return
            except Exception as exc:  # noqa: BLE001
                if attempt < _SEND_RETRIES:
                    logger.warning(
                        "消息发送失败,%s 秒后重试 user=%s origin=%s err=%s",
                        _SEND_RETRY_DELAY_S,
                        reply.user_id,
                        origin,
                        exc,
                    )
                    await asyncio.sleep(_SEND_RETRY_DELAY_S)
                else:
                    logger.error(
                        "消息发送最终失败,该条已丢弃 user=%s origin=%s err=%s",
                        reply.user_id,
                        origin,
                        exc,
                    )


def make_dispatcher(
    adapter: ChannelAdapter,
    *,
    is_allowed: Callable[[str], bool],
    run_agent: RunAgentFn,
    reset_session: Callable[[str], None],
) -> ChannelDispatcher:
    """组装一个 dispatcher(隐藏 _DispatcherDeps 这个内部结构)。"""
    return ChannelDispatcher(
        adapter,
        _DispatcherDeps(is_allowed=is_allowed, run_agent=run_agent, reset_session=reset_session),
    )
