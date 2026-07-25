"""add ignored_at to library_file

待识别清单的「忽略」此前是**硬删台账行**，而扫描器判定"新文件"的依据就是
台账里有没有这条路径——行删了，下一次扫描（手动/watchdog 去抖/6 小时对账）
就把它当全新文件重走识别链，照样认不出、照样回到清单。对活跃的库来说忽略
撑不过几分钟，用户看到的是"忽略了又自己回来"。

改为标记：带 ignored_at 的行扫描时直接秒过，不重走识别链、不进清单、不计入
待识别统计，行本身保留——「已忽略」清单里可一键恢复（识别器升级后想重试）。

Revision ID: e7b0d5f2a481
Revises: d6a9c4e1f370
Create Date: 2026-07-25 22:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e7b0d5f2a481"
down_revision: str | None = "d6a9c4e1f370"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("library_file", schema=None) as batch_op:
        batch_op.add_column(sa.Column("ignored_at", sa.DateTime(), nullable=True))
        batch_op.create_index("ix_library_file_ignored_at", ["ignored_at"], unique=False)


def downgrade() -> None:
    with op.batch_alter_table("library_file", schema=None) as batch_op:
        batch_op.drop_index("ix_library_file_ignored_at")
        batch_op.drop_column("ignored_at")
