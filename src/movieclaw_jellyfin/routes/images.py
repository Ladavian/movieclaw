"""图片接口（设计文档 5.6）。

资产映射：Movie/Series Primary→poster_file、Backdrop/0→backdrop_file、
Season Primary→media_season.poster_file、Episode Primary→media_episode.still_file。
`tag` 纯缓存语义：不校验、回显进 ETag（带引号）+ 一年 immutable。
缩放参数（maxWidth/quality/fillWidth…）接受但忽略——原图直出（海报资产
本身是 TMDB 中等尺寸），不为此引入图像处理依赖（偏离，见设计文档 9.5）。
"""

from __future__ import annotations

import mimetypes

from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, Response
from sqlalchemy import select
from sqlmodel.ext.asyncio.session import AsyncSession

from movieclaw_db.engine import get_database
from movieclaw_db.models import MediaEpisode, MediaMetadata, MediaSeason
from movieclaw_jellyfin.errors import JellyfinError, not_found
from movieclaw_jellyfin.ids import EntityKind, decode_guid
from movieclaw_jellyfin.routes.common import dto_context

router = APIRouter()


async def _resolve_asset(
    session: AsyncSession, item_id: str, image_type: str
) -> str | None:
    """按条目 GUID + 图片类型解析资产相对路径；无资产返回 None。"""
    ref = decode_guid(item_id)
    if ref is None:
        raise not_found()
    itype = image_type.lower()

    if ref.kind == EntityKind.LIBRARY:
        return None

    if ref.kind == EntityKind.ITEM:
        meta = (
            await session.execute(
                select(MediaMetadata).where(MediaMetadata.media_item_id == ref.entity_id)
            )
        ).scalar_one_or_none()
        if meta is None:
            return None
        if itype == "primary":
            return meta.poster_file
        if itype == "backdrop":
            return meta.backdrop_file
        return None

    if ref.kind == EntityKind.SEASON and itype == "primary":
        row = (
            await session.execute(
                select(MediaSeason).where(
                    MediaSeason.media_item_id == ref.entity_id,
                    MediaSeason.season_number == ref.season,
                )
            )
        ).scalar_one_or_none()
        return row.poster_file if row else None

    if ref.kind == EntityKind.EPISODE and itype == "primary":
        row = (
            await session.execute(
                select(MediaEpisode).where(
                    MediaEpisode.media_item_id == ref.entity_id,
                    MediaEpisode.season_number == ref.season,
                    MediaEpisode.episode_number == ref.episode,
                )
            )
        ).scalar_one_or_none()
        return row.still_file if row else None
    return None


@router.get("/Items/{item_id}/Images/{image_type}")
@router.head("/Items/{item_id}/Images/{image_type}")
@router.get("/Items/{item_id}/Images/{image_type}/{image_index}")
@router.head("/Items/{item_id}/Images/{image_type}/{image_index}")
async def get_item_image(
    request: Request, item_id: str, image_type: str, image_index: int = 0
) -> Response:
    ctx = await dto_context()
    async with get_database().session() as session:
        rel_path = await _resolve_asset(session, item_id, image_type)
    if not rel_path:
        # 条目在但无该类型图：text 文案 404（对齐 ImageController.cs:1875）
        raise JellyfinError(404, text=f"Item does not have an image of type {image_type}")

    target = (ctx.assets_root / rel_path).resolve()
    if not target.is_relative_to(ctx.assets_root.resolve()) or not target.is_file():
        raise not_found()

    tag = request.query_params.get("tag")
    headers = {"Vary": "Accept"}
    no_cache = "no-cache" in (request.headers.get("Cache-Control") or "")
    if no_cache:
        # 客户端明确要新鲜内容：不缓存也不做 304 协商
        headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
    elif tag:
        headers["Cache-Control"] = "public, max-age=31536000, immutable"
        headers["ETag"] = f'"{tag}"'
        inm = request.headers.get("If-None-Match", "")
        if inm.strip('"') == tag:
            return Response(status_code=304, headers=headers)
    else:
        headers["Cache-Control"] = "public"

    if not no_cache:
        # If-Modified-Since 协商（只发它不发 ETag 的客户端也要能 304）
        ims = request.headers.get("If-Modified-Since")
        if ims:
            from email.utils import parsedate_to_datetime

            try:
                since = parsedate_to_datetime(ims).timestamp()
                if target.stat().st_mtime <= since:
                    return Response(status_code=304, headers=headers)
            except (TypeError, ValueError, OSError):
                pass

    media_type = mimetypes.guess_type(str(target))[0] or "image/jpeg"
    return FileResponse(target, media_type=media_type, headers=headers)
