"""媒体身份层纯函数的单元测试：档案拉取、别名构建、豆瓣收敛三分支（MockTransport，不出网）。"""

from __future__ import annotations

import httpx

from movieclaw_media.library import (
    ResolveStatus,
    fetch_media_profile,
    list_image_candidates,
    pick_backdrop,
    pick_poster,
    resolve_douban_to_tmdb,
)
from movieclaw_media.models import MediaKind
from movieclaw_media.tmdb import TmdbClient

_KEY = "0123456789abcdef0123456789abcdef"


def _client(routes: dict[str, dict], captured: list[httpx.Request] | None = None) -> TmdbClient:
    """按 URL path 路由返回固定 JSON 的假 TMDB。未注册的 path 一律 404。"""

    def handler(request: httpx.Request) -> httpx.Response:
        if captured is not None:
            captured.append(request)
        payload = routes.get(request.url.path)
        if payload is None:
            return httpx.Response(404, json={})
        return httpx.Response(200, json=payload)

    return TmdbClient(_KEY, transport=httpx.MockTransport(handler))


_MOVIE_DETAIL = {
    "id": 693134,
    "title": "沙丘2",
    "original_title": "Dune: Part Two",
    "release_date": "2024-02-27",
    "status": "Released",
    "poster_path": "/poster.jpg",
    "backdrop_path": "/backdrop.jpg",
    # 展示层（media_metadata）字段：有中文简介时不触发英文兜底的第二次请求
    "overview": "保罗·厄崔迪与弗雷曼人汇合，踏上复仇之路。",
    "tagline": "Long live the fighters.",
    "runtime": 167,
    "vote_average": 8.16,
    "vote_count": 5000,
    "original_language": "en",
    "genres": [{"id": 878, "name": "科幻"}, {"id": 12, "name": "冒险"}],
    "production_companies": [{"name": "Legendary Pictures"}],
    "production_countries": [{"iso_3166_1": "US"}],
    "release_dates": {
        "results": [
            {"iso_3166_1": "US", "release_dates": [{"certification": "PG-13"}]},
        ]
    },
    "credits": {
        "crew": [{"name": "Denis Villeneuve", "job": "Director"}],
        "cast": [
            {
                "name": "Timothée Chalamet",
                "character": "Paul",
                "order": 0,
                "profile_path": "/tc.jpg",
            },
            {"name": "Zendaya", "character": "Chani", "order": 1, "profile_path": None},
        ],
    },
    "external_ids": {"imdb_id": "tt15239678"},
    "alternative_titles": {
        "titles": [
            {"iso_3166_1": "CN", "title": "沙丘：第二部"},
            {"iso_3166_1": "US", "title": "Dune Part 2"},
            {"iso_3166_1": "FR", "title": "Dune Deuxième Partie"},  # 不在收集范围
            {"iso_3166_1": "HK", "title": "沙丘瀚战：第二章"},
        ]
    },
    "translations": {
        "translations": [
            {"iso_639_1": "zh", "data": {"title": "沙丘2"}},  # 与主标题重复，应去重
            {"iso_639_1": "en", "data": {"title": "Dune: Part Two"}},  # 与原名重复
            {"iso_639_1": "ja", "data": {"title": "デューン 砂の惑星PART2"}},  # 不收集
        ]
    },
}


async def test_fetch_movie_profile_fields_and_aliases() -> None:
    """电影档案：字段齐全；别名=主标题+原名+指定地区/语言，跨来源精确去重。"""
    client = _client({"/3/movie/693134": _MOVIE_DETAIL})
    profile = await fetch_media_profile(client, MediaKind.MOVIE, 693134)

    assert profile.imdb_id == "tt15239678"
    assert profile.title == "沙丘2"
    assert profile.original_title == "Dune: Part Two"
    assert profile.year == 2024
    assert profile.status == "Released"
    assert profile.poster_path == "/poster.jpg"
    assert profile.seasons == []
    assert profile.aliases == [
        "沙丘2",
        "Dune: Part Two",
        "沙丘：第二部",
        "Dune Part 2",
        "沙丘瀚战：第二章",
    ]
    # 展示层字段（media_metadata 的数据源）随同一次请求拉齐
    assert profile.overview == "保罗·厄崔迪与弗雷曼人汇合，踏上复仇之路。"
    assert profile.tagline == "Long live the fighters."
    assert profile.genres == ["科幻", "冒险"]
    assert profile.runtime_minutes == 167
    assert profile.content_rating == "PG-13"
    assert profile.vote_average == 8.2
    assert profile.studios == ["Legendary Pictures"]
    assert profile.origin_countries == ["US"]
    assert profile.directors == ["Denis Villeneuve"]
    assert [c.name for c in profile.cast] == ["Timothée Chalamet", "Zendaya"]
    assert profile.cast[0].character == "Paul"


async def test_fetch_movie_uses_append_to_response() -> None:
    """整个电影建档只发一次请求：别名/译名/外部 ID/演职员/候选图/分级全走
    append_to_response 合并（有中文简介时不触发英文兜底）。"""
    captured: list[httpx.Request] = []
    client = _client({"/3/movie/693134": _MOVIE_DETAIL}, captured)
    await fetch_media_profile(client, MediaKind.MOVIE, 693134)

    assert len(captured) == 1
    params = dict(captured[0].url.params)
    assert params["append_to_response"] == (
        "alternative_titles,translations,external_ids,credits,images,release_dates"
    )
    assert params["language"] == "zh-CN"
    # 选图策略要的是"无文字"(null) 与中英文候选，不带这个参数 images 几乎必空
    assert params["include_image_language"] == "null,zh,en"


async def test_fetch_movie_english_overview_fallback() -> None:
    """中文简介缺失时补拉一次英文兜底（仅条目级，docs/design/metadata.md 第 3 节）。"""
    detail = {**_MOVIE_DETAIL, "overview": "", "tagline": ""}
    captured: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request)
        if dict(request.url.params).get("language") == "en-US":
            return httpx.Response(
                200, json={**detail, "overview": "Paul seeks revenge.", "tagline": "EN tagline"}
            )
        return httpx.Response(200, json=detail)

    client = TmdbClient(_KEY, transport=httpx.MockTransport(handler))
    profile = await fetch_media_profile(client, MediaKind.MOVIE, 693134)
    assert len(captured) == 2
    assert profile.overview == "Paul seeks revenge."


_TV_DETAIL = {
    "id": 94997,
    "name": "龙之家族",
    "original_name": "House of the Dragon",
    "first_air_date": "2022-08-21",
    "status": "Returning Series",
    "poster_path": "/tv.jpg",
    "backdrop_path": None,
    "external_ids": {"imdb_id": "tt11198330"},
    # 剧集的 alternative_titles 用 "results" 键（TMDB 接口差异）
    "alternative_titles": {"results": [{"iso_3166_1": "TW", "title": "龍族前傳"}]},
    "translations": {"translations": []},
    "seasons": [
        {"season_number": 0},
        {"season_number": 1},
        {"season_number": 2},
    ],
}

_SEASONS = {
    "/3/tv/94997/season/0": {"name": "特别篇", "air_date": None, "episodes": []},
    "/3/tv/94997/season/1": {
        "name": "第 1 季",
        "air_date": "2022-08-21",
        "episodes": [
            {"episode_number": 1, "name": "龙之继承人", "air_date": "2022-08-21"},
            {"episode_number": 2, "name": "反叛的王子", "air_date": "2022-08-28"},
        ],
    },
    "/3/tv/94997/season/2": {
        "name": "第 2 季",
        "air_date": "2024-06-16",
        "episodes": [
            {"episode_number": 1, "name": "黑色之子", "air_date": "2024-06-16"},
            {"episode_number": 2, "name": None, "air_date": None},  # 未定档集
        ],
    },
}


async def test_fetch_tv_profile_with_seasons_and_episodes() -> None:
    """剧集档案：季按季号齐全（含特别季 0），集列表带播出日期，tv 别名键兼容。"""
    client = _client({"/3/tv/94997": _TV_DETAIL, **_SEASONS})
    profile = await fetch_media_profile(client, MediaKind.TV, 94997)

    assert profile.title == "龙之家族"
    assert profile.year == 2022
    assert "龍族前傳" in profile.aliases
    assert [s.season_number for s in profile.seasons] == [0, 1, 2]

    season1 = profile.seasons[1]
    assert season1.episode_count == 2
    assert season1.episodes[0].air_date == "2022-08-21"
    # 未定档集：air_date 为 None 而非假日期
    assert profile.seasons[2].episodes[1].air_date is None
    assert profile.seasons[2].episodes[1].name == ""


# ---------------------------------------------------------------------------
# 选图策略（docs/design/metadata.md 6.3）
# ---------------------------------------------------------------------------


def _img(path: str, *, lang: str | None, width: int, avg: float, count: int) -> dict:
    return {
        "file_path": path,
        "iso_639_1": lang,
        "width": width,
        "height": int(width * 9 / 16),
        "vote_average": avg,
        "vote_count": count,
    }


def test_pick_backdrop_prefers_textless() -> None:
    """背景首选无文字图：带片名文字的横图铺全屏很脏，哪怕它票数更高。"""
    data = {
        "images": {
            "backdrops": [
                _img("/with-text.jpg", lang="en", width=3840, avg=9.0, count=500),
                _img("/clean.jpg", lang=None, width=1920, avg=6.0, count=40),
            ]
        }
    }
    assert pick_backdrop(data) == "/clean.jpg"


def test_pick_backdrop_weighted_score_beats_low_vote_outlier() -> None:
    """加权票数：1 票 10 分的冷门图不该盖过几百票的公认好图。"""
    data = {
        "images": {
            "backdrops": [
                _img("/outlier.jpg", lang=None, width=1920, avg=10.0, count=1),
                _img("/popular.jpg", lang=None, width=1920, avg=7.0, count=400),
            ]
        }
    }
    assert pick_backdrop(data) == "/popular.jpg"


def test_pick_backdrop_falls_back_when_all_below_width() -> None:
    """候选全都低于分辨率门槛时不放弃——宁可给小图也不要没有图。"""
    data = {"images": {"backdrops": [_img("/small.jpg", lang=None, width=1280, avg=8.0, count=9)]}}
    assert pick_backdrop(data) == "/small.jpg"


def test_pick_backdrop_none_without_candidates() -> None:
    """没有候选返回 None（调用方回落 TMDB 默认 backdrop_path）。"""
    assert pick_backdrop({"images": {"backdrops": []}}) is None
    assert pick_backdrop({}) is None


def test_pick_poster_prefers_localized() -> None:
    """海报相反：**要**文字，中文版比英文原版更符合中文用户预期。"""
    data = {
        "images": {
            "posters": [
                _img("/en.jpg", lang="en", width=2000, avg=9.5, count=900),
                _img("/zh.jpg", lang="zh", width=1000, avg=5.0, count=10),
            ]
        }
    }
    assert pick_poster(data, "zh-CN") == "/zh.jpg"
    # 当前语言没有海报时退回全部候选里的最优
    assert pick_poster(data, "ja-JP") == "/en.jpg"


def test_list_candidates_ordering_matches_auto_pick() -> None:
    """弹层首张 == 自动策略会选的那张（用户一眼看出默认给的是哪张），
    且带文字/非本地化的候选仍在列表里可选。"""
    data = {
        "images": {
            "posters": [
                _img("/p-en.jpg", lang="en", width=2000, avg=9.0, count=800),
                _img("/p-zh.jpg", lang="zh", width=1000, avg=6.0, count=30),
            ],
            "backdrops": [
                _img("/b-text.jpg", lang="en", width=3840, avg=9.5, count=700),
                _img("/b-clean.jpg", lang=None, width=1920, avg=6.5, count=50),
            ],
        }
    }
    posters, backdrops = list_image_candidates(data, "zh-CN")
    assert posters[0]["file_path"] == pick_poster(data, "zh-CN") == "/p-zh.jpg"
    assert backdrops[0]["file_path"] == pick_backdrop(data) == "/b-clean.jpg"
    assert {i["file_path"] for i in posters} == {"/p-zh.jpg", "/p-en.jpg"}
    assert {i["file_path"] for i in backdrops} == {"/b-clean.jpg", "/b-text.jpg"}


async def test_fetch_profile_poster_default_backdrop_picked() -> None:
    """海报以 TMDB 默认为准（与发现页看到的一致，订阅前后不跳变）；
    背景仍从候选里按无文字策略重选。"""
    detail = {
        **_MOVIE_DETAIL,
        "poster_path": "/default-poster.jpg",
        "backdrop_path": "/default-backdrop.jpg",
        "images": {
            "posters": [_img("/picked-poster.jpg", lang="zh", width=1000, avg=7.0, count=50)],
            "backdrops": [_img("/picked-backdrop.jpg", lang=None, width=1920, avg=7.0, count=50)],
        },
    }
    client = _client({"/3/movie/693134": detail})
    profile = await fetch_media_profile(client, MediaKind.MOVIE, 693134)
    assert profile.poster_path == "/default-poster.jpg"
    assert profile.backdrop_path == "/picked-backdrop.jpg"


async def test_fetch_profile_poster_falls_back_to_pick() -> None:
    """TMDB 默认海报缺失时才走选图策略从候选里兜底。"""
    detail = {
        **_MOVIE_DETAIL,
        "poster_path": None,
        "images": {
            "posters": [_img("/picked-poster.jpg", lang="zh", width=1000, avg=7.0, count=50)],
        },
    }
    client = _client({"/3/movie/693134": detail})
    profile = await fetch_media_profile(client, MediaKind.MOVIE, 693134)
    assert profile.poster_path == "/picked-poster.jpg"


async def test_fetch_profile_falls_back_to_default_images() -> None:
    """条目没有 images 候选时回落 TMDB 默认字段，不会把图丢成 None。"""
    client = _client({"/3/movie/693134": _MOVIE_DETAIL})
    profile = await fetch_media_profile(client, MediaKind.MOVIE, 693134)
    assert profile.poster_path == "/poster.jpg"
    assert profile.backdrop_path == "/backdrop.jpg"


# ---------------------------------------------------------------------------
# 豆瓣收敛三分支
# ---------------------------------------------------------------------------


def _search_result(*items: dict) -> dict:
    return {"results": list(items)}


def _movie(tmdb_id: int, title: str, original: str, year: int) -> dict:
    return {
        "id": tmdb_id,
        "title": title,
        "original_title": original,
        "release_date": f"{year}-01-01",
        "poster_path": "/p.jpg",
    }


async def test_resolve_matched_when_unique_after_year_filter() -> None:
    """年份过滤后唯一 → 直接命中，无需用户确认。"""
    client = _client(
        {
            "/3/search/movie": _search_result(
                _movie(1, "沙丘2", "Dune: Part Two", 2024),
                _movie(2, "沙丘", "Dune", 1984),
            )
        }
    )
    result = await resolve_douban_to_tmdb(client, MediaKind.MOVIE, "沙丘2", year=2024)
    assert result.status is ResolveStatus.MATCHED
    assert result.tmdb_id == 1


async def test_resolve_matched_by_exact_title_and_year_among_many() -> None:
    """过滤后仍多个，但标题+年份精确相等者唯一 → 命中。"""
    client = _client(
        {
            "/3/search/movie": _search_result(
                _movie(1, "小丑", "Joker", 2019),
                _movie(2, "小丑回魂", "It", 2019),
            )
        }
    )
    result = await resolve_douban_to_tmdb(client, MediaKind.MOVIE, "小丑", year=2019)
    assert result.status is ResolveStatus.MATCHED
    assert result.tmdb_id == 1


async def test_resolve_ambiguous_returns_candidates() -> None:
    """无法唯一判定 → 歧义，候选交给弹层确认，绝不静默错配。"""
    client = _client(
        {
            "/3/search/movie": _search_result(
                _movie(1, "机器人总动员", "WALL·E", 2008),
                _movie(2, "机器人总动员2", "WALL·E 2", 2008),
            )
        }
    )
    result = await resolve_douban_to_tmdb(client, MediaKind.MOVIE, "机器人", year=2008)
    assert result.status is ResolveStatus.AMBIGUOUS
    assert [c.tmdb_id for c in result.candidates] == [1, 2]


async def test_resolve_not_found() -> None:
    """TMDB 未收录 → not_found（上层据此拒绝创建无锚条目）。"""
    client = _client({"/3/search/movie": _search_result()})
    result = await resolve_douban_to_tmdb(client, MediaKind.MOVIE, "极冷门条目", year=2001)
    assert result.status is ResolveStatus.NOT_FOUND
    assert result.candidates == []


async def test_resolve_year_mismatch_falls_back_to_all_candidates() -> None:
    """年份全对不上时退回全量候选做歧义确认——豆瓣年份可能有误。"""
    client = _client(
        {
            "/3/search/movie": _search_result(
                _movie(1, "某片", "Film A", 2010),
                _movie(2, "某片", "Film B", 2015),
            )
        }
    )
    result = await resolve_douban_to_tmdb(client, MediaKind.MOVIE, "某片", year=1990)
    assert result.status is ResolveStatus.AMBIGUOUS
    assert len(result.candidates) == 2
