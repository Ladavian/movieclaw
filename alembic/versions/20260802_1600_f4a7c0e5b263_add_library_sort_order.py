"""add library.sort_order: 媒体库展示顺序

媒体库首页的卡片区与「最近添加」分区此前只能按创建顺序（id）排列，
用户无法调整。加 ``sort_order``（升序，越小越靠前）承载展示顺序：
存量库回填为各自的 id（保持现有顺序不变），新库创建时置尾（max+1），
排序接口一次性重写全部库的取值。

Revision ID: f4a7c0e5b263
Revises: e3f6b9d4a152
Create Date: 2026-08-02 16:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f4a7c0e5b263"
down_revision: str | None = "e3f6b9d4a152"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("library", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("sort_order", sa.Integer(), nullable=False, server_default="0")
        )
        batch_op.create_index("ix_library_sort_order", ["sort_order"])
    # 存量库回填为 id：与迁移前的展示顺序（按 id）完全一致
    op.execute("UPDATE library SET sort_order = id")


def downgrade() -> None:
    with op.batch_alter_table("library", schema=None) as batch_op:
        batch_op.drop_index("ix_library_sort_order")
        batch_op.drop_column("sort_order")
