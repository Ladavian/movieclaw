from __future__ import annotations

from datetime import datetime

from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin


class JellyfinDevice(TimestampMixin, table=True):
    """Jellyfin 兼容层的播放器设备凭据（协议层专属，docs/design/jellyfin-compat.md 4.3）。

    每个播放器设备一行，token 即 Jellyfin 协议里的 AccessToken（设备级长期
    凭据，用户不会频繁在电视上重登录）。与 Web 控制台会话 token 体系分开。

    安全约定（对齐真 Jellyfin 的行为）：``device_id`` 唯一——同一设备重复
    登录时**覆盖本行并换发新 token**（旧 token 即刻失效），防止客户端反复
    重装/重登录无限累积长期有效凭据。
    """

    __tablename__ = "jellyfin_device"

    id: int | None = Field(default=None, primary_key=True)

    token: str = Field(unique=True, index=True, description="AccessToken（32 位 hex）")
    device_id: str = Field(
        unique=True, index=True, description="客户端上报的设备标识（重登录覆盖键）"
    )
    client: str = Field(default="", description="客户端名（如 Infuse）")
    device_name: str = Field(default="", description="设备名（如 Apple TV）")
    version: str = Field(default="", description="客户端版本")
    last_seen_at: datetime | None = Field(
        default=None, description="最近一次使用该 token 的时间"
    )
