"""add channel_account.agent_session_id

微信通道对话接入 Web「最近会话」:每个绑定账号持有一个当前 Agent 会话 id,
转录复用 agent_session 体系,/reset 清空后下条消息新建。设计背景见
movieclaw_db.models.channel_account 模块头。

Revision ID: d2e5a8c3f041
Revises: c1d4f7a2b930
Create Date: 2026-08-01 15:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d2e5a8c3f041"
down_revision: str | None = "c1d4f7a2b930"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "channel_account", sa.Column("agent_session_id", sa.String(), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("channel_account", "agent_session_id")
