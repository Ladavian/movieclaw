from __future__ import annotations

from datetime import datetime

from sqlalchemy import Column, ForeignKey, Integer, UniqueConstraint
from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin


class PlaybackState(TimestampMixin, table=True):
    """观看状态——"看到哪了/看没看过"的领域事实（docs/design/jellyfin-compat.md 5.4/8.5）。

    协议无关的领域层数据：Jellyfin 兼容层与未来的网页端播放器共用同一张表、
    同一套读写服务。因此进度单位是**毫秒**而非 Jellyfin 的 100ns ticks——
    ticks 换算收敛在协议边界做。

    键沿用全局约定的 (media_item_id, season_number, episode_number) 数字对
    （电影 (0,0) 哨兵，与 wanted_item / library_file 同源），不锚 media_episode
    行 id（集行随元数据刷新可增删重建）。单管理员形态不带 user 维度，
    将来真要多用户时加列即可向前兼容。
    """

    __tablename__ = "playback_state"
    __table_args__ = (
        UniqueConstraint(
            "media_item_id",
            "season_number",
            "episode_number",
            name="uq_playback_state_unit",
        ),
    )

    id: int | None = Field(default=None, primary_key=True)

    media_item_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("media_item.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        description="媒体条目身份锚",
    )
    season_number: int = Field(default=0, description="季号；电影=0（哨兵）")
    episode_number: int = Field(default=0, description="集号；电影=0（哨兵）")

    position_ms: int = Field(default=0, description="续播位置（毫秒）；0=无续播点")
    played: bool = Field(default=False, index=True, description="是否已看完")
    play_count: int = Field(default=0, description="播放次数（开始播放时 +1）")
    is_favorite: bool = Field(default=False, description="是否收藏")
    last_played_at: datetime | None = Field(
        default=None, index=True, description="最近一次播放活动时间；NULL=从未播放"
    )
