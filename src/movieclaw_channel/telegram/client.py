"""Telegram Bot API 的最小 HTTP 客户端。

只封装本项目用到的四个方法(getMe / getUpdates / sendMessage / sendChatAction),
不引入 python-telegram-bot 这类重依赖。出口走 egress_transport("telegram"):
国内部署 api.telegram.org 被墙,用户在「设置 → 网络」勾选 telegram 走代理即可。
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx

from movieclaw_net import egress_transport

#: 401/404 = bot token 无效或被吊销,对应通道层的凭据失效语义
_AUTH_ERROR_STATUS = (401, 404)


class TelegramApiError(Exception):
    """Bot API 返回 ok=false 时抛出;``auth_failed`` 标记凭据级错误。"""

    def __init__(self, message: str, *, auth_failed: bool = False) -> None:
        super().__init__(message)
        self.auth_failed = auth_failed


class TelegramClient:
    """单 bot 的 Bot API 客户端(线程不安全,通道层单任务使用)。"""

    def __init__(self, token: str) -> None:
        self._base = f"https://api.telegram.org/bot{token}"
        # 长轮询 50s + 余量;连接池与微信客户端同款「每账号一个」
        self._http = httpx.AsyncClient(
            transport=egress_transport("telegram"),
            timeout=httpx.Timeout(65.0, connect=10.0),
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, payload: dict[str, Any] | None = None) -> Any:
        resp = await self._http.post(f"{self._base}/{method}", json=payload or {})
        if resp.status_code in _AUTH_ERROR_STATUS:
            raise TelegramApiError(
                f"Telegram bot token 无效或已吊销(HTTP {resp.status_code})", auth_failed=True
            )
        data = resp.json()
        if not data.get("ok"):
            raise TelegramApiError(
                f"Telegram API {method} 失败:{data.get('description') or resp.status_code}"
            )
        return data.get("result")

    async def get_me(self) -> dict[str, Any]:
        return await self._call("getMe")

    async def get_updates(
        self, offset: int | None, *, timeout_s: int = 50, stop: asyncio.Event | None = None
    ) -> list[dict[str, Any]]:
        """长轮询取增量消息;stop 置位时通过取消在飞请求尽快返回。"""
        payload: dict[str, Any] = {
            "timeout": timeout_s,
            "allowed_updates": ["message"],
        }
        if offset is not None:
            payload["offset"] = offset
        call = asyncio.ensure_future(self._call("getUpdates", payload))
        if stop is None:
            return await call
        stop_wait = asyncio.ensure_future(stop.wait())
        try:
            done, _ = await asyncio.wait({call, stop_wait}, return_when=asyncio.FIRST_COMPLETED)
            if call in done:
                return call.result()
            call.cancel()
            return []
        finally:
            stop_wait.cancel()

    async def send_message(self, chat_id: str | int, text: str) -> None:
        # 纯文本发送:Agent 输出是 Markdown,但 TG 的 parse_mode 对不配对的
        # 星号/下划线会整条报错,宁可裸发也不能丢消息
        await self._call("sendMessage", {"chat_id": chat_id, "text": text})

    async def send_chat_action(self, chat_id: str | int, action: str = "typing") -> None:
        await self._call("sendChatAction", {"chat_id": chat_id, "action": action})
