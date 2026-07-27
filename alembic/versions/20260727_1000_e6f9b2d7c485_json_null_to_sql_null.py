"""normalize library_file tri-state JSON columns from JSON 'null' to SQL NULL

存量数据订正。SQLAlchemy 的 JSON 类型默认把 Python None 序列化成 JSON 的
``null``（文本 'null'），于是这四个「三态」列在 SQL 层压根不是 NULL：

- ``WHERE review_suggestion IS NOT NULL`` 匹配到**每一行**——身份复核清单
  因此把整个媒体库都列了出来（LibraryFileRepository.list_review）；
- ``WHERE audio_streams IS NULL`` 匹配不到任何行——「装好 ffmpeg 后重新
  扫描补探规格」的候选集永远是空的。

模型侧已改用 JSON(none_as_null=True)，新写入的行落的是真 SQL NULL；这里把
既有行一并订正。列本身可空，'null' 文本又不是任何合法取值（三态语义里
「没有」就是 NULL），所以直接改写是安全的、且幂等。

Revision ID: e6f9b2d7c485
Revises: d5e8a1c6f394
Create Date: 2026-07-27 10:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "e6f9b2d7c485"
down_revision: str | None = "d5e8a1c6f394"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_COLUMNS = (
    "audio_streams",
    "subtitle_streams",
    "unidentified_candidates",
    "review_suggestion",
)


def upgrade() -> None:
    for column in _COLUMNS:
        op.execute(f"UPDATE library_file SET {column} = NULL WHERE {column} = 'null'")  # noqa: S608


def downgrade() -> None:
    # 不回填 'null' 文本：那是个 bug 形态，没有任何代码依赖它。
    # 旧版本代码读回来同样是 Python None，行为一致
    pass
