"""媒体身份层的纯数据获取与收敛逻辑（无数据库依赖）。

本模块是订阅功能"媒体条目"的 TMDB 侧实现（docs/design/subscription.md 第 1 节）：

- ``fetch_media_profile``：一次拉齐条目的身份信息（外部 ID、标题、别名集合、
  季集结构），产出与持久层解耦的 ``MediaProfile``；
- ``resolve_douban_to_tmdb``：豆瓣入口的收敛兜底通路（标题+年份搜索），
  命中 / 歧义 / 未找到三分支。

职责边界：本包不依赖 movieclaw_db，落库编排由 API 层的
``movieclaw_api.services.media_library`` 完成。
"""

from __future__ import annotations

import asyncio
import math
from datetime import date
from enum import StrEnum

from pydantic import BaseModel, Field

from movieclaw_media.models import MediaKind
from movieclaw_media.tmdb import TmdbClient

# 别名收集范围：中文圈 + 英语圈的地区别名，加 zh/en 两种语言的译名。
# 种子命名以英文为主、副标题以中文为主，这两组覆盖了匹配内核的需要。
_ALIAS_REGIONS = frozenset({"CN", "HK", "TW", "SG", "US", "GB"})
_ALIAS_LANGUAGES = frozenset({"zh", "en"})

# 歧义时返回给前端确认弹层的候选数量上限
_MAX_CANDIDATES = 8


class EpisodeInfo(BaseModel):
    """单集信息（订阅骨架 + 展示元数据，media_episode 表的传输形态）。

    air_date 保持 ISO 字符串形态（TMDB 原样），落库时再解析。
    """

    episode_number: int
    name: str = ""
    air_date: str | None = Field(default=None, description="ISO 日期字符串；None=未定档")
    overview: str | None = None
    runtime_minutes: int | None = None
    vote_average: float | None = None
    still_path: str | None = Field(default=None, description="TMDB 剧照相对路径")


class SeasonProfile(BaseModel):
    """一季的骨架与展示信息：季级订阅、wanted 生成与详情页所需。"""

    season_number: int
    name: str = ""
    air_date: date | None = None
    episode_count: int | None = None
    overview: str | None = None
    poster_path: str | None = None
    episodes: list[EpisodeInfo] = Field(default_factory=list)


class CastMember(BaseModel):
    """演员表一行（media_metadata.cast JSON 元素的传输形态）。"""

    name: str
    character: str | None = None
    order: int = 0
    profile_path: str | None = Field(default=None, description="TMDB 头像相对路径")
    # 姓名不是身份（同名不同人 / 一人多译名），人物页的链接与 person 表的匹配
    # 一律走这个 id。老数据的 JSON 里没有这个字段，读取方按缺失处理
    tmdb_person_id: int | None = Field(default=None, description="TMDB 影人 ID")


class PersonCredit(BaseModel):
    """一条参演/执导关系（喂 person 与 media_item_person 两张表）。

    与 ``CastMember`` 并存而不是复用：那个是「该影片档案里的演员表」（进
    media_metadata 的 JSON、写 NFO），这个是「人与影片的关系」（进关系表），
    两者字段需求不同——前者不需要 department，后者不需要为缺 id 的人留位置
    （没有 person id 就没法建关系，直接跳过）。
    """

    tmdb_person_id: int
    name: str
    original_name: str | None = None
    profile_path: str | None = None
    department: str = Field(description="cast=演员 / director=导演（剧集为主创）")
    character: str | None = None
    credit_order: int = 0


class MediaProfile(BaseModel):
    """条目档案的传输模型：TMDB 原始数据 → 持久层字段的中间形态。

    身份字段供 media_item（匹配最小闭包），展示字段供 media_metadata
    （自足媒体库的作品档案，docs/design/metadata.md）；同一次请求拉齐。
    """

    kind: MediaKind
    tmdb_id: int
    imdb_id: str | None = None
    title: str
    original_title: str
    year: int | None = None
    aliases: list[str] = Field(default_factory=list)
    status: str | None = None
    poster_path: str | None = None
    backdrop_path: str | None = None
    seasons: list[SeasonProfile] = Field(default_factory=list, description="仅剧集非空")

    # -- 展示层（media_metadata）--------------------------------------------
    overview: str | None = None
    tagline: str | None = None
    genres: list[str] = Field(default_factory=list)
    # genre 的 TMDB ID（与 genres 同源同序）：genres 是刮削语言的本地化名，
    # 媒体库路由的匹配必须用语言无关的 ID（docs/design/library-routing.md）
    genre_ids: list[int] = Field(default_factory=list)
    runtime_minutes: int | None = None
    release_date: date | None = None
    content_rating: str | None = None
    original_language: str | None = None
    origin_countries: list[str] = Field(default_factory=list)
    studios: list[str] = Field(default_factory=list)
    vote_average: float | None = None
    vote_count: int | None = None
    directors: list[str] = Field(default_factory=list)
    cast: list[CastMember] = Field(default_factory=list)
    # 人物页的数据来源。与上面两个字段同源但独立呈现：那两个是展示用的
    # 姓名/演员表（进 media_metadata），这个是带 TMDB person id 的关系集合
    people: list[PersonCredit] = Field(default_factory=list)


def extract_genre_ids(data: dict) -> list[int]:
    """TMDB 详情 → genre ID 列表。

    刮削（fetch_media_profile）与路由的轻量详情兜底（library_routing）
    共用本函数——两处口径必须一致，各写各的迟早漂移。
    """
    return [g["id"] for g in data.get("genres", []) if g.get("id") is not None]


def extract_origin_countries(data: dict) -> list[str]:
    """TMDB 详情 → 制片国家/地区码：剧集用 origin_country，电影回落
    production_countries（口径与 extract_genre_ids 同理，两处消费共用）。"""
    return [c for c in data.get("origin_country", []) if c] or [
        c.get("iso_3166_1") for c in data.get("production_countries", []) if c.get("iso_3166_1")
    ]


async def fetch_media_profile(
    client: TmdbClient,
    kind: MediaKind,
    tmdb_id: int,
    *,
    language: str = "zh-CN",
) -> MediaProfile:
    """拉取条目的完整档案（一次详情请求 + 剧集逐季并发拉集列表）。

    append_to_response 把别名/译名/外部 ID/演职员/分级合并进详情请求，
    整个电影建档只需一次往返；剧集另按季数并发拉集列表（受客户端漏桶
    限流约束）。冷门条目当前语言简介缺失时，补拉一次英文兜底（仅条目级，
    控制请求量——docs/design/metadata.md 第 3 节）。
    """
    rating_append = "release_dates" if kind is MediaKind.MOVIE else "content_ratings"
    data = await client.get(
        f"{kind.value}/{tmdb_id}",
        {
            "language": language,
            "append_to_response": (
                f"alternative_titles,translations,external_ids,credits,images,{rating_append}"
            ),
            # images 默认只回当前语言的图，几乎必空；显式带上"无语言"(null，
            # 即无文字烧录的干净图)与中英文，选图策略才有候选可挑
            "include_image_language": f"null,{language.split('-')[0]},en",
        },
    )

    title = data.get("title") or data.get("name") or ""
    original_title = data.get("original_title") or data.get("original_name") or title
    release_date = data.get("release_date") or data.get("first_air_date") or ""

    seasons: list[SeasonProfile] = []
    if kind is MediaKind.TV:
        numbers = [
            s["season_number"]
            for s in data.get("seasons", [])
            if s.get("season_number") is not None
        ]
        seasons = list(
            await asyncio.gather(
                *(_fetch_season(client, tmdb_id, n, language) for n in numbers)
            )
        )

    overview = (data.get("overview") or "").strip() or None
    tagline = (data.get("tagline") or "").strip() or None
    if overview is None and not language.lower().startswith("en"):
        overview, tagline = await _fetch_english_text(client, kind, tmdb_id, tagline)

    return MediaProfile(
        kind=kind,
        tmdb_id=tmdb_id,
        imdb_id=(data.get("external_ids") or {}).get("imdb_id") or None,
        title=title,
        original_title=original_title,
        year=_parse_year(release_date),
        aliases=_build_aliases(data, title, original_title),
        status=data.get("status") or None,
        poster_path=pick_poster(data, language) or data.get("poster_path"),
        backdrop_path=pick_backdrop(data) or data.get("backdrop_path"),
        seasons=seasons,
        overview=overview,
        tagline=tagline,
        genres=[g["name"] for g in data.get("genres", []) if g.get("name")],
        genre_ids=extract_genre_ids(data),
        runtime_minutes=_parse_runtime(kind, data),
        release_date=_parse_date(release_date),
        content_rating=_parse_certification(kind, data),
        original_language=data.get("original_language") or None,
        origin_countries=extract_origin_countries(data),
        studios=_parse_studios(kind, data),
        vote_average=round(float(data["vote_average"]), 1) if data.get("vote_average") else None,
        vote_count=data.get("vote_count") or None,
        directors=_parse_directors(kind, data),
        cast=_parse_cast(data),
        people=_parse_people(kind, data),
    )


# ---------------------------------------------------------------------------
# 选图策略（docs/design/metadata.md 6.3）
# ---------------------------------------------------------------------------
#
# TMDB 详情里的 backdrop_path/poster_path 是"按投票排序的第一张"，直接用有
# 两个通病：① 票王常是**烧了片名文字的横图**（海报化背景），铺全屏做沉浸
# 背景很脏；② 少量投票就能把一张低分辨率图顶上去。这里从 images 全量候选
# 里按明确规则重选，规则本身是纯函数，便于单测与后续调参。

# 背景图的分辨率门槛：低于 1080p 宽度的图铺满视口会糊
_MIN_BACKDROP_WIDTH = 1920
# 海报的分辨率门槛（w500 展示位，500 宽以下的源图放大会糊）
_MIN_POSTER_WIDTH = 500


def _weighted_score(image: dict) -> float:
    """加权评分：均分 × log(票数+1)。

    防"1 票 10 分"的冷门图冒顶——纯按 vote_average 排序时，只有个位数
    投票的图会盖过几百票的公认好图（TMDB 自己的默认排序即有此病）。
    """
    average = float(image.get("vote_average") or 0.0)
    count = int(image.get("vote_count") or 0)
    return average * math.log(count + 1)


def _sorted_candidates(images: list[dict], min_width: int) -> list[dict]:
    """达到分辨率门槛的候选按加权分降序；门槛过滤掉全部时不设门槛重来
    （宁可给一张小图，也不要没有图）。"""
    pool = [i for i in images if int(i.get("width") or 0) >= min_width] or list(images)
    return sorted(pool, key=_weighted_score, reverse=True)


def pick_backdrop(data: dict) -> str | None:
    """从候选里挑一张做沉浸背景：**无文字优先**，其次分辨率与加权票数。

    ``iso_639_1 is None`` 即"无语言"——TMDB 用它标记没有烧录任何文字的
    干净图，正是背景该用的那种；全是带字图时退回加权分最高的一张。
    没有任何候选返回 None（调用方回落 TMDB 默认 backdrop_path）。
    """
    images = (data.get("images") or {}).get("backdrops") or []
    if not images:
        return None
    textless = [i for i in images if i.get("iso_639_1") is None]
    ranked = _sorted_candidates(textless or images, _MIN_BACKDROP_WIDTH)
    return ranked[0].get("file_path") if ranked else None


def pick_poster(data: dict, language: str) -> str | None:
    """从候选里挑一张海报：**本地化语言优先**，其次分辨率与加权票数。

    与背景相反，海报**要**文字——中文版海报（含中文片名）比英文原版更
    符合中文用户的预期。当前语言没有海报时退回全部候选。
    """
    images = (data.get("images") or {}).get("posters") or []
    if not images:
        return None
    lang = language.split("-")[0]
    localized = [i for i in images if i.get("iso_639_1") == lang]
    ranked = _sorted_candidates(localized or images, _MIN_POSTER_WIDTH)
    return ranked[0].get("file_path") if ranked else None


def list_image_candidates(data: dict, language: str) -> tuple[list[dict], list[dict]]:
    """条目的全部候选图 (海报, 背景)，按各自策略的排序返回。

    供"更换图片"弹层展示——排序与自动选图一致，所以列表第一张就是
    自动策略会选的那张，用户一眼看出"默认给的是哪张"。
    """
    images = data.get("images") or {}
    posters = images.get("posters") or []
    backdrops = images.get("backdrops") or []
    lang = language.split("-")[0]
    localized = [i for i in posters if i.get("iso_639_1") == lang]
    poster_ranked = _sorted_candidates(localized or posters, _MIN_POSTER_WIDTH) if posters else []
    # 海报：本地化优先在前，其余按分排在后（弹层里仍可选到英文原版）
    rest = [i for i in posters if i not in poster_ranked]
    poster_list = [*poster_ranked, *_sorted_candidates(rest, _MIN_POSTER_WIDTH)]
    textless = [i for i in backdrops if i.get("iso_639_1") is None]
    backdrop_ranked = _sorted_candidates(textless, _MIN_BACKDROP_WIDTH) if textless else []
    with_text = [i for i in backdrops if i.get("iso_639_1") is not None]
    backdrop_list = [*backdrop_ranked, *_sorted_candidates(with_text, _MIN_BACKDROP_WIDTH)]
    return poster_list, backdrop_list


async def _fetch_english_text(
    client: TmdbClient, kind: MediaKind, tmdb_id: int, tagline: str | None
) -> tuple[str | None, str | None]:
    """当前语言无简介时的英文兜底（一次轻量详情请求，失败静默保持 None）。"""
    try:
        data = await client.get(f"{kind.value}/{tmdb_id}", {"language": "en-US"})
    except Exception:  # noqa: BLE001 -- 兜底文案拉不到不影响档案主体
        return None, tagline
    return (
        (data.get("overview") or "").strip() or None,
        tagline or (data.get("tagline") or "").strip() or None,
    )


def _parse_runtime(kind: MediaKind, data: dict) -> int | None:
    if kind is MediaKind.MOVIE:
        runtime = data.get("runtime")
    else:
        run_times = data.get("episode_run_time") or []
        runtime = run_times[0] if run_times else None
    return int(runtime) if runtime else None


def _parse_certification(kind: MediaKind, data: dict) -> str | None:
    """分级：优先 CN，无则 US，再无取第一个非空（电影与剧集接口结构不同）。"""
    entries: list[tuple[str, str]] = []
    if kind is MediaKind.MOVIE:
        for country in (data.get("release_dates") or {}).get("results", []):
            for release in country.get("release_dates", []):
                cert = (release.get("certification") or "").strip()
                if cert:
                    entries.append((country.get("iso_3166_1") or "", cert))
                    break
    else:
        for country in (data.get("content_ratings") or {}).get("results", []):
            cert = (country.get("rating") or "").strip()
            if cert:
                entries.append((country.get("iso_3166_1") or "", cert))
    for region in ("CN", "US"):
        for iso, cert in entries:
            if iso == region:
                return cert
    return entries[0][1] if entries else None


def _parse_studios(kind: MediaKind, data: dict) -> list[str]:
    source = data.get("production_companies") if kind is MediaKind.MOVIE else data.get("networks")
    return [s["name"] for s in source or [] if s.get("name")][:5]


def _parse_directors(kind: MediaKind, data: dict) -> list[str]:
    if kind is MediaKind.MOVIE:
        crew = (data.get("credits") or {}).get("crew", [])
        names = [c["name"] for c in crew if c.get("job") == "Director" and c.get("name")]
    else:
        names = [c["name"] for c in data.get("created_by", []) if c.get("name")]
    return names[:5]


# 演员数量上限：详情页与 NFO 展示前 40 位足够（与 library_nfo._MAX_ACTORS 一致）
_MAX_CAST = 40


def _parse_cast(data: dict) -> list[CastMember]:
    return [
        CastMember(
            name=c["name"],
            character=(c.get("character") or "").strip() or None,
            order=c.get("order") or 0,
            profile_path=c.get("profile_path"),
            tmdb_person_id=c.get("id"),
        )
        for c in (data.get("credits") or {}).get("cast", [])[:_MAX_CAST]
        if c.get("name")
    ]


def _parse_people(kind: MediaKind, data: dict) -> list[PersonCredit]:
    """credits → 参演/执导关系集合（只收 cast 与导演/主创两类，见 MediaItemPerson）。

    没有 TMDB person id 的条目直接跳过：关系表以 id 为身份，拿姓名硬凑只会
    把同名不同人合并到一起。一人分饰两角时把角色名合并成「A / B」，
    与关系表 (media_item_id, person_id, department) 的唯一键对齐。
    """
    credits = data.get("credits") or {}
    people: dict[tuple[int, str], PersonCredit] = {}

    def add(raw: dict, department: str, *, character: str | None, order: int) -> None:
        person_id = raw.get("id")
        name = (raw.get("name") or "").strip()
        if not isinstance(person_id, int) or not name:
            return
        key = (person_id, department)
        existing = people.get(key)
        if existing is None:
            people[key] = PersonCredit(
                tmdb_person_id=person_id,
                name=name,
                original_name=(raw.get("original_name") or "").strip() or None,
                profile_path=raw.get("profile_path"),
                department=department,
                character=character,
                credit_order=order,
            )
            return
        # 同一个人同一身份出现第二次：合并角色名，顺序取更靠前的那个
        if character and character not in (existing.character or ""):
            existing.character = (
                f"{existing.character} / {character}" if existing.character else character
            )
        existing.credit_order = min(existing.credit_order, order)

    for c in credits.get("cast", [])[:_MAX_CAST]:
        add(
            c,
            "cast",
            character=(c.get("character") or "").strip() or None,
            order=c.get("order") or 0,
        )

    if kind is MediaKind.MOVIE:
        crew = [c for c in credits.get("crew", []) if c.get("job") == "Director"]
    else:
        crew = list(data.get("created_by") or [])
    for i, c in enumerate(crew[:5]):
        add(c, "director", character=None, order=i)

    return list(people.values())


async def _fetch_season(
    client: TmdbClient, tmdb_id: int, season_number: int, language: str
) -> SeasonProfile:
    data = await client.get(f"tv/{tmdb_id}/season/{season_number}", {"language": language})
    episodes = [
        EpisodeInfo(
            episode_number=e["episode_number"],
            name=e.get("name") or "",
            air_date=e.get("air_date") or None,
            overview=(e.get("overview") or "").strip() or None,
            runtime_minutes=int(e["runtime"]) if e.get("runtime") else None,
            vote_average=round(float(e["vote_average"]), 1) if e.get("vote_average") else None,
            still_path=e.get("still_path"),
        )
        for e in data.get("episodes", [])
        if e.get("episode_number") is not None
    ]
    return SeasonProfile(
        season_number=season_number,
        name=data.get("name") or "",
        air_date=_parse_date(data.get("air_date") or ""),
        # 详情季对象可能带 episode_count；集列表已拉到时以实际长度为准
        episode_count=len(episodes) or data.get("episode_count"),
        overview=(data.get("overview") or "").strip() or None,
        poster_path=data.get("poster_path"),
        episodes=episodes,
    )


def _build_aliases(data: dict, title: str, original_title: str) -> list[str]:
    """构建别名集合：主标题 + 原名 + 地区别名 + zh/en 译名，保序精确去重。

    存原样文本——归一化（大小写/全半角/繁简）是匹配内核的职责，
    规则进化时数据无需重写。
    """
    collected: list[str] = [title, original_title]

    alt = data.get("alternative_titles") or {}
    # TMDB 的接口差异：电影用 "titles" 键，剧集用 "results" 键
    for entry in alt.get("titles") or alt.get("results") or []:
        if entry.get("iso_3166_1") in _ALIAS_REGIONS:
            collected.append(entry.get("title") or "")

    for trans in (data.get("translations") or {}).get("translations") or []:
        if trans.get("iso_639_1") in _ALIAS_LANGUAGES:
            payload = trans.get("data") or {}
            collected.append(payload.get("title") or payload.get("name") or "")

    seen: set[str] = set()
    aliases: list[str] = []
    for text in collected:
        cleaned = text.strip()
        if cleaned and cleaned not in seen:
            seen.add(cleaned)
            aliases.append(cleaned)
    return aliases


def _parse_year(iso_date: str) -> int | None:
    if len(iso_date) >= 4 and iso_date[:4].isdigit():
        return int(iso_date[:4])
    return None


def _parse_date(iso_date: str) -> date | None:
    try:
        return date.fromisoformat(iso_date)
    except ValueError:
        return None


# ---------------------------------------------------------------------------
# 豆瓣入口收敛：标题+年份搜索兜底（设计稿 1.5 的②通路）
# ---------------------------------------------------------------------------


class ResolveStatus(StrEnum):
    """收敛结果三分支。"""

    MATCHED = "matched"  # 唯一命中，可直接建档
    AMBIGUOUS = "ambiguous"  # 多候选，需用户在弹层确认一次
    NOT_FOUND = "not_found"  # TMDB 未收录，不建无锚条目


class ResolveCandidate(BaseModel):
    """返回给确认弹层的候选条目。"""

    tmdb_id: int
    title: str
    original_title: str
    year: int | None = None
    poster_path: str | None = None


class DoubanResolution(BaseModel):
    """豆瓣→TMDB 收敛结果。matched 时 tmdb_id 非空；ambiguous 时 candidates 非空。"""

    status: ResolveStatus
    tmdb_id: int | None = None
    candidates: list[ResolveCandidate] = Field(default_factory=list)


async def resolve_douban_to_tmdb(
    client: TmdbClient,
    kind: MediaKind,
    title: str,
    *,
    year: int | None = None,
    language: str = "zh-CN",
) -> DoubanResolution:
    """按标题+年份把豆瓣条目收敛到 TMDB 锚。

    判定规则（保守优先，绝不静默错配）：
    1. 年份过滤（容差 ±1，豆瓣与 TMDB 偶有跨年差异）后唯一 → 命中；
    2. 过滤后多个，但"标题精确相等且年份精确相等"者唯一 → 命中；
    3. 其余 → 歧义，返回候选让用户确认；搜索结果为空 → 未找到。
    """
    data = await client.get(f"search/{kind.value}", {"query": title, "language": language})
    candidates = [_to_candidate(raw) for raw in data.get("results", [])]
    candidates = [c for c in candidates if c is not None]
    if not candidates:
        return DoubanResolution(status=ResolveStatus.NOT_FOUND)

    pool = candidates
    if year is not None:
        filtered = [c for c in candidates if c.year is not None and abs(c.year - year) <= 1]
        # 年份全部对不上时退回全量候选——豆瓣年份可能就是错的，交给用户判断
        pool = filtered or candidates

    if len(pool) == 1:
        return DoubanResolution(status=ResolveStatus.MATCHED, tmdb_id=pool[0].tmdb_id)

    if year is not None:
        wanted = _loose(title)
        exact = [
            c
            for c in pool
            if c.year == year and wanted in (_loose(c.title), _loose(c.original_title))
        ]
        if len(exact) == 1:
            return DoubanResolution(status=ResolveStatus.MATCHED, tmdb_id=exact[0].tmdb_id)

    return DoubanResolution(status=ResolveStatus.AMBIGUOUS, candidates=pool[:_MAX_CANDIDATES])


def _to_candidate(raw: dict) -> ResolveCandidate | None:
    tmdb_id = raw.get("id")
    title = raw.get("title") or raw.get("name") or ""
    if not tmdb_id or not title:
        return None
    return ResolveCandidate(
        tmdb_id=tmdb_id,
        title=title,
        original_title=raw.get("original_title") or raw.get("original_name") or title,
        year=_parse_year(raw.get("release_date") or raw.get("first_air_date") or ""),
        poster_path=raw.get("poster_path"),
    )


def _loose(text: str) -> str:
    """仅用于"精确相等"判定的宽松形态：忽略大小写与空白差异。"""
    return "".join(text.split()).casefold()
