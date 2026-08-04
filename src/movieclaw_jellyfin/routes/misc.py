"""启动期轻量接口与敷衍实现（设计文档 §2 P1）。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Response
from fastapi.responses import JSONResponse, PlainTextResponse

from movieclaw_jellyfin.security import require_device

router = APIRouter()


@router.get("/Branding/Configuration")
async def branding_configuration() -> JSONResponse:
    return JSONResponse({"SplashscreenEnabled": False})


@router.get("/Branding/Css")
@router.get("/Branding/Css.css")
async def branding_css() -> PlainTextResponse:
    return PlainTextResponse("", media_type="text/css")


@router.get("/QuickConnect/Enabled")
async def quickconnect_enabled() -> JSONResponse:
    # 恒 false（有意偏离：真默认 true）——客户端据此隐藏 QuickConnect 入口
    return JSONResponse(False)


@router.get("/Sessions", dependencies=[Depends(require_device)])
async def sessions() -> JSONResponse:
    return JSONResponse([])


@router.post(
    "/Sessions/Capabilities", status_code=204, dependencies=[Depends(require_device)]
)
@router.post(
    "/Sessions/Capabilities/Full",
    status_code=204,
    dependencies=[Depends(require_device)],
)
async def sessions_capabilities() -> Response:
    # 204 且不存储 DeviceProfile：PlaybackInfo 因此永远无 profile 可回退，
    # 等价于"无转码权限的 Jellyfin"（设计文档 6.1）
    return Response(status_code=204)


@router.delete(
    "/Videos/ActiveEncodings", status_code=204, dependencies=[Depends(require_device)]
)
async def active_encodings() -> Response:
    # 部分客户端退出时无条件发一次转码清理；我们无转码，204 即可
    return Response(status_code=204)
