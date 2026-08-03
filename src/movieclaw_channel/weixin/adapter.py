"""WeixinAdapter —— 实现通道协议的微信收发实现。

收消息循环(镜像 openclaw-weixin 的 monitor 语义):
- getupdates 长轮询,服务端可通过 longpolling_timeout_ms 动态调整下轮超时;
- ``get_updates_buf`` 是增量游标:每轮返回新值就持久化,重启续传(至少一次
  投递,幂等去重由 dispatcher 负责);
- errcode -14(token 失效)→ 抛 ChannelAuthError,由 manager 停账号标 stale;
- 其余错误:连续失败 <3 次退避 2s,达到 3 次退避 30s,自愈后清零。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

import httpx

from movieclaw_channel.adapter import ChannelContext
from movieclaw_channel.types import ChannelAuthError, InboundMessage, ReplyContext
from movieclaw_channel.weixin.client import (
    STALE_TOKEN_ERRCODE,
    WeixinApiError,
    WeixinClient,
)

logger = logging.getLogger("movieclaw_channel.weixin.adapter")

CHANNEL_ID = "weixin"

_MAX_CONSECUTIVE_FAILURES = 3
_RETRY_DELAY_S = 2.0
_BACKOFF_DELAY_S = 30.0
_DEFAULT_LONG_POLL_S = 35.0

#: 消息 item 类型(iLink 协议):1=文本 3=语音
_ITEM_TEXT = 1
_ITEM_VOICE = 3
#: message_type:2=BOT(自己发出的,收侧跳过)
_MSG_TYPE_BOT = 2


def _extract_text(item_list: list[dict[str, Any]] | None) -> str:
    """从 item_list 提取文本正文:文本 item 优先,语音 item 取平台转写文字。"""
    for item in item_list or []:
        if item.get("type") == _ITEM_TEXT:
            text = (item.get("text_item") or {}).get("text")
            if text:
                return str(text)
        if item.get("type") == _ITEM_VOICE:
            text = (item.get("voice_item") or {}).get("text")
            if text:
                return str(text)
    return ""


class WeixinAdapter:
    """微信通道适配器(单账号)。"""

    channel_id = CHANNEL_ID
    #: 微信单条文本约 4000 字上限,留余量
    max_text_len = 3800

    def __init__(self, client: WeixinClient, account_id: str) -> None:
        self._client = client
        self._account_id = account_id

    async def run(self, ctx: ChannelContext) -> None:
        stop = ctx.stop or asyncio.Event()
        await self._client.notify_start()
        logger.info("微信收消息循环启动 account=%s", self._account_id)

        cursor = ctx.initial_cursor
        timeout_s = _DEFAULT_LONG_POLL_S
        failures = 0

        try:
            while not stop.is_set():
                try:
                    resp = await self._client.get_updates(cursor, stop=stop, timeout_s=timeout_s)
                except (httpx.HTTPError, WeixinApiError) as exc:
                    # 传输层/网关错误在循环内退避重试(与业务错误同节奏),
                    # 不抛给 supervisor 整层重启——那会重发 notifystart 且粒度太粗
                    # 与 telegram 侧同款：退避线性升到上限后保持,不再周期性归零
                    # (归零会让持续故障循环打快速重试 2s,2s,30s,2s…),成功后才清零
                    failures += 1
                    delay = min(_BACKOFF_DELAY_S, _RETRY_DELAY_S * failures)
                    logger.error(
                        "getupdates 请求失败(连续第 %d 次),%.0f 秒后重试:%s",
                        failures,
                        delay,
                        exc,
                    )
                    await self._sleep(stop, delay)
                    continue

                ret = resp.get("ret") or 0
                errcode = resp.get("errcode") or 0
                if ret == STALE_TOKEN_ERRCODE or errcode == STALE_TOKEN_ERRCODE:
                    raise ChannelAuthError(
                        f"微信 bot 凭据已失效(errcode {STALE_TOKEN_ERRCODE}),请重新扫码绑定"
                    )
                if ret != 0 or errcode != 0:
                    failures += 1
                    delay = min(_BACKOFF_DELAY_S, _RETRY_DELAY_S * failures)
                    logger.error(
                        "getupdates 返回错误 ret=%s errcode=%s errmsg=%s"
                        "(连续第 %d 次,%.0f 秒后重试)",
                        ret,
                        errcode,
                        resp.get("errmsg"),
                        failures,
                        delay,
                    )
                    await self._sleep(stop, delay)
                    continue
                failures = 0

                # 服务端建议的下轮长轮询超时
                suggested = resp.get("longpolling_timeout_ms")
                if isinstance(suggested, int) and suggested > 0:
                    timeout_s = suggested / 1000

                new_cursor = resp.get("get_updates_buf")
                if new_cursor:
                    cursor = new_cursor
                    await ctx.save_cursor(new_cursor)

                for raw in resp.get("msgs") or []:
                    msg = self._normalize(raw)
                    if msg is not None:
                        await ctx.on_inbound(msg)
        finally:
            await self._client.notify_stop()
            logger.info("微信收消息循环退出 account=%s", self._account_id)

    def _normalize(self, raw: dict[str, Any]) -> InboundMessage | None:
        """iLink WeixinMessage → 归一化入站消息;非用户消息返回 None。"""
        if raw.get("message_type") == _MSG_TYPE_BOT:
            return None
        from_user_id = str(raw.get("from_user_id") or "")
        if not from_user_id:
            return None

        token: dict[str, Any] = {}
        context_token = raw.get("context_token")
        if context_token:
            token["context_token"] = context_token

        provider_id = str(raw.get("message_id") or raw.get("client_id") or raw.get("seq") or "")
        reply = ReplyContext(
            channel_id=self.channel_id,
            account_id=self._account_id,
            user_id=from_user_id,
            token=token,
        )
        return InboundMessage(
            channel_id=self.channel_id,
            account_id=self._account_id,
            user_id=from_user_id,
            text=_extract_text(raw.get("item_list")),
            reply=reply,
            provider_message_id=provider_id,
            timestamp_ms=int(raw.get("create_time_ms") or 0),
        )

    async def send_text(self, reply: ReplyContext, text: str) -> None:
        await self._client.send_text(reply.user_id, text, reply.token.get("context_token"))

    @staticmethod
    async def _sleep(stop: asyncio.Event, seconds: float) -> None:
        """可被 stop 打断的退避等待。"""
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(stop.wait(), timeout=seconds)
