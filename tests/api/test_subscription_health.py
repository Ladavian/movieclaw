"""订阅链路体检的测试（docs/design/library-routing.md 订阅设定页）。

覆盖：无下载器判 error、映射覆盖/不覆盖的判定与修复指引、inplace 与 watch
两种模式的链路段构成、硬链同盘检测、监听未生效的 warn 降级、auto 规则
兜底进入其它库的链路。判定与真实投递同一批原语——这里断言的是"体检
结论与投递行为一致"这个口径本身。
"""

from __future__ import annotations

import pytest
import pytest_asyncio

from movieclaw_api.core.config import get_settings
from movieclaw_api.services.subscription.health import pipeline_health
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.models import ImportWatch
from movieclaw_db.models.downloader_client import ClientType, DownloaderClient
from movieclaw_db.models.site_credential import ConfigStatus
from movieclaw_db.repositories.library_repo import LibraryRepository


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'health.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


async def _make_library(db, *, name="电影库", kind="movie", root) -> int:
    root.mkdir(parents=True, exist_ok=True)
    async with db.session() as session:
        row = await LibraryRepository(session).create(name=name, kind=kind, root_paths=[str(root)])
        return row.id


async def _make_downloader(db, *, mappings: list[dict] | None = None) -> None:
    async with db.session() as session:
        session.add(
            DownloaderClient(
                name="qb",
                client_type=ClientType.QBITTORRENT,
                url="http://127.0.0.1:8080",
                is_default=True,
                enabled=True,
                status=ConfigStatus.ACTIVE,
                path_mappings=mappings,
            )
        )
        await session.commit()


async def _make_site(db) -> None:
    from movieclaw_db.models import SiteCredential
    from movieclaw_db.models.site_credential import AuthType

    async with db.session() as session:
        session.add(
            SiteCredential(
                site_id="testsite",
                auth_type=AuthType.COOKIE,
                cookie="x",
                enabled=True,
                status=ConfigStatus.ACTIVE,
            )
        )
        await session.commit()


def _check(pipeline: dict, key: str) -> dict:
    matches = [c for c in pipeline["checks"] if c["key"] == key]
    assert matches, f"链路里缺少 {key} 段：{[c['key'] for c in pipeline['checks']]}"
    return matches[0]


@pytest.mark.asyncio
async def test_site_check_is_global_first_segment(db, tmp_path) -> None:
    """全局站点段：无站点 error 且拖垮整体；接入后 ok 并计数。"""
    await _make_library(db, root=tmp_path / "movies")
    await _make_downloader(db)
    async with db.session() as session:
        result = await pipeline_health(session)
    assert result["site_check"]["status"] == "error"
    assert result["site_check"]["fix_section"] == "sites"
    assert result["status"] == "error"  # 各库自身无恙，但搜不到资源照样跑不起来

    await _make_site(db)
    async with db.session() as session:
        result = await pipeline_health(session)
    assert result["site_check"]["status"] == "ok" and "1 个" in result["site_check"]["detail"]
    assert result["status"] == "ok" and result["downloader_ok"] is True


@pytest.mark.asyncio
async def test_configured_but_broken_is_not_setup_state(db, tmp_path) -> None:
    """配置过但失效 ≠ 从未配置：站点 FAILED 时 sites_configured 仍为 True。

    前端开局清单只看 configured 标志——cookie 过期的老用户看到的必须是
    体检红项，而不是"把订阅跑起来需要三步"的新手清单。
    """
    from movieclaw_db.models import SiteCredential
    from movieclaw_db.models.site_credential import AuthType

    await _make_library(db, root=tmp_path / "movies")
    await _make_downloader(db)
    async with db.session() as session:
        session.add(
            SiteCredential(
                site_id="dead",
                auth_type=AuthType.COOKIE,
                cookie="x",
                enabled=True,
                status=ConfigStatus.FAILED,
            )
        )
        await session.commit()
        result = await pipeline_health(session)
    assert result["sites_configured"] is True and result["downloaders_configured"] is True
    assert result["site_check"]["status"] == "error"
    assert "不可用" in result["site_check"]["detail"]  # 文案区分"配了但坏了"


@pytest.mark.asyncio
async def test_preview_rejects_auto_dir_without_libraries(db, tmp_path) -> None:
    """零媒体库 + auto 监听规则：种子投得出去但无库可入——预检不得报可行。"""
    from movieclaw_api.services.subscription.dispatch import preview_dispatch_route

    watch = tmp_path / "auto"
    watch.mkdir()
    await _make_site(db)
    await _make_downloader(db)
    async with db.session() as session:
        # 绕过 CRUD 校验直插（校验会拦"无可路由库"——这里模拟规则建好后库被删光）
        session.add(
            ImportWatch(source_path=str(watch), strategy="copy", library_id=None, kind="tv")
        )
        await session.commit()
        preview = await preview_dispatch_route(session, kind="tv", library_id=None)
    assert preview["mode"] == "watch" and preview["path"] == str(watch)
    assert preview["ok"] is False
    assert "无法自动入库" in (preview["warning"] or "")


@pytest.mark.asyncio
async def test_no_downloader_is_error(db, tmp_path) -> None:
    await _make_library(db, root=tmp_path / "movies")
    async with db.session() as session:
        result = await pipeline_health(session)
    assert result["status"] == "error" and result["error_count"] == 1
    pipeline = result["libraries"][0]
    assert pipeline["mode"] == "inplace"
    downloader = _check(pipeline, "downloader")
    assert downloader["status"] == "error" and downloader["fix_section"] == "downloaders"


@pytest.mark.asyncio
async def test_mapping_coverage_verdicts(db, tmp_path) -> None:
    root = tmp_path / "movies"
    await _make_library(db, root=root)
    await _make_site(db)
    # 映射不覆盖库根：error + 指向下载器设置（与真实投递被拒同口径）
    await _make_downloader(db, mappings=[{"local": "/somewhere/else", "remote": "/dl"}])
    async with db.session() as session:
        result = await pipeline_health(session)
    mapping = _check(result["libraries"][0], "mapping")
    assert mapping["status"] == "error" and mapping["fix_section"] == "downloaders"
    assert str(root) in mapping["detail"]

    # 补上覆盖后：整链全绿
    async with db.session() as session:
        from sqlmodel import select

        row = (await session.execute(select(DownloaderClient))).scalars().one()
        row.path_mappings = [{"local": str(tmp_path), "remote": "/dl"}]
        await session.commit()
        result = await pipeline_health(session)
    assert result["status"] == "ok" and result["error_count"] == 0
    assert _check(result["libraries"][0], "mapping")["status"] == "ok"


@pytest.mark.asyncio
async def test_watch_mode_transfer_segments(db, tmp_path) -> None:
    """watch 模式：同盘硬链 ok + 监听未生效 warn（测试环境无 watcher）。"""
    root, watch = tmp_path / "movies", tmp_path / "watch"
    watch.mkdir()
    library_id = await _make_library(db, root=root)
    await _make_site(db)
    await _make_downloader(db)
    async with db.session() as session:
        session.add(ImportWatch(source_path=str(watch), strategy="hardlink", library_id=library_id))
        await session.commit()
        result = await pipeline_health(session)

    pipeline = result["libraries"][0]
    assert pipeline["mode"] == "watch" and pipeline["path"] == str(watch)
    assert _check(pipeline, "transfer_disk")["status"] == "ok"  # tmp 下同一文件系统
    active = _check(pipeline, "watch_active")
    assert active["status"] == "warn" and "兜底巡检" in active["detail"]
    assert result["status"] == "warn"  # 降级不算故障


@pytest.mark.asyncio
async def test_auto_rule_covers_routed_libraries(db, tmp_path) -> None:
    """没有专属规则的库经同 kind auto 规则获得 watch 链路（投递联动同口径）。"""
    tv_root, anime_root, watch = tmp_path / "tv", tmp_path / "anime", tmp_path / "auto"
    watch.mkdir()
    await _make_library(db, name="剧集库", kind="tv", root=tv_root)
    await _make_library(db, name="动漫库", kind="tv", root=anime_root)
    await _make_downloader(db)
    async with db.session() as session:
        session.add(
            ImportWatch(source_path=str(watch), strategy="copy", library_id=None, kind="tv")
        )
        await session.commit()
        result = await pipeline_health(session)

    for pipeline in result["libraries"]:
        assert pipeline["mode"] == "watch" and pipeline["path"] == str(watch)
        # copy 策略无同盘检测段；监听未生效 warn
        assert [c["key"] for c in pipeline["checks"] if c["key"] == "transfer_disk"] == []
