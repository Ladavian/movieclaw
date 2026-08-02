"""消息推送配置域(「设置 → 消息推送」的推送内容开关)。

控制哪些系统事件会推送到已绑定的 IM 通道(微信/Telegram/Discord)。
事件键与 ``services.channel_push.notify_channels`` 的 ``event`` 参数一一对应,
新增推送事件时在这里加开关字段。
"""

from __future__ import annotations

from pydantic import Field

from movieclaw_api.settings.base import SettingSchema, register_setting

CHANNEL_PUSH_NAMESPACE = "channels.push"


@register_setting(namespace=CHANNEL_PUSH_NAMESPACE, title="消息推送")
class ChannelPushSetting(SettingSchema):
    """推送内容开关(默认全开,与未配置时的行为一致)。"""

    push_dispatch: bool = Field(
        default=True, description="订阅命中并开始下载时推送(「开始下载」)"
    )
    push_imported: bool = Field(
        default=True, description="下载完成整理入库时推送(「已入库」)"
    )
