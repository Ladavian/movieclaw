"""add poster_locked / backdrop_locked to media_metadata

手动选图锁（docs/design/metadata.md 6.3）：用户在「更换图片」里挑过的图，
自动选图策略与 force 刷新都不再覆盖——精挑的图被下次刷新冲掉，比选不了
图更伤（Emby/TMM 同款"改过即锁"语义）。

Revision ID: f8c1e6a3b592
Revises: e7b0d5f2a481
Create Date: 2026-07-26 10:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "f8c1e6a3b592"
down_revision: str | None = "e7b0d5f2a481"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("media_metadata", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("poster_locked", sa.Boolean(), nullable=False, server_default=sa.false())
        )
        batch_op.add_column(
            sa.Column("backdrop_locked", sa.Boolean(), nullable=False, server_default=sa.false())
        )


def downgrade() -> None:
    with op.batch_alter_table("media_metadata", schema=None) as batch_op:
        batch_op.drop_column("backdrop_locked")
        batch_op.drop_column("poster_locked")
