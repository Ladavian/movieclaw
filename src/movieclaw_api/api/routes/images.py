"""通用图片代理接口（带本地磁盘缓存）与刮削图片资产直出。"""

from fastapi import APIRouter, Query
from fastapi.responses import FileResponse

from movieclaw_api.exceptions import NotFoundException
from movieclaw_api.services.image_cache import get_image_cache

router = APIRouter(prefix="/images", tags=["images"])


@router.get(
    "/proxy",
    response_class=FileResponse,
    summary="代理并缓存远程图片",
    operation_id="images.proxy",
    openapi_extra={"x-cli-hidden": True},
)
async def proxy_image(url: str = Query(min_length=1, max_length=2048)) -> FileResponse:
    """前端所有远程图片的统一入口：命中读本地缓存，未命中回源抓取后落盘。

    域名安全（SSRF 防护）、类型和体积校验在 ImageProxy 服务层完成。
    图床 URL 对应的内容事实上不可变，浏览器侧直接给一年 immutable 缓存。
    """
    cached = await get_image_cache().get_or_fetch(url)
    return FileResponse(
        cached.path,
        media_type=cached.content_type,
        headers={"Cache-Control": "public, max-age=31536000, immutable"},
    )


@router.get(
    "/assets/{path:path}",
    response_class=FileResponse,
    summary="刮削图片资产直出（data/metadata/images 下的本地文件）",
    operation_id="images.asset",
    openapi_extra={"x-cli-hidden": True},
)
async def get_metadata_asset(path: str) -> FileResponse:
    """海报/剧照等刮削资产的服务通道（docs/design/metadata.md 6.1）。

    路径限定在资产根目录内（防目录穿越）。force 刷新会原地覆盖同名文件，
    故不给 immutable，一天后重新校验即可。
    """
    from movieclaw_api.services.media_scrape import assets_root

    root = assets_root().resolve()
    target = (root / path).resolve()
    if not target.is_relative_to(root) or not target.is_file():
        raise NotFoundException("图片资产不存在")
    return FileResponse(
        target,
        media_type="image/jpeg",
        headers={"Cache-Control": "public, max-age=86400"},
    )
