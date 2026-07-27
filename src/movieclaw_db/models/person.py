from __future__ import annotations

from sqlalchemy import Column, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin


class Person(TimestampMixin, table=True):
    """影人实体——演职员的一等公民化（人物页 /people/[id] 的数据源）。

    为什么从 ``media_metadata.cast`` 的 JSON 里独立出来
    ----------------------------------------------------
    JSON 内嵌的演员表按**影片**存：一个演员参演 N 部片，就在 N 份 JSON 里
    重复 N 次，数据库层面根本没有"人"这个概念。于是「这个演员在我库里还有
    哪些片」这种反向查询无从下手——那正是人物页要回答的问题。

    ``media_metadata.cast`` 保留不动：它是**该影片档案的一部分**（写 NFO、
    详情页演职员条都读它，断网可用），与本表职责不同。本表是全局的人物索引，
    两者由同一次刮削一起写入，不存在谁派生谁。

    以 ``tmdb_person_id`` 为唯一锚，而不是姓名
    -----------------------------------------
    姓名不是身份：TMDB 里同名不同人很常见（多个「李丽」），同一个人又会因
    语言/译名不同而有多种写法。用姓名做键必然出现「两个人被合并成一个」和
    「一个人被拆成两个」，且改译名就会断链。TMDB 的 person id 是稳定的。

    三态铁律同全表：可缺失字段 NULL=未知（TMDB 也没有），语义空值用空串。
    """

    __tablename__ = "person"
    __table_args__ = (
        UniqueConstraint("tmdb_person_id", name="uq_person_tmdb"),
        # 人物搜索/按名筛选用（人物页暂不用，但索引很便宜、避免将来全表扫）
        Index("ix_person_name", "name"),
    )

    id: int | None = Field(default=None, primary_key=True)

    tmdb_person_id: int = Field(description="TMDB 影人 ID（唯一锚）")

    name: str = Field(description="姓名（按刮削语言，通常是中文译名）")
    original_name: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="原名（原语言拼写）；与 name 相同或 TMDB 未提供时为 NULL",
    )
    profile_path: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="TMDB 头像相对路径（如 /abc.jpg）；TMDB 没有这个人的照片时为 NULL",
    )


class MediaItemPerson(TimestampMixin, table=True):
    """影片 ↔ 影人的参演/执导关系（人物页的反向查询靠它）。

    ``department`` 区分身份：``cast``=演员（``character`` 为饰演角色）、
    ``director``=导演/主创（``character`` 为 NULL）。只收这两类——它们是详情页
    实际展示、也是用户会想「按这个人翻库」的身份；编剧、摄影等全量 crew
    动辄上百条，收进来只会把关系表撑大而没人查。

    唯一键取 ``(media_item_id, person_id, department)``：同一个人在同一部片里
    以同一身份只有一行。一人分饰两角（同 department 出现两次）时把角色名合并
    成「A / B」写在同一行——这比放两行更贴近展示需求，也让唯一键保持简单。

    条目或影人删除时级联清理（engine 已开启 SQLite 外键约束）。
    """

    __tablename__ = "media_item_person"
    __table_args__ = (
        UniqueConstraint(
            "media_item_id", "person_id", "department", name="uq_item_person_department"
        ),
        # 人物页的核心查询：按人反查影片，按 department 分组
        Index("ix_item_person_person", "person_id", "department"),
    )

    id: int | None = Field(default=None, primary_key=True)

    media_item_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("media_item.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        )
    )
    person_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("person.id", ondelete="CASCADE"),
            nullable=False,
        )
    )

    department: str = Field(description="身份：cast=演员 / director=导演（剧集为主创）")
    character: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="饰演角色（仅 cast）；一人分饰两角时为「A / B」，未提供为 NULL",
    )
    credit_order: int = Field(
        default=0, description="剧组给的主次顺序（TMDB order），越小越靠前"
    )
