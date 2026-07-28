"""监听导入规则的接口：源目录 → 目标库 搬运配置的 CRUD。

媒体库之上的独立功能（详见 services.import_watch_config 与
services.library_ingest 模块头）：媒体库只有一套目录体系；把外部目录里
下载完成的内容搬进库，由这里配置的规则驱动。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Literal

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services.import_watch_config import ImportWatchConfigService
from movieclaw_api.services.library_config import LibraryConfigService
from movieclaw_db.engine import get_session
from movieclaw_db.models import ImportWatch

router = APIRouter(prefix="/import-watch", tags=["import-watch"])


class ImportWatchPayload(BaseModel):
    """创建/更新监听导入规则的请求体。

    目标二选一：``library_id`` 指定库；为 null 即**自动路由**（识别出作品后
    按各库收藏范围选库），此时 ``kind`` 必填（识别链需要 movie/tv 先验）。
    """

    source_path: str = Field(description="源目录（绝对路径，不得与任何库根路径重叠）")
    strategy: Literal["hardlink", "copy"] = Field(
        description="搬运策略：hardlink（零占用需与目标库主根同盘）/ copy（可跨盘）"
    )
    library_id: int | None = Field(
        default=None, description="目标媒体库；null=自动路由（按收藏范围选库）"
    )
    kind: Literal["movie", "tv"] | None = Field(
        default=None, description="自动路由的媒体类型；指定库时忽略"
    )


class ImportWatchView(BaseModel):
    """一条监听导入规则（带目标展示信息）。"""

    id: int
    source_path: str
    strategy: Literal["hardlink", "copy"]
    library_id: int | None = Field(default=None, description="null=自动路由")
    library_name: str | None = None
    kind: Literal["movie", "tv"] | None = Field(
        default=None, description="自动路由的媒体类型（指定库时为 null）"
    )
    target_label: str = Field(description="目标展示名：库名或「自动路由（电影/剧集）」")
    created_at: datetime

    @classmethod
    def from_model(cls, row: ImportWatch, *, library_name: str | None) -> ImportWatchView:
        created = row.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if row.library_id is not None:
            label = library_name or "?"
        else:
            label = f"自动路由（{'电影' if row.kind == 'movie' else '剧集'}）"
        return cls(
            id=row.id,  # type: ignore[arg-type]
            source_path=row.source_path,
            strategy=row.strategy,  # type: ignore[arg-type]
            library_id=row.library_id,
            library_name=library_name,
            kind=row.kind,  # type: ignore[arg-type]
            target_label=label,
            created_at=created,
        )


async def _views(session: AsyncSession, rows: list[ImportWatch]) -> list[ImportWatchView]:
    names = {lib.id: lib.name for lib in await LibraryConfigService(session).list_all()}
    return [ImportWatchView.from_model(r, library_name=names.get(r.library_id)) for r in rows]


@router.get(
    "",
    response_model=ApiResponse[list[ImportWatchView]],
    summary="列出全部监听导入规则",
)
async def list_rules(
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[list[ImportWatchView]]:
    service = ImportWatchConfigService(session)
    return ok(await _views(session, await service.list_all()))


@router.post(
    "",
    response_model=ApiResponse[ImportWatchView],
    summary="创建监听导入规则（硬链接策略保存即做同盘检测）",
)
async def create_rule(
    payload: ImportWatchPayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[ImportWatchView]:
    service = ImportWatchConfigService(session)
    row = await service.create(
        source_path=payload.source_path,
        strategy=payload.strategy,
        library_id=payload.library_id,
        kind=payload.kind,
    )
    views = await _views(session, [row])
    return ok(views[0], message=f"已创建监听导入规则：{row.source_path}")


@router.put(
    "/{rule_id}",
    response_model=ApiResponse[ImportWatchView],
    summary="更新监听导入规则",
)
async def update_rule(
    rule_id: int,
    payload: ImportWatchPayload,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[ImportWatchView]:
    service = ImportWatchConfigService(session)
    row = await service.update(
        rule_id,
        source_path=payload.source_path,
        strategy=payload.strategy,
        library_id=payload.library_id,
        kind=payload.kind,
    )
    views = await _views(session, [row])
    return ok(views[0], message="已更新")


@router.delete(
    "/{rule_id}",
    response_model=ApiResponse[dict],
    summary="删除监听导入规则（不动磁盘，仅停止监听）",
)
async def delete_rule(
    rule_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    service = ImportWatchConfigService(session)
    await service.delete(rule_id)
    return ok({}, message="已删除（源目录与已导入的文件均未受影响）")
