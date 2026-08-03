"""取流领域服务：本地文件 Range 直连与 strm 网盘直链解析。

- 本地文件：交给 Starlette FileResponse（原生 Range/206/If-Range/HEAD），
  Content-Type 按容器查表；
- strm 占位文件：读第一个非空、非 ``#`` 开头的行，**只接受
  http/https/rtsp/rtp 绝对 URI**——File 协议/相对路径必须拒绝，否则 strm
  就成了任意本地文件读取漏洞（对齐 Jellyfin ProbeProvider.FetchShortcutInfo
  与 BaseItem.cs:1191-1194 的安全语义）。播放走 302 重定向，不做反向代理
  （零网盘流量，docs/design/jellyfin-compat.md 6.4）。
"""

from __future__ import annotations

import logging
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger("movieclaw_playback.streaming")

STRM_EXT = ".strm"

_ALLOWED_STRM_SCHEMES = {"http", "https", "rtsp", "rtp"}

# 容器 → MIME（对齐 Jellyfin MimeTypes.cs 的常用子集；未知视频容器兜底 video/{ext}）
_CONTAINER_MIME = {
    "mkv": "video/x-matroska",
    "mp4": "video/mp4",
    "m4v": "video/x-m4v",
    "ts": "video/mp2t",
    "m2ts": "video/mp2t",
    "avi": "video/x-msvideo",
    "mov": "video/quicktime",
    "webm": "video/webm",
    "wmv": "video/x-ms-wmv",
    "flv": "video/x-flv",
    "mpg": "video/mpeg",
    "mpeg": "video/mpeg",
    "iso": "application/x-iso9660-image",
}


def container_mime_type(container: str | None) -> str:
    """按容器名取 Content-Type；未知容器给 video/{ext} 兜底。"""
    if not container:
        return "application/octet-stream"
    ext = container.lower().lstrip(".")
    return _CONTAINER_MIME.get(ext, f"video/{ext}")


def is_strm(file_path: str) -> bool:
    return file_path.lower().endswith(STRM_EXT)


def resolve_strm_url(file_path: str | Path) -> str | None:
    """读 strm 文件内容解析出远程播放地址；不合法返回 None。

    取第一个非空且不以 ``#`` 开头的行；仅接受白名单 scheme 的绝对 URI。
    每次播放现读文件——strm 内容可能是带时效签名的直链，不缓存。
    """
    try:
        text = Path(file_path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.warning("strm 文件读取失败：%s", file_path)
        return None
    for line in text.splitlines():
        candidate = line.strip()
        if not candidate or candidate.startswith("#"):
            continue
        scheme = urlsplit(candidate).scheme.lower()
        if scheme in _ALLOWED_STRM_SCHEMES:
            return candidate
        logger.warning(
            "strm 内容不是允许的远程地址（仅接受 http/https/rtsp/rtp），已拒绝：%s",
            file_path,
        )
        return None
    return None
