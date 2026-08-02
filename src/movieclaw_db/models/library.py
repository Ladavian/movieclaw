from __future__ import annotations

from sqlalchemy import JSON, Column
from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin


class Library(TimestampMixin, table=True):
    """媒体库——"我拥有哪些影视内容、放在哪里"的权威定义（docs/design/library.md）。

    L1 阶段的最小形态：库只是"类型 + 落盘根路径"的命名实体，职责是给
    订阅与手动下载提供**入库目标**（save_path 由主根推导）。入库管线的
    transfer_sources、扫描统计等字段随 L2/L3 的消费实现同期加列——
    不预留"存而不用"的配置（moviebot 稻草人配置的教训，见设计文档 1 节）。

    约定：
    - 每库单一类型（movie/tv），命名规范与订阅联通都按类型走；
    - ``root_paths`` 是字符串数组，**第一个为主根**（新入库落主根，
      其余为扩展根，供 L3 盘点对账）——库只有这一套目录体系，对目录的
      用途不做任何假设（它可以同时是下载目录，扫描的完整性检测兜底）；
      "把外部内容搬进库"是独立模块「监听导入」（import_watch）的职责；
    - 每 kind 至多一个默认库（``is_default``），订阅/手动下载不选库时用它。
      不变量由 Repository 维护：同 kind 第一个库自动成为默认；删除默认库时
      默认让给同 kind 剩下最早创建的一个；
    - ``match_rules`` 是库的**收藏范围声明**（docs/design/library-routing.md）：
      条件列表，路由（library_routing.route）据此在同 kind 的库里自动选目标。
      空列表 = 未声明——不参与自动命中，只作为显式指定或默认库兜底的目标。
    """

    __tablename__ = "library"

    id: int | None = Field(default=None, primary_key=True)
    # 展示名（如"电影库"/"剧集库"/"动漫库"），全局唯一
    name: str = Field(index=True, unique=True, description="库的展示名")
    # movie / tv——创建后不可改（订阅按 kind 挂库，改类型会让既有关联失义）
    kind: str = Field(index=True, description="媒体类型：movie / tv")
    # 根路径数组，第一个为主根；路径指 movieclaw 视角的绝对路径
    root_paths: list = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
        description="根路径列表（绝对路径，第一个为主根）",
    )
    # 每 kind 至多一个默认库
    is_default: bool = Field(default=False, description="是否为该类型的默认库")
    # 展示顺序（越小越靠前），决定媒体库首页卡片区与「最近添加」分区的排列。
    # 新库置尾（max+1），用户在库卡片菜单里前移/后移调整；同值按 id 兜底
    sort_order: int = Field(default=0, index=True, description="展示顺序（升序）")
    # 收藏范围声明：条件列表，条件间 AND、条件内 any_of（交集即满足）。
    # 每条形如 {"field": "genres", "op": "any_of", "values": [16]}——
    # genres 存 TMDB genre **ID**（genre 名随刮削语言变化，存名字会在用户
    # 切换 tmdb_language 后静默失效）；origin_countries 存 ISO 3166-1 国家码。
    # 通用条件结构是有意为之：后续加字段（导演/公司/系列）零迁移零引擎改动
    match_rules: list = Field(
        default_factory=list,
        sa_column=Column(JSON, nullable=False),
        description="收藏范围条件列表；空=未声明（只作兜底目标）",
    )
    # 刮削成果镜像写入媒体目录（poster.jpg/fanart.jpg/分集 thumb + 完整 NFO，
    # Kodi/Emby 规范，只增不覆盖不删除——docs/design/metadata.md 6.2）。
    # 默认开：无破坏性且反哺播放器生态；不想污染目录的用户按库关闭
    write_media_assets: bool = Field(default=True, description="刮削图片/NFO 是否写入媒体目录")

    @property
    def primary_root(self) -> str | None:
        """主根路径（新入库的落点）；未配置任何根时为 None。"""
        return self.root_paths[0] if self.root_paths else None
