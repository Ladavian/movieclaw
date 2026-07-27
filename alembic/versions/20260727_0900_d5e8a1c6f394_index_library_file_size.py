"""add (library_id, size_bytes) index on library_file

改名归并（library_scan._try_relink）在**每个新文件**落账前都要查一次「同库同
尺寸的旧行」。size_bytes 上没有索引，这条查询只能靠 library_id 的索引把范围
缩到本库，然后逐行比尺寸——于是首次扫描是 O(文件数²)：2 万个文件的库要做
2 万次 2 万行的扫描。加上复合索引后这条查询变成一次索引定位。

放 (library_id, size_bytes) 而不是单列 size_bytes：查询恒带库过滤，复合索引
能一次定位到「本库 + 该尺寸」，单列索引还要回表逐行筛库。

Revision ID: d5e8a1c6f394
Revises: c4d7a9e2b581
Create Date: 2026-07-27 09:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "d5e8a1c6f394"
down_revision: str | None = "c4d7a9e2b581"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_index(
        "ix_library_file_library_size",
        "library_file",
        ["library_id", "size_bytes"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_library_file_library_size", table_name="library_file")
