"""add system_notice table

全局待处理事项（状态化告警中心）：需要用户行动才能推进的运行时故障，
按 dedupe_key 幂等收敛、问题消失时自动 resolve。设计背景见
movieclaw_db.models.system_notice 模块头。

Revision ID: b9c2e5a0f718
Revises: a8b1d4f9e607
Create Date: 2026-07-31 09:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b9c2e5a0f718"
down_revision: str | None = "a8b1d4f9e607"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "system_notice",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("dedupe_key", sa.Text(), nullable=False),
        sa.Column("severity", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("title", sa.String(), nullable=False),
        sa.Column("message", sa.Text(), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("resolved_at", sa.DateTime(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_system_notice_dedupe_key"), "system_notice", ["dedupe_key"], unique=True
    )
    op.create_index(op.f("ix_system_notice_source"), "system_notice", ["source"], unique=False)
    op.create_index(op.f("ix_system_notice_status"), "system_notice", ["status"], unique=False)


def downgrade() -> None:
    op.drop_index(op.f("ix_system_notice_status"), table_name="system_notice")
    op.drop_index(op.f("ix_system_notice_source"), table_name="system_notice")
    op.drop_index(op.f("ix_system_notice_dedupe_key"), table_name="system_notice")
    op.drop_table("system_notice")
