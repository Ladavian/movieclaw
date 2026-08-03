"""DiscordAdapter —— 实现通道协议的 Discord 收发实现。

收消息走 Gateway websocket(Bot API 没有 HTTP 长轮询收消息的口):
Hello → Identify → 心跳保活 → MESSAGE_CREATE 事件。刻意不做 RESUME——
断线后直接重新 Identify,本场景消息量极小,丢 RESUME 换实现简单。

只处理私聊(无 guild_id 的消息):私聊内容不需要 MESSAGE CONTENT 特权
intent,用户在开发者后台建 bot 时零额外配置。发消息走 REST(client.py)。

代理:websocket 连接按 egress 配置解析 "discord" 标签的代理地址,
与 REST 同一开关(websockets 库原生支持 http/socks 代理)。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import random
from typing import Any

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

from movieclaw_channel.adapter import ChannelContext
from movieclaw_channel.discord.client import DiscordClient
from movieclaw_channel.types import ChannelAuthError, InboundMessage, ReplyContext
from movieclaw_net import resolve_proxy_url

logger = logging.getLogger("movieclaw_channel.discord.adapter")

CHANNEL_ID = "discord"

_GATEWAY_URL = "wss://gateway.discord.gg/?v=10&encoding=json"
#: DIRECT_MESSAGES;私聊内容不需要 MESSAGE_CONTENT 特权 intent
_INTENTS = 1 << 12
#: 凭据级关闭码:4004 鉴权失败;4014 intents 未被允许(理论上不会,兜底)
_AUTH_CLOSE_CODES = (4004, 4014)
_RECONNECT_DELAY_S = 5.0

_OP_DISPATCH = 0
_OP_HEARTBEAT = 1
_OP_IDENTIFY = 2
_OP_RECONNECT = 7
_OP_INVALID_SESSION = 9
_OP_HELLO = 10


class DiscordAdapter:
    """Discord 通道适配器(单 bot)。"""

    channel_id = CHANNEL_ID
    #: Discord 单条消息 2000 字符上限,留余量
    max_text_len = 1900

    def __init__(self, client: DiscordClient, account_id: str) -> None:
        self._client = client
        self._account_id = account_id

    async def run(self, ctx: ChannelContext) -> None:
        stop = ctx.stop or asyncio.Event()
        logger.info("Discord Gateway 循环启动 account=%s", self._account_id)
        while not stop.is_set():
            try:
                await self._connect_once(ctx, stop)
                # op 7/9 等服务端指示的正常返回也要退避:Identify 有严格限频
                # (官方要求 5 秒 1 次),无延迟紧循环重连会被 Gateway 拉黑
                if not stop.is_set():
                    delay = 1.0 + random.random() * 4.0
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(stop.wait(), timeout=delay)
            except ChannelAuthError:
                raise
            except Exception as exc:  # noqa: BLE001 -- 网络抖动统一退避重连
                logger.warning("Discord Gateway 断开,%s 秒后重连:%s", _RECONNECT_DELAY_S, exc)
                with contextlib.suppress(TimeoutError):
                    await asyncio.wait_for(stop.wait(), timeout=_RECONNECT_DELAY_S)
        logger.info("Discord Gateway 循环退出 account=%s", self._account_id)

    async def _connect_once(self, ctx: ChannelContext, stop: asyncio.Event) -> None:
        """一次完整的 Gateway 会话:连接 → Identify → 收事件直到断开或 stop。"""
        proxy = resolve_proxy_url("discord")
        # proxy 参数要求 websockets>=14,已在 pyproject 显式声明
        ws_ctx = connect(_GATEWAY_URL, proxy=proxy, open_timeout=15, close_timeout=5)

        async with ws_ctx as ws:
            heartbeat_task: asyncio.Task[None] | None = None
            #: 最近收到的事件序号(心跳携带);用单元素列表让心跳任务读到最新值
            seq_holder: list[int | None] = [None]
            try:
                while not stop.is_set():
                    recv = asyncio.ensure_future(ws.recv())
                    stop_wait = asyncio.ensure_future(stop.wait())
                    done, _ = await asyncio.wait(
                        {recv, stop_wait}, return_when=asyncio.FIRST_COMPLETED
                    )
                    stop_wait.cancel()
                    if recv not in done:
                        recv.cancel()
                        return
                    payload = json.loads(recv.result())

                    op = payload.get("op")
                    if payload.get("s") is not None:
                        seq_holder[0] = payload["s"]

                    if op == _OP_HELLO:
                        interval = payload["d"]["heartbeat_interval"] / 1000
                        heartbeat_task = asyncio.create_task(
                            self._heartbeat(ws, interval, seq_holder),
                            name=f"discord-heartbeat-{self._account_id}",
                        )
                        await ws.send(
                            json.dumps(
                                {
                                    "op": _OP_IDENTIFY,
                                    "d": {
                                        "token": self._client.token,
                                        "intents": _INTENTS,
                                        "properties": {
                                            "os": "linux",
                                            "browser": "movieclaw",
                                            "device": "movieclaw",
                                        },
                                    },
                                }
                            )
                        )
                    elif op == _OP_DISPATCH and payload.get("t") == "MESSAGE_CREATE":
                        msg = self._normalize(payload.get("d") or {})
                        if msg is not None:
                            await ctx.on_inbound(msg)
                    elif op in (_OP_RECONNECT, _OP_INVALID_SESSION):
                        logger.info("Discord 要求重连(op=%s)", op)
                        return
                    # HEARTBEAT_ACK 及其他事件忽略
            except ConnectionClosed as exc:
                # 凭据失效只会以关闭码 4004/4014 表达:Gateway 握手阶段不带
                # token(鉴权发生在 Identify),握手被拒(InvalidStatus,如
                # Cloudflare 429 限流、代理 5xx)是暂时性网络问题,绝不能当作
                # 凭据失效——否则账号会被标记 stale 并永久停止收发。这类异常
                # 直接抛给 run() 的通用退避重连路径处理。
                if exc.rcvd is not None and exc.rcvd.code in _AUTH_CLOSE_CODES:
                    raise ChannelAuthError(
                        f"Discord bot token 无效或权限不足(关闭码 {exc.rcvd.code}),请重新绑定"
                    ) from exc
                raise
            finally:
                if heartbeat_task is not None:
                    heartbeat_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await heartbeat_task

    @staticmethod
    async def _heartbeat(ws: Any, interval_s: float, seq_holder: list[int | None]) -> None:
        """按服务端节奏发心跳;首拍加随机抖动(协议建议)。"""
        await asyncio.sleep(interval_s * random.random())
        while True:
            await ws.send(json.dumps({"op": _OP_HEARTBEAT, "d": seq_holder[0]}))
            await asyncio.sleep(interval_s)

    def _normalize(self, data: dict[str, Any]) -> InboundMessage | None:
        if data.get("guild_id"):
            return None  # 只处理私聊
        author = data.get("author") or {}
        if author.get("bot"):
            return None
        user_id = str(author.get("id") or "")
        channel_id = str(data.get("channel_id") or "")
        if not user_id or not channel_id:
            return None
        reply = ReplyContext(
            channel_id=self.channel_id,
            account_id=self._account_id,
            user_id=user_id,
            token={"dm_channel_id": channel_id},
        )
        return InboundMessage(
            channel_id=self.channel_id,
            account_id=self._account_id,
            user_id=user_id,
            text=str(data.get("content") or ""),
            reply=reply,
            provider_message_id=str(data.get("id") or ""),
        )

    async def send_text(self, reply: ReplyContext, text: str) -> None:
        channel_id = reply.token.get("dm_channel_id") or await self._client.get_dm_channel(
            reply.user_id
        )
        await self._client.send_message(str(channel_id), text)
