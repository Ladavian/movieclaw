"""spec 内容指纹的进程内缓存。

指纹用途（docs/design/cli.md §2.1）：CLI 比对「内置基线 spec 的 hash」与
「服务器返回的 hash」即可零成本发现版本偏斜。指纹随 /health 响应与所有
/api/v1 响应头（X-Movieclaw-Spec-Hash）下发；算法与 export_openapi.spec_hash
完全一致——两端必须同一算法，否则偏斜检测失效。
"""

from __future__ import annotations

from fastapi import FastAPI

from movieclaw_api.export_openapi import spec_hash

SPEC_HASH_HEADER = "X-Movieclaw-Spec-Hash"


def get_spec_hash(app: FastAPI) -> str:
    """取当前 app 的 spec 指纹（首次计算后缓存到 app.state）。"""
    cached = getattr(app.state, "spec_hash", None)
    if cached is None:
        cached = spec_hash(app.openapi())
        app.state.spec_hash = cached
    return cached
