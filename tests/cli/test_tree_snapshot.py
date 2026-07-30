"""命令树快照守护（docs/design/cli.md §3.2「没有静默的第三条路」）。

两条保证：
1. 生成命令集合与快照文件一致——路由增删改必然反映为快照 diff，
   评审时一目了然；新端点没被生成器认出来也会在这里现形。
2. 生成范围之外的 GET 端点必须在已知豁免清单里（SSE / 文件直出 /
   非信封响应）——出现未知形态的新端点时强制显式处理，不允许静默跳过。
"""

from __future__ import annotations

from pathlib import Path

from movieclaw_cli.gen.spec_loader import load_baseline
from movieclaw_cli.gen.tree_builder import (
    RESERVED_PARAM_NAMES,
    generated_command_paths,
    is_generable,
    iter_operations,
)

_SNAPSHOT = Path(__file__).parent / "command_tree_snapshot.txt"

# P0 已知不生成命令的 GET 端点：SSE 流（P1 x-cli-stream 接入）、
# 文件直出（P1 --output-file 接入）、非信封响应（health 由精选命令 status 覆盖）
KNOWN_NON_GENERATED_GET = {
    "agent.runs.stream",
    "appearance.backdrops.download",
    "auth.avatar.download",
    "health.check",
    "images.asset",
    "images.proxy",
    "lib.files.thumb",
    "lib.items.artwork-download",
    "search.stream",
}


def test_command_tree_matches_snapshot() -> None:
    expected = _SNAPSHOT.read_text(encoding="utf-8").splitlines()
    actual = generated_command_paths(load_baseline())
    assert actual == expected, (
        "生成命令树与快照不一致。确认属预期变更后更新快照：\n"
        '  .venv/bin/python -c "from movieclaw_cli.gen.spec_loader import load_baseline; '
        "from movieclaw_cli.gen.tree_builder import generated_command_paths; "
        "open('tests/cli/command_tree_snapshot.txt','w').write("
        "'\\n'.join(generated_command_paths(load_baseline()))+'\\n')\""
    )


def test_api_params_do_not_shadow_global_flags() -> None:
    """API 参数名不得与可尾随的全局标志重名，否则该命令会失去全局标志。"""
    clashes = sorted(
        f"{op['operation_id']}: {p['name']}"
        for op in iter_operations(load_baseline())
        for p in op["params"]
        if p["name"] in RESERVED_PARAM_NAMES
    )
    assert not clashes, f"以下端点参数与全局标志重名，请在路由侧改名：{clashes}"


def test_non_generated_get_endpoints_are_all_known() -> None:
    unknown = sorted(
        op["operation_id"]
        for op in iter_operations(load_baseline())
        if op["method"] == "get"
        and not is_generable(op)
        and op["operation_id"] not in KNOWN_NON_GENERATED_GET
    )
    assert not unknown, (
        f"出现生成器不认识的新 GET 端点形态：{unknown}。"
        "二选一：扩展 tree_builder 映射规则，或加入 KNOWN_NON_GENERATED_GET 并说明理由"
    )
