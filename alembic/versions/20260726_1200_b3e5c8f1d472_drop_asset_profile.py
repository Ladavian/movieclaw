"""drop media_metadata.asset_profile

档位记录机制被"整库刷新即全量重刷"取代（用户决策 2026-07-26）：刷新本身
就会按当前档位重下全部图片，不需要再记一份档位指纹去做差异判断。
不留存而不用的列。

Revision ID: b3e5c8f1d472
Revises: a9d2f7c4e830
Create Date: 2026-07-26 12:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b3e5c8f1d472"
down_revision: str | None = "a9d2f7c4e830"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("media_metadata", schema=None) as batch_op:
        batch_op.drop_column("asset_profile")


def downgrade() -> None:
    with op.batch_alter_table("media_metadata", schema=None) as batch_op:
        batch_op.add_column(
            sa.Column("asset_profile", sa.String(), nullable=False, server_default="")
        )
