"""IM 通道(微信绑定)相关的请求/响应模型。"""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, Field

from movieclaw_db.models.channel_account import ChannelAccount


class WeixinAccountView(BaseModel):
    """已绑定账号(绑定页列表项)。"""

    account_id: str
    #: 扫码绑定人的微信用户 id(白名单)
    bound_user_id: str | None
    #: active=正常;stale=凭据失效,需重新扫码
    status: str
    #: 当前进程内收发循环是否在运行
    running: bool
    last_error: str | None
    bound_at: datetime

    @classmethod
    def from_model(cls, row: ChannelAccount, *, running: bool) -> WeixinAccountView:
        return cls(
            account_id=row.account_id,
            bound_user_id=row.bound_user_id,
            status=row.status,
            running=running,
            last_error=row.last_error,
            bound_at=row.created_at,
        )


class WeixinBindingStartView(BaseModel):
    """发起绑定的返回:前端渲染二维码并开始 poll。"""

    challenge_id: str
    #: 二维码内容链接(前端用它渲染二维码图)
    qrcode_url: str
    message: str


class WeixinBindingStatusView(BaseModel):
    """绑定状态快照(前端每 1-2 秒 poll 一次,后端只读内存,毫秒级返回)。

    status 取值:
    pending / scanned / need_verify_code / confirmed / already_bound /
    expired / failed。confirmed 时通道已启动,account 字段附带账号信息。
    """

    challenge_id: str
    status: str
    message: str
    #: 二维码可能中途刷新(过期自动换新),前端每次 poll 都以此为准重绘
    qrcode_url: str
    account: WeixinAccountView | None = None


class WeixinVerifyCodePayload(BaseModel):
    """提交手机微信上显示的配对数字。"""

    code: str = Field(min_length=1, max_length=16, description="配对码")
