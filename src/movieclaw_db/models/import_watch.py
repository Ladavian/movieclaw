from __future__ import annotations

from sqlalchemy import Column, ForeignKey, Integer, Text
from sqlmodel import Field

from movieclaw_db.models.base import TimestampMixin


class ImportWatch(TimestampMixin, table=True):
    """监听导入规则——媒体库之上的独立功能：源目录 → 目标库的搬运配置。

    设计定位（与媒体库解耦的关键）：媒体库只有一套目录体系（根路径），
    只做盘点与守护、不承载"内容从哪来"的假设；把下载完成的内容搬进库
    是本模块的职责。每条规则声明：监听哪个源目录、完成的内容硬链/复制
    到哪里。

    目标三态（docs/design/library-routing.md 2.3、docs/design/strm-workflow.md）：
    - **指定库**：``library_id`` 非空，内容固定进该库（识别按库类型走）；
    - **自动路由**：``library_id`` 与 ``target_path`` 均为 NULL 且 ``kind``
      必填——识别出作品后按各库的收藏范围声明（library_routing.route）决定
      目标库；kind 仍须先验，因为识别链的语义按 movie/tv 分叉（条目名解析、
      季集分配不同）。每 kind 至多一条 auto 规则（多条会让投递落点歧义）；
    - **自定义目录**：``target_path`` 非空 + ``kind`` 必填——识别改名后落
      该目录、**不进入任何媒体库**（不写库台账、不生成资产）。适合整理
      结果还需外部流转（上传网盘、转存、人工确认）再进库的场景：文件
      后续出现在某个库根时由扫描自动入账。每 kind 至多一条（同 auto 理由）。

    约束（写入侧校验，import_watch_config）：
    - ``library_id`` 与 ``target_path`` 互斥；
    - 源目录全局唯一，且不得与**任何**库的根路径前缀重叠——落在库根下
      会被那个库当存量扫走，双头管理必乱；``target_path`` 同理，还不得
      与任何监听源目录重叠（整理输出落回监听区会被再次消费）；
    - 策略 hardlink 时保存即做同盘检测（指定库对其主根；auto 对该 kind
      全部可能目标库的主根逐一检测；自定义目录对 ``target_path``）。
    """

    __tablename__ = "import_watch"

    id: int | None = Field(default=None, primary_key=True)

    source_path: str = Field(
        sa_column=Column(Text, nullable=False, unique=True, index=True),
        description="监听的源目录（movieclaw 视角的绝对路径）",
    )
    strategy: str = Field(description="搬运策略：hardlink（需同盘）/ copy（可跨盘）")
    library_id: int | None = Field(
        default=None,
        sa_column=Column(
            Integer,
            ForeignKey("library.id", ondelete="CASCADE"),
            nullable=True,
            index=True,
        ),
        description="目标媒体库；NULL=自动路由（按收藏范围选库，此时 kind 必填）",
    )
    kind: str | None = Field(
        default=None,
        description="自动路由/自定义目录的媒体类型（movie/tv）；指定库时为 NULL（由库推导）",
    )
    target_path: str | None = Field(
        default=None,
        sa_column=Column(Text, nullable=True),
        description="自定义目录目标（绝对路径）；与 library_id 互斥，非空时 kind 必填",
    )
