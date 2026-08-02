"""add import_watch.target_path: 自定义目录目标

监听导入目标三态化（docs/design/strm-workflow.md）：指定库 / 自动路由 /
**自定义目录**。``target_path`` 非空即自定义目录规则——识别改名后落该
目录、不进入任何媒体库（网盘 strm 等"整理结果需外部流转"玩法的落点）；
与 ``library_id`` 互斥，非空时 ``kind`` 必填。

Revision ID: e3f6b9d4a152
Revises: d2e5a8c3f041
Create Date: 2026-08-02 10:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e3f6b9d4a152"
down_revision: str | None = "d2e5a8c3f041"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("import_watch", schema=None) as batch_op:
        batch_op.add_column(sa.Column("target_path", sa.Text(), nullable=True))


def downgrade() -> None:
    # 回退前清掉自定义目录规则：旧代码把 library_id=NULL 一律当自动路由，
    # 留着这些行会让它们被误当 auto 规则消费
    op.execute("DELETE FROM import_watch WHERE target_path IS NOT NULL")
    with op.batch_alter_table("import_watch", schema=None) as batch_op:
        batch_op.drop_column("target_path")
