"""spec 装载（docs/design/cli.md §2.1）。

P0 只有主通道：内置基线 spec（构建期由 movieclaw_api.export_openapi
导出、随包分发的 data/spec.json）。P1 增加辅通道：/health 的 spec_hash
偏斜检测 + /api/v1/spec 刷新缓存，届时本模块是唯一改动点。
"""

from __future__ import annotations

import json
from functools import lru_cache
from importlib import resources
from typing import Any

from movieclaw_cli.core.errors import CliError, ExitCode


@lru_cache(maxsize=1)
def load_baseline() -> dict[str, Any]:
    """读取随包分发的内置基线 spec。"""
    try:
        text = (
            resources.files("movieclaw_cli").joinpath("data/spec.json").read_text(encoding="utf-8")
        )
    except FileNotFoundError as exc:
        raise CliError(
            "内置基线 spec 缺失（data/spec.json），安装包不完整",
            exit_code=ExitCode.USAGE,
            hint="重新安装 movieclaw CLI；开发环境执行 "
            "python -m movieclaw_api.export_openapi -o src/movieclaw_cli/data/spec.json",
        ) from exc
    return json.loads(text)
