"""TMDB 类型（genre）与国家/区域的静态映射——媒体库路由的展示层素材。

收藏范围条件里 genres 存的是 TMDB genre **ID**（语言无关，见
docs/design/library-routing.md 1.1 的决策），展示与理由文案需要 ID→中文名；
TMDB 的 genre 集合多年稳定，内置常量表即可，真出现新类型补一行不影响
既有规则。国家码同理：条件存 ISO 3166-1 码，展示用中文名（映射外的
少见国家直接显示码，不猜）。

区域预设组是**配置界面的输入捷径**（一键勾选一组国家码），存储永远是
展开后的国家码列表——路由引擎只认码，不认预设名。
"""

from __future__ import annotations

from movieclaw_media.models import MediaKind

# TMDB 电影 genre（ID 全语言稳定；名字取 zh-CN 官方文案）
MOVIE_GENRES: dict[int, str] = {
    28: "动作",
    12: "冒险",
    16: "动画",
    35: "喜剧",
    80: "犯罪",
    99: "纪录",
    18: "剧情",
    10751: "家庭",
    14: "奇幻",
    36: "历史",
    27: "恐怖",
    10402: "音乐",
    9648: "悬疑",
    10749: "爱情",
    878: "科幻",
    10770: "电视电影",
    53: "惊悚",
    10752: "战争",
    37: "西部",
}

# TMDB 剧集 genre
TV_GENRES: dict[int, str] = {
    10759: "动作冒险",
    16: "动画",
    35: "喜剧",
    80: "犯罪",
    99: "纪录",
    18: "剧情",
    10751: "家庭",
    10762: "儿童",
    9648: "悬疑",
    10763: "新闻",
    10764: "真人秀",
    10765: "科幻奇幻",
    10766: "肥皂剧",
    10767: "脱口秀",
    10768: "战争政治",
    37: "西部",
}


def genre_names(kind: MediaKind | str) -> dict[int, str]:
    """某类型可用的 genre ID→中文名（配置界面 chips 与理由文案共用）。"""
    value = kind.value if isinstance(kind, MediaKind) else kind
    return MOVIE_GENRES if value == MediaKind.MOVIE.value else TV_GENRES


def genre_label(kind: MediaKind | str, genre_id: int) -> str:
    """单个 genre ID 的展示名；映射外的未知 ID 原样显示数字（不猜）。"""
    return genre_names(kind).get(genre_id, str(genre_id))


# 常见制片国家/地区码 → 中文名（理由文案与配置界面展示；映射外显示原码）
COUNTRY_NAMES: dict[str, str] = {
    "CN": "中国大陆",
    "HK": "香港",
    "TW": "台湾",
    "SG": "新加坡",
    "JP": "日本",
    "KR": "韩国",
    "US": "美国",
    "GB": "英国",
    "FR": "法国",
    "DE": "德国",
    "IT": "意大利",
    "ES": "西班牙",
    "CA": "加拿大",
    "AU": "澳大利亚",
    "NZ": "新西兰",
    "IE": "爱尔兰",
    "IN": "印度",
    "TH": "泰国",
    "RU": "俄罗斯",
}


def country_label(code: str) -> str:
    """国家码的展示名；映射外原样显示码。"""
    return COUNTRY_NAMES.get(code, code)


# 区域预设组：配置界面的一键勾选捷径，保存时展开为国家码列表。
# 分组粒度对齐 PT 用户常见的分库习惯（大陆剧库/港台库/美剧库…）；
# 捷径不必覆盖所有国家——新加坡/加拿大/澳大利亚/新西兰归不进任何组，
# 界面上仍可逐个勾选。白名单模型表达不了"其他"——收不进任何声明库的
# 作品自然落该类型默认库，默认库就是"其他"的承接者
# （docs/design/library-routing.md 1.1）
REGION_PRESETS: list[dict] = [
    {"key": "mainland", "label": "大陆", "countries": ["CN"]},
    {"key": "hk_tw", "label": "港台", "countries": ["HK", "TW"]},
    {"key": "jp_kr", "label": "日韩", "countries": ["JP", "KR"]},
    {"key": "us", "label": "美国", "countries": ["US"]},
    {"key": "europe", "label": "欧洲", "countries": ["GB", "FR", "DE", "IT", "ES", "IE"]},
    {"key": "minor_lang", "label": "小语种", "countries": ["IN", "TH", "RU"]},
]
