"""应用设置接口（「设置 → 应用设置」页的后端）。

三个端点（实现全部在 services/app_config.py，本文件只做参数进出）：
- GET  /app/config  —— 读取当前配置（外部访问地址）；
- PUT  /app/config  —— 保存，即时生效；
- POST /app/restart —— 优雅重启应用（Docker 镜像由 entrypoint 重启循环拉起）。
"""

from __future__ import annotations

from fastapi import APIRouter

from movieclaw_api.schemas.app_config import AppConfigPayload, AppConfigView
from movieclaw_api.schemas.response import ApiResponse, ok
from movieclaw_api.services import app_config

router = APIRouter(prefix="/app", tags=["app"])


@router.get(
    "/config",
    response_model=ApiResponse[AppConfigView],
    summary="读取应用设置",
    operation_id="app.show",
)
async def get_app_config() -> ApiResponse[AppConfigView]:
    return ok(await app_config.build_config_view())


@router.put(
    "/config",
    response_model=ApiResponse[AppConfigView],
    summary="保存应用设置（即时生效）",
    operation_id="app.set",
)
async def save_app_config(payload: AppConfigPayload) -> ApiResponse[AppConfigView]:
    return ok(await app_config.save_config(payload))


@router.post(
    "/restart",
    response_model=ApiResponse[None],
    summary="重启应用（优雅停机后由容器入口/进程守护拉起）",
    operation_id="app.restart",
)
async def restart_app() -> ApiResponse[None]:
    app_config.schedule_restart()
    return ok(None, message="应用正在重启，稍候将自动恢复")
