"""add asset_profile to media_metadata

图片资产的尺寸档位组合（"海报|背景|剧照"）。用户调整 TMDB_*_SIZE 配置后，
存量资产与当前档位不符即在下次刷新时按新档位重下——否则调高画质只对
新条目生效，存量库永远停在旧档位。

Revision ID: a9d2f7c4e830
Revises: a1c9e4b7d203
Create Date: 2026-07-26 11:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a9d2f7c4e830"
down_revision: str | None = "a1c9e4b7d203"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("media_metadata", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("asset_profile", sa.String(), nullable=False, server_default="")
        )


def downgrade() -> None:
    with op.batch_alter_table("media_metadata", schema=None) as batch_op:
        batch_op.drop_column("asset_profile")
