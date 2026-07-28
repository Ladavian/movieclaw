"""入库的公共命名约定与共享常量（扫描 / 监听导入 / 整理共用）。

历史沿革：本模块曾承载订阅专属的"下载完成 → 硬链入库"管线
（import_completed_torrent）。架构定稿"订阅止于投递"后，搬运统一由
监听导入（library_ingest，按 info_hash 认领订阅身份）与库扫描（原地
入账）完成，工单由库存对账关闭（wanted_fulfillment），订阅专属管线
退役。这里沉淀的是三个入库引擎共用的约定：

- ``VIDEO_EXTS``：视频文件扩展名（入库对象）；
- ``IN_PROGRESS_MARKERS``：下载器/浏览器的"未完成"标记后缀
  （扫描与监听导入的完整性检测共用）；
- ``season_from_dir`` / ``entry_dirs``：季目录与条目目录的判定——识别链
  （取片名证据）、待识别分组、条目真实删除三处必须是同一套约定，各写
  各的会出事（实测隐患：删除按"库根直接子目录"算条目目录，遇到
  ``剧集/大陆/风筝 (2017)/`` 这种分类分组层会把整个「大陆」目录删掉）；
- ``entry_base_name``：条目级规范名 ``标题 (年份)``——库目录名与
  规范文件名的公共前缀，与 ``derive_save_path`` 的目录名一致。
"""

from __future__ import annotations

import re
from pathlib import Path

from movieclaw_api.services.library_config import sanitize_folder_name
from movieclaw_db.models import MediaItem

# 视频文件扩展名（入库对象）；其余（字幕/nfo/图片）v1 不搬运
VIDEO_EXTS = {
    ".mkv",
    ".mp4",
    ".avi",
    ".ts",
    ".m2ts",
    ".wmv",
    ".mov",
    ".flv",
    ".rmvb",
    ".mpg",
    ".mpeg",
    ".m4v",
    ".webm",
}
# 文件名/路径含这些标记的视频不入库（样品片段等）
_IGNORE_MARKERS = ("sample",)

# 下载器/浏览器的"未完成"标记（文件名小写后缀匹配）：qBittorrent .!qb、
# aria2 控制文件 .aria2、Chrome .crdownload、Firefox/迅雷等 .part/.td、
# BitComet .bc!、通用临时后缀。扫描器与监听导入共用（放在本模块避免
# scan ↔ ingest 的循环导入）
IN_PROGRESS_MARKERS = (
    ".!qb",
    ".part",
    ".aria2",
    ".crdownload",
    ".download",
    ".downloading",
    ".td",
    ".bc!",
    ".tmp",
    ".temp",
    ".unfinished",
)


# 季目录名："Season 02" / "S02" / "Specials" / "特别篇"
_SEASON_DIR = re.compile(r"^(?:season[ ._-]*(\d{1,3})|s(\d{1,3}))$", re.IGNORECASE)
_SPECIALS_DIR = re.compile(r"^(?:specials?|特别篇|特典)$", re.IGNORECASE)


def season_from_dir(directory: Path) -> int | None:
    """目录名声明的季号（特别篇为 0）；不是季目录返回 None。"""
    name = directory.name.strip()
    if _SPECIALS_DIR.match(name):
        return 0
    match = _SEASON_DIR.match(name)
    return int(match.group(1) or match.group(2)) if match else None


def entry_dirs(root: Path, file: Path) -> list[Path]:
    """库根与文件之间的各级目录，由近及远；季目录跳过（它不带片名信息）。

    ``{root}/大陆/风筝 (2017)/Season 1/x.mkv`` → ``[风筝 (2017), 大陆]``。
    第一个元素就是**条目目录**；文件直接躺在库根下、或不在库根之下时为空。

    为什么不是"库根的直接子目录"：分类分组层（大陆/欧美/日韩、按年代
    分文件夹）在真实媒体库里非常普遍，认死第一层会把分组名当条目名。
    """
    dirs: list[Path] = []
    current = file.parent
    while current != root and current.parent != current:
        try:
            current.relative_to(root)
        except ValueError:
            break
        if season_from_dir(current) is None:
            dirs.append(current)
        current = current.parent
    return dirs


def entry_dir_of(roots: list[Path], file: Path) -> Path | None:
    """文件归属的条目目录（多库根版本）；不在任何根下或裸文件时为 None。"""
    for root in roots:
        try:
            file.relative_to(root)
        except ValueError:
            continue
        dirs = entry_dirs(root, file)
        return dirs[0] if dirs else None
    return None


def entry_base_name(item: MediaItem) -> str:
    """条目级规范名：``标题 (年份)``（中文优先，与库目录名一致）。"""
    base = sanitize_folder_name(item.title)
    return f"{base} ({item.year})" if item.year is not None else base
