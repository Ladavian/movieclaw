"""add unidentified_code to library_file

待识别清单此前只有 ``unidentified_reason``（给人看的整句）：前端要给不同
失败原因配不同的标签/颜色/动作，只能去猜那句话的前缀。文案会随体验迭代
反复改写，分类不该跟着晃——拆出稳定的 code（unparsable / tmdb_unreachable
/ ambiguous / no_match），文案继续只负责"说人话"。

Revision ID: a1c9e4b7d203
Revises: f8c1e6a3b592
Create Date: 2026-07-26 11:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a1c9e4b7d203"
down_revision: str | None = "f8c1e6a3b592"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("library_file", schema=None) as batch_op:
        batch_op.add_column(sa.Column("unidentified_code", sa.String(), nullable=True))
        batch_op.create_index("ix_library_file_unidentified_code", ["unidentified_code"])


def downgrade() -> None:
    with op.batch_alter_table("library_file", schema=None) as batch_op:
        batch_op.drop_index("ix_library_file_unidentified_code")
        batch_op.drop_column("unidentified_code")
