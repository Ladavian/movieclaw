"""进度判定规则——通行的 scrobble 语义（设计文档第 7 节，v1.1 按源码修正版）。

对齐 Jellyfin UserDataManager.cs:449-473 的三分支（阈值 5% / 90% / 300 秒）：
- 进度 < 5% → 位置清零、已看状态**不变**（视为未开始）；
- 进度 > 90%（或距结尾不足 1 秒）→ 位置清零、标记已看；
- 5%~90% 之间：时长 < 300 秒的短片直接算看完；否则记录续播位置；
- 无时长数据 → 直接标记已看（无从判定，宁可标完）。

play_count 与 last_played_at 在**开始播放**时更新（SessionManager.cs:845），
不在结束时——这也是"只发 Progress 不发 Stopped 的客户端（强杀 App）
不会漏计"的原因。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from movieclaw_db.models.base import utcnow

MIN_RESUME_PCT = 5.0
MAX_RESUME_PCT = 90.0
MIN_RESUME_DURATION_MS = 300 * 1000


@dataclass(frozen=True)
class ProgressOutcome:
    """一次进度上报应落库的结果。"""

    position_ms: int
    played: bool


def resolve_progress(
    position_ms: int, runtime_ms: int | None, *, currently_played: bool
) -> ProgressOutcome:
    """按阈值三分支把"播到哪了"折算成落库状态。

    Progress 与 Stopped 都走本函数——播过 90% 后强杀 App（只发过 Progress）
    的场景同样要被标已看。
    """
    if position_ms <= 0:
        # 无位置信息的上报（如刚开始播放）：不动任何状态
        return ProgressOutcome(position_ms=0, played=currently_played)
    if not runtime_ms or runtime_ms <= 0:
        return ProgressOutcome(position_ms=0, played=True)

    pct = position_ms / runtime_ms * 100
    if pct < MIN_RESUME_PCT:
        return ProgressOutcome(position_ms=0, played=currently_played)
    if pct > MAX_RESUME_PCT or position_ms >= runtime_ms - 1000:
        return ProgressOutcome(position_ms=0, played=True)
    if runtime_ms < MIN_RESUME_DURATION_MS:
        # 短片没有"续播"的意义：过了开头就算看完
        return ProgressOutcome(position_ms=0, played=True)
    return ProgressOutcome(position_ms=position_ms, played=currently_played)


def resolve_mark_played(
    *, play_count: int, date_played: datetime | None, last_played_at: datetime | None
) -> tuple[int, datetime]:
    """显式"标记已看"的计数语义（BaseItem.MarkPlayed）：

    传了 date_played 才 +1；否则只保证至少为 1（幂等标已看不刷次数）。
    返回 (新 play_count, 新 last_played_at)。
    """
    if date_played is not None:
        return play_count + 1, date_played
    return max(play_count, 1), last_played_at or utcnow()
