from __future__ import annotations

from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_db.models.base import utcnow
from movieclaw_db.models.library_file import IdentitySource, LibraryFile


class LibraryFileRepository:
    """库存台账的数据访问层。

    ``file_path`` 是全局唯一键：入库/扫描的写入统一走 ``upsert_by_path``
    ——同一路径重复发现（重扫、重复入库）更新而非重复插入。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- 查询 --------------------------------------------------------------

    async def get_by_path(self, file_path: str) -> LibraryFile | None:
        result = await self._session.execute(
            select(LibraryFile).where(LibraryFile.file_path == file_path)
        )
        return result.scalar_one_or_none()

    async def list_by_library(self, library_id: int) -> list[LibraryFile]:
        """某库的全部台账（含 missing 行，展示层自行标注）。"""
        result = await self._session.execute(
            select(LibraryFile).where(LibraryFile.library_id == library_id).order_by(LibraryFile.id)
        )
        return list(result.scalars().all())

    async def list_unidentified(self, *, library_id: int | None = None) -> list[LibraryFile]:
        """待识别清单：落了账、没挂上身份锚、且用户没忽略过的文件。"""
        stmt = select(LibraryFile).where(
            LibraryFile.media_item_id.is_(None),  # type: ignore[union-attr]
            LibraryFile.ignored_at.is_(None),  # type: ignore[union-attr]
        )
        if library_id is not None:
            stmt = stmt.where(LibraryFile.library_id == library_id)
        result = await self._session.execute(stmt.order_by(LibraryFile.file_path))
        return list(result.scalars().all())

    async def list_review(self, *, library_id: int | None = None) -> list[LibraryFile]:
        """身份复核清单：识别器升级后新旧结论不一致、等用户拍板的行。"""
        stmt = select(LibraryFile).where(LibraryFile.review_suggestion.is_not(None))  # type: ignore[union-attr]
        if library_id is not None:
            stmt = stmt.where(LibraryFile.library_id == library_id)
        result = await self._session.execute(stmt.order_by(LibraryFile.file_path))
        return list(result.scalars().all())

    async def list_ignored(self, *, library_id: int | None = None) -> list[LibraryFile]:
        """已忽略清单：用户说过"别再问"的文件（可一键恢复重新参与识别）。"""
        stmt = select(LibraryFile).where(LibraryFile.ignored_at.is_not(None))  # type: ignore[union-attr]
        if library_id is not None:
            stmt = stmt.where(LibraryFile.library_id == library_id)
        result = await self._session.execute(stmt.order_by(LibraryFile.file_path))
        return list(result.scalars().all())

    async def find_by_size(self, library_id: int, size_bytes: int) -> list[LibraryFile]:
        """同库同尺寸的台账行——改名归并的候选池（尺寸是改名/移动的不变量）。"""
        result = await self._session.execute(
            select(LibraryFile).where(
                LibraryFile.library_id == library_id,
                LibraryFile.size_bytes == size_bytes,
            )
        )
        return list(result.scalars().all())

    async def owned_units(self, media_item_id: int) -> set[tuple[int, int]]:
        """某条目在库的期望单元集合（库存 H）——wanted 生成跳过已有的依据。

        只算"在位"的文件（missing 的不算拥有）。
        """
        result = await self._session.execute(
            select(LibraryFile.season_number, LibraryFile.episode_number)
            .where(
                LibraryFile.media_item_id == media_item_id,
                LibraryFile.missing_since.is_(None),  # type: ignore[union-attr]
            )
            .distinct()
        )
        return {(row[0], row[1]) for row in result.all()}

    # -- 写入 --------------------------------------------------------------

    async def upsert_by_path(self, row: LibraryFile) -> LibraryFile:
        """按 file_path 幂等写入：已有则整体更新（文件回归时清 missing 标记）。"""
        existing = await self.get_by_path(row.file_path)
        if existing is None:
            self._session.add(row)
            await self._session.commit()
            await self._session.refresh(row)
            return row
        existing.library_id = row.library_id
        existing.media_item_id = row.media_item_id
        existing.season_number = row.season_number
        existing.episode_number = row.episode_number
        existing.size_bytes = row.size_bytes
        existing.container = row.container
        existing.resolution = row.resolution
        existing.video_codec = row.video_codec
        existing.hdr = row.hdr
        existing.bit_depth = row.bit_depth
        existing.duration_seconds = row.duration_seconds
        existing.bit_rate = row.bit_rate
        existing.audio_streams = row.audio_streams
        existing.subtitle_streams = row.subtitle_streams
        existing.media_source = row.media_source
        existing.release_group = row.release_group
        existing.source = row.source
        existing.site_id = row.site_id
        existing.torrent_id = row.torrent_id
        existing.unidentified_reason = row.unidentified_reason
        existing.unidentified_code = row.unidentified_code
        existing.unidentified_candidates = row.unidentified_candidates
        existing.identity_source = row.identity_source
        existing.resolved_version = row.resolved_version
        existing.review_suggestion = row.review_suggestion
        existing.missing_since = None  # 再次发现即在位
        existing.updated_at = utcnow()
        await self._session.commit()
        await self._session.refresh(existing)
        return existing

    async def relocate(self, file_id: int, *, file_path: str, container: str | None) -> None:
        """改名归并：把台账行迁到新路径，身份锚与介质信息原样保留。"""
        row = await self._session.get(LibraryFile, file_id)
        if row is None:
            return
        row.file_path = file_path
        row.container = container
        row.missing_since = None
        row.updated_at = utcnow()
        await self._session.commit()

    async def claim_identity(
        self,
        file_id: int,
        *,
        media_item_id: int,
        season_number: int,
        episode_number: int,
        resolved_version: int | None = None,
    ) -> LibraryFile | None:
        """人工认领：给未识别文件挂上身份锚。不存在返回 None。

        认领即人工拍板，``identity_source`` 记为 manual——身份对账机制永不
        自动翻案人工结论。``resolved_version`` 由调用方传当前识别器版本。
        """
        row = await self._session.get(LibraryFile, file_id)
        if row is None:
            return None
        row.media_item_id = media_item_id
        row.season_number = season_number
        row.episode_number = episode_number
        row.unidentified_reason = None  # 已有身份，失败原因/分类/候选随之失义
        row.unidentified_code = None
        row.unidentified_candidates = None
        row.identity_source = IdentitySource.MANUAL
        row.resolved_version = resolved_version
        row.review_suggestion = None
        row.updated_at = utcnow()
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def mark_missing(self, file_id: int, *, since: datetime | None = None) -> None:
        """对账：标记文件消失（不删记录）。"""
        row = await self._session.get(LibraryFile, file_id)
        if row is None:
            return
        row.missing_since = since or utcnow()
        row.updated_at = utcnow()
        await self._session.commit()

    async def mark_ignored(self, file_ids: list[int]) -> int:
        """待识别清单的「忽略」：打标记而非删行，返回实际标记的条数。

        **不能删行**——扫描器判定"新文件"的唯一依据就是台账里有没有这条
        路径，删了下轮扫描就当新文件重走识别链，认不出照样回清单（对活跃
        的库连几分钟都撑不住）。打标记后扫描直接秒过，且行还在、可恢复。
        """
        marked = 0
        now = utcnow()
        for file_id in file_ids:
            row = await self._session.get(LibraryFile, file_id)
            if row is None or row.ignored_at is not None:
                continue
            row.ignored_at = now
            row.updated_at = now
            marked += 1
        await self._session.commit()
        return marked

    async def restore_ignored(self, file_ids: list[int]) -> int:
        """恢复已忽略的文件：清标记，重新参与识别与待识别清单。"""
        restored = 0
        now = utcnow()
        for file_id in file_ids:
            row = await self._session.get(LibraryFile, file_id)
            if row is None or row.ignored_at is None:
                continue
            row.ignored_at = None
            row.updated_at = now
            restored += 1
        await self._session.commit()
        return restored

    async def clear_missing_flag(self, file_id: int) -> None:
        """清除 missing 标记（已忽略的文件回归时用：忽略状态原样保留）。"""
        row = await self._session.get(LibraryFile, file_id)
        if row is None or row.missing_since is None:
            return
        row.missing_since = None
        row.updated_at = utcnow()
        await self._session.commit()
