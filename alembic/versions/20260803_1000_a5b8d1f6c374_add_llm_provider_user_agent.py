"""add llm_provider.user_agent: 自定义 AI 端点的 User-Agent

接入自建网关 / 反代（openai_compat、或给 OpenAI 官方配镜像端点）时，
对端常按 User-Agent 放行或限流，而 openai SDK 固定发送自带的
「AsyncOpenAI/Python x.y.z」。加 ``user_agent`` 让用户可覆盖：
留空（NULL）保持 SDK 默认行为，与迁移前完全一致。

Revision ID: a5b8d1f6c374
Revises: f4a7c0e5b263
Create Date: 2026-08-03 10:00:00.000000

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
    with op.batch_alter_table("llm_provider", schema=None) as batch_op:
        batch_op.add_column(sa.Column("user_agent", sa.String(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("llm_provider", schema=None) as batch_op:
        batch_op.drop_column("user_agent")
