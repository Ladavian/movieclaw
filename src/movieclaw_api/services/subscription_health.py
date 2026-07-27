"""订阅链路体检：对每个库预演「投递 → 转移 → 入库」全链路，联合约束一次亮清。

为什么放在订阅名下（用户决策 2026-07-27）：下载器路径映射、监听导入、库根
路径分散在三个配置页，各自保存时都有前置校验，但**正确性是联合约束**
（映射要覆盖投递目录、硬链要求源目录与库主根同盘、投递目录又取决于库有
没有监听规则）。用户真正的问题是"订阅后能不能自动下载并入库"——体检以
这个问题为锚，把每个库的链路逐段陈述事实，红项给修复去处。

口径同源原则：判定全部复用真实投递/搬运用的同一批原语（resolve_dispatch_rule
的兜底顺序、torrent_submit.mapping_covers 的映射覆盖、同盘检测的 st_dev
比对），不另写一套"体检版"逻辑——否则迟早出现"体检说好、投递却挂"。

三档结论：
- ok：这一段没有问题；
- warn：能转但降级（如 watchdog 缺失退化为每小时兜底巡检、目录未就绪
  暂无法检测）——不阻断入库，值得知道；
- error：投递会被拒绝或内容不会自动入库，必须修。
"""

from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field

from sqlalchemy.ext.asyncio import AsyncSession
from sqlmodel import select

from movieclaw_db.models import ImportWatch, Library
from movieclaw_db.models.downloader_client import DownloaderClient
from movieclaw_db.models.site_credential import ConfigStatus
from movieclaw_db.repositories.library_repo import LibraryRepository

# 状态严重度：聚合时取最坏
_SEVERITY = {"ok": 0, "warn": 1, "error": 2}


@dataclass
class HealthCheck:
    """链路上的一段检查结论。"""

    key: str  # downloader / mapping / transfer_disk / watch_active / ingest
    label: str  # 段落名（前端左列）
    status: str  # ok / warn / error
    detail: str  # 中文事实陈述（前端右列，直接展示）
    fix_section: str | None = None  # 修复去处：settings 分区 id 或 "libraries"


@dataclass
class LibraryPipeline:
    """一个库的完整链路结论。"""

    library_id: int
    library_name: str
    kind: str
    is_default: bool
    mode: str  # watch / inplace / downloader_default
    path: str | None  # 投递基底目录（movieclaw 视角）
    status: str  # 全链路最坏状态
    checks: list[HealthCheck] = field(default_factory=list)


def _worst(statuses: list[str]) -> str:
    return max(statuses, key=lambda s: _SEVERITY.get(s, 0)) if statuses else "ok"


async def _default_downloader(session: AsyncSession) -> DownloaderClient | None:
    """可用的默认下载器（与投递预检同一查询口径）。"""
    result = await session.execute(
        select(DownloaderClient).where(
            DownloaderClient.is_default.is_(True),  # type: ignore[attr-defined]
            DownloaderClient.enabled.is_(True),  # type: ignore[attr-defined]
            DownloaderClient.status == ConfigStatus.ACTIVE,
        )
    )
    return result.scalars().first()


def _watched_dirs() -> frozenset[str]:
    """实时监听中的源目录集合；监听器未启动（如测试环境）返回空集。"""
    from movieclaw_api.services.library_ingest import get_ingest_watcher

    watcher = get_ingest_watcher()
    return watcher.watched_keys() if watcher is not None else frozenset()


def _check_transfer(
    rule: ImportWatch, library: Library, watched: frozenset[str]
) -> list[HealthCheck]:
    """watch 模式的「转移」段：硬链同盘 + 监听是否实际生效。"""
    checks: list[HealthCheck] = []
    if rule.strategy == "hardlink" and library.primary_root:
        try:
            same = os.stat(rule.source_path).st_dev == os.stat(library.primary_root).st_dev  # type: ignore[arg-type]
        except OSError:
            checks.append(
                HealthCheck(
                    key="transfer_disk",
                    label="硬链接同盘",
                    status="warn",
                    detail=(
                        f"目录暂不可达（{rule.source_path} 或库主根未挂载），无法检测；"
                        "挂载就绪后重新体检"
                    ),
                    fix_section="import-watch",
                )
            )
        else:
            if same:
                checks.append(
                    HealthCheck(
                        key="transfer_disk",
                        label="硬链接同盘",
                        status="ok",
                        detail="源目录与库主根在同一文件系统，硬链接零占用可用",
                    )
                )
            else:
                checks.append(
                    HealthCheck(
                        key="transfer_disk",
                        label="硬链接同盘",
                        status="error",
                        detail=(
                            f"源目录与「{library.name}」的主根不在同一文件系统，"
                            "硬链接会失败——请把监听导入规则的策略改为「复制」，"
                            "或调整存储布局"
                        ),
                        fix_section="import-watch",
                    )
                )
    if rule.source_path in watched:
        checks.append(
            HealthCheck(
                key="watch_active",
                label="目录监听",
                status="ok",
                detail="源目录正在实时监听，下载完成即处理",
            )
        )
    else:
        checks.append(
            HealthCheck(
                key="watch_active",
                label="目录监听",
                status="warn",
                detail=(
                    "源目录未在实时监听（watchdog 缺失或目录未就绪），"
                    "由每小时的兜底巡检接手——能入库，但不及时"
                ),
                fix_section="import-watch",
            )
        )
    return checks


async def pipeline_health(session: AsyncSession) -> dict:
    """全部库的链路体检。返回 dict（路由层直接进响应模型）。"""
    from movieclaw_api.services.import_watch_config import resolve_dispatch_rule
    from movieclaw_api.services.torrent_submit import mapping_covers

    downloader = await _default_downloader(session)
    watched = _watched_dirs()
    libraries = await LibraryRepository(session).list_all()

    pipelines: list[LibraryPipeline] = []
    for library in libraries:
        assert library.id is not None
        checks: list[HealthCheck] = []

        # —— 段 1：投递（下载器 + 目录 + 映射守门，与 dispatch 三级兜底同源）
        if downloader is None:
            checks.append(
                HealthCheck(
                    key="downloader",
                    label="下载器",
                    status="error",
                    detail="没有可用的默认下载器——订阅只能记录意愿，无法真实下载",
                    fix_section="downloaders",
                )
            )
        else:
            checks.append(
                HealthCheck(
                    key="downloader",
                    label="下载器",
                    status="ok",
                    detail=f"默认下载器「{downloader.name}」已就绪",
                )
            )

        rule = await resolve_dispatch_rule(session, library.id, kind=library.kind)
        base = rule.source_path if rule else library.primary_root
        mode = "watch" if rule else ("inplace" if library.primary_root else "downloader_default")

        if base is None:
            checks.append(
                HealthCheck(
                    key="dispatch_dir",
                    label="投递目录",
                    status="error",
                    detail="库没有根路径，下载会落到下载器默认目录且不会自动入库",
                    fix_section="libraries",
                )
            )
        elif mode == "watch":
            checks.append(
                HealthCheck(
                    key="dispatch_dir",
                    label="投递目录",
                    status="ok",
                    detail=f"投递到监听导入目录 {base}，下载完成后自动整理入库",
                )
            )
        else:
            checks.append(
                HealthCheck(
                    key="dispatch_dir",
                    label="投递目录",
                    status="ok",
                    detail=f"直接下载进库根 {base}，完成后库扫描自动入账",
                )
            )

        if downloader is not None and base is not None and downloader.path_mappings:
            if mapping_covers(base, downloader.path_mappings):
                checks.append(
                    HealthCheck(
                        key="mapping",
                        label="路径映射",
                        status="ok",
                        detail=f"投递目录已被「{downloader.name}」的路径映射覆盖",
                    )
                )
            else:
                checks.append(
                    HealthCheck(
                        key="mapping",
                        label="路径映射",
                        status="error",
                        detail=(
                            f"目录 {base} 不在下载器「{downloader.name}」的路径映射"
                            "覆盖范围内，投递会被拒绝——请补一条路径映射"
                            "（下载器可直达同名路径时，添加两边相同的一条即可）"
                        ),
                        fix_section="downloaders",
                    )
                )

        # —— 段 2：转移（仅 watch 模式：硬链同盘 + 监听生效）
        if rule is not None:
            checks.extend(_check_transfer(rule, library, watched))

        status = _worst([c.status for c in checks])
        pipelines.append(
            LibraryPipeline(
                library_id=library.id,
                library_name=library.name,
                kind=library.kind,
                is_default=library.is_default,
                mode=mode,
                path=base,
                status=status,
                checks=checks,
            )
        )

    overall = _worst([p.status for p in pipelines])
    return {
        "status": overall,
        "error_count": sum(1 for p in pipelines if p.status == "error"),
        "warn_count": sum(1 for p in pipelines if p.status == "warn"),
        "libraries": [asdict(p) for p in pipelines],
    }
