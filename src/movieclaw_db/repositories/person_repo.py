from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_db.models.library_file import LibraryFile
from movieclaw_db.models.media_item import MediaItem
from movieclaw_db.models.person import MediaItemPerson, Person


@dataclass(frozen=True)
class PersonCredit:
    """人物页的一行作品：条目身份 + 这个人在其中的身份。

    ``library_id`` 是**任取一个**拥有该条目文件的库——条目挂全局（一部片的
    文件可能散在多个库），而条目详情页的路由是 /library/{id}/item/{itemId}，
    必须给出一个可跳转的库。取不到（文件都被删了、只剩档案）时为 None，
    人物页据此渲染为不可点。
    """

    media_item: MediaItem
    library_id: int | None
    department: str
    character: str | None
    credit_order: int


class PersonRepository:
    """影人（``person`` / ``media_item_person``）的数据访问层。

    只回答两个问题：这个人是谁（按 TMDB person id 取）、他在我库里参演/执导过
    哪些条目（反向查询）。收敛判定与展示编排在 services 层。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    async def get_by_tmdb_id(self, tmdb_person_id: int) -> Person | None:
        """按 TMDB 影人 ID 取人；库里没有这个人返回 None。"""
        result = await self._session.execute(
            select(Person).where(Person.tmdb_person_id == tmdb_person_id)
        )
        return result.scalars().first()

    async def list_credits(self, person_id: int) -> list[PersonCredit]:
        """这个人在库内的全部作品，按「主演在前、同档按年份倒序」排。

        排序口径：先按 department（cast 在前，与详情页的呈现顺序一致），
        再按剧组给的主次顺序（credit_order），最后按年份倒序——一个人的
        作品列表里，「他是主角的新片」最该排在前面。
        """
        rows = (
            await self._session.execute(
                select(MediaItemPerson, MediaItem)
                .join(MediaItem, MediaItem.id == MediaItemPerson.media_item_id)
                .where(MediaItemPerson.person_id == person_id)
            )
        ).all()
        if not rows:
            return []

        # 条目 → 任一所属库。一次查完再配对，避免逐条目查库（N+1）
        item_ids = [item.id for _, item in rows if item.id is not None]
        library_of: dict[int, int] = {}
        if item_ids:
            for media_item_id, library_id in (
                await self._session.execute(
                    select(LibraryFile.media_item_id, LibraryFile.library_id).where(
                        LibraryFile.media_item_id.in_(item_ids)  # type: ignore[union-attr]
                    )
                )
            ).all():
                if media_item_id is not None:
                    library_of.setdefault(media_item_id, library_id)

        credits = [
            PersonCredit(
                media_item=item,
                library_id=library_of.get(item.id) if item.id is not None else None,
                department=link.department,
                character=link.character,
                credit_order=link.credit_order,
            )
            for link, item in rows
        ]
        credits.sort(
            key=lambda c: (
                0 if c.department == "cast" else 1,
                c.credit_order,
                -(c.media_item.year or 0),
            )
        )
        return credits
