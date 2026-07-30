"""发现页数据编排：把 TMDB 的榜单接口转换为前端可渲染的行数据。

页面构成（Netflix 式）
--------------------
- hero：本周趋势榜里挑「有宽幅剧照且有中文简介」的前几名做大横幅轮播；
- rows：每行对应一个 TMDB 榜单/发现查询（行定义见 _movie_rows / _tv_rows）。

发现页按「布局 + 单行」两级提供：布局（行清单）是纯配置即时返回，前端据此
撑起骨架后逐行拉数据渐进填充；单行失败只影响那一行（该行接口报错，前端
收起该行），不拖垮整页——错误隔离口径与站点搜索一致。

缓存口径
--------
行/Hero 30 分钟、类型表 24 小时、详情 6 小时——都是全站共享的只读榜单数据，
无个性化成分，进程内 TTL 缓存即可（见 cache.py 的说明）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from movieclaw_cache import AsyncTTLCache
from movieclaw_media.models import (
    DiscoverLayout,
    DiscoverRowStub,
    MediaCard,
    MediaCastMember,
    MediaDetail,
    MediaFacts,
    MediaImage,
    MediaKind,
    MediaRow,
    MediaSearchItem,
    MediaSource,
)
from movieclaw_media.tmdb import TmdbClient, TmdbError

logger = logging.getLogger("movieclaw_media.service")

_PAGE_TTL = 30 * 60
_GENRE_TTL = 24 * 60 * 60
_DETAIL_TTL = 6 * 60 * 60
_SEARCH_TTL = 10 * 60
# Hero 轮播的精选数量
_HERO_COUNT = 6
# 一行少于这个数就不值得占一行位置（如所选地区暂无「正在热映」数据）
_MIN_ROW_ITEMS = 4
# 相似推荐最多取多少部
_RELATED_LIMIT = 10
# 详情页剧照/海报各取多少张（TMDB 已按社区评分排好序，取前 N 即最佳 N 张）
_IMAGES_LIMIT = 20
# 详情页演职员条最多取多少位。TMDB 的 cast 已按 order 字段（剧组给的主次顺序）
# 排好，取前 N 即主要演员；条目页是横滚条，比原先「词条信息里一行文字」能放得下更多
_CAST_LIMIT = 16

# TMDB 详情接口不随 language 参数翻译制片地区/语言字段，这里映射高频值，
# 映射不到的原样展示（英文/ISO 代码），不影响功能
_COUNTRY_NAMES = {
    "US": "美国", "CN": "中国大陆", "HK": "中国香港", "TW": "中国台湾",
    "JP": "日本", "KR": "韩国", "GB": "英国", "FR": "法国", "DE": "德国",
    "IT": "意大利", "ES": "西班牙", "CA": "加拿大", "AU": "澳大利亚",
    "IN": "印度", "TH": "泰国", "RU": "俄罗斯", "NZ": "新西兰",
    "IE": "爱尔兰", "DK": "丹麦", "SE": "瑞典", "NO": "挪威",
    "BE": "比利时", "NL": "荷兰", "BR": "巴西", "MX": "墨西哥",
}
_LANGUAGE_NAMES = {
    "en": "英语", "zh": "汉语普通话", "cn": "粤语", "ja": "日语", "ko": "韩语",
    "fr": "法语", "de": "德语", "es": "西班牙语", "it": "意大利语",
    "ru": "俄语", "pt": "葡萄牙语", "hi": "印地语", "th": "泰语",
    "sv": "瑞典语", "da": "丹麦语", "no": "挪威语", "nl": "荷兰语",
    "pl": "波兰语", "tr": "土耳其语",
}


@dataclass(frozen=True)
class _RowSpec:
    """一行榜单的声明式定义：行标识/中文标题/TMDB 端点/查询参数。"""

    row_id: str
    title: str
    path: str
    params: dict[str, Any] = field(default_factory=dict)
    ranked: bool = False
    limit: int = 20


def _movie_rows(region: str) -> tuple[_RowSpec, ...]:
    # 行序参考 Netflix 发现页编排：趋势排名行打头，院线时效内容随后，热门与
    # 口碑居中，地区行与类型行垫后。discover 查询统一带 vote_count 下限挡住
    # 冷门残缺条目；类型 ID（878 科幻、28 动作等）是 TMDB 官方文档的稳定常量。
    return (
        _RowSpec("trending-day", "今日热榜 Top 10", "trending/movie/day", ranked=True, limit=10),
        _RowSpec("now-playing", "正在热映", "movie/now_playing", {"region": region}),
        _RowSpec("upcoming", "即将上映", "movie/upcoming", {"region": region}),
        _RowSpec("popular", "热门电影", "movie/popular"),
        _RowSpec("top-rated", "高分经典", "movie/top_rated"),
        _RowSpec(
            "chinese", "华语佳片", "discover/movie",
            {"with_original_language": "zh", "sort_by": "popularity.desc", "vote_count.gte": 50},
        ),
        _RowSpec(
            "japanese", "日本电影", "discover/movie",
            {"with_origin_country": "JP", "sort_by": "popularity.desc", "vote_count.gte": 100},
        ),
        _RowSpec(
            "korean", "韩国电影", "discover/movie",
            {"with_origin_country": "KR", "sort_by": "popularity.desc", "vote_count.gte": 100},
        ),
        _RowSpec(
            "scifi", "科幻巨制", "discover/movie",
            {"with_genres": "878", "sort_by": "popularity.desc", "vote_count.gte": 300},
        ),
        # with_genres 里的竖线是「或」（逗号才是「且」）：28|12 = 动作或冒险
        _RowSpec(
            "action", "动作与冒险", "discover/movie",
            {"with_genres": "28|12", "sort_by": "popularity.desc", "vote_count.gte": 300},
        ),
        _RowSpec(
            "thriller", "悬疑惊悚", "discover/movie",
            {"with_genres": "53|9648", "sort_by": "popularity.desc", "vote_count.gte": 300},
        ),
        _RowSpec(
            "comedy", "喜剧佳作", "discover/movie",
            {"with_genres": "35", "sort_by": "popularity.desc", "vote_count.gte": 200},
        ),
        _RowSpec(
            "romance", "爱情电影", "discover/movie",
            {"with_genres": "10749", "sort_by": "popularity.desc", "vote_count.gte": 200},
        ),
        _RowSpec(
            "horror", "恐怖片", "discover/movie",
            {"with_genres": "27", "sort_by": "popularity.desc", "vote_count.gte": 200},
        ),
        _RowSpec(
            "animation", "动画电影", "discover/movie",
            {"with_genres": "16", "sort_by": "popularity.desc", "vote_count.gte": 200},
        ),
        _RowSpec(
            "documentary", "纪录片", "discover/movie",
            {"with_genres": "99", "sort_by": "popularity.desc", "vote_count.gte": 50},
        ),
    )


def _tv_rows() -> tuple[_RowSpec, ...]:
    # 剧集侧同理：趋势与在播内容在前，热门/口碑居中，然后是地区行与播出平台
    # 品牌行（Netflix/HBO 是发现页里辨识度最高的两块金字招牌），类型行垫后。
    return (
        _RowSpec("trending-day", "今日热榜 Top 10", "trending/tv/day", ranked=True, limit=10),
        _RowSpec("on-the-air", "正在播出", "tv/on_the_air"),
        _RowSpec("popular", "热门剧集", "tv/popular"),
        _RowSpec("top-rated", "高分神剧", "tv/top_rated"),
        _RowSpec(
            "chinese", "华语剧集", "discover/tv",
            {"with_original_language": "zh", "sort_by": "popularity.desc", "vote_count.gte": 20},
        ),
        _RowSpec(
            "us", "热门美剧", "discover/tv",
            {"with_origin_country": "US", "sort_by": "popularity.desc", "vote_count.gte": 50},
        ),
        _RowSpec(
            "japanese", "热门日剧", "discover/tv",
            {"with_origin_country": "JP", "sort_by": "popularity.desc", "vote_count.gte": 20},
        ),
        _RowSpec(
            "korean", "热门韩剧", "discover/tv",
            {"with_origin_country": "KR", "sort_by": "popularity.desc", "vote_count.gte": 20},
        ),
        # 播出平台行：with_networks 的 213=Netflix、49=HBO（TMDB network ID）
        _RowSpec(
            "netflix", "Netflix 出品", "discover/tv",
            {"with_networks": "213", "sort_by": "popularity.desc", "vote_count.gte": 50},
        ),
        _RowSpec(
            "hbo", "HBO 出品", "discover/tv",
            {"with_networks": "49", "sort_by": "popularity.desc", "vote_count.gte": 50},
        ),
        _RowSpec(
            "scifi-fantasy", "科幻与奇幻", "discover/tv",
            {"with_genres": "10765", "sort_by": "popularity.desc", "vote_count.gte": 100},
        ),
        _RowSpec(
            "crime-mystery", "悬疑罪案", "discover/tv",
            {"with_genres": "80|9648", "sort_by": "popularity.desc", "vote_count.gte": 50},
        ),
        _RowSpec(
            "comedy", "喜剧剧集", "discover/tv",
            {"with_genres": "35", "sort_by": "popularity.desc", "vote_count.gte": 100},
        ),
        _RowSpec(
            "animation", "动画剧集", "discover/tv",
            {"with_genres": "16", "sort_by": "popularity.desc", "vote_count.gte": 100},
        ),
        _RowSpec(
            "reality", "真人秀", "discover/tv",
            {"with_genres": "10764", "sort_by": "popularity.desc", "vote_count.gte": 20},
        ),
        _RowSpec(
            "documentary", "纪录片", "discover/tv",
            {"with_genres": "99", "sort_by": "popularity.desc", "vote_count.gte": 20},
        ),
    )


class MediaDiscoverService:
    """发现页/详情的编排服务：TMDB 原始数据 → 前端可渲染的模型。"""

    def __init__(
        self,
        client: TmdbClient,
        *,
        image_base_url: str,
        language: str = "zh-CN",
        region: str = "CN",
    ) -> None:
        self._client = client
        self._image_base = image_base_url.rstrip("/")
        self._language = language
        self._region = region
        self._cache = AsyncTTLCache()

    # ------------------------------------------------------------------
    # 发现页
    # ------------------------------------------------------------------

    def layout(self, kind: MediaKind) -> DiscoverLayout:
        """发现页布局：行清单来自静态配置，不触发任何 TMDB 请求。"""
        return DiscoverLayout(
            has_hero=True,
            rows=[
                DiscoverRowStub(id=s.row_id, title=s.title, ranked=s.ranked)
                for s in self._row_specs(kind)
            ],
        )

    async def discover_hero(self, kind: MediaKind) -> list[MediaCard]:
        """Hero 大横幅精选：本周趋势榜里挑「有宽幅剧照且有简介」的前几名。"""
        return await self._cache.get_or_set(
            f"hero:{kind.value}", _PAGE_TTL, lambda: self._build_hero(kind)
        )

    async def discover_row(self, kind: MediaKind, row_id: str) -> MediaRow | None:
        """拉取布局中的一行；row_id 不在布局里时返回 None（路由层译为 404）。"""
        spec = next((s for s in self._row_specs(kind) if s.row_id == row_id), None)
        if spec is None:
            return None
        return await self._cache.get_or_set(
            f"row:{kind.value}:{row_id}", _PAGE_TTL, lambda: self._build_row(kind, spec)
        )

    def _row_specs(self, kind: MediaKind) -> tuple[_RowSpec, ...]:
        return _movie_rows(self._region) if kind is MediaKind.MOVIE else _tv_rows()

    async def _build_hero(self, kind: MediaKind) -> list[MediaCard]:
        genre_map = await self._genre_map(kind)
        cards = await self._fetch_cards(f"trending/{kind.value}/week", {}, kind, genre_map)
        return [c for c in cards if c.backdrop_url and c.overview][:_HERO_COUNT]

    async def _build_row(self, kind: MediaKind, spec: _RowSpec) -> MediaRow:
        genre_map = await self._genre_map(kind)
        items = (await self._fetch_cards(spec.path, spec.params, kind, genre_map))[: spec.limit]
        # 条目太少不值得占一行位置：清空 items，前端按「空行」统一收起
        if len(items) < _MIN_ROW_ITEMS:
            items = []
        return MediaRow(id=spec.row_id, title=spec.title, ranked=spec.ranked, items=items)

    async def _fetch_cards(
        self, path: str, params: dict[str, Any], kind: MediaKind, genre_map: dict[int, str]
    ) -> list[MediaCard]:
        data = await self._client.get(path, {"language": self._language, **params})
        cards = (self._to_card(raw, kind, genre_map) for raw in data.get("results", []))
        return [c for c in cards if c is not None]

    def _to_card(
        self, raw: dict[str, Any], kind: MediaKind, genre_map: dict[int, str]
    ) -> MediaCard | None:
        """TMDB 列表条目 → 海报卡片；缺海报/标题/年份的条目没法上墙，返回 None 剔除。"""
        title = raw.get("title") or raw.get("name") or ""
        date = raw.get("release_date") or raw.get("first_air_date") or ""
        poster = raw.get("poster_path")
        if not poster or not title or len(date) < 4 or not date[:4].isdigit():
            return None
        # 详情接口给的是 genres 对象列表，列表接口给的是 genre_ids，两者兼容
        genre_ids = raw.get("genre_ids") or [g["id"] for g in raw.get("genres", [])]
        backdrop = raw.get("backdrop_path")
        return MediaCard(
            id=str(raw["id"]),
            type=kind,
            title=title,
            original_title=raw.get("original_title") or raw.get("original_name") or title,
            year=int(date[:4]),
            rating=round(float(raw.get("vote_average") or 0.0), 1),
            genres=[genre_map[g] for g in genre_ids if g in genre_map][:3],
            overview=(raw.get("overview") or "").strip(),
            poster_url=f"{self._image_base}/w500{poster}",
            backdrop_url=f"{self._image_base}/w1280{backdrop}" if backdrop else None,
        )

    async def _genre_map(self, kind: MediaKind) -> dict[int, str]:
        """TMDB 类型 ID → 中文名（如 878 → 科幻）。全站共享，缓存 24 小时。"""

        async def load() -> dict[int, str]:
            data = await self._client.get(
                f"genre/{kind.value}/list", {"language": self._language}
            )
            return {g["id"]: g["name"] for g in data.get("genres", [])}

        return await self._cache.get_or_set(f"genres:{kind.value}", _GENRE_TTL, load)

    # ------------------------------------------------------------------
    # 搜索
    # ------------------------------------------------------------------

    async def search(self, keyword: str) -> list[MediaSearchItem]:
        """TMDB multi 搜索 → 轻量候选（自带年份和 movie/tv 类型，豆瓣搜索没有）。

        用 search/multi 而非分别搜电影/剧集：一次请求，且两类条目按 TMDB
        的全局热度统一排序，不需要自己拼接排序。相同关键词缓存十分钟，
        口径与豆瓣搜索一致。
        """
        normalized = keyword.strip()

        async def load() -> list[MediaSearchItem]:
            data = await self._client.get(
                "search/multi", {"language": self._language, "query": normalized}
            )
            items = (self._to_search_item(raw) for raw in data.get("results", []))
            return [item for item in items if item is not None]

        return await self._cache.get_or_set(
            f"search:{normalized.casefold()}", _SEARCH_TTL, load
        )

    def _to_search_item(self, raw: dict[str, Any]) -> MediaSearchItem | None:
        """multi 搜索条目 → 轻量候选；人物条目和缺海报/标题的条目剔除。"""
        media_type = raw.get("media_type")
        if media_type not in (MediaKind.MOVIE.value, MediaKind.TV.value):
            return None
        title = raw.get("title") or raw.get("name") or ""
        poster = raw.get("poster_path")
        if not title or not poster:
            return None
        date = raw.get("release_date") or raw.get("first_air_date") or ""
        return MediaSearchItem(
            id=str(raw["id"]),
            source=MediaSource.TMDB,
            title=title,
            year=int(date[:4]) if date[:4].isdigit() else None,
            type=MediaKind(media_type),
            rating=round(float(raw.get("vote_average") or 0), 1),
            poster_url=f"{self._image_base}/w342{poster}",
        )

    # ------------------------------------------------------------------
    # 条目详情
    # ------------------------------------------------------------------

    async def media_detail(self, kind: MediaKind, tmdb_id: int) -> MediaDetail:
        return await self._cache.get_or_set(
            f"detail:{kind.value}:{tmdb_id}",
            _DETAIL_TTL,
            lambda: self._build_detail(kind, tmdb_id),
        )

    async def _build_detail(self, kind: MediaKind, tmdb_id: int) -> MediaDetail:
        genre_map = await self._genre_map(kind)
        # append_to_response：演职员/相似推荐/图片集随详情一次请求带回，省三次往返。
        # include_image_language 必须显式给：默认按 language 过滤会把图滤到几乎没有
        # ——剧照大多不带语言标注（null），海报则按语言分版本。
        primary_language = self._language.split("-")[0]
        data = await self._client.get(
            f"{kind.value}/{tmdb_id}",
            {
                "language": self._language,
                "append_to_response": "credits,recommendations,images",
                "include_image_language": f"{primary_language},en,null",
            },
        )
        card = self._to_card(data, kind, genre_map)
        if card is None:
            raise TmdbError("该条目在 TMDB 中缺少海报或标题等必要信息，无法展示")
        card.extent = self._extent(data, kind)

        related_raw = (data.get("recommendations") or {}).get("results", [])
        related = [
            c
            for c in (self._to_card(r, kind, genre_map) for r in related_raw)
            if c is not None
        ][:_RELATED_LIMIT]
        backdrops, posters = self._images(data)
        return MediaDetail(
            card=card,
            facts=self._facts(data, kind),
            backdrops=backdrops,
            posters=posters,
            related=related,
        )

    def _images(self, data: dict[str, Any]) -> tuple[list[MediaImage], list[MediaImage]]:
        """详情里的图片集 → 剧照/海报列表（各取评分最高的前 N 张）。

        海报把配置语言（如中文版）排到最前——用户点开更可能想要本地化海报；
        剧照无语言之分，保持 TMDB 的评分排序。
        """
        images = data.get("images") or {}
        primary_language = self._language.split("-")[0]

        def build(raw: dict[str, Any], preview_size: str) -> MediaImage | None:
            path = raw.get("file_path")
            if not path:
                return None
            return MediaImage(
                preview_url=f"{self._image_base}/{preview_size}{path}",
                full_url=f"{self._image_base}/original{path}",
                width=raw.get("width") or 0,
                height=raw.get("height") or 0,
            )

        backdrops = [
            img
            for img in (build(r, "w780") for r in images.get("backdrops", []))
            if img is not None
        ][:_IMAGES_LIMIT]

        posters_raw = sorted(
            images.get("posters", []),
            key=lambda r: r.get("iso_639_1") != primary_language,  # 配置语言在前，组内保持原序
        )
        posters = [
            img
            for img in (build(r, "w342") for r in posters_raw)
            if img is not None
        ][:_IMAGES_LIMIT]
        return backdrops, posters

    @staticmethod
    def _extent(data: dict[str, Any], kind: MediaKind) -> str:
        if kind is MediaKind.MOVIE:
            runtime = data.get("runtime")
            return f"{runtime} 分钟" if runtime else ""
        seasons = data.get("number_of_seasons")
        return f"{seasons} 季" if seasons else ""

    def _cast(self, credits: dict[str, Any]) -> list[MediaCastMember]:
        """credits.cast → 演职员条。

        头像用 w185（横滚条里每格才 104px 宽，再大是白下载）；没有 profile_path
        的人照样保留——名字与角色本身就是信息，前端渲染占位块即可。
        """
        members: list[MediaCastMember] = []
        for person in credits.get("cast", [])[:_CAST_LIMIT]:
            name = (person.get("name") or "").strip()
            if not name:
                continue
            profile = person.get("profile_path")
            members.append(
                MediaCastMember(
                    name=name,
                    role=(person.get("character") or "").strip() or None,
                    avatar_url=f"{self._image_base}/w185{profile}" if profile else None,
                    tmdb_person_id=person.get("id"),
                )
            )
        return members

    def _facts(self, data: dict[str, Any], kind: MediaKind) -> MediaFacts:
        credits = data.get("credits") or {}
        if kind is MediaKind.MOVIE:
            directors = [c["name"] for c in credits.get("crew", []) if c.get("job") == "Director"]
            country_codes = [c.get("iso_3166_1", "") for c in data.get("production_countries", [])]
        else:
            directors = [c["name"] for c in data.get("created_by", [])]
            country_codes = data.get("origin_country") or [
                c.get("iso_3166_1", "") for c in data.get("production_countries", [])
            ]

        networks = data.get("networks") or []
        original_language = data.get("original_language") or ""
        return MediaFacts(
            directors=directors[:3],
            cast=self._cast(credits),
            country=" / ".join(_COUNTRY_NAMES.get(c, c) for c in country_codes if c),
            language=_LANGUAGE_NAMES.get(original_language, original_language),
            released=data.get("release_date") or data.get("first_air_date") or "",
            network=networks[0].get("name") if kind is MediaKind.TV and networks else None,
        )

    async def aclose(self) -> None:
        await self._client.aclose()
