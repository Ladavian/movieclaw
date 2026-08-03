"""add import_watch.process_existing / baseline_done: 存量处理选项

监听导入规则新增「是否整理存量」：``process_existing`` 为 False 时，
首轮巡检把源目录里当时已有的条目批量标记为已忽略（ingest_entry 台账行，
可在清单中恢复），之后只处理新增；``baseline_done`` 记录该基线是否已打。
既有规则回填为 True（整理存量）——与升级前行为完全一致。

Revision ID: a5b8d1f6c374
Revises: f4a7c0e5b263
Create Date: 2026-08-03 09:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a5b8d1f6c374"
down_revision: str | None = "f4a7c0e5b263"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("import_watch", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("process_existing", sa.Boolean(), nullable=False, server_default=sa.true())
        )
        batch_op.add_column(
            sa.Column("baseline_done", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    # 已打基线的 ignored 台账行保留：旧代码把它当"非 failed"跳过（指纹不变
    # 不重处理），行为安全；新状态 pending/ignored 对旧代码同理无害
    with op.batch_alter_table("import_watch", schema=None) as batch_op:
        batch_op.drop_column("baseline_done")
        batch_op.drop_column("process_existing")
