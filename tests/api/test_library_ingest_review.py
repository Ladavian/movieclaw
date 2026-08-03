"""监听导入的台账分档与人工拍板测试。

覆盖：「跳过存量」基线（存量盖章忽略、下载中不盖章、基线后只处理新增、
忽略行指纹变化也不复活）、人工忽略/恢复/认领动作、NFO 身份声明优先、
失败告警按源目录聚合（清零熄灭）。识别链本体由扫描器测试覆盖，此处打桩。
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest
import pytest_asyncio
from sqlmodel import select

import movieclaw_api.services.library.ingest as ingest_mod
from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import BadRequestException
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import (
    ImportWatch,
    IngestEntry,
    IngestStatus,
    Library,
    LibraryFile,
    MediaItem,
    NoticeStatus,
    SystemNotice,
)
from movieclaw_db.repositories.library_repo import LibraryRepository
from movieclaw_media.models import MediaKind

_FAKE_SPEC = SimpleNamespace(
    resolution="1080p",
    video_codec="hevc",
    hdr=None,
    bit_depth=10,
    duration_seconds=3600,
    bit_rate=None,
    audio_streams=[],
    subtitle_streams=[],
)


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'review.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    monkeypatch.setattr(ingest_mod, "_stability", {})
    monkeypatch.setattr(ingest_mod, "QUIET_SECONDS", 0)
    monkeypatch.setattr(ingest_mod, "_briefs_cache", (float("-inf"), None))
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def _make_library(db, *, kind=MediaKind.MOVIE, root) -> int:
    root.mkdir(parents=True, exist_ok=True)
    async with db.session() as session:
        row = await LibraryRepository(session).create(
            name=f"测试{kind.value}库", kind=kind.value, root_paths=[str(root)]
        )
        return row.id


async def _make_rule(db, *, library_id: int, source, process_existing=True) -> int:
    async with db.session() as session:
        row = ImportWatch(
            source_path=str(source),
            strategy="hardlink",
            library_id=library_id,
            process_existing=process_existing,
        )
        session.add(row)
        await session.commit()
        await session.refresh(row)
        return row.id


async def _make_item(db, *, kind=MediaKind.MOVIE, title="某电影", year=2020) -> MediaItem:
    async with db.session() as session:
        item = MediaItem(kind=kind.value, tmdb_id=300, title=title, original_title=title, year=year)
        session.add(item)
        await session.commit()
        await session.refresh(item)
        return item


async def _sweep(db, rule_id: int, times: int = 1) -> None:
    """按 DB 里的真实规则行巡检（基线需要 rule.id 与 baseline_done 持久化）。"""
    for _ in range(times):
        async with db.session() as session:
            rule = await session.get(ImportWatch, rule_id)
            assert rule is not None
            library = (
                await session.get(Library, rule.library_id) if rule.library_id else None
            )
        await ingest_mod._sweep_dir(rule, library)


def _stub_identify(monkeypatch, item):
    async def identify(session, kind, watch_root, main, spec):
        return item

    monkeypatch.setattr(ingest_mod, "_identify", identify)


def _stub_media_library(monkeypatch):
    """按 tmdb_id 直取库内条目的假 MediaLibraryService（免真实 TMDB 建档）。"""

    class Fake:
        def __init__(self, session, tmdb):
            self._session = session

        async def ensure_media_item(self, kind, tmdb_id):
            return (
                await self._session.execute(
                    select(MediaItem).where(MediaItem.tmdb_id == tmdb_id)
                )
            ).scalar_one()

    monkeypatch.setattr(ingest_mod, "MediaLibraryService", Fake)
    monkeypatch.setattr(ingest_mod, "get_tmdb_client", lambda: None)


async def _records(db) -> list[IngestEntry]:
    async with db.session() as session:
        return list((await session.execute(select(IngestEntry))).scalars().all())


@pytest.mark.asyncio
async def test_skip_existing_baseline(db, tmp_path, monkeypatch):
    """「跳过存量」：首轮巡检把存量盖章忽略（下载中的不盖），之后只处理新增。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, root=root)
    rule_id = await _make_rule(db, library_id=library_id, source=watch, process_existing=False)
    item = await _make_item(db)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)

    calls = {"n": 0}

    async def identify(session, kind, watch_root, main, spec):
        calls["n"] += 1
        return item

    monkeypatch.setattr(ingest_mod, "_identify", identify)

    # 存量：两个已完成条目 + 一个还在下载（带 .aria2 标记）
    for name in ("老片A (2001)", "老片B (2002)"):
        entry = watch / name
        entry.mkdir()
        (entry / "movie.mkv").write_bytes(b"old")
    downloading = watch / "下载中 (2003)"
    downloading.mkdir()
    (downloading / "movie.mkv").write_bytes(b"part")
    (downloading / "movie.mkv.aria2").write_bytes(b"ctl")

    # 首轮：只打基线，不处理任何条目
    await _sweep(db, rule_id)
    records = await _records(db)
    assert {r.entry_path for r in records} == {
        str(watch / "老片A (2001)"),
        str(watch / "老片B (2002)"),
    }
    assert all(r.status == IngestStatus.IGNORED for r in records)
    assert all("跳过存量" in (r.message or "") for r in records)
    assert calls["n"] == 0
    async with db.session() as session:
        rule = await session.get(ImportWatch, rule_id)
        assert rule is not None and rule.baseline_done

    # 基线后：存量不再被碰（指纹变化也不复活）；新增条目正常处理
    (watch / "老片A (2001)" / "movie.mkv").write_bytes(b"old-changed")
    fresh = watch / "新片 (2024)"
    fresh.mkdir()
    (fresh / "movie.mkv").write_bytes(b"new")
    await _sweep(db, rule_id, times=2)
    assert calls["n"] == 1  # 只有新片走了识别
    async with db.session() as session:
        files = list((await session.execute(select(LibraryFile))).scalars().all())
    assert len(files) == 1 and "某电影" in files[0].file_path

    # 下载完成（标记消失）的"下载中"条目按新增处理，不因基线漏掉
    (downloading / "movie.mkv.aria2").unlink()
    await _sweep(db, rule_id, times=2)
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_ignore_and_restore_actions(db, tmp_path, monkeypatch):
    """人工忽略后永不处理；恢复后重新进入流程。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, root=root)
    rule_id = await _make_rule(db, library_id=library_id, source=watch)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)

    calls = {"n": 0}

    async def identify_none(session, kind, watch_root, main, spec):
        calls["n"] += 1
        return None

    monkeypatch.setattr(ingest_mod, "_identify", identify_none)

    entry = watch / "认不出的东西"
    entry.mkdir()
    video = entry / "video.mkv"
    video.write_bytes(b"x")
    await _sweep(db, rule_id, times=2)
    records = await _records(db)
    assert len(records) == 1 and records[0].status == IngestStatus.PENDING

    # 忽略：指纹变化也不再处理
    async with db.session() as session:
        row = await ingest_mod.ignore_entry(session, records[0].id)
        assert row.status == IngestStatus.IGNORED
    video.write_bytes(b"xy")
    await _sweep(db, rule_id, times=2)
    assert calls["n"] == 1

    # 恢复：重新进入处理流程（识别再次被调用）
    async with db.session() as session:
        row = await ingest_mod.restore_entry(session, records[0].id)
        assert row.status == IngestStatus.PENDING and row.fingerprint == ""
    await _sweep(db, rule_id, times=2)
    assert calls["n"] == 2


@pytest.mark.asyncio
async def test_claim_entry_imports_immediately(db, tmp_path, monkeypatch):
    """认领：钉到指定 TMDB 身份并立即入库，不等下轮巡检。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, root=root)
    rule_id = await _make_rule(db, library_id=library_id, source=watch)
    await _make_item(db, title="认领电影", year=2021)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)
    _stub_media_library(monkeypatch)

    async def identify_none(session, kind, watch_root, main, spec):
        return None

    monkeypatch.setattr(ingest_mod, "_identify", identify_none)

    entry = watch / "obscure-release-x264"
    entry.mkdir()
    (entry / "video.mkv").write_bytes(b"x")
    await _sweep(db, rule_id, times=2)
    records = await _records(db)
    assert records[0].status == IngestStatus.PENDING

    async with db.session() as session:
        row = await ingest_mod.claim_entry(session, records[0].id, 300)
        assert row.status == IngestStatus.IMPORTED
        assert "认领电影" in (row.message or "")
    async with db.session() as session:
        files = list((await session.execute(select(LibraryFile))).scalars().all())
    assert len(files) == 1 and "认领电影 (2021)" in files[0].file_path

    # 已入库的不允许再忽略/认领
    async with db.session() as session:
        with pytest.raises(BadRequestException):
            await ingest_mod.ignore_entry(session, records[0].id)
        with pytest.raises(BadRequestException):
            await ingest_mod.claim_entry(session, records[0].id, 300)


@pytest.mark.asyncio
async def test_nfo_identity_preferred(db, tmp_path, monkeypatch):
    """条目自带 NFO（tmdbid）：免名称解析直接锚定身份（真 _identify 路径）。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, root=root)
    rule_id = await _make_rule(db, library_id=library_id, source=watch)
    await _make_item(db, title="NFO电影", year=2019)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)
    _stub_media_library(monkeypatch)

    async def no_resolve(*args, **kwargs):  # noqa: ANN002, ANN003
        raise AssertionError("NFO 命中后不应再走名称解析收敛")

    monkeypatch.setattr(ingest_mod, "verify_resolve", no_resolve)

    entry = watch / "third.party.release.2019"
    entry.mkdir()
    (entry / "video.mkv").write_bytes(b"x")
    (entry / "movie.nfo").write_text(
        "<movie><tmdbid>300</tmdbid></movie>", encoding="utf-8"
    )

    await _sweep(db, rule_id, times=2)
    records = await _records(db)
    assert records[0].status == IngestStatus.IMPORTED
    async with db.session() as session:
        files = list((await session.execute(select(LibraryFile))).scalars().all())
    assert len(files) == 1 and "NFO电影 (2019)" in files[0].file_path


@pytest.mark.asyncio
async def test_failed_notice_aggregated_per_source(db, tmp_path, monkeypatch):
    """失败告警按源目录聚合成一条；全部修复后自动熄灭。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, root=root)
    rule_id = await _make_rule(db, library_id=library_id, source=watch)
    item = await _make_item(db)
    # 环境故障：ffprobe 在但探测全失败
    monkeypatch.setattr(ingest_mod, "ffprobe_available", lambda: True)
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: None)
    _stub_identify(monkeypatch, item)

    for name in ("坏文件A", "坏文件B"):
        entry = watch / name
        entry.mkdir()
        (entry / "video.mkv").write_bytes(b"x")
    await _sweep(db, rule_id, times=2)

    key = f"ingest-dir:{watch}"
    async with db.session() as session:
        notice = (
            await session.execute(select(SystemNotice).where(SystemNotice.dedupe_key == key))
        ).scalar_one()
        assert notice.status == NoticeStatus.ACTIVE.value
        assert "2 个条目" in notice.title

    # 修复（探测恢复）+ 退避归零 → 重试成功 → 告警熄灭
    monkeypatch.setattr(ingest_mod, "probe_media", lambda p: _FAKE_SPEC)
    monkeypatch.setattr(ingest_mod, "FAILED_RETRY_SECONDS", 0)
    await _sweep(db, rule_id, times=2)
    async with db.session() as session:
        notice = (
            await session.execute(select(SystemNotice).where(SystemNotice.dedupe_key == key))
        ).scalar_one()
        assert notice.status == NoticeStatus.RESOLVED.value
        records = list((await session.execute(select(IngestEntry))).scalars().all())
    assert all(r.status == IngestStatus.IMPORTED for r in records)
