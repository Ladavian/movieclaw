"""待识别文件的人工认领与身份复核拍板（routes/libraries 的实现层）。

这是识别链「机器认不准 → 人来拍板」的收口：
- ``claim_files``：把一个或一组待识别文件挂到指定 TMDB 条目（单个认领与
  整组认领共用同一段编排，行为差异只在季集号来源）；
- ``resolve_review``：识别器升级后的复核分歧拍板（采纳建议 / 维持现状），
  两种拍板都把身份来源转为 manual——用户看过并决定过的身份，后续识别器
  升级不再提建议。

两条路径都在身份变更后做库存对账（close_fulfilled_wanted）：新条目的
单元"在库"成立，关闭对应的订阅工单。资产补齐（ensure_assets）是后台
任务，由路由层用 BackgroundTasks 调度——本模块只返回需要补的条目 id。
"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.exceptions import BadRequestException, NotFoundException
from movieclaw_api.services import media_discover
from movieclaw_api.services.library_config import LibraryConfigService
from movieclaw_api.services.library_scan import RESOLVER_VERSION
from movieclaw_api.services.media_library import MediaLibraryService
from movieclaw_api.services.subscription import close_fulfilled_wanted
from movieclaw_db.models import LibraryFile, MediaItem, utcnow
from movieclaw_db.models.library_file import IdentitySource
from movieclaw_db.repositories.library_file_repo import LibraryFileRepository
from movieclaw_media.models import MediaKind


async def claim_files(
    session: AsyncSession,
    file_ids: list[int],
    *,
    tmdb_id: int,
    explicit_unit: tuple[int | None, int | None] | None = None,
) -> tuple[MediaItem, int]:
    """把一组待识别文件认领到指定 TMDB 条目，返回 ``(条目, 认领数)``。

    季集号来源（单个与整组的唯一差异）：
    - ``explicit_unit`` 给定（单个认领）：用调用方指定的季集号；电影不允许
      带季集号（400）。
    - 缺省（整组认领）：每个文件沿用扫描时从文件名解析出的季集号——这正是
      "一次纠正全生效"能成立的前提（片名认不出不代表季集号也认不出）；
      电影统一用 (0,0) 哨兵。

    约束：一次只能认领同一个媒体库内的文件（409 之外的语义用 400 表达）。
    """
    rows = [row for fid in file_ids if (row := await session.get(LibraryFile, fid))]
    if not rows:
        raise NotFoundException("这些台账记录都不存在（可能已被认领或忽略）")
    library_ids = {row.library_id for row in rows}
    if len(library_ids) > 1:
        raise BadRequestException("一次只能认领同一个媒体库内的文件")
    library = await LibraryConfigService(session).get(rows[0].library_id)
    kind = MediaKind(library.kind)
    if explicit_unit is not None and kind is MediaKind.MOVIE and any(explicit_unit):
        raise BadRequestException("电影文件不需要季集号")

    # 经模块属性取 TMDB 客户端（而非 from-import 绑定名），保证测试可打桩
    tmdb = media_discover.get_tmdb_client()
    item = await MediaLibraryService(session, tmdb).ensure_media_item(kind, tmdb_id)
    assert item.id is not None
    repo = LibraryFileRepository(session)
    movie = kind is MediaKind.MOVIE
    for row in rows:
        assert row.id is not None
        if explicit_unit is not None:
            season_number, episode_number = explicit_unit
        else:
            season_number = 0 if movie else row.season_number
            episode_number = 0 if movie else row.episode_number
        await repo.claim_identity(
            row.id,
            media_item_id=item.id,
            season_number=season_number,
            episode_number=episode_number,
            resolved_version=RESOLVER_VERSION,
        )
    # 库存对账：认领让单元"在库"成立，关闭对应的订阅工单
    await close_fulfilled_wanted(session, item.id)
    return item, len(rows)


async def resolve_review(
    session: AsyncSession, file_ids: list[int], *, accept: bool
) -> tuple[int, str | None]:
    """对复核清单里的文件拍板，返回 ``(处理数, 采纳条目标题)``。

    - ``accept=True``：改挂到建议条目；
    - ``accept=False``：维持现有身份。
    两种拍板都把身份来源转为 manual 并清掉建议（不再提醒）。
    改挂后对每个新条目做库存对账，关闭订阅工单。
    """
    rows = [row for fid in file_ids if (row := await session.get(LibraryFile, fid))]
    pending = [row for row in rows if row.review_suggestion]
    if not pending:
        raise NotFoundException("这些文件没有待拍板的复核建议（可能已被处理）")
    accepted_items: set[int] = set()
    title: str | None = None
    for row in pending:
        suggestion = row.review_suggestion or {}
        if accept and suggestion.get("media_item_id"):
            row.media_item_id = suggestion["media_item_id"]
            title = suggestion.get("title") or title
            accepted_items.add(suggestion["media_item_id"])
        row.identity_source = IdentitySource.MANUAL
        row.resolved_version = RESOLVER_VERSION
        row.review_suggestion = None
        row.updated_at = utcnow()
    await session.commit()
    # 库存对账：改挂让新条目的单元"在库"成立，关闭对应的订阅工单
    for item_id in accepted_items:
        await close_fulfilled_wanted(session, item_id)
    return len(pending), title
