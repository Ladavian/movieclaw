"""Jellyfin 兼容路由总装与应用挂载。

- 所有路由 `include_in_schema=False`：这是协议模仿层，不进业务 OpenAPI，
  也不受业务鉴权守护测试约束（它有自己的 token 体系与守护测试）；
- 同时注册 `/emby` 前缀别名（非 Jellyfin 行为，纯为个别客户端探测兜底）；
- JellyfinError 由本层的异常处理器渲染（四形态语义），不走业务统一处理器。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, FastAPI, Request
from fastapi.responses import Response

from movieclaw_jellyfin.errors import JellyfinError, render_error
from movieclaw_jellyfin.routes.common import require_enabled
from movieclaw_jellyfin.routes.images import router as images_router
from movieclaw_jellyfin.routes.library import router as library_router
from movieclaw_jellyfin.routes.misc import router as misc_router
from movieclaw_jellyfin.routes.playback import router as playback_router
from movieclaw_jellyfin.routes.playstate import router as playstate_router
from movieclaw_jellyfin.routes.system import router as system_router
from movieclaw_jellyfin.routes.users import router as users_router

# Jellyfin 命名空间的首段（大小写归一化中间件据此识别本层请求）
NAMESPACE_PREFIXES = {
    "system",
    "users",
    "userviews",
    "useritems",
    "userplayeditems",
    "userfavoriteitems",
    "items",
    "videos",
    "shows",
    "sessions",
    "playingitems",
    "branding",
    "quickconnect",
    "emby",
}


# 注册顺序即匹配顺序：字面路径的模块在参数路径之前
_SUB_ROUTERS = [
    system_router,
    users_router,
    misc_router,
    playstate_router,
    playback_router,
    images_router,
    library_router,
]


def build_router() -> APIRouter:
    router = APIRouter(dependencies=[Depends(require_enabled)])
    for sub in _SUB_ROUTERS:
        router.include_router(sub)
    return router


def register(app: FastAPI) -> None:
    """把兼容层挂到 FastAPI 应用（根路径 + /emby 别名）。"""
    router = build_router()
    app.include_router(router, include_in_schema=False)
    app.include_router(router, prefix="/emby", include_in_schema=False)

    @app.exception_handler(JellyfinError)
    async def jellyfin_error_handler(_request: Request, exc: JellyfinError) -> Response:
        return render_error(exc)

    # 大小写归一化（设计文档 9.1）：ASP.NET 路由天生大小写不敏感而 Starlette
    # 敏感。预存**本层**路由模板各字面段的"小写 → 规范"映射，命中 Jellyfin
    # 命名空间的请求逐段替换；路径参数段（GUID 等）不在映射里、原样保留。
    # 直接扫各子路由模块（避开 FastAPI 惰性 include 的反射不确定性），也不扫
    # 业务路由——防止同名小写段（如 items）污染规范形态。
    literal_map: dict[str, str] = {}
    for sub in _SUB_ROUTERS:
        for route in sub.routes:
            template = getattr(route, "path", "")
            for segment in template.split("/"):
                if segment and "{" not in segment:
                    literal_map.setdefault(segment.lower(), segment)

    @app.middleware("http")
    async def jellyfin_case_normalizer(request: Request, call_next):  # type: ignore[no-untyped-def]
        path = request.scope.get("path", "")
        parts = path.split("/")
        if len(parts) > 1 and parts[1].lower() in NAMESPACE_PREFIXES:
            request.scope["path"] = "/".join(
                literal_map.get(p.lower(), p) if p else p for p in parts
            )
        return await call_next(request)
