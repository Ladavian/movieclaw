"""监听导入规则保存时校验的专项测试——聚焦 auto + hardlink 的同盘检测口径。

同盘检测只覆盖**路由可达**的库（声明收藏范围的库 + 默认库，与 route()
及 docs/design/library-routing.md 2.3 同一口径）：无声明且非默认的库
（典型如跨盘的 strm 归档库）路由永远不会选中，不该有资格否决规则创建
（issue #79 的误伤正源于此）。

现有测试的目录全在 tmp 下、``st_dev`` 恒等，这条校验天然测不到跨盘分支
——这里把 ``os`` 在被测模块的命名空间内替换为按路径前缀返回伪造
``st_dev`` 的桩，目录布局直接复用 issue 的真实场景（/video 与 /strm
两块盘）。
"""
from __future__ import annotations

import os
from types import SimpleNamespace

import pytest
import pytest_asyncio

from movieclaw_api.core.config import get_settings
from movieclaw_api.exceptions import BadRequestException
from movieclaw_api.services import import_watch_config
from movieclaw_api.services.import_watch_config import ImportWatchConfigService
from movieclaw_db.engine import dispose_db, get_database, init_db
from movieclaw_db.migrations import run_migrations
from movieclaw_db.repositories.library_repo import LibraryRepository


@pytest_asyncio.fixture
async def db(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{tmp_path / 'watch.db'}")
    get_settings.cache_clear()
    init_db(get_settings().database_url, echo=False)
    await run_migrations()
    yield get_database()
    await dispose_db()
    get_settings.cache_clear()


class _FakeOs:
    """按路径前缀返回伪造 st_dev 的 os 桩；其余属性透传真实 os 模块。"""

    def __init__(self, devices: dict[str, int]) -> None:
        self._devices = devices

    def stat(self, path):  # noqa: ANN001 -- 与 os.stat 签名对齐即可
        p = str(path).rstrip("/")
        for prefix, dev in self._devices.items():
            root = prefix.rstrip("/")
            if p == root or p.startswith(root + "/"):
                return SimpleNamespace(st_dev=dev)
        raise FileNotFoundError(path)

    def __getattr__(self, name: str):
        return getattr(os, name)


@pytest.fixture
def two_disks(monkeypatch):
    """issue #79 的存储布局：/video 与 /strm 是两块不同的盘。"""
    monkeypatch.setattr(import_watch_config, "os", _FakeOs({"/video": 1, "/strm": 2}))


async def _make_library(db, *, name: str, root: str, kind="movie", match_rules=None):
    async with db.session() as session:
        row = await LibraryRepository(session).create(
            name=name, kind=kind, root_paths=[root], match_rules=match_rules or []
        )
        await session.refresh(row)
        return row


_US_RULE = [{"field": "origin_countries", "op": "any_of", "values": ["US"]}]


async def test_cross_disk_unroutable_library_allowed(db, two_disks):
    """issue #79 场景：跨盘库无收藏范围且非默认——路由不可达，不参与
    同盘检测，auto 硬链规则应能创建。"""
    # 第一个库自动成为默认库（同盘），strm 归档库随后创建（跨盘、不可路由）
    await _make_library(db, name="MR电影", root="/video/MR/电影")
    await _make_library(db, name="strm库", root="/strm/video/电影")
    async with db.session() as session:
        rule = await ImportWatchConfigService(session).create(
            source_path="/video/MP", strategy="hardlink", library_id=None, kind="movie"
        )
        assert rule.id is not None
        assert rule.kind == "movie"


async def test_cross_disk_declared_library_rejected(db, two_disks):
    """跨盘库声明了收藏范围——路由可达，必须拦截，且文案讲明成因。"""
    await _make_library(db, name="MR电影", root="/video/MR/电影")
    await _make_library(db, name="strm库", root="/strm/video/电影", match_rules=_US_RULE)
    async with db.session() as session:
        with pytest.raises(BadRequestException) as exc:
            await ImportWatchConfigService(session).create(
                source_path="/video/MP", strategy="hardlink", library_id=None, kind="movie"
            )
    assert "strm库" in str(exc.value)
    assert "声明了收藏范围" in str(exc.value)


async def test_cross_disk_default_library_rejected(db, two_disks):
    """跨盘库恰是该类型默认库——路由兜底会落到它，必须拦截并讲明成因。"""
    await _make_library(db, name="strm库", root="/strm/video/电影")  # 首库自动成默认
    async with db.session() as session:
        with pytest.raises(BadRequestException) as exc:
            await ImportWatchConfigService(session).create(
                source_path="/video/MP", strategy="hardlink", library_id=None, kind="movie"
            )
    assert "strm库" in str(exc.value)
    assert "默认库" in str(exc.value)


async def test_copy_strategy_skips_disk_check(db, two_disks):
    """复制策略不做同盘检测：跨盘的可路由库不影响 copy 规则创建。"""
    await _make_library(db, name="strm库", root="/strm/video/电影", match_rules=_US_RULE)
    async with db.session() as session:
        rule = await ImportWatchConfigService(session).create(
            source_path="/video/MP", strategy="copy", library_id=None, kind="movie"
        )
        assert rule.id is not None
