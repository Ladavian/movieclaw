"""媒体身份层的落库编排：任何入口的订阅先在这里收敛成统一媒体条目。

分工（docs/design/subscription.md 第 1 节）：
- ``movieclaw_media.library``：纯 TMDB 拉取与收敛判定（不碰数据库）；
- ``MediaItemRepository``：原始存取；
- 本服务：把两者接起来——建档幂等、豆瓣入口收敛、来源信息回填。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_db.models import MediaEpisode, MediaItem, MediaMetadata, MediaSeason, utcnow
from movieclaw_db.repositories import MediaItemRepository
from movieclaw_media.library import (
    DoubanResolution,
    MediaProfile,
    ResolveStatus,
    fetch_media_profile,
    resolve_douban_to_tmdb,
)
from movieclaw_media.models import MediaKind
from movieclaw_media.tmdb import TmdbClient

logger = logging.getLogger("movieclaw_api.media_library")


class MediaLibraryService:
    """媒体条目服务：``ensure_media_item`` 是订阅创建链路的第一步。"""

    def __init__(
        self, session: AsyncSession, tmdb_client: TmdbClient, *, language: str = "zh-CN"
    ) -> None:
        self._session = session
        self._repo = MediaItemRepository(session)
        self._client = tmdb_client
        self._language = language

    async def ensure_media_item(
        self,
        kind: MediaKind,
        tmdb_id: int,
        *,
        douban_id: str | None = None,
        extra_aliases: Sequence[str] = (),
    ) -> MediaItem:
        """按锚建档或复用媒体条目（幂等）。

        - 已存在：不重复请求 TMDB（元数据保鲜是刷新任务的职责），只回填
          调用方带来的新信息（douban_id、入口标题等别名）；
        - 不存在：一次拉齐 TMDB 档案落库——身份信息（media_item + 季集）
          与展示元数据（media_metadata / media_episode）同一事务写入，
          这就是"一次入库刮削"的文本部分（图片资产由 ensure_assets 补齐，
          docs/design/metadata.md 4.1）。
        """
        existing = await self._repo.get_by_anchor(kind.value, tmdb_id)
        if existing is not None:
            return await self._backfill(existing, douban_id, extra_aliases)

        profile = await fetch_media_profile(self._client, kind, tmdb_id, language=self._language)
        item, seasons, episodes, metadata = self._to_rows(profile, douban_id, extra_aliases)
        try:
            item = await self._repo.create_with_seasons(item, seasons, episodes, metadata)
        except IntegrityError:
            # 并发建档撞唯一锚：让出胜者，读回对方落库的条目
            await self._repo.rollback()
            logger.info("媒体条目并发建档冲突，复用已有条目：%s/%s", kind.value, tmdb_id)
            existing = await self._repo.get_by_anchor(kind.value, tmdb_id)
            if existing is None:  # 理论不可达：冲突意味着对方已提交
                raise
            return await self._backfill(existing, douban_id, extra_aliases)
        # 影人关系要等 media_item.id 落库后才能建（人物页的数据来源）。
        # 与刷新路径（media_scrape.apply_display_profile）同一个函数，口径一致。
        #
        # 必须自己 commit：create_with_seasons 已经提交过，这里写的是**新事务**，
        # 而 get_session 明确不提交（事务边界交给上层）。调用方里只有扫描链路
        # 后续恰好会提交，订阅 prepare 那条只读不提交——不自己收口的话，
        # 通过订阅建档的条目会静默丢掉全部影人数据。
        from movieclaw_api.services.media_scrape import apply_people_credits

        if item.id is not None:
            await apply_people_credits(self._session, item.id, profile)
            await self._session.commit()
        logger.info(
            "媒体条目已建档：%s/%s《%s》(%s)，别名 %d 个，季 %d 个",
            kind.value,
            tmdb_id,
            item.title,
            item.year or "年份未知",
            len(item.aliases),
            len(seasons),
        )
        return item

    async def resolve_douban(
        self,
        kind: MediaKind,
        title: str,
        *,
        year: int | None = None,
        douban_id: str | None = None,
    ) -> tuple[DoubanResolution, MediaItem | None]:
        """豆瓣入口收敛：命中即建档并回填豆瓣身份，歧义/未找到交给调用方处理。

        返回 (收敛结果, 条目)；仅 status=MATCHED 时条目非 None。
        歧义时由前端弹层让用户从 candidates 里确认，确认后走
        ``ensure_media_item(kind, tmdb_id, douban_id=..., extra_aliases=[豆瓣标题])``。
        """
        resolution = await resolve_douban_to_tmdb(
            self._client, kind, title, year=year, language=self._language
        )
        if resolution.status is not ResolveStatus.MATCHED:
            return resolution, None
        assert resolution.tmdb_id is not None
        item = await self.ensure_media_item(
            kind,
            resolution.tmdb_id,
            douban_id=douban_id,
            extra_aliases=[title],
        )
        return resolution, item

    # ------------------------------------------------------------------
    # 内部
    # ------------------------------------------------------------------

    async def _backfill(
        self, item: MediaItem, douban_id: str | None, extra_aliases: Sequence[str]
    ) -> MediaItem:
        """把调用方带来的新信息合并进已有条目；无变化则不产生写。"""
        changed = False
        if douban_id and not item.douban_id:
            item.douban_id = douban_id
            changed = True
        merged = self._merge_aliases(item.aliases, extra_aliases)
        if merged is not None:
            item.aliases = merged
            changed = True
        return await self._repo.save(item) if changed else item

    @staticmethod
    def _merge_aliases(current: list, extra: Sequence[str]) -> list | None:
        """追加新别名（保序精确去重）；没有新增返回 None。"""
        seen = set(current)
        additions = []
        for text in extra:
            cleaned = text.strip()
            if cleaned and cleaned not in seen:
                seen.add(cleaned)
                additions.append(cleaned)
        return [*current, *additions] if additions else None

    def _to_rows(
        self,
        profile: MediaProfile,
        douban_id: str | None,
        extra_aliases: Sequence[str],
    ) -> tuple[MediaItem, list[MediaSeason], list[MediaEpisode], MediaMetadata]:
        """传输模型 → ORM 行（身份 + 展示层）。入口带来的别名合并进别名集合。"""
        from movieclaw_api.services.media_scrape import build_display_rows

        aliases = list(profile.aliases)
        merged = self._merge_aliases(aliases, extra_aliases)
        if merged is not None:
            aliases = merged
        item = MediaItem(
            kind=profile.kind.value,
            tmdb_id=profile.tmdb_id,
            imdb_id=profile.imdb_id,
            douban_id=douban_id,
            title=profile.title,
            original_title=profile.original_title,
            year=profile.year,
            aliases=aliases,
            status=profile.status,
            poster_path=profile.poster_path,
            backdrop_path=profile.backdrop_path,
            metadata_refreshed_at=utcnow(),
            # NULL=立即到期：刷新任务首个 tick 会处理并按 status 分档重排
            next_refresh_at=None,
        )
        seasons, episodes, metadata = build_display_rows(profile, self._language)
        return item, seasons, episodes, metadata
