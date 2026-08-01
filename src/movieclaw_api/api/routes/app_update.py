"""应用内更新接口（「设置 → 关于与更新」页的后端）。

五个端点（实现全部在 services/app_update.py，本文件只做参数进出）：
- GET  /app/update/status    —— 当前版本/来源/是否支持更新/可否回退；
- POST /app/update/check     —— 查 GitHub 最新 Release 并比对；
- POST /app/update/apply     —— 发起更新（后台执行，进度轮询下方接口）；
- GET  /app/update/progress  —— 更新执行进度；
- POST /app/update/rollback  —— 回退到上一版本（或镜像基线）。
"""

from __future__ import annotations

from fastapi import APIRouter

from movieclaw_api.schemas.app_update import (
    UpdateCheckView,
    UpdateProgressView,
    UpdateStatusView,
)
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services import app_update

router = APIRouter(prefix="/app/update", tags=["app"])


@router.get(
    "/status",
    response_model=ApiResponse[UpdateStatusView],
    summary="读取应用版本与更新能力状态",
    operation_id="app.update_status",
)
async def get_update_status() -> ApiResponse[UpdateStatusView]:
    return ok(app_update.build_status())


@router.post(
    "/check",
    response_model=ApiResponse[UpdateCheckView],
    summary="检查是否有新版本（比对 GitHub 最新 Release）",
    operation_id="app.update_check",
)
async def check_update() -> ApiResponse[UpdateCheckView]:
    return ok(await app_update.check_update())


@router.post(
    "/apply",
    response_model=ApiResponse[UpdateProgressView],
    summary="应用最新版本（下载校验后自动重启生效）",
    operation_id="app.update_apply",
)
async def apply_update() -> ApiResponse[UpdateProgressView]:
    view = await app_update.start_update()
    return ok(view, message="更新已开始，可通过进度接口跟踪")


@router.get(
    "/progress",
    response_model=ApiResponse[UpdateProgressView],
    summary="读取更新执行进度",
    operation_id="app.update_progress",
)
async def get_update_progress() -> ApiResponse[UpdateProgressView]:
    return ok(app_update.get_progress())


@router.post(
    "/rollback",
    response_model=ApiResponse[None],
    summary="回退到上一版本（无上一版本时回落镜像内置版本）",
    operation_id="app.update_rollback",
)
async def rollback_update() -> ApiResponse[None]:
    message = app_update.rollback()
    return ok(None, message=message)
