"""内置基线 spec 的漂移守护（docs/design/cli.md §2.1）。

CLI 的命令树来自随包分发的 data/spec.json。改了路由却忘记重新导出，
镜像里的 CLI 就会与服务器不同版——这里直接红，并给出修复命令。
"""

from __future__ import annotations

from movieclaw_api.export_openapi import build_spec, spec_hash
from movieclaw_cli.gen.spec_loader import _hash_spec, load_baseline


def test_baseline_spec_matches_code() -> None:
    baseline = load_baseline()
    current = build_spec()
    assert spec_hash(baseline) == spec_hash(current), (
        "内置基线 spec 与当前代码不一致，请重新导出：\n"
        "  python -m movieclaw_api.export_openapi -o src/movieclaw_cli/data/spec.json"
    )


def test_hash_algorithms_are_identical() -> None:
    """CLI 侧与服务端的 spec 指纹算法必须逐字节一致，否则偏斜检测失效。

    CLI 刻意不导入服务端的 spec_hash（远程安装形态不依赖服务端包），
    一致性由本测试守护。
    """
    baseline = load_baseline()
    assert _hash_spec(baseline) == spec_hash(baseline)
