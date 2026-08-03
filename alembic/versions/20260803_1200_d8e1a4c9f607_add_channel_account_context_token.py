"""add channel_account.context_token

微信主动推送修复:iLink 网关的发送接口必须绑定会话,会话令牌只随入站消息
下发,而订阅投递/入库完成这类主动推送没有对应的入站消息。这里把最近一次
入站的令牌持久化下来供推送复用。设计背景见
movieclaw_db.models.channel_account 模块头。

Revision ID: d8e1a4c9f607
Revises: c7d0f3b8e596
Create Date: 2026-08-03 12:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d8e1a4c9f607"
down_revision: str | None = "c7d0f3b8e596"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("channel_account", sa.Column("context_token", sa.Text(), nullable=True))


def downgrade() -> None:
    op.drop_column("channel_account", "context_token")
