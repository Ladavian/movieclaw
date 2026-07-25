"""add unidentified_candidates to library_file

收敛放弃时把 TMDB 候选原样存下来：机器判不了的歧义（实测：《风筝》2017
正片 46 集 vs「送审版」51 集，同名同年、别名互相污染）人一眼就能认出来，
待识别清单直接列出候选点选认领，不必让用户自己去 TMDB 查 id 手输。

Revision ID: b4e7a2c9d158
Revises: a3d8e6f4c927
Create Date: 2026-07-25 16:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b4e7a2c9d158"
down_revision: str | None = "a3d8e6f4c927"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("library_file", schema=None) as batch_op:
        batch_op.add_column(sa.Column("unidentified_candidates", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("library_file", schema=None) as batch_op:
        batch_op.drop_column("unidentified_candidates")
