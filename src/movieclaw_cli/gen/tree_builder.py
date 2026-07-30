"""spec → click 命令树（docs/design/cli.md §3.2 参数映射规则）。

命令名完全由 operation_id 决定：`sub.list` → `mclaw sub list`，
`lib.items.claim` → `mclaw lib items claim`。help 来自 summary/description，
参数来自 parameters——spec 写好，命令即成，CLI 侧零手工维护。

P0 生成范围（见设计文档实施路线）：GET + 仅 path/query 参数 + 响应为
ApiResponse 信封的端点。SSE / 文件下载 / 写操作在 P1 由 x-cli-* 元数据
驱动接入；范围之外的端点这里显式跳过，由命令树快照测试保证「跳过的都
是已知形态」，不会静默漏掉新端点。
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any

import click

from movieclaw_cli.core.http import Api
from movieclaw_cli.core.output import emit

# 允许写在子命令尾部的全局标志（kubectl 惯例：`mclaw sub list -o json`）。
# 根组上的同名标志依然有效，叶子上的取值优先。
_OVERRIDE_VALUE_OPTS = ("output", "server", "timeout")
_OVERRIDE_FLAG_OPTS = ("quiet", "debug")
RESERVED_PARAM_NAMES = set(_OVERRIDE_VALUE_OPTS) | set(_OVERRIDE_FLAG_OPTS)


def _global_override_options() -> list[click.Option]:
    return [
        click.Option(
            ["-o", "--output"],
            type=click.Choice(["table", "json", "yaml"]),
            default=None,
            help="输出格式（覆盖全局设置）",
        ),
        click.Option(["--server"], default=None, help="服务器地址（覆盖全局设置）"),
        click.Option(["--timeout"], type=float, default=None, help="请求超时秒数（覆盖全局设置）"),
        click.Option(["--quiet"], is_flag=True, default=False, help="成功时不输出数据"),
        click.Option(["--debug"], is_flag=True, default=False, help="打印调试信息到 stderr"),
    ]


def merge_settings(kwargs: dict[str, Any]) -> Any:
    """从命令参数中取出全局覆盖标志，合并进当前 Settings（叶子优先）。"""
    ctx = click.get_current_context()
    settings = ctx.obj
    overrides: dict[str, Any] = {}
    for key in _OVERRIDE_VALUE_OPTS:
        value = kwargs.pop(key, None)
        if value is not None:
            overrides[key] = value
    for key in _OVERRIDE_FLAG_OPTS:
        if kwargs.pop(key, False):
            overrides[key] = True
    return replace(settings, **overrides) if overrides else settings


# 域分组的一行简介（click group 的 help；生成命令自身的 help 来自 spec）
DOMAIN_HELP = {
    "auth": "账号与会话",
    "appearance": "外观（背景图库）",
    "agent": "AI 助手会话与运行",
    "discover": "发现页与影视元数据",
    "dl": "下载器与一键下载",
    "extension": "浏览器插件 Cookie 同步",
    "fs": "服务器目录浏览",
    "health": "服务健康检查",
    "lib": "媒体库",
    "llm": "AI 模型供应商",
    "logs": "系统日志",
    "net": "网络与代理",
    "people": "影人档案（本地库）",
    "rules": "订阅规则组",
    "search": "站点资源搜索",
    "site": "PT 站点配置",
    "sub": "订阅",
    "ui": "界面偏好",
    "watch": "监听导入",
}


def _resolve_schema(schema: dict[str, Any], components: dict[str, Any]) -> dict[str, Any]:
    """展开 $ref 与 anyOf（可空类型取非 null 分支）。"""
    if "$ref" in schema:
        name = schema["$ref"].rsplit("/", 1)[-1]
        schema = components.get(name, {})
    if "anyOf" in schema:
        for candidate in schema["anyOf"]:
            resolved = _resolve_schema(candidate, components)
            if resolved.get("type") != "null":
                return resolved
    return schema


def _click_type(schema: dict[str, Any]) -> Any:
    if enum := schema.get("enum"):
        return click.Choice([str(v) for v in enum])
    return {
        "integer": click.INT,
        "number": click.FLOAT,
        "boolean": click.BOOL,
    }.get(schema.get("type", "string"), click.STRING)


def iter_operations(spec: dict[str, Any]) -> list[dict[str, Any]]:
    """展平 spec 中的全部操作，附带解析后的参数信息。"""
    components = spec.get("components", {}).get("schemas", {})
    ops: list[dict[str, Any]] = []
    for path, methods in spec["paths"].items():
        for method, op in methods.items():
            if method not in {"get", "post", "put", "patch", "delete"}:
                continue
            params = []
            for param in op.get("parameters", []):
                if param.get("in") not in {"path", "query"}:
                    continue  # 会话 Cookie 等鉴权参数由 http 层注入，不进命令
                schema = _resolve_schema(param.get("schema", {}), components)
                params.append(
                    {
                        "name": param["name"],
                        "in": param["in"],
                        "required": bool(param.get("required")),
                        "schema": schema,
                        "description": param.get("description") or schema.get("description"),
                    }
                )
            ref = (
                op.get("responses", {})
                .get("200", {})
                .get("content", {})
                .get("application/json", {})
                .get("schema", {})
                .get("$ref", "")
            )
            ops.append(
                {
                    "operation_id": op.get("operationId", ""),
                    "method": method,
                    "path": path,
                    "summary": op.get("summary", ""),
                    "description": op.get("description", ""),
                    "params": params,
                    "envelope": ref.rsplit("/", 1)[-1].startswith("ApiResponse_"),
                    "has_body": "requestBody" in op,
                }
            )
    return ops


def is_generable(op: dict[str, Any]) -> bool:
    """P0 生成范围判定（详见模块 docstring）。"""
    return op["method"] == "get" and not op["has_body"] and op["envelope"]


def _api_path(path: str) -> str:
    """spec 里的路径带 /api/v1 前缀，http 层会再拼一次，这里剥掉。"""
    prefix = "/api/v1"
    return path[len(prefix) :] if path.startswith(prefix) else path


def _make_command(op: dict[str, Any]) -> click.Command:
    path_params = [p for p in op["params"] if p["in"] == "path"]
    query_params = [p for p in op["params"] if p["in"] == "query"]

    def callback(**kwargs: Any) -> None:
        settings = merge_settings(kwargs)
        api: Api = settings.make_api()
        try:
            url = _api_path(op["path"])
            for p in path_params:
                url = url.replace("{" + p["name"] + "}", str(kwargs[p["name"]]))
            query = {
                p["name"]: kwargs[p["name"]]
                for p in query_params
                if kwargs.get(p["name"]) is not None
            }
            data = api.request("GET", url, params=query or None)
            emit(data, output=settings.output, quiet=settings.quiet)
        finally:
            api.close()

    cli_params: list[click.Parameter] = []
    for p in path_params:
        cli_params.append(click.Argument([p["name"]], type=_click_type(p["schema"])))
    for p in query_params:
        flag = "--" + p["name"].replace("_", "-")
        default = p["schema"].get("default")
        cli_params.append(
            click.Option(
                [flag],
                type=_click_type(p["schema"]),
                required=p["required"],
                default=default,
                show_default=default is not None,
                help=p["description"],
            )
        )

    # API 参数与全局覆盖标志重名时 API 参数优先（守护测试保证目前无重名）
    taken = {p["name"] for p in op["params"]}
    cli_params.extend(o for o in _global_override_options() if o.name not in taken)

    help_text = op["summary"]
    if op["description"]:
        help_text = f"{op['summary']}\n\n{op['description']}"
    return click.Command(
        name=op["operation_id"].rsplit(".", 1)[-1],
        callback=callback,
        params=cli_params,
        help=help_text,
        short_help=op["summary"],
    )


def build_tree(root: click.Group, spec: dict[str, Any]) -> None:
    """把生成命令挂到根命令组。已存在的同名命令（精选层）优先，不覆盖。"""
    for op in iter_operations(spec):
        if not is_generable(op) or not op["operation_id"]:
            continue
        segments = op["operation_id"].split(".")
        group = root
        for segment in segments[:-1]:
            existing = group.commands.get(segment)
            if existing is None:
                existing = click.Group(name=segment, help=DOMAIN_HELP.get(segment))
                group.add_command(existing)
            elif not isinstance(existing, click.Group):
                break  # 精选层占了同名命令，生成命令让位
            group = existing
        else:
            if segments[-1] not in group.commands:
                group.add_command(_make_command(op))


def generated_command_paths(spec: dict[str, Any]) -> list[str]:
    """全部生成命令的完整命令路径（快照测试的数据源）。"""
    return sorted(
        op["operation_id"].replace(".", " ")
        for op in iter_operations(spec)
        if is_generable(op) and op["operation_id"]
    )
