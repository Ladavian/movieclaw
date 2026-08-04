"""add library.auto_clear_missing

扫描后自动清理已确认丢失的库存记录（库级开关，默认关）：开启后扫描的
收尾阶段把"本轮确认未回归"的 missing 行从台账删除，file_count 与磁盘
对齐，用户不必再手动执行一次缺失清理。纯加列、带默认值，向前兼容。

Revision ID: e9f2b5da1728
Revises: 6254ae56c723
Create Date: 2026-08-04 10:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e9f2b5da1728"
down_revision: str | None = "6254ae56c723"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("library", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column(
                "auto_clear_missing",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )


def downgrade() -> None:
    with op.batch_alter_table("library", schema=None) as batch_op:
        batch_op.drop_column("auto_clear_missing")
