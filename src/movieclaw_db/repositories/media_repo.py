from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_db.models.base import utcnow
from movieclaw_db.models.media_item import MediaItem, MediaSeason
from movieclaw_db.models.media_metadata import MediaEpisode, MediaMetadata


class MediaItemRepository:
    """媒体条目（``media_item`` / ``media_season`` / ``media_episode`` /
    ``media_metadata``）的数据访问层。

    职责边界：只做按锚存取与整体落库，不理解 TMDB 数据结构、不做收敛判定——
    这些语义在 ``movieclaw_api.services.media_library`` 与 ``movieclaw_media.library``。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_anchor(self, kind: str, tmdb_id: int) -> MediaItem | None:
        """按唯一锚 (kind, tmdb_id) 读取条目；不存在返回 None。"""
        result = await self._session.execute(
            select(MediaItem).where(MediaItem.kind == kind, MediaItem.tmdb_id == tmdb_id)
        )
        return result.scalar_one_or_none()

    async def list_seasons(self, media_item_id: int) -> list[MediaSeason]:
        """返回条目的全部季，按季号升序（特别季 0 在最前）。"""
        result = await self._session.execute(
            select(MediaSeason)
            .where(MediaSeason.media_item_id == media_item_id)
            .order_by(MediaSeason.season_number)
        )
        return list(result.scalars().all())

    async def list_episodes(
        self, media_item_id: int, season_number: int | None = None
    ) -> list[MediaEpisode]:
        """返回条目的集（可限定一季），按 (季号, 集号) 升序。"""
        statement = select(MediaEpisode).where(MediaEpisode.media_item_id == media_item_id)
        if season_number is not None:
            statement = statement.where(MediaEpisode.season_number == season_number)
        statement = statement.order_by(
            MediaEpisode.season_number, MediaEpisode.episode_number  # type: ignore[arg-type]
        )
        result = await self._session.execute(statement)
        return list(result.scalars().all())

    async def get_metadata(self, media_item_id: int) -> MediaMetadata | None:
        """读取条目的展示元数据档案；未刮削过返回 None。"""
        result = await self._session.execute(
            select(MediaMetadata).where(MediaMetadata.media_item_id == media_item_id)
        )
        return result.scalar_one_or_none()

    async def create_with_seasons(
        self,
        item: MediaItem,
        seasons: list[MediaSeason],
        episodes: list[MediaEpisode] | None = None,
        metadata: MediaMetadata | None = None,
    ) -> MediaItem:
        """建档：条目、季、集与展示档案一次事务落库，返回带 id 的条目。"""
        self._session.add(item)
        await self._session.flush()  # 先拿到 item.id 供子表行引用
        for season in seasons:
            season.media_item_id = item.id
            self._session.add(season)
        for episode in episodes or []:
            episode.media_item_id = item.id
            self._session.add(episode)
        if metadata is not None:
            metadata.media_item_id = item.id
            self._session.add(metadata)
        # 提交后不 refresh：会话是 expire_on_commit=False，提交不会让属性过期，
        # 而全部列（含 id 与时间戳）都是应用侧赋的值，没有库端默认值要读回来。
        # 每建一档白搭一次全表列的 SELECT，首次扫描上千部片就是上千次
        await self._session.commit()
        return item

    async def rollback(self) -> None:
        """回滚当前事务（并发建档撞唯一锚后，会话须先回滚才能继续查询）。"""
        await self._session.rollback()

    async def save(self, item: MediaItem) -> MediaItem:
        """保存对已加载条目的修改（如回填 douban_id / 合并别名）。"""
        item.updated_at = utcnow()
        self._session.add(item)
        await self._session.commit()
        return item
