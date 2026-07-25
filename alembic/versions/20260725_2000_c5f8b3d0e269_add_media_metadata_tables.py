"""add media_metadata / media_episode tables for self-sufficient metadata

自足媒体库（docs/design/metadata.md）：条目展示元数据（简介/评分/演职员）
与分集详情结构化落库，详情页断网可用。同时：

- media_season 增加季级展示列（overview/poster_path/poster_file），
  删除 episodes JSON——集数据唯一事实源迁至 media_episode 表；
- library 增加 write_media_assets 开关（刮削图片/NFO 镜像写入媒体目录）。

dev 阶段不做数据搬运（用户决策 2026-07-25：可删库重来），episodes JSON
直接删列。

Revision ID: c5f8b3d0e269
Revises: b4e7a2c9d158
Create Date: 2026-07-25 20:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c5f8b3d0e269"
down_revision: str | None = "b4e7a2c9d158"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "media_metadata",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column(
            "media_item_id",
            sa.Integer(),
            sa.ForeignKey("media_item.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
            index=True,
        ),
        sa.Column("overview", sa.Text(), nullable=True),
        sa.Column("tagline", sa.String(), nullable=True),
        sa.Column("genres", sa.JSON(), nullable=False),
        sa.Column("runtime_minutes", sa.Integer(), nullable=True),
        sa.Column("release_date", sa.Date(), nullable=True),
        sa.Column("content_rating", sa.String(), nullable=True),
        sa.Column("original_language", sa.String(), nullable=True),
        sa.Column("origin_countries", sa.JSON(), nullable=False),
        sa.Column("studios", sa.JSON(), nullable=False),
        sa.Column("vote_average", sa.Float(), nullable=True),
        sa.Column("vote_count", sa.Integer(), nullable=True),
        sa.Column("directors", sa.JSON(), nullable=False),
        sa.Column("cast", sa.JSON(), nullable=False),
        sa.Column("poster_file", sa.String(), nullable=True),
        sa.Column("backdrop_file", sa.String(), nullable=True),
        sa.Column("scraped_at", sa.DateTime(), nullable=True),
        sa.Column("scrape_language", sa.String(), nullable=False),
    )

    op.create_table(
        "media_episode",
        sa.Column("id", sa.Integer(), primary_key=True),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column(
            "media_item_id",
            sa.Integer(),
            sa.ForeignKey("media_item.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        sa.Column("season_number", sa.Integer(), nullable=False),
        sa.Column("episode_number", sa.Integer(), nullable=False),
        sa.Column("name", sa.String(), nullable=False),
        sa.Column("overview", sa.Text(), nullable=True),
        sa.Column("air_date", sa.Date(), nullable=True),
        sa.Column("runtime_minutes", sa.Integer(), nullable=True),
        sa.Column("vote_average", sa.Float(), nullable=True),
        sa.Column("still_path", sa.String(), nullable=True),
        sa.Column("still_file", sa.String(), nullable=True),
        sa.UniqueConstraint(
            "media_item_id", "season_number", "episode_number", name="uq_media_episode_unit"
        ),
    )
    op.create_index(
        "ix_media_episode_item_season", "media_episode", ["media_item_id", "season_number"]
    )

    with op.batch_alter_table("media_season", schema=None) as batch_op:
        batch_op.add_column(sa.Column("overview", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("poster_path", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("poster_file", sa.String(), nullable=True))
        batch_op.drop_column("episodes")

    with op.batch_alter_table("library", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "write_media_assets",
                sa.Boolean(),
                nullable=False,
                server_default=sa.true(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("library", schema=None) as batch_op:
        batch_op.drop_column("write_media_assets")

    with op.batch_alter_table("media_season", schema=None) as batch_op:
        batch_op.add_column(sa.Column("episodes", sa.JSON(), nullable=False, server_default="[]"))
        batch_op.drop_column("poster_file")
        batch_op.drop_column("poster_path")
        batch_op.drop_column("overview")

    op.drop_index("ix_media_episode_item_season", table_name="media_episode")
    op.drop_table("media_episode")
    op.drop_table("media_metadata")
