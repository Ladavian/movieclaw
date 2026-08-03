"""TelegramAdapter —— 实现通道协议的 Telegram 收发实现。

收消息循环:getUpdates 长轮询,游标 = 已确认的 update_id + 1(字符串形式
持久化到 channel_account.cursor,重启续传)。只处理私聊文本消息——群聊
场景的鉴权与打扰控制是另一回事,P0 不做。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import httpx

from movieclaw_channel.adapter import ChannelContext
from movieclaw_channel.telegram.client import TelegramApiError, TelegramClient
from movieclaw_channel.types import ChannelAuthError, InboundMessage, ReplyContext

logger = logging.getLogger("movieclaw_channel.telegram.adapter")

CHANNEL_ID = "telegram"

#: 单次失败的基础重试间隔;退避 = min(上限, 基础 × 连续失败次数)
_RETRY_DELAY_S = 2.0
#: 退避间隔上限
_BACKOFF_DELAY_S = 30.0


class TelegramAdapter:
    """Telegram 通道适配器(单 bot)。"""

    channel_id = CHANNEL_ID
    #: Telegram 单条消息 4096 字符上限,留余量
    max_text_len = 3900

    def __init__(self, client: TelegramClient, account_id: str) -> None:
        self._client = client
        self._account_id = account_id

    async def run(self, ctx: ChannelContext) -> None:
        stop = ctx.stop or asyncio.Event()
        logger.info("Telegram 收消息循环启动 account=%s", self._account_id)
        offset = int(ctx.initial_cursor) if ctx.initial_cursor.isdigit() else None
        failures = 0

        while not stop.is_set():
            try:
                updates = await self._client.get_updates(offset, stop=stop)
            except TelegramApiError as exc:
                if exc.auth_failed:
                    raise ChannelAuthError(str(exc)) from exc
                # 退避随连续失败次数线性上升到上限,只在成功后清零——计数不能
                # 在达到上限时重置,否则持续故障会循环打快速重试(2s,2s,30s,2s…)
                failures += 1
                delay = min(_BACKOFF_DELAY_S, _RETRY_DELAY_S * failures)
                logger.error("getUpdates 失败(连续第 %d 次),%.0f 秒后重试:%s", failures, delay, exc)
                await self._sleep(stop, delay)
                continue
            except httpx.HTTPError as exc:
                failures += 1
                delay = min(_BACKOFF_DELAY_S, _RETRY_DELAY_S * failures)
                logger.error(
                    "getUpdates 网络错误(连续第 %d 次),%.0f 秒后重试:%s", failures, delay, exc
                )
                await self._sleep(stop, delay)
                continue
            failures = 0

            for update in updates:
                update_id = update.get("update_id")
                if isinstance(update_id, int):
                    offset = update_id + 1
                    await ctx.save_cursor(str(offset))
                msg = self._normalize(update)
                if msg is not None:
                    await ctx.on_inbound(msg)

        logger.info("Telegram 收消息循环退出 account=%s", self._account_id)

    def _normalize(self, update: dict[str, Any]) -> InboundMessage | None:
        message = update.get("message")
        if not isinstance(message, dict):
            return None
        chat = message.get("chat") or {}
        if chat.get("type") != "private":
            return None
        sender = message.get("from") or {}
        if sender.get("is_bot"):
            return None
        user_id = str(sender.get("id") or "")
        if not user_id:
            return None
        reply = ReplyContext(
            channel_id=self.channel_id,
            account_id=self._account_id,
            user_id=user_id,
            token={"chat_id": chat.get("id")},
        )
        return InboundMessage(
            channel_id=self.channel_id,
            account_id=self._account_id,
            user_id=user_id,
            text=str(message.get("text") or message.get("caption") or ""),
            reply=reply,
            provider_message_id=f"{chat.get('id')}:{message.get('message_id')}",
            timestamp_ms=int(message.get("date") or 0) * 1000,
        )

    async def send_text(self, reply: ReplyContext, text: str) -> None:
        # 主动推送时 token 里没有 chat_id,私聊场景 chat_id == user_id
        await self._client.send_message(reply.token.get("chat_id") or reply.user_id, text)

    async def send_photo(self, reply: ReplyContext, photo: bytes, caption: str) -> None:
        """图文消息(通道协议的可选能力,发送泵 getattr 探测后调用)。"""
        await self._client.send_photo(reply.token.get("chat_id") or reply.user_id, photo, caption)

    @staticmethod
    async def _sleep(stop: asyncio.Event, seconds: float) -> None:
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=seconds)
