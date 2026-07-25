"""add identity review columns to library_file

身份对账（复核清单）三件套：
- identity_source：身份是怎么来的（manual/nfo/path_tag/resolved）——人工认领
  的身份永不被自动翻案，这是任何自动对账机制的前置条件；
- resolved_version：识别时的识别器版本号。识别链升级（RESOLVER_VERSION +1）
  后，版本落后且非人工的行会在下次扫描时重走识别链复核；
- review_suggestion：复核发现新旧结论不一致时的建议（不直接改身份，进
  「身份复核」清单让用户拍板——宁可待确认，不静默翻案）。

三列均可空：NULL = 特性上线前的旧数据，一律视为「来源未知、版本落后」，
下次扫描全量复核一遍（正是存量库沐浴识别器升级红利的入口）。

Revision ID: d6a9c4e1f370
Revises: c5f8b3d0e269
Create Date: 2026-07-25 21:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d6a9c4e1f370"
down_revision: str | None = "c5f8b3d0e269"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("library_file", schema=None) as batch_op:
        batch_op.add_column(sa.Column("identity_source", sa.String(), nullable=True))
        batch_op.add_column(sa.Column("resolved_version", sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column("review_suggestion", sa.JSON(), nullable=True))


def downgrade() -> None:
    with op.batch_alter_table("library_file", schema=None) as batch_op:
        batch_op.drop_column("review_suggestion")
        batch_op.drop_column("resolved_version")
        batch_op.drop_column("identity_source")
