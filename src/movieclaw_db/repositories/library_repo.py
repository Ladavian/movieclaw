from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_db.models.base import utcnow
from movieclaw_db.models.library import Library


class LibraryRepository:
    """媒体库表的数据访问层。

    默认库不变量（按 kind 各自维护，与下载器的全局默认同理）：
    同 kind 只要存在库，就有且只有一个默认——第一个自动成为默认；
    删除默认库时默认让给同 kind 剩下最早创建的一个。
    """

    def __init__(self, session: AsyncSession) -> None:
        self._session = session

    # -- 查询 --------------------------------------------------------------

    async def get(self, library_id: int) -> Library | None:
        """按主键查询；不存在返回 None。"""
        return await self._session.get(Library, library_id)

    async def get_by_name(self, name: str) -> Library | None:
        """按名称查询（名称全局唯一）；不存在返回 None。"""
        result = await self._session.execute(select(Library).where(Library.name == name))
        return result.scalar_one_or_none()

    async def list_all(self, *, kind: str | None = None) -> list[Library]:
        """返回全部库（可按类型过滤），按用户排定的展示顺序（sort_order 升序，
        id 兜底）——媒体库首页的卡片区与「最近添加」分区都按这个顺序排。"""
        stmt = select(Library).order_by(Library.sort_order, Library.id)
        if kind is not None:
            stmt = stmt.where(Library.kind == kind)
        result = await self._session.execute(stmt)
        return list(result.scalars().all())

    async def get_default(self, kind: str) -> Library | None:
        """返回某类型的默认库；该类型一个库都没有时返回 None。"""
        result = await self._session.execute(
            select(Library).where(
                Library.kind == kind,
                Library.is_default == True,  # noqa: E712
            )
        )
        return result.scalar_one_or_none()

    async def count(self) -> int:
        """库总数（首启种子判空用）。"""
        result = await self._session.execute(select(Library.id))
        return len(result.scalars().all())

    # -- 写入 --------------------------------------------------------------

    async def create(
        self,
        *,
        name: str,
        kind: str,
        root_paths: list[str],
        match_rules: list | None = None,
    ) -> Library:
        """新增一个库。该 kind 尚无默认库时，新库自动成为默认。新库置于
        展示顺序末尾（现有最大 sort_order + 1，不打乱用户排好的顺序）。"""
        existing = await self.list_all()
        row = Library(
            name=name,
            kind=kind,
            root_paths=list(root_paths),
            match_rules=list(match_rules or []),
            is_default=await self.get_default(kind) is None,
            sort_order=max((lib.sort_order for lib in existing), default=0) + 1,
        )
        self._session.add(row)
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def reorder(self, ordered_ids: list[int]) -> None:
        """按给定的 id 顺序重写全部库的展示顺序（1 起步的连续序号）。
        调用方（service 层）保证 ordered_ids 与现存库集合完全一致。"""
        rows = {row.id: row for row in await self.list_all()}
        now = utcnow()
        for position, library_id in enumerate(ordered_ids, start=1):
            row = rows[library_id]
            if row.sort_order != position:
                row.sort_order = position
                row.updated_at = now
        await self._session.commit()

    async def update(
        self,
        library_id: int,
        *,
        name: str,
        root_paths: list[str],
        match_rules: list | None = None,
    ) -> Library | None:
        """更新名称/根路径/收藏范围（kind 创建后不可改）；不存在返回 None。"""
        row = await self.get(library_id)
        if row is None:
            return None
        row.name = name
        row.root_paths = list(root_paths)
        row.match_rules = list(match_rules or [])
        row.updated_at = utcnow()
        await self._session.commit()
        await self._session.refresh(row)
        return row

    async def set_default(self, library_id: int) -> bool:
        """把某库设为其类型的默认库（同时清掉同 kind 其他库的默认标记）。"""
        row = await self.get(library_id)
        if row is None:
            return False
        now = utcnow()
        for other in await self.list_all(kind=row.kind):
            if other.is_default and other.id != library_id:
                other.is_default = False
                other.updated_at = now
        row.is_default = True
        row.updated_at = now
        await self._session.commit()
        return True

    async def delete(self, library_id: int) -> bool:
        """删除某库。返回是否命中记录。

        - 引用它的订阅经外键 SET NULL 回落到"用该类型默认库"；
        - 删除的是默认库时，把默认让给同 kind 剩下最早创建的一个。
        """
        row = await self.get(library_id)
        if row is None:
            return False
        was_default, kind = row.is_default, row.kind
        await self._session.delete(row)
        await self._session.commit()
        if was_default:
            remaining = await self.list_all(kind=kind)
            if remaining:
                remaining[0].is_default = True
                remaining[0].updated_at = utcnow()
                await self._session.commit()
        return True
