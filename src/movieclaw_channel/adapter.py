"""通道适配器协议:一个 IM 平台接入 movieclaw 需要实现的最小面。

刻意保持极小(收消息循环 + 发文本):
- 绑定流程各平台差异太大(微信是扫码+配对码状态机),不进通用协议,
  由各自的 binding 模块实现、API 服务层直接调用;
- 媒体收发是 P2 能力,届时再扩展协议,避免为未来做空设计。
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Protocol

from movieclaw_channel.types import InboundMessage, ReplyContext


@dataclass(slots=True)
class ChannelContext:
    """manager 注入给 adapter 收消息循环的运行时上下文。"""

    account_id: str
    #: 入站回调:adapter 归一化一条消息后调用(必须快速返回,内部只入队)
    on_inbound: Callable[[InboundMessage], Awaitable[None]]
    #: 游标持久化回调(微信的 get_updates_buf;重启后从上次位置续传)
    save_cursor: Callable[[str], Awaitable[None]]
    #: 上次持久化的游标,空串表示从头开始
    initial_cursor: str = ""
    #: 停止信号:置位后 adapter 应尽快中断在飞的长轮询并退出循环
    stop: asyncio.Event | None = None


class ChannelAdapter(Protocol):
    """通道适配器:平台只需知道「怎么收字节、怎么发文本」。"""

    channel_id: str
    #: 单条文本消息的长度上限(超长由发送泵分片)
    max_text_len: int

    async def run(self, ctx: ChannelContext) -> None:
        """收消息主循环:阻塞运行直到 ``ctx.stop`` 置位。

        凭据失效时抛 ``ChannelAuthError``(manager 停账号、标记 stale);
        其余异常向上抛,由 manager 做退避重启。
        """
        ...

    async def send_text(self, reply: ReplyContext, text: str) -> None:
        """发送一条文本消息(调用方已完成分片,text 不超过 max_text_len)。"""
        ...
