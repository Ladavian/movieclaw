"""add ingest_entry.claimed_tmdb_id/claimed_kind: 持久化人工认领的身份

此前认领身份（claim_entry 的 forced_item）只是一次性入参：认领当轮因
环境故障失败（探测失败/搬运出错/TMDB 不可达 → failed）后，自动退避
重试会重新走识别链、识别不出便落回 pending——用户的拍板被悄悄作废。
把认领身份钉进台账（``claimed_tmdb_id`` + ``claimed_kind``），重试时
优先按钉住的身份重建条目，不再重新识别。NULL = 未认领，与迁移前行为
完全一致；仅加列，向前兼容。

Revision ID: c7d0f3b8e596
Revises: b6c9e2a7d485
Create Date: 2026-08-03 11:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c7d0f3b8e596"
down_revision: str | None = "b6c9e2a7d485"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("ingest_entry", schema=None) as batch_op:
        batch_op.add_column(sa.Column("claimed_tmdb_id", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("claimed_kind", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("ingest_entry", schema=None) as batch_op:
        batch_op.drop_column("claimed_kind")
        batch_op.drop_column("claimed_tmdb_id")
