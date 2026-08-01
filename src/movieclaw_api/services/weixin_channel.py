"""微信通道服务编排:绑定注册表 + 通道管理器 + Agent 装配的粘合层。

职责边界:
- movieclaw_channel 只管「收发字节与事件调度」,不知道 LLM/工具/历史;
- 本服务把三者拼起来:绑定确认 → 落库 + 热启动;入站消息 → 装配受限
  工具集的 AgentRunner → StepReplyPusher → 出站队列。

安全红线(与设计讨论一致):微信侧 Agent 用**受限工具集**——只挂 mclaw
产品操作工具,不开 bash/read/write/edit 工作区工具。IM 是远程入口,
即使有白名单,也不该让它拿到宿主机 shell。

会话历史(P0):内存态 deque,进程重启即清空;每会话保留最近 40 条消息。
持久化转录是后续迭代(复用 agent_sessions 存储)的事,不在本层内实现。
"""

from __future__ import annotations

import logging
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path

from movieclaw_agent import AgentRunner, AgentStartParams
from movieclaw_agent.tools import make_mclaw_tool
from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import NotFoundException
from movieclaw_api.services import auth as auth_service
from movieclaw_api.services.llm_config import acquire_llm_router
from movieclaw_api.services.mclaw_tool import render_service_map
from movieclaw_channel import ChannelManager, InboundMessage
from movieclaw_channel.dispatcher import make_dispatcher
from movieclaw_channel.weixin import (
    BindingResult,
    WeixinAdapter,
    WeixinBindingRegistry,
    WeixinClient,
)
from movieclaw_channel.weixin.adapter import CHANNEL_ID
from movieclaw_db.engine import get_database
from movieclaw_db.models.channel_account import ChannelAccount, ChannelAccountStatus
from movieclaw_db.repositories.channel_account_repo import ChannelAccountRepository
from movieclaw_llm import ChatMessage

logger = logging.getLogger("movieclaw_api.weixin_channel")

#: 每会话跨轮历史上限(条):IM 场景轻上下文,超出后滚动遗忘
_HISTORY_MAX_MESSAGES = 40
#: 微信侧 Agent 的步数上限:IM 对话不该跑出超长循环,收紧防失控
_WEIXIN_MAX_STEPS = 40


class WeixinChannelService:
    """进程级单例:随 lifespan 启停。"""

    def __init__(self) -> None:
        self.manager = ChannelManager()
        self.binding = WeixinBindingRegistry(
            on_confirmed=self._on_binding_confirmed,
            get_local_tokens=self._local_tokens,
        )
        #: session_key → 跨轮对话历史(内存态)
        self._histories: dict[str, deque[ChatMessage]] = {}
        #: 每账号一个持有连接池的 iLink 客户端,停账号时关闭
        self._clients: dict[str, WeixinClient] = {}

    # ------------------------------------------------------------------
    # 生命周期
    # ------------------------------------------------------------------
    async def start(self) -> None:
        """启动时拉起所有已绑定且未失效的账号。"""
        async with get_database().session() as session:
            rows = await ChannelAccountRepository(session).list_by_channel(CHANNEL_ID)
            accounts = [
                (row, ChannelAccountRepository.decrypted_token(row))
                for row in rows
                if row.status == ChannelAccountStatus.ACTIVE
            ]
        for row, token in accounts:
            try:
                await self._start_account(row, token)
            except Exception:
                logger.exception("微信账号启动失败 account=%s", row.account_id)
        if accounts:
            logger.info("微信通道已启动 %d 个账号", len(accounts))

    async def stop(self) -> None:
        await self.binding.close()
        await self.manager.close()
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()

    # ------------------------------------------------------------------
    # 账号装配
    # ------------------------------------------------------------------
    async def _start_account(self, row: ChannelAccount, token: str) -> None:
        account_id = row.account_id
        # 重新绑定热替换时,先关旧客户端
        old = self._clients.pop(account_id, None)
        if old is not None:
            await self.manager.stop_account(CHANNEL_ID, account_id)
            await old.aclose()

        client = WeixinClient(row.base_url, token)
        self._clients[account_id] = client
        adapter = WeixinAdapter(client, account_id)

        bound_user = (row.bound_user_id or "").strip()
        dispatcher = make_dispatcher(
            adapter,
            # P0 白名单 = 扫码人本人;bound_user 缺失(异常数据)时拒绝所有人
            is_allowed=lambda uid, _bound=bound_user: bool(_bound) and uid == _bound,
            run_agent=self._run_agent,
            reset_session=lambda key: self._histories.pop(key, None) and None,
        )

        await self.manager.start_account(
            adapter,
            dispatcher,
            account_id=account_id,
            initial_cursor=row.cursor or "",
            save_cursor=self._make_cursor_saver(account_id),
            on_auth_error=self._on_auth_error,
        )

    @staticmethod
    def _make_cursor_saver(account_id: str) -> Callable[[str], Awaitable[None]]:
        async def save(cursor: str) -> None:
            async with get_database().session() as session:
                await ChannelAccountRepository(session).save_cursor(account_id, cursor)

        return save

    async def _on_auth_error(self, account_id: str) -> None:
        """凭据失效回调:落库标记 stale,绑定页据此引导重新扫码。"""
        async with get_database().session() as session:
            await ChannelAccountRepository(session).mark_stale(
                account_id, "微信 bot 凭据已失效,请重新扫码绑定"
            )

    # ------------------------------------------------------------------
    # 绑定流程回调
    # ------------------------------------------------------------------
    async def _local_tokens(self) -> list[str]:
        """已绑定账号的 token 列表(服务端判断「是否已绑定过本实例」用)。"""
        async with get_database().session() as session:
            rows = await ChannelAccountRepository(session).list_by_channel(CHANNEL_ID)
            return [ChannelAccountRepository.decrypted_token(r) for r in rows]

    async def _on_binding_confirmed(self, result: BindingResult) -> None:
        """扫码确认:凭据落库 → 热启动收发循环(成功后前端才看到 confirmed)。"""
        async with get_database().session() as session:
            row = await ChannelAccountRepository(session).upsert(
                channel_id=CHANNEL_ID,
                account_id=result.bot_id,
                token=result.bot_token,
                base_url=result.base_url,
                bound_user_id=result.user_id or None,
            )
        await self._start_account(row, result.bot_token)

    # ------------------------------------------------------------------
    # 账号管理(API 层调用)
    # ------------------------------------------------------------------
    async def unbind(self, account_id: str) -> bool:
        """解绑:停收发循环、关客户端、删凭据行。"""
        await self.manager.stop_account(CHANNEL_ID, account_id)
        client = self._clients.pop(account_id, None)
        if client is not None:
            await client.aclose()
        # 清掉该账号的全部会话历史
        prefix = f"{CHANNEL_ID}:{account_id}:"
        for key in [k for k in self._histories if k.startswith(prefix)]:
            del self._histories[key]
        async with get_database().session() as session:
            return await ChannelAccountRepository(session).delete(account_id)

    # ------------------------------------------------------------------
    # Agent 装配与执行(dispatcher 的 run_agent 回调)
    # ------------------------------------------------------------------
    async def _run_agent(
        self, msg: InboundMessage, emit: Callable[[str], Awaitable[None]]
    ) -> None:
        from movieclaw_channel.pusher import StepReplyPusher

        try:
            async with get_database().session() as session:
                llm_router = await acquire_llm_router(session)
        except NotFoundException as exc:
            await emit(f"⚠️ {exc.message}")
            return

        history = self._histories.setdefault(
            msg.session_key, deque(maxlen=_HISTORY_MAX_MESSAGES)
        )
        runner = AgentRunner(
            llm_router,
            tools=await self._restricted_tools(msg.session_key),
            max_steps=_WEIXIN_MAX_STEPS,
        )
        params = AgentStartParams(input=msg.text, history=list(history))
        pusher = StepReplyPusher(emit)

        async for ev in runner.start(params):
            await pusher.feed(ev)

        # 跨轮记忆:只记「用户输入 + 终答」;工具轨迹不进历史(每轮重跑更可靠)
        if not pusher.errored:
            history.append(ChatMessage(role="user", content=msg.text))
            if pusher.final_text:
                history.append(ChatMessage(role="assistant", content=pusher.final_text))

    async def _restricted_tools(self, session_key: str):
        """微信侧的受限工具集:仅 mclaw 产品操作工具,不开工作区 shell。"""
        settings = get_settings()
        workdir = Path(settings.agent_workspace_dir).resolve() / "weixin"
        workdir.mkdir(parents=True, exist_ok=True)
        token = await auth_service.issue_agent_token(session_key)
        cli_env = {
            "MOVIECLAW_SERVER": f"http://127.0.0.1:{settings.port}",
            "MOVIECLAW_TOKEN": token,
        }
        return [make_mclaw_tool(workdir, cli_env, render_service_map())]


# ---------------------------------------------------------------------------
# 进程级单例(与 agent_runs 同款 init/get/close 约定)
# ---------------------------------------------------------------------------

_service: WeixinChannelService | None = None


async def init_weixin_channel() -> WeixinChannelService:
    """lifespan 启动时调用:创建单例并拉起已绑定账号。"""
    global _service
    _service = WeixinChannelService()
    await _service.start()
    return _service


def get_weixin_channel() -> WeixinChannelService:
    if _service is None:
        raise RuntimeError("微信通道服务尚未初始化")
    return _service


async def close_weixin_channel() -> None:
    global _service
    if _service is not None:
        await _service.stop()
        _service = None
