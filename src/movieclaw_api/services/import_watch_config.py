"""监听导入规则的配置服务：CRUD 与校验（媒体库之上的独立功能）。

规则 = 源目录 → 目标 的搬运声明（策略：硬链接/复制），由
``library_ingest`` 引擎消费。目标二选一（docs/design/library-routing.md 2.3）：

- **指定库**（``library_id`` 非空）：内容固定进该库；
- **自动路由**（``library_id`` 为 NULL + ``kind`` 必填）：识别出作品后按
  各库的收藏范围声明选库。kind 仍须先验（识别链按 movie/tv 分叉）；
  每 kind 至多一条 auto 规则——多条会让订阅投递的落点歧义。

校验要点：

- 源目录不得与**任何**库的根路径前缀重叠（双头管理必乱；库侧改根路径
  时做反向校验，见 LibraryConfigService）；
- 源目录全局唯一（数据库唯一索引兜底，这里给可读中文报错）；
- 策略选硬链接时做**同盘检测**（源目录与目标库主根的 st_dev 比对）——
  把"跨文件系统无法硬链"从第一次搬运失败前置到保存配置时；auto 规则
  对该 kind **全部可能目标库**（声明收藏范围的库 + 默认库）逐一检测，
  列出不同盘的库名；任一目录尚不存在（挂载未就绪）时跳过检测，
  搬运失败的中文引导兜底。
"""

from __future__ import annotations

import logging
import os

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_api.exceptions import BadRequestException, NotFoundException
from movieclaw_db.models import ImportWatch, Library
from movieclaw_db.models.base import utcnow

logger = logging.getLogger("movieclaw_api.import_watch_config")

STRATEGIES = ("hardlink", "copy")
_KINDS = ("movie", "tv")
_KIND_LABELS = {"movie": "电影", "tv": "剧集"}


def rule_target_label(rule: ImportWatch, library_name: str | None) -> str:
    """规则目标的展示名：库名 或「自动路由（电影/剧集）」（日志与接口共用）。"""
    if rule.library_id is not None:
        return f"「{library_name or '?'}」"
    return f"自动路由（{_KIND_LABELS.get(rule.kind or '', rule.kind or '?')}）"


class ImportWatchConfigService:
    """监听导入规则的业务服务。绑定一个数据库会话。"""

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def list_all(self) -> list[ImportWatch]:
        result = await self._session.execute(select(ImportWatch).order_by(ImportWatch.id))
        return list(result.scalars().all())

    async def get(self, rule_id: int) -> ImportWatch:
        row = await self._session.get(ImportWatch, rule_id)
        if row is None:
            raise NotFoundException(f"监听导入规则不存在：id={rule_id}")
        return row

    async def create(
        self,
        *,
        source_path: str,
        strategy: str,
        library_id: int | None,
        kind: str | None = None,
    ) -> ImportWatch:
        source, kind = await self._validate(
            source_path=source_path, strategy=strategy, library_id=library_id, kind=kind
        )
        row = ImportWatch(source_path=source, strategy=strategy, library_id=library_id, kind=kind)
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        await _refresh_watcher()
        library = await self._session.get(Library, library_id) if library_id else None
        logger.info(
            "已创建监听导入规则：%s → %s（%s）",
            source,
            rule_target_label(row, library.name if library else None),
            "硬链接" if strategy == "hardlink" else "复制",
        )
        return row

    async def update(
        self,
        rule_id: int,
        *,
        source_path: str,
        strategy: str,
        library_id: int | None,
        kind: str | None = None,
    ) -> ImportWatch:
        row = await self.get(rule_id)
        source, kind = await self._validate(
            source_path=source_path,
            strategy=strategy,
            library_id=library_id,
            kind=kind,
            exclude_id=rule_id,
        )
        row.source_path = source
        row.strategy = strategy
        row.library_id = library_id
        row.kind = kind
        row.updated_at = utcnow()
        await self._session.commit()
        await self._session.refresh(row)
        await _refresh_watcher()
        return row

    async def delete(self, rule_id: int) -> None:
        row = await self.get(rule_id)
        await self._session.delete(row)
        await self._session.commit()
        await _refresh_watcher()
        logger.info("已删除监听导入规则：%s", row.source_path)

    # -- 校验 --------------------------------------------------------------

    async def _validate(
        self,
        *,
        source_path: str,
        strategy: str,
        library_id: int | None,
        kind: str | None,
        exclude_id: int | None = None,
    ) -> tuple[str, str | None]:
        source = source_path.strip().rstrip("/")
        if not source or not source.startswith("/"):
            raise BadRequestException("源目录必须是绝对路径")
        if strategy not in STRATEGIES:
            raise BadRequestException("搬运策略必须是硬链接（hardlink）或复制（copy）")

        # 目标解析：指定库（kind 由库推导、存 NULL）或 auto（kind 必填）
        hardlink_targets: list[Library]
        if library_id is not None:
            library = await self._session.get(Library, library_id)
            if library is None:
                raise NotFoundException(f"目标媒体库不存在：id={library_id}")
            if not library.primary_root:
                raise BadRequestException(
                    f"媒体库「{library.name}」没有配置根路径，无法作为导入目标"
                )
            kind = None
            hardlink_targets = [library]
        else:
            if kind not in _KINDS:
                raise BadRequestException("自动路由规则必须指定媒体类型（movie / tv）")
            # 每 kind 至多一条 auto 规则：多条会让订阅投递的落点歧义
            existing_auto = (
                await self._session.execute(
                    select(ImportWatch).where(
                        ImportWatch.library_id.is_(None),  # type: ignore[union-attr]
                        ImportWatch.kind == kind,
                    )
                )
            ).scalar_one_or_none()
            if existing_auto is not None and existing_auto.id != exclude_id:
                raise BadRequestException(
                    f"{_KIND_LABELS[kind]}已有自动路由规则（{existing_auto.source_path}）——"
                    "每个类型至多一条，请编辑既有规则"
                )
            candidates = list(
                (await self._session.execute(select(Library).where(Library.kind == kind)))
                .scalars()
                .all()
            )
            # auto 目录的可能目标不止"声明库+默认库"：订阅认领的内容沿用
            # 订阅**定格**的库，而定格可以是用户手选的任意同类型库——
            # 硬链同盘检测必须覆盖全部有根路径的同类型库，漏一个都会在
            # 未来某次搬运时才暴露跨盘失败
            hardlink_targets = [lib for lib in candidates if lib.primary_root]
            routable = [
                lib
                for lib in candidates
                if (lib.match_rules or lib.is_default) and lib.primary_root
            ]
            if not routable:
                raise BadRequestException(
                    f"当前没有任何可路由的{_KIND_LABELS[kind]}库（需要有根路径的默认库"
                    "或声明了收藏范围的库），请先到「媒体库」创建"
                )

        # 与所有库的根路径不重叠（不只是目标库：落在任何库根下都会被扫描双头管理）
        libraries = list((await self._session.execute(select(Library))).scalars().all())
        for lib in libraries:
            for root in lib.root_paths:
                r = root.rstrip("/")
                if source == r or source.startswith(r + "/") or r.startswith(source + "/"):
                    raise BadRequestException(
                        f"源目录与媒体库「{lib.name}」的根路径重叠：{source} ↔ {root}"
                    )

        # 源目录全局唯一
        existing = (
            await self._session.execute(
                select(ImportWatch).where(ImportWatch.source_path == source)
            )
        ).scalar_one_or_none()
        if existing is not None and existing.id != exclude_id:
            raise BadRequestException(f"该源目录已有监听导入规则：{source}")

        # 硬链接的同盘检测：跨盘当场提示，不留到第一次搬运失败。auto 规则
        # 逐库检测——路由到哪个库都可能，任何一个不同盘将来都会搬运失败
        if strategy == "hardlink":
            offenders: list[str] = []
            try:
                source_dev = os.stat(source).st_dev
            except OSError:
                source_dev = None  # 目录未就绪（挂载中）：跳过检测，搬运失败的中文引导兜底
            if source_dev is not None:
                for lib in hardlink_targets:
                    try:
                        root_dev = os.stat(lib.primary_root).st_dev  # type: ignore[arg-type]
                    except OSError:
                        continue
                    if root_dev != source_dev:
                        offenders.append(lib.name)
            if offenders:
                names = "」「".join(offenders)
                raise BadRequestException(
                    f"源目录与媒体库「{names}」的主根不在同一文件系统，"
                    "硬链接无法工作；请把策略改为「复制」，或把它们放到同一存储卷"
                )
        return source, kind


async def resolve_dispatch_rule(
    session: AsyncSession, library_id: int | None, *, kind: str | None = None
) -> ImportWatch | None:
    """投递会命中的监听导入规则：目标库的专属规则 → 同 kind 的自动路由规则 → None。

    订阅/手动下载止于投递——把种子投到会被监听导入接管的目录（分离布局），
    或不指定目录退下载器默认。auto 规则兜底让"一个混合下载目录服务所有库"
    成立：完成后按 info_hash 认领回订阅身份，目标库=订阅定格的库，与投递
    预检的结论必然一致（docs/design/library-routing.md 2.3）。
    多条规则指向同一库时取最早创建的一条。
    投递方（dispatch/预检）只关心源目录，链路体检还要读策略——故返回规则本体。
    """
    if library_id is not None:
        rule = (
            (
                await session.execute(
                    select(ImportWatch)
                    .where(ImportWatch.library_id == library_id)
                    .order_by(ImportWatch.id)
                )
            )
            .scalars()
            .first()
        )
        if rule is not None:
            return rule
    if kind is not None:
        return (
            (
                await session.execute(
                    select(ImportWatch)
                    .where(
                        ImportWatch.library_id.is_(None),  # type: ignore[union-attr]
                        ImportWatch.kind == kind,
                    )
                    .order_by(ImportWatch.id)
                )
            )
            .scalars()
            .first()
        )
    return None


async def _refresh_watcher() -> None:
    """规则变更后重建监听（监听器未启动时为 no-op）。"""
    from movieclaw_api.services.library.ingest import get_ingest_watcher

    watcher = get_ingest_watcher()
    if watcher is not None:
        await watcher.refresh_watches()
