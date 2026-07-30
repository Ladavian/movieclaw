"""输出层（docs/design/cli.md §5.2 / §8.3）。

契约：
- stdout 只放数据，stderr 放提示与错误；
- 非 TTY（Agent/管道）默认输出 JSON——服务端 data 字段原样，字段名即
  API schema，是脚本与 Agent 的稳定契约；TTY 下默认表格（人类副产品）；
- --quiet 抑制成功输出（配合退出码使用）。

表格渲染刻意保持极简（对齐分栏，无第三方依赖）：表格是给人扫一眼的，
机器一律走 -o json。
"""

from __future__ import annotations

import json
import sys
from typing import Any

import yaml


def resolve_format(explicit: str | None) -> str:
    if explicit:
        return explicit
    return "table" if sys.stdout.isatty() else "json"


def emit(data: Any, *, output: str | None = None, quiet: bool = False) -> None:
    if quiet:
        return
    fmt = resolve_format(output)
    if fmt == "json":
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    elif fmt == "yaml":
        print(yaml.safe_dump(data, allow_unicode=True, sort_keys=False, default_flow_style=False))
    else:
        _print_table(data)


def _display(value: Any) -> str:
    if value is None:
        return "-"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, (dict, list)):
        text = json.dumps(value, ensure_ascii=False, default=str)
        return text if len(text) <= 60 else text[:57] + "..."
    return str(value)


def _width(text: str) -> int:
    """终端显示宽度：CJK 字符占两列，对齐时必须按显示宽度算。"""
    return sum(2 if ord(ch) > 0x2E7F else 1 for ch in text)


def _pad(text: str, width: int) -> str:
    return text + " " * (width - _width(text))


def _print_table(data: Any) -> None:
    if isinstance(data, list) and data and all(isinstance(row, dict) for row in data):
        columns: list[str] = []
        for row in data:
            for key in row:
                if key not in columns:
                    columns.append(key)
        rows = [[_display(row.get(col)) for col in columns] for row in data]
        widths = [max(_width(col), *(_width(r[i]) for r in rows)) for i, col in enumerate(columns)]
        print("  ".join(_pad(col, widths[i]) for i, col in enumerate(columns)))
        for r in rows:
            print("  ".join(_pad(cell, widths[i]) for i, cell in enumerate(r)))
        print(f"（共 {len(data)} 条）", file=sys.stderr)
    elif isinstance(data, dict):
        width = max((_width(str(k)) for k in data), default=0)
        for key, value in data.items():
            print(f"{_pad(str(key), width)}  {_display(value)}")
    elif isinstance(data, list) and not data:
        print("（空）", file=sys.stderr)
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
