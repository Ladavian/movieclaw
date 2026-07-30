"""库存聚合接口的播出状态与缺集统计：海报悬浮三分支（追新/补齐/已入库）的数据依据。"""

from __future__ import annotations

from datetime import date

import pytest_asyncio
from sqlmodel import select

from movieclaw_api.api.routes.libraries import list_library_items
from movieclaw_api.core.config import get_settings
from movieclaw_api.schemas.library import derive_air_status
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import FileSource, LibraryFile, MediaEpisode, MediaItem, utcnow
from movieclaw_db.repositories.library_repo import LibraryRepository


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'items.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


def _season(item_id: int, number: int, air_dates: list[str | None]) -> list[MediaEpisode]:
    """构造一季的集数据（media_episode 行）：按顺序编号 1..N，
    air_date 逐集指定（None=未定档）。"""
    return [
        MediaEpisode(
            media_item_id=item_id,
            season_number=number,
            episode_number=i + 1,
            air_date=date.fromisoformat(d) if d else None,
        )
        for i, d in enumerate(air_dates)
    ]


def _file(
    library_id: int, item_id: int, season: int, episode: int, *, missing: bool = False
) -> LibraryFile:
    return LibraryFile(
        library_id=library_id,
        media_item_id=item_id,
        season_number=season,
        episode_number=episode,
        file_path=f"/tv{library_id}/{item_id}/S{season:02d}E{episode:02d}.mkv",
        size_bytes=1,
        source=FileSource.SCANNED,
        missing_since=utcnow() if missing else None,
    )


async def test_items_air_status_and_missing_episodes(db) -> None:
    """三分支数据齐备：在播剧带 airing；完结缺集算已播−在位；齐全为 0。"""
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=["/tv"]
        )
        airing = MediaItem(
            kind="tv", tmdb_id=301, title="在播剧", original_title="A", status="Returning Series"
        )
        ended_gap = MediaItem(
            kind="tv", tmdb_id=302, title="完结缺集剧", original_title="B", status="Ended"
        )
        ended_full = MediaItem(
            kind="tv", tmdb_id=303, title="完结齐全剧", original_title="C", status="Canceled"
        )
        session.add_all([airing, ended_gap, ended_full])
        await session.flush()
        assert library.id and airing.id and ended_gap.id and ended_full.id

        session.add_all(
            [
                # 在播剧：S1 已播 3 集，库里在位 2 集
                *_season(airing.id, 1, ["2020-01-01", "2020-01-08", "2020-01-15"]),
                _file(library.id, airing.id, 1, 1),
                _file(library.id, airing.id, 1, 2),
                # 完结缺集剧：S1 已播 2 集 + 1 集未定档；在位 1 集，另 1 集文件缺失
                # （missing 不算拥有）→ 缺 1 集。特别季 0 有已播集但不参与统计。
                *_season(ended_gap.id, 1, ["2020-01-01", "2020-01-08", None]),
                *_season(ended_gap.id, 0, ["2020-06-01"]),
                _file(library.id, ended_gap.id, 1, 1),
                _file(library.id, ended_gap.id, 1, 2, missing=True),
                # 完结齐全剧：S1 已播 2 集全在位
                *_season(ended_full.id, 1, ["2020-01-01", "2020-01-08"]),
                _file(library.id, ended_full.id, 1, 1),
                _file(library.id, ended_full.id, 1, 2),
            ]
        )
        await session.flush()

        resp = await list_library_items(library.id, session=session)
        rows = {r.media_item_id: r for r in resp.data}

        assert rows[airing.id].air_status == "airing"
        assert rows[airing.id].missing_episode_count == 1

        assert rows[ended_gap.id].air_status == "ended"
        assert rows[ended_gap.id].missing_episode_count == 1

        assert rows[ended_full.id].air_status == "ended"
        assert rows[ended_full.id].missing_episode_count == 0


async def test_items_missing_episodes_counts_across_libraries(db) -> None:
    """缺集按跨库统计：同一部剧的集分散在两个库时，任一库在位即算拥有。

    口径必须与订阅生成工单的 E−H 一致（subscription._owned_units 不按库过滤），
    否则会出现「海报卡提示补齐缺集，订阅弹窗却说整季已在库、且一个工单都不生成」。
    """
    async with db.session() as session:
        repo = LibraryRepository(session)
        main = await repo.create(name="剧集库", kind="tv", root_paths=["/tv"])
        other = await repo.create(name="大陆剧集", kind="tv", root_paths=["/media/tv"])
        item = MediaItem(kind="tv", tmdb_id=501, title="双库剧", original_title="D", status="Ended")
        session.add(item)
        await session.flush()
        assert main.id and other.id and item.id

        session.add_all(
            [
                # S1 已播 3 集：主库只有第 1 集，另外 2 集在「大陆剧集」库里
                *_season(item.id, 1, ["2020-01-01", "2020-01-08", "2020-01-15"]),
                _file(main.id, item.id, 1, 1),
                _file(other.id, item.id, 1, 2),
                _file(other.id, item.id, 1, 3),
            ]
        )
        await session.flush()

        resp = await list_library_items(main.id, session=session)
        row = {r.media_item_id: r for r in resp.data}[item.id]

        # 本库库存仍按本库口径展示（海报墙显示的是这个库里有什么）
        assert row.file_count == 1
        assert row.episode_count == 1
        # 但「补齐缺集」的依据是跨库的：全 3 集都已拥有，不该再提示补齐
        assert row.missing_episode_count == 0


async def test_items_movie_and_unknown_status(db) -> None:
    """电影不参与播出状态判断；剧集 status 未知/映射外时不猜（None）。"""
    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=["/movies"]
        )
        movie = MediaItem(
            kind="movie", tmdb_id=401, title="某电影", original_title="M", status="Released"
        )
        unknown = MediaItem(kind="tv", tmdb_id=402, title="无状态剧", original_title="U")
        session.add_all([movie, unknown])
        await session.flush()
        assert library.id and movie.id and unknown.id

        session.add_all(
            [
                LibraryFile(
                    library_id=library.id,
                    media_item_id=movie.id,
                    season_number=0,
                    episode_number=0,
                    file_path="/movies/M/M.mkv",
                    size_bytes=1,
                    source=FileSource.SCANNED,
                ),
                *_season(unknown.id, 1, ["2020-01-01"]),
                _file(library.id, unknown.id, 1, 1),
            ]
        )
        await session.flush()

        resp = await list_library_items(library.id, session=session)
        rows = {r.media_item_id: r for r in resp.data}

        assert rows[movie.id].air_status is None
        assert rows[movie.id].missing_episode_count == 0
        assert rows[unknown.id].air_status is None


async def test_items_sort_and_paging(db) -> None:
    """服务端排序 + 分页：按标题/最近入账各自有序，翻页不重不漏。"""
    from datetime import timedelta

    from movieclaw_api.api.routes.libraries import list_library_item_ids

    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="电影库", kind="movie", root_paths=["/movies"]
        )
        assert library.id
        # 标题序 A→D 与入账序（A 最新）刻意相反，才能验出排序真按各自的键走
        base = utcnow()
        titles = ["A片", "B片", "C片", "D片"]
        ids: list[int] = []
        for index, title in enumerate(titles):
            item = MediaItem(kind="movie", tmdb_id=500 + index, title=title, original_title=title)
            session.add(item)
            await session.flush()
            assert item.id
            ids.append(item.id)
            session.add(
                LibraryFile(
                    library_id=library.id,
                    media_item_id=item.id,
                    season_number=0,
                    episode_number=0,
                    file_path=f"/movies/{title}/{title}.mkv",
                    size_bytes=1,
                    source=FileSource.SCANNED,
                    created_at=base - timedelta(minutes=index),  # A 最新、D 最旧
                )
            )
        await session.flush()

        by_title = await list_library_items(library.id, session=session)
        assert [r.title for r in by_title.data] == titles

        recent = await list_library_items(library.id, sort="added_at", limit=2, session=session)
        assert [r.title for r in recent.data] == ["A片", "B片"]

        page1 = await list_library_items(library.id, limit=2, session=session)
        page2 = await list_library_items(library.id, limit=2, offset=2, session=session)
        assert [r.title for r in page1.data] + [r.title for r in page2.data] == titles

        # 补探优先：给 A 片探出音轨后它就不再是"待处理"，让位给尚未探测的 B 片
        a_file = (
            await session.execute(select(LibraryFile).where(LibraryFile.media_item_id == ids[0]))
        ).scalar_one()
        a_file.audio_streams = []
        session.add(a_file)
        await session.flush()
        probing = await list_library_items(library.id, sort="probing", limit=2, session=session)
        assert [r.title for r in probing.data] == ["B片", "C片"]

        empty = await list_library_items(library.id, limit=2, offset=4, session=session)
        assert empty.data == []

        id_resp = await list_library_item_ids(library.id, session=session)
        assert sorted(id_resp.data) == sorted(ids)


async def test_items_pinyin_order_and_index(db) -> None:
    """按标题排序走拼音序，A-Z 索引条的分档与 offset 与之同源。"""
    from movieclaw_api.api.routes.libraries import list_library_item_index

    async with db.session() as session:
        library = await LibraryRepository(session).create(
            name="剧集库", kind="tv", root_paths=["/tv"]
        )
        assert library.id
        # 码点序会排成「三体 < 漫长的季节 < 爱很美味 < 重庆森林」（全错）；
        # 拼音序应是 爱(a) < 漫长(m) < 三体(s) < 重庆(chongqing→c) 之后的 C < M < S
        titles = ["三体", "漫长的季节", "爱很美味", "重庆森林", "9号秘事"]
        for index, title in enumerate(titles):
            item = MediaItem(kind="tv", tmdb_id=600 + index, title=title, original_title=title)
            session.add(item)
            await session.flush()
            assert item.id
            session.add(_file(library.id, item.id, 1, 1))
        await session.flush()

        rows = await list_library_items(library.id, session=session)
        assert [r.title for r in rows.data] == [
            "爱很美味",  # a
            "重庆森林",  # chongqing——多音字，码点序绝排不到这
            "漫长的季节",  # m
            "三体",  # s
            "9号秘事",  # 数字归 #，排在最后
        ]

        index_rows = await list_library_item_index(library.id, session=session)
        assert [(e.initial, e.count, e.offset) for e in index_rows.data] == [
            ("A", 1, 0),
            ("C", 1, 1),
            ("M", 1, 2),
            ("S", 1, 3),
            ("#", 1, 4),
        ]
        # 索引条给的 offset 直接可用：点「M」拿到的就是该档第一格
        jumped = await list_library_items(library.id, limit=1, offset=2, session=session)
        assert [r.title for r in jumped.data] == ["漫长的季节"]


def test_derive_air_status_mapping() -> None:
    assert derive_air_status("Returning Series") == "airing"
    assert derive_air_status("In Production") == "airing"
    assert derive_air_status("Ended") == "ended"
    assert derive_air_status("Canceled") == "ended"
    assert derive_air_status("莫名其妙的值") is None
    assert derive_air_status(None) is None
