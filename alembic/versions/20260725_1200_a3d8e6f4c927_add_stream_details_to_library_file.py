"""add audio/subtitle stream details to library_file

条目详情页要展示"这份文件到底是什么规格"：音轨（格式/声道/语言）与
内封字幕轨来自 ffprobe 对文件本体的探测，扫描入账时写入。
三态：NULL=未探测（旧数据由详情页按需补探后回填），[]=探测过但没有该类流。

Revision ID: a3d8e6f4c927
Revises: f7b2d5e9c341
Create Date: 2026-07-25 12:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a3d8e6f4c927"
down_revision: str | None = "f7b2d5e9c341"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("library_file", schema=None) as batch_op:
        batch_op.add_column(sa.Column("audio_streams", sa.JSON(), nullable=True))
        batch_op.add_column(sa.Column("subtitle_streams", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("library_file", schema=None) as batch_op:
        batch_op.drop_column("subtitle_streams")
        batch_op.drop_column("audio_streams")
