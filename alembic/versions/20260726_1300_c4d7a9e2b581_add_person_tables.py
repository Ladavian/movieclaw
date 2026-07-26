"""add person and media_item_person tables

演职员的一等公民化：把「人」从 media_metadata.cast 的 JSON 里独立成实体表，
让「这个演员在我库里还有哪些片」这种反向查询成立（人物页 /people/[id]）。
media_metadata.cast 保留不动——它是该影片档案的一部分（写 NFO、详情页演职员
条都读它，断网可用），与全局人物索引职责不同。

已有条目不做数据回填：老的 cast JSON 里没有 TMDB person id，姓名不足以充当
身份（同名不同人 / 一人多译名）。存量库在下一次「刷新元数据」时自然补齐，
人物页对尚未补齐的条目表现为「查不到这个人」而不是给出错的关联。

Revision ID: c4d7a9e2b581
Revises: b3e5c8f1d472
Create Date: 2026-07-26 13:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
import sqlmodel
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "c4d7a9e2b581"
down_revision: str | None = "b3e5c8f1d472"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    # 影人实体：以 TMDB person id 为唯一锚（姓名不是身份，见模型 docstring）
    op.create_table(
        "person",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("tmdb_person_id", sa.Integer(), nullable=False),
        sa.Column("name", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("original_name", sa.Text(), nullable=True),
        sa.Column("profile_path", sa.Text(), nullable=True),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("tmdb_person_id", name="uq_person_tmdb"),
    )
    op.create_index("ix_person_name", "person", ["name"], unique=False)

    # 影片 ↔ 影人关系：department 区分 cast / director
    op.create_table(
        "media_item_person",
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("id", sa.Integer(), nullable=False),
        sa.Column("media_item_id", sa.Integer(), nullable=False),
        sa.Column("person_id", sa.Integer(), nullable=False),
        sa.Column("department", sqlmodel.sql.sqltypes.AutoString(), nullable=False),
        sa.Column("character", sa.Text(), nullable=True),
        sa.Column("credit_order", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["media_item_id"], ["media_item.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["person_id"], ["person.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "media_item_id", "person_id", "department", name="uq_item_person_department"
        ),
    )
    op.create_index(
        "ix_media_item_person_media_item_id", "media_item_person", ["media_item_id"], unique=False
    )
    op.create_index(
        "ix_item_person_person", "media_item_person", ["person_id", "department"], unique=False
    )


def downgrade() -> None:
    op.drop_index("ix_item_person_person", table_name="media_item_person")
    op.drop_index("ix_media_item_person_media_item_id", table_name="media_item_person")
    op.drop_table("media_item_person")
    op.drop_index("ix_person_name", table_name="person")
    op.drop_table("person")
