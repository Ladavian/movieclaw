"""add library routing columns: match_rules / genre_ids / import_watch auto mode

媒体库路由「库自述收藏范围」（docs/design/library-routing.md）的三处模型改动：

- ``library.match_rules``：收藏范围条件列表（JSON，默认空=未声明）；
- ``media_metadata.genre_ids``：类型的 TMDB genre ID（语言无关，路由匹配用；
  老档案行为空列表，路由回落 TMDB 轻量详情，重刮自愈）；
- ``import_watch.library_id`` 放宽为可空 + 新增 ``kind``：NULL=自动路由模式
  （识别出作品后按收藏范围选库，kind 提供识别链必需的类型先验）。

Revision ID: f7a0c3e8d596
Revises: e6f9b2d7c485
Create Date: 2026-07-27 12:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f7a0c3e8d596"
down_revision: str | None = "e6f9b2d7c485"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("library", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("match_rules", sa.JSON(), nullable=False, server_default="[]")
        )
    with op.batch_alter_table("media_metadata", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("genre_ids", sa.JSON(), nullable=False, server_default="[]")
        )
    with op.batch_alter_table("import_watch", schema=None) as batch_op:
        batch_op.alter_column("library_id", existing_type=sa.Integer(), nullable=True)
        batch_op.add_column(sa.Column("kind", sa.String(), nullable=True))


def downgrade() -> None:
    # 回退前清掉 auto 规则（library_id 为 NULL 的行），否则 NOT NULL 收紧失败
    op.execute("DELETE FROM import_watch WHERE library_id IS NULL")
    with op.batch_alter_table("import_watch", schema=None) as batch_op:
        batch_op.drop_column("kind")
        batch_op.alter_column("library_id", existing_type=sa.Integer(), nullable=False)
    with op.batch_alter_table("media_metadata", schema=None) as batch_op:
        batch_op.drop_column("genre_ids")
    with op.batch_alter_table("library", schema=None) as batch_op:
        batch_op.drop_column("match_rules")
