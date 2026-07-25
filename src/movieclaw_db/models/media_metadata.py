from __future__ import annotations

from datetime import date, datetime

from sqlalchemy import JSON, Column, Float, ForeignKey, Index, Integer, Text, UniqueConstraint
from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin


class MediaMetadata(TimestampMixin, table=True):
    """条目展示元数据——自足媒体库的"作品档案"（docs/design/metadata.md 2.1）。

    定位
    ----
    与 ``media_item`` 1:1。身份层（media_item）与展示层（本表）分开的原因：
    两层的写入方、刷新节奏、失败语义都不同——刮削失败只是详情页降级，
    订阅追新、种子匹配、库存台账不受影响。

    元数据挂**全局条目**而非库（moviebot 把 media_metadata 挂 library_id 的
    反面教训：同一部片两个库两份元数据）：一部片全局一份档案，无论文件
    散在几个库。

    演职员以 JSON 内嵌（前 40 位，与 NFO 展示上限一致），不建 person 实体表
    ——暂无"按演员浏览"需求，建表属过度工程；未来做演员页时再迁移。

    三态铁律：可缺失字段 NULL=未知（TMDB 也没有 / 尚未下载），
    语义空值用空串/空列表。
    """

    __tablename__ = "media_metadata"

    id: int | None = Field(default=None, primary_key=True)

    # 条目删除时档案级联删除（engine 已开启 SQLite 外键约束）
    media_item_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("media_item.id", ondelete="CASCADE"),
            nullable=False,
            unique=True,
            index=True,
        ),
        description="所属媒体条目（1:1）",
    )

    # -- 文案 ---------------------------------------------------------------
    overview: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True), description="简介；NULL=TMDB 也没有"
    )
    tagline: str | None = Field(default=None, description="宣传语")

    # -- 事实字段 ------------------------------------------------------------
    genres: list = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
        description="类型（如 [\"剧情\", \"科幻\"]）",
    )
    runtime_minutes: int | None = Field(
        default=None, description="片长（电影）/单集常规时长（剧集）"
    )
    release_date: date | None = Field(default=None, description="上映（电影）/首播（剧集）日期")
    content_rating: str | None = Field(default=None, description="分级（优先 CN，无则 US）")
    original_language: str | None = Field(default=None, description="原始语言")
    origin_countries: list = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
        description="制片国家/地区代码",
    )
    studios: list = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
        description="电影=制作公司，剧集=播出网络",
    )

    # -- 评分 ---------------------------------------------------------------
    vote_average: float | None = Field(
        default=None, sa_column=Column(Float, nullable=True), description="TMDB 评分（0~10）"
    )
    vote_count: int | None = Field(default=None, description="评分人数")

    # -- 演职员（JSON 内嵌）--------------------------------------------------
    directors: list = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
        description="电影=导演，剧集=创作者",
    )
    cast: list = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
        description="演员表 JSON：[{name, character, order, profile_path}]，前 40 位",
    )

    # -- 本地图片资产（相对 data/metadata/images/，NULL=未下载/下载失败）------
    poster_file: str | None = Field(default=None, description="本地海报资产相对路径")
    backdrop_file: str | None = Field(default=None, description="本地剧照资产相对路径")

    # -- 手动选图锁（Emby/TMM 同款语义：改过即锁）---------------------------
    # 用户在「更换图片」里挑过的图，自动选图策略与 force 刷新都**不再覆盖**
    # ——精挑的图被下一次刷新冲掉，比选不了图更伤。想换回自动选的图，
    # 在弹层里点「恢复自动」解锁即可
    poster_locked: bool = Field(default=False, description="海报由用户手动选定，刷新不覆盖")
    backdrop_locked: bool = Field(default=False, description="背景由用户手动选定，刷新不覆盖")

    # -- 刮削台账 ------------------------------------------------------------
    scraped_at: datetime | None = Field(default=None, description="最近一次成功刮削时间")
    scrape_language: str = Field(default="", description="刮削使用的 TMDB language")


class MediaEpisode(TimestampMixin, table=True):
    """分集展示元数据——集数据的唯一事实源（docs/design/metadata.md 2.2）。

    既服务详情页分集区（集名/简介/剧照），也服务订阅的期望集合展开
    （``expected_units`` 按 air_date 判断已播/未播）。原 ``media_season.episodes``
    JSON 已由本表取代：单集简介可达数百字，几百集的剧一行 JSON 会膨胀到
    MB 级，且按集 upsert / 按季查询都需要真正的行。

    注意与引用约定的边界（library.md 第 1 节决策不变）：``library_file`` /
    ``wanted_item`` 仍用 (season_number, episode_number) 数字对引用，
    不对本表设外键——本表行可随刷新增删，引用不能跟着摇晃。
    """

    __tablename__ = "media_episode"
    __table_args__ = (
        # 刷新按 (条目, 季, 集) upsert 的依据
        UniqueConstraint(
            "media_item_id",
            "season_number",
            "episode_number",
            name="uq_media_episode_unit",
        ),
        # 详情页分集区逐季读取的热路径
        Index("ix_media_episode_item_season", "media_item_id", "season_number"),
    )

    id: int | None = Field(default=None, primary_key=True)

    media_item_id: int = Field(
        sa_column=Column(
            Integer,
            ForeignKey("media_item.id", ondelete="CASCADE"),
            nullable=False,
            index=True,
        ),
        description="所属媒体条目",
    )
    season_number: int = Field(description="季号；0=特别季")
    episode_number: int = Field(description="集号")

    name: str = Field(default="", description="集名；语义空值为空串")
    overview: str | None = Field(
        default=None, sa_column=Column(Text, nullable=True), description="单集简介"
    )
    air_date: date | None = Field(default=None, description="播出日期；NULL=未定档")
    runtime_minutes: int | None = Field(default=None, description="单集时长")
    vote_average: float | None = Field(
        default=None, sa_column=Column(Float, nullable=True), description="TMDB 单集评分"
    )
    still_path: str | None = Field(default=None, description="TMDB 剧照相对路径")
    still_file: str | None = Field(default=None, description="本地剧照资产相对路径")
