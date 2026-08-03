from __future__ import annotations

from enum import StrEnum

from sqlalchemy import Column, Text
from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin


class ChannelAccountStatus(StrEnum):
    """通道账号状态。

    - ACTIVE:凭据有效,收发循环正常运行(或待启动);
    - STALE:凭据已被平台判定失效(如微信 errcode -14),须重新扫码绑定。
      stale 账号启动时跳过,绑定页展示「需重新绑定」引导。
    """

    ACTIVE = "active"
    STALE = "stale"


class ChannelAccount(TimestampMixin, table=True):
    """IM 通道账号表:一行 = 一个已绑定的 bot 账号(微信为首个通道)。

    安全:``token`` 经 SecretBox 加密后落库(``enc::`` 前缀密文),
    加解密统一在 Repository 层完成,与站点凭据/LLM Key 同款约定。

    ``cursor`` 是收消息增量游标(微信的 get_updates_buf,base64 长文本):
    每轮长轮询返回新值即覆盖,进程重启后从该位置续传,不丢不重的「不重」
    部分由 dispatcher 的消息 id 去重兜底(游标语义是至少一次投递)。

    ``bound_user_id`` 是扫码人的平台用户 id,同时充当 P0 的白名单:
    只有这个用户发来的消息会被处理(谁扫码谁才能用,防陌生人探测)。

    ``context_token`` 只对微信有意义:iLink 网关的发送接口必须绑定一条会话,
    该令牌随每条入站消息下发。回复入站消息时原样回带即可,但**主动推送**
    (订阅投递/入库完成等)没有对应的入站消息,只能复用最近一次记住的这个值
    ——否则消息发出去也定位不到会话。Telegram/Discord 靠 user_id 就能直投,
    不需要本字段。
    """

    __tablename__ = "channel_account"

    id: int | None = Field(default=None, primary_key=True)
    channel_id: str = Field(index=True, description="通道类型:weixin")
    account_id: str = Field(unique=True, description="平台侧账号 id(微信 ilink_bot_id)")
    token: str = Field(description="平台凭据(SecretBox 加密密文)")
    base_url: str = Field(description="平台网关地址(绑定时服务端指定的就近接入)")
    bound_user_id: str | None = Field(
        default=None, description="扫码绑定人的平台用户 id(即白名单)"
    )
    cursor: str | None = Field(
        default=None, sa_column=Column(Text), description="收消息增量游标(get_updates_buf)"
    )
    context_token: str | None = Field(
        default=None,
        sa_column=Column(Text),
        description="最近一次入站消息的会话令牌(仅微信,主动推送复用它定位会话)",
    )
    status: ChannelAccountStatus = Field(
        default=ChannelAccountStatus.ACTIVE, description="账号状态(active/stale)"
    )
    last_error: str | None = Field(default=None, description="最近一次异常说明(中文)")
    # 该账号当前对话对应的 Agent 会话 id(agent_session 表 + JSONL 转录):
    # 微信里的对话与 Web「最近会话」共用同一套持久化,首条消息时创建,
    # /reset 后清空、下条消息再建新会话(旧会话保留在列表里)。
    agent_session_id: str | None = Field(
        default=None, description="当前绑定的 Agent 会话 id(/reset 后换新)"
    )
