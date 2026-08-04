"""图片接口（设计文档 5.6）。

资产映射：Movie/Series Primary→poster_file、Backdrop/0→backdrop_file、
Season Primary→media_season.poster_file、Episode Primary→media_episode.still_file；
库 Primary→服务端渲染的氛围光货架拼贴（library.cover 服务，双端共用）。
`tag` 纯缓存语义：不校验、回显进 ETag（带引号）+ 一年 immutable。
缩放：maxWidth/maxHeight/width/height/fillWidth/fillHeight 任一存在时按
fit-within 等比缩小（只缩不放），产物落 data/cache/jellyfin-images 复用。
"""

from __future__ import annotations

import asyncio
import hashlib
import mimetypes
from pathlib import Path

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


async def _person_image(person_id: int, image_type: str) -> Response:
    """影人头像：TMDB profile 经图片代理（SSRF 防护 + 本地缓存）落盘直出。

    离线/图床不可达时 404——播放器按无头像降级，不阻断详情页。"""
    if image_type.lower() != "primary":
        raise JellyfinError(404, text=f"Item does not have an image of type {image_type}")
    from movieclaw_api.core.config import get_settings
    from movieclaw_api.services.image_cache import get_image_cache
    from movieclaw_db.models import Person

    async with get_database().session() as session:
        person = await session.get(Person, person_id)
    if person is None or not person.profile_path:
        raise JellyfinError(404, text="Item does not have an image of type Primary")
    base = get_settings().tmdb_image_base_url.rstrip("/")
    try:
        cached = await get_image_cache().get_or_fetch(f"{base}/w300{person.profile_path}")
    except Exception:
        raise not_found() from None
    import hashlib

    tag = hashlib.md5(person.profile_path.encode()).hexdigest()
    return FileResponse(
        cached.path,
        media_type=cached.content_type,
        headers={
            "Cache-Control": "public, max-age=31536000, immutable",
            "ETag": f'"{tag}"',
        },
    )


async def _resolve_asset(
    session: AsyncSession, item_id: str, image_type: str
) -> str | None:
    """按条目 GUID + 图片类型解析资产相对路径；无资产返回 None。"""
    ref = decode_guid(item_id)
    if ref is None:
        raise not_found()
    itype = image_type.lower()

    if ref.kind == EntityKind.LIBRARY:
        return None  # 库封面走拼贴专路（get_item_image 特判），不经资产目录

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
    # 库封面：服务端渲染的氛围光货架拼贴（与控制台媒体库页同一张图）
    ref = decode_guid(item_id)
    if ref is not None and ref.kind == EntityKind.LIBRARY:
        if image_type.lower() != "primary":
            raise JellyfinError(
                404, text=f"Item does not have an image of type {image_type}"
            )
        from movieclaw_api.services.library.cover import ensure_library_cover

        cover = await ensure_library_cover(ref.entity_id)
        if cover is None:
            raise JellyfinError(404, text="Item does not have an image of type Primary")
        return FileResponse(
            cover[0],
            media_type="image/jpeg",
            headers={
                "Vary": "Accept",
                "Cache-Control": "public, max-age=31536000, immutable",
                "ETag": f'"{cover[1]}"',
            },
        )
    if ref is not None and ref.kind == EntityKind.PERSON:
        return await _person_image(ref.entity_id, image_type)
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

    target = await _maybe_scaled(target, request)
    media_type = mimetypes.guess_type(str(target))[0] or "image/jpeg"
    return FileResponse(target, media_type=media_type, headers=headers)


_SCALE_PARAMS = ("maxWidth", "maxHeight", "width", "height", "fillWidth", "fillHeight")


def _scale_bounds(request: Request) -> tuple[int, int] | None:
    """从缩放参数取目标框（fit-within 语义）；无参数返回 None。"""
    values: list[tuple[str, int]] = []
    for name in _SCALE_PARAMS:
        raw = request.query_params.get(name)
        if raw is None:
            continue
        try:
            value = int(raw)
        except ValueError:
            continue
        if value > 0:
            values.append((name, value))
    if not values:
        return None
    widths = [v for n, v in values if "idth" in n]
    heights = [v for n, v in values if "eight" in n]
    return (min(widths) if widths else 8192, min(heights) if heights else 8192)


def _render_scaled(src: Path, out: Path, bounds: tuple[int, int]) -> None:
    from PIL import Image

    img = Image.open(src)
    img.thumbnail(bounds)  # 等比缩小，不放大
    img.convert("RGB").save(out, "JPEG", quality=90)


async def _maybe_scaled(target: Path, request: Request) -> Path:
    """按需生成缩放变体（缓存复用）；无缩放参数或缩放失败时原图直出。"""
    bounds = _scale_bounds(request)
    if bounds is None:
        return target
    try:
        stat = target.stat()
    except OSError:
        return target
    from movieclaw_api.core.config import get_settings

    cache_dir = Path(get_settings().image_cache_dir) / "jellyfin-scaled"
    key = hashlib.md5(
        f"{target}:{stat.st_mtime_ns}:{bounds[0]}x{bounds[1]}".encode()
    ).hexdigest()
    cached = cache_dir / f"{key}.jpg"
    if cached.is_file():
        return cached
    try:
        cache_dir.mkdir(parents=True, exist_ok=True)
        await asyncio.to_thread(_render_scaled, target, cached, bounds)
    except Exception:
        return target
    return cached
