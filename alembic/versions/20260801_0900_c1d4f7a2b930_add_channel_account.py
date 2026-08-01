"""add channel_account table

IM 通道账号表(微信为首个通道):扫码绑定后的凭据(SecretBox 密文)、
就近接入地址、收消息增量游标与白名单用户。设计背景见
movieclaw_db.models.channel_account 模块头。

Revision ID: c1d4f7a2b930
Revises: b9c2e5a0f718
Create Date: 2026-08-01 09:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c1d4f7a2b930"
down_revision: str | None = "b9c2e5a0f718"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "channel_account",
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("channel_id", sa.String(), nullable=False),
        sa.Column("account_id", sa.String(), nullable=False),
        sa.Column("token", sa.String(), nullable=False),
        sa.Column("base_url", sa.String(), nullable=False),
        sa.Column("bound_user_id", sa.String(), nullable=True),
        sa.Column("cursor", sa.Text(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("last_error", sa.String(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        op.f("ix_channel_account_channel_id"), "channel_account", ["channel_id"], unique=False
    )
    op.create_index(
        op.f("ix_channel_account_account_id"), "channel_account", ["account_id"], unique=True
    )


def downgrade() -> None:
    op.drop_index(op.f("ix_channel_account_account_id"), table_name="channel_account")
    op.drop_index(op.f("ix_channel_account_channel_id"), table_name="channel_account")
    op.drop_table("channel_account")
