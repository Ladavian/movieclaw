"""台账三态 JSON 列的 SQL 语义：Python None 必须落成 SQL NULL。

背景：SQLAlchemy 的 JSON 类型默认把 None 序列化成 JSON 的 ``null``（文本
'null'）。对「NULL=没有 / []=有但为空」的三态列来说，这让 SQL 层的
``IS NULL`` / ``IS NOT NULL`` 全部失灵——身份复核清单曾因此列出整个媒体库，
扫描的规格补探则永远找不到候选。

这批断言直接打在 SQL 上（而不是读回 ORM 对象再比 Python None）：只在
Python 层比较的话，'null' 反序列化回来同样是 None，测试会一路绿灯而 bug
照旧——这正是它当初逃过测试的原因。
"""

from __future__ import annotations

import pytest_asyncio
from sqlalchemy import text
from sqlmodel import select

from movieclaw_api.core.config import get_settings
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import FileSource, LibraryFile
from movieclaw_db.repositories.library_file_repo import LibraryFileRepository
from movieclaw_db.repositories.library_repo import LibraryRepository

_TRISTATE = ("audio_streams", "subtitle_streams", "unidentified_candidates", "review_suggestion")


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'tri.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def _seed(db) -> None:
    """两行：一行四列全空，一行全部有值（其中音轨是空列表 = 探过但没音轨）。"""
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=["/media"]
        )
        session.add(
            LibraryFile(
                library_id=library.id,
                file_path="/media/空.mkv",
                size_bytes=1,
                source=FileSource.SCANNED,
            )
        )
        session.add(
            LibraryFile(
                library_id=library.id,
                file_path="/media/有值.mkv",
                size_bytes=2,
                source=FileSource.SCANNED,
                audio_streams=[],  # 探过，确实没有音轨——与"没探过"必须区分
                subtitle_streams=[{"codec": "subrip"}],
                unidentified_candidates=[{"tmdb_id": 1}],
                review_suggestion={"tmdb_id": 1},
            )
        )
        await session.commit()


async def test_none_is_stored_as_sql_null(db) -> None:
    """四个三态列写 None 时，数据库里存的必须是 NULL 而不是文本 'null'。"""
    await _seed(db)
    async with db.session() as session:
        for column in _TRISTATE:
            kinds = (
                await session.execute(
                    text(f"SELECT typeof({column}) FROM library_file ORDER BY id")  # noqa: S608
                )
            ).scalars().all()
            assert kinds[0] == "null", f"{column} 的 None 被存成了 {kinds[0]}，不是 SQL NULL"


async def test_is_null_predicates_select_the_right_rows(db) -> None:
    """``IS NULL`` 只命中没写过值的那行；空列表算「有值」，不该被当成 NULL。"""
    await _seed(db)
    async with db.session() as session:
        empty = (
            (
                await session.execute(
                    select(LibraryFile.file_path).where(
                        LibraryFile.audio_streams.is_(None)  # type: ignore[union-attr]
                    )
                )
            )
            .scalars()
            .all()
        )
    assert empty == ["/media/空.mkv"]


async def test_review_list_only_returns_flagged_rows(db) -> None:
    """身份复核清单只列有复核建议的行。

    修复前它用 ``review_suggestion IS NOT NULL`` 过滤，而每一行存的都是文本
    'null'——于是整个媒体库都被列了出来。
    """
    await _seed(db)
    async with db.session() as session:
        rows = await LibraryFileRepository(session).list_review()
    assert [r.file_path for r in rows] == ["/media/有值.mkv"]
