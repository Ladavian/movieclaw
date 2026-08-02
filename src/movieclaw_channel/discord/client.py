"""Discord 的最小 REST 客户端(收消息走 Gateway,见 adapter)。

只封装用到的四个接口:校验 token(users/@me)、建私聊频道、发消息、typing。
不引入 discord.py 这类重依赖。出口走 egress_transport("discord")。
"""

from __future__ import annotations

import contextlib
from typing import Any

import httpx

from movieclaw_net import egress_transport

_API_BASE = "https://discord.com/api/v10"


class DiscordApiError(Exception):
    """REST 调用失败;``auth_failed`` 标记 token 级错误(401)。"""

    def __init__(self, message: str, *, auth_failed: bool = False) -> None:
        super().__init__(message)
        self.auth_failed = auth_failed


class DiscordClient:
    """单 bot 的 Discord REST 客户端。"""

    def __init__(self, token: str) -> None:
        self.token = token
        self._http = httpx.AsyncClient(
            transport=egress_transport("discord"),
            timeout=httpx.Timeout(30.0, connect=10.0),
            headers={"Authorization": f"Bot {token}"},
        )
        #: user_id → 私聊频道 id 缓存(建私聊频道是幂等接口,缓存只省往返)
        self._dm_channels: dict[str, str] = {}

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _call(self, method: str, path: str, payload: dict[str, Any] | None = None) -> Any:
        resp = await self._http.request(method, f"{_API_BASE}{path}", json=payload)
        if resp.status_code == 401:
            raise DiscordApiError("Discord bot token 无效或已吊销(HTTP 401)", auth_failed=True)
        if resp.status_code >= 400:
            detail = ""
            with contextlib.suppress(Exception):  # 非 JSON 响应,状态码足够定位
                detail = str(resp.json().get("message") or "")
            raise DiscordApiError(f"Discord API {path} 失败(HTTP {resp.status_code}){detail}")
        if resp.status_code == 204:
            return None
        return resp.json()

    async def get_me(self) -> dict[str, Any]:
        return await self._call("GET", "/users/@me")

    async def get_dm_channel(self, user_id: str) -> str:
        """取(或创建)与某用户的私聊频道 id。"""
        cached = self._dm_channels.get(user_id)
        if cached:
            return cached
        data = await self._call("POST", "/users/@me/channels", {"recipient_id": user_id})
        channel_id = str(data["id"])
        self._dm_channels[user_id] = channel_id
        return channel_id

    async def send_message(self, channel_id: str, text: str) -> None:
        await self._call("POST", f"/channels/{channel_id}/messages", {"content": text})

    async def trigger_typing(self, channel_id: str) -> None:
        await self._call("POST", f"/channels/{channel_id}/typing", {})
