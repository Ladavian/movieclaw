"""监听导入规则的接口：源目录 → 目标库 搬运配置的 CRUD。

媒体库之上的独立功能（详见 services.import_watch_config 与
services.library.ingest 模块头）：媒体库只有一套目录体系；把外部目录里
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
from movieclaw_api.services.library.config import LibraryConfigService
from movieclaw_db.engine import get_session
from movieclaw_db.models import ImportWatch

router = APIRouter(prefix="/import-watch", tags=["import-watch"])


class ImportWatchPayload(BaseModel):
    """创建/更新监听导入规则的请求体。

    目标三态：``library_id`` 指定库；``target_path`` 自定义目录（识别改名后
    落该目录、不进任何媒体库——整理结果需外部流转再进库的场景，此时
    ``kind`` 必填）；两者都为 null 即**自动路由**（识别出作品后按各库收藏
    范围选库，``kind`` 同样必填）。``library_id`` 与 ``target_path`` 互斥。
    """

    source_path: str = Field(description="源目录（绝对路径，不得与任何库根路径重叠）")
    strategy: Literal["hardlink", "copy"] = Field(
        description="搬运策略：hardlink（零占用需与落点同盘）/ copy（可跨盘）"
    )
    library_id: int | None = Field(
        default=None, description="目标媒体库；null=自动路由或自定义目录"
    )
    target_path: str | None = Field(
        default=None,
        description="自定义目录目标（绝对路径，不得与库根/监听源重叠）；与 library_id 互斥",
    )
    kind: Literal["movie", "tv"] | None = Field(
        default=None, description="自动路由/自定义目录的媒体类型；指定库时忽略"
    )


class ImportWatchView(BaseModel):
    """一条监听导入规则（带目标展示信息）。"""

    id: int
    source_path: str
    strategy: Literal["hardlink", "copy"]
    library_id: int | None = Field(default=None, description="null=自动路由或自定义目录")
    library_name: str | None = None
    target_path: str | None = Field(default=None, description="自定义目录目标（其余目标为 null）")
    kind: Literal["movie", "tv"] | None = Field(
        default=None, description="自动路由/自定义目录的媒体类型（指定库时为 null）"
    )
    target_label: str = Field(
        description="目标展示名：库名 /「自动路由（电影/剧集）」/「自定义目录 …」"
    )
    created_at: datetime

    @classmethod
    def from_model(cls, row: ImportWatch, *, library_name: str | None) -> ImportWatchView:
        created = row.created_at
        if created.tzinfo is None:
            created = created.replace(tzinfo=UTC)
        if row.library_id is not None:
            label = library_name or "?"
        elif row.target_path:
            label = f"自定义目录（{'电影' if row.kind == 'movie' else '剧集'} · {row.target_path}）"
        else:
            label = f"自动路由（{'电影' if row.kind == 'movie' else '剧集'}）"
        return cls(
            id=row.id,  # type: ignore[arg-type]
            source_path=row.source_path,
            strategy=row.strategy,  # type: ignore[arg-type]
            library_id=row.library_id,
            library_name=library_name,
            target_path=row.target_path,
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
    operation_id="watch.list",
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
    operation_id="watch.create",
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
        target_path=payload.target_path,
    )
    views = await _views(session, [row])
    return ok(views[0], message=f"已创建监听导入规则：{row.source_path}")


@router.put(
    "/{rule_id}",
    response_model=ApiResponse[ImportWatchView],
    summary="更新监听导入规则",
    operation_id="watch.update",
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
        target_path=payload.target_path,
    )
    views = await _views(session, [row])
    return ok(views[0], message="已更新")


@router.delete(
    "/{rule_id}",
    response_model=ApiResponse[dict],
    summary="删除监听导入规则（不动磁盘，仅停止监听）",
    operation_id="watch.delete",
    openapi_extra={"x-cli-dangerous": "confirm"},
)
async def delete_rule(
    rule_id: int,
    session: AsyncSession = Depends(get_session),
) -> ApiResponse[dict]:
    service = ImportWatchConfigService(session)
    await service.delete(rule_id)
    return ok({}, message="已删除（源目录与已导入的文件均未受影响）")
