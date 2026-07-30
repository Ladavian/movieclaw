"""标题排序键与首字母分档（海报墙的按标题排序 + A-Z 快速定位）。

为什么排序键在 Python 里算、不落库：

SQLite 对中文按 UTF-8 码点排序，「三体」排在「万里」前面纯属码点巧合，
对用户毫无意义——中文库必须按拼音排。而 SQLite 没有拼音排序规则，要在
SQL 里 ORDER BY 就得落一列排序键 + 加索引 + 在改名/重识别时维护同步。
但海报墙分页真正贵的是**聚合**（台账行、缺集判定、海报资产、序列化），
取一列标题排个序几乎不花钱：一万个条目也就是几百 KB 元组加一次 sorted()。
所以这里的取舍是——不加列、不迁移，把排序键算在内存里，等条目规模真的
大到「每次翻页排全部标题」有感了，再考虑落库。

首字母分档口径（与 Emby/Plex 的索引条一致）：
- 中文取拼音首字母（多音字/姓氏由 pypinyin 词库判定）；
- 拉丁字母取大写首字母；
- 数字、符号、以及拼不出首字母的（日文假名、韩文等）统一归入 ``#``。
"""

from __future__ import annotations

from functools import lru_cache

from pypinyin import Style, lazy_pinyin

# 落不进 A-Z 的条目统一归档到这一档（数字开头、符号开头、假名/谚文等）
OTHER_INITIAL = "#"

# A-Z + #：索引条的完整档位，顺序即排序顺序（# 排在最后，与主流媒体库一致）
INITIALS: list[str] = [chr(c) for c in range(ord("A"), ord("Z") + 1)] + [OTHER_INITIAL]

_INITIAL_ORDER = {value: index for index, value in enumerate(INITIALS)}


@lru_cache(maxsize=8192)
def title_sort_key(title: str) -> tuple[int, str]:
    """标题 → (首字母档位序号, 拼音串)。

    缓存到进程内：同一批标题在轮询里被反复排序，转换只在标题第一次出现时做。
    """
    cleaned = title.strip()
    if not cleaned:
        return _INITIAL_ORDER[OTHER_INITIAL], ""
    # 逐字转拼音后拼接：整串比较才排得对（"三国" 与 "三体" 要看第二个字）
    syllables = lazy_pinyin(cleaned, style=Style.NORMAL, errors=lambda item: item.lower())
    pinyin = "".join(syllables).lower()
    head = pinyin[:1].upper()
    initial = head if "A" <= head <= "Z" else OTHER_INITIAL
    return _INITIAL_ORDER[initial], pinyin


def title_initial(title: str) -> str:
    """标题的首字母档（A-Z 或 ``#``）——索引条分档与跳转都用它。"""
    return INITIALS[title_sort_key(title)[0]]
