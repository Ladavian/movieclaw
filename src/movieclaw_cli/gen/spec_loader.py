"""spec 装载与版本偏斜刷新（docs/design/cli.md §2.1 / §3.1）。

主通道：内置基线 spec（构建期由 movieclaw_api.export_openapi 导出、随包
分发的 data/spec.json）——同仓同镜像构建保证镜像内 CLI 与服务器严格同版。

辅通道（远程安装的 CLI 落后于服务器时）：每次请求的响应头都带服务端
spec 指纹（X-Movieclaw-Spec-Hash），与当前装载的 spec 指纹不一致时，
命令执行完后自动拉取受鉴权的 /api/v1/spec 写入本地缓存——**下次调用**
优先装载缓存。缓存解析/建树失败一律回退内置基线并提示升级 CLI，绝不半瘫。
"""

from __future__ import annotations

import hashlib
import json
import sys
from importlib import resources
from pathlib import Path
from typing import Any

from movieclaw_cli.core import config as cfg
from movieclaw_cli.core.errors import CliError, ExitCode

# 本次进程装载的 spec 指纹（与响应头比对的基准）
active_spec_hash: str | None = None


def _hash_spec(spec: dict[str, Any]) -> str:
    """与 movieclaw_api.export_openapi.spec_hash 完全一致的指纹算法。

    刻意复制而非导入：CLI 远程安装形态下不应依赖服务端包的可导入性；
    两端一致性由 tests/cli/test_spec_baseline.py 守护。
    """
    canonical = json.dumps(spec, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:16]


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


def _cache_path(server: str) -> Path:
    digest = hashlib.sha256(server.encode("utf-8")).hexdigest()[:16]
    return cfg.config_dir() / "spec-cache" / f"{digest}.json"


def load_active(server: str | None) -> tuple[dict[str, Any], bool]:
    """装载生效 spec：目标服务器有刷新缓存则优先，否则内置基线。

    返回 (spec, 是否来自缓存)。缓存损坏时静默回退基线（下次偏斜检测会
    重新触发刷新，自愈）。同时记录指纹供偏斜比对。
    """
    global active_spec_hash
    if server:
        path = _cache_path(server.rstrip("/"))
        if path.is_file():
            try:
                spec = json.loads(path.read_text(encoding="utf-8"))
                if "paths" not in spec:
                    raise ValueError("缓存内容不是 OpenAPI spec")
                active_spec_hash = _hash_spec(spec)
                return spec, True
            except (ValueError, OSError):
                path.unlink(missing_ok=True)
    spec = load_baseline()
    active_spec_hash = _hash_spec(spec)
    return spec, False


def guess_server_for_startup(argv: list[str]) -> str | None:
    """启动期（click 解析前）尽力猜出目标服务器，用于选择缓存 spec。

    只做无副作用的轻量解析：--server 标志 > 环境变量 > 配置文件当前上下文。
    猜不出（或配置损坏）返回 None，走内置基线——功能不受影响。
    """
    for i, arg in enumerate(argv):
        if arg == "--server" and i + 1 < len(argv):
            return argv[i + 1].rstrip("/")
        if arg.startswith("--server="):
            return arg.split("=", 1)[1].rstrip("/")
    try:
        return cfg.resolve_server(None, None)
    except CliError:
        return None


def maybe_refresh(settings: Any) -> None:
    """命令执行完后的偏斜检查：服务端指纹 ≠ 本地装载指纹 → 拉取 /spec 写缓存。

    刷新只影响**下次调用**（本次命令树已构建完成）；失败静默跳过（下次
    再试），不影响本次命令的结果与退出码。
    """
    from movieclaw_cli.core import http as http_mod

    seen = http_mod.last_seen_spec_hash
    server = http_mod.last_seen_server
    if not seen or not server or seen == active_spec_hash:
        return
    try:
        api = settings.make_api(server=server)
        try:
            spec, _resp = api.request_raw("GET", "/spec")
        finally:
            api.close()
        if not isinstance(spec, dict) or "paths" not in spec:
            raise ValueError("响应不是 OpenAPI spec")
        path = _cache_path(server)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        print(
            "提示：服务器接口目录已更新，命令目录将在下次调用时刷新生效",
            file=sys.stderr,
        )
    except (CliError, ValueError, OSError):
        # 拉取失败不影响本次命令；常见原因是未登录或服务器版本过旧
        if getattr(settings, "debug", False):
            print("[debug] spec 偏斜刷新失败（已跳过）", file=sys.stderr)
