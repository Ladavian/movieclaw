"""rename match watermark namespace to dotted convention

被动匹配水位从直写 app_setting 收编进配置内核（@register_setting +
SettingStore），namespace 随之遵守点号分层约定：

    subscription_match_watermark → subscription.match_watermark

纯数据迁移：value_json 结构不变（{"last_id": N}），与新模型字段一致，
无需转换。若新 namespace 已存在（理论上不可能），保守跳过改名。

Revision ID: a8b1d4f9e607
Revises: f7a0c3e8d596
Create Date: 2026-07-28 12:00:00.000000

"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "a8b1d4f9e607"
down_revision: str | None = "f7a0c3e8d596"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

_OLD = "subscription_match_watermark"
_NEW = "subscription.match_watermark"


def upgrade() -> None:
    conn = op.get_bind()
    exists = conn.execute(
        sa.text("SELECT 1 FROM app_setting WHERE namespace = :ns"), {"ns": _NEW}
    ).first()
    if exists is None:
        conn.execute(
            sa.text("UPDATE app_setting SET namespace = :new WHERE namespace = :old"),
            {"new": _NEW, "old": _OLD},
        )


def downgrade() -> None:
    conn = op.get_bind()
    conn.execute(
        sa.text("UPDATE app_setting SET namespace = :old WHERE namespace = :new"),
        {"new": _NEW, "old": _OLD},
    )
