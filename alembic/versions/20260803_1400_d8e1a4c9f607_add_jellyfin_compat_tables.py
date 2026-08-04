"""add playback_state / jellyfin_device: Jellyfin 兼容层与播放领域状态

playback_state 是协议无关的观看状态领域表（进度毫秒/已看/次数/收藏），
Jellyfin 兼容层与未来网页端播放共用；jellyfin_device 是兼容层专属的
播放器设备凭据表（device_id 唯一，重登录覆盖换发 token）。
纯增表，向前兼容（docs/design/jellyfin-compat.md 8.2）。

Revision ID: d8e1a4c9f607
Revises: c7d0f3b8e596
Create Date: 2026-08-03 14:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d8e1a4c9f607"
down_revision: str | None = "c7d0f3b8e596"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "playback_state",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column(
            "media_item_id",
            sa.Integer(),
            sa.ForeignKey("media_item.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("season_number", sa.Integer(), nullable=False),
        sa.Column("episode_number", sa.Integer(), nullable=False),
        sa.Column("position_ms", sa.Integer(), nullable=False),
        sa.Column("played", sa.Boolean(), nullable=False),
        sa.Column("play_count", sa.Integer(), nullable=False),
        sa.Column("is_favorite", sa.Boolean(), nullable=False),
        sa.Column("last_played_at", sa.DateTime(), nullable=True),
        sa.UniqueConstraint(
            "media_item_id",
            "season_number",
            "episode_number",
            name="uq_playback_state_unit",
        ),
    )
    op.create_index(
        "ix_playback_state_media_item_id", "playback_state", ["media_item_id"]
    )
    op.create_index("ix_playback_state_played", "playback_state", ["played"])
    op.create_index(
        "ix_playback_state_last_played_at", "playback_state", ["last_played_at"]
    )

    op.create_table(
        "jellyfin_device",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("token", sa.String(), nullable=False),
        sa.Column("device_id", sa.String(), nullable=False),
        sa.Column("client", sa.String(), nullable=False),
        sa.Column("device_name", sa.String(), nullable=False),
        sa.Column("version", sa.String(), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_jellyfin_device_token", "jellyfin_device", ["token"], unique=True
    )
    op.create_index(
        "ix_jellyfin_device_device_id", "jellyfin_device", ["device_id"], unique=True
    )


def downgrade() -> None:
    op.drop_table("jellyfin_device")
    op.drop_table("playback_state")
