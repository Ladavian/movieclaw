"""entrypoint 的 overlay 解析矩阵（docs/design/in-app-update.md M2）。

entrypoint.sh 提供 `resolve` 测试钩子：只解析启动指向并打印 key=value，
不拉起任何进程；APP_ROOT / DATA_DIR / RUNTIME_FILE 均可用环境变量覆盖，
因此可以在临时目录里完整验证解析逻辑——这是应用内更新的安全地基，
坏 overlay 在任何情况下都必须回落到可运行的版本。
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

_ENTRYPOINT = Path(__file__).resolve().parents[2] / "docker" / "entrypoint.sh"

# 镜像的 runtime 版本，测试里统一用 1
_RUNTIME = "1"


@pytest.fixture()
def env_root(tmp_path: Path) -> Path:
    """搭一个最小的容器文件布局：app 根 + data 卷 + runtime 版本文件。"""
    (tmp_path / "app").mkdir()
    (tmp_path / "data").mkdir()
    (tmp_path / "runtime").write_text(_RUNTIME + "\n", encoding="utf-8")
    return tmp_path


def _make_overlay(
    root: Path,
    version: str,
    *,
    requires_runtime: int = 1,
    complete: bool = True,
    manifest_text: str | None = None,
) -> Path:
    """在 data 卷上摆一个 overlay 版本目录（布局与真实产物解包后一致）。"""
    vdir = root / "data" / "updates" / "versions" / f"v{version}"
    (vdir / "backend" / "src" / "movieclaw_api").mkdir(parents=True)
    (vdir / "backend" / "src" / "movieclaw_api" / "main.py").touch()
    (vdir / "backend" / "alembic.ini").touch()
    if complete:
        (vdir / "web" / "apps" / "web").mkdir(parents=True)
        (vdir / "web" / "apps" / "web" / "server.js").touch()
    if manifest_text is None:
        manifest_text = json.dumps({"schema": 1, "version": version, "requires_runtime": requires_runtime})
    (vdir / "manifest.json").write_text(manifest_text, encoding="utf-8")
    return vdir


def _link(root: Path, name: str, target: Path) -> None:
    link = root / "data" / "updates" / name
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)


def _resolve(root: Path) -> dict[str, str]:
    result = subprocess.run(
        ["bash", str(_ENTRYPOINT), "resolve"],
        capture_output=True,
        text=True,
        env={
            "PATH": "/usr/bin:/bin:/usr/local/bin",
            "MOVIECLAW_APP_ROOT": str(root / "app"),
            "MOVIECLAW_DATA_DIR": str(root / "data"),
            "MOVIECLAW_RUNTIME_FILE": str(root / "runtime"),
            "MOVIECLAW_PYTHON_BIN": sys.executable,
        },
        check=True,
    )
    return dict(
        line.split("=", 1) for line in result.stdout.strip().splitlines() if "=" in line
    )


def test_no_overlay_uses_baseline(env_root: Path) -> None:
    out = _resolve(env_root)
    assert out["source"] == "baseline"
    assert out["backend_root"] == str(env_root / "app")
    assert out["web_root"] == str(env_root / "app" / "web")


def test_valid_overlay_wins(env_root: Path) -> None:
    vdir = _make_overlay(env_root, "0.2.0")
    _link(env_root, "current", vdir)
    out = _resolve(env_root)
    assert out["source"] == "overlay"
    assert out["version"] == "0.2.0"
    assert out["backend_root"] == str(vdir / "backend")
    assert out["web_root"] == str(vdir / "web")


def test_runtime_mismatch_falls_back_to_baseline(env_root: Path) -> None:
    vdir = _make_overlay(env_root, "0.2.0", requires_runtime=2)
    _link(env_root, "current", vdir)
    assert _resolve(env_root)["source"] == "baseline"


def test_corrupt_manifest_falls_back_to_baseline(env_root: Path) -> None:
    vdir = _make_overlay(env_root, "0.2.0", manifest_text="{oops")
    _link(env_root, "current", vdir)
    assert _resolve(env_root)["source"] == "baseline"


def test_incomplete_layout_falls_back_to_baseline(env_root: Path) -> None:
    vdir = _make_overlay(env_root, "0.2.0", complete=False)
    _link(env_root, "current", vdir)
    assert _resolve(env_root)["source"] == "baseline"


def test_dangling_current_falls_back_to_baseline(env_root: Path) -> None:
    _link(env_root, "current", env_root / "data" / "updates" / "versions" / "v9.9.9")
    assert _resolve(env_root)["source"] == "baseline"


def test_bad_marker_skips_current_and_uses_previous(env_root: Path) -> None:
    bad = _make_overlay(env_root, "0.3.0")
    prev = _make_overlay(env_root, "0.2.0")
    _link(env_root, "current", bad)
    _link(env_root, "previous", prev)
    state = env_root / "data" / "updates" / "state"
    state.mkdir(parents=True)
    (state / "bad-0.3.0").touch()
    out = _resolve(env_root)
    assert out["source"] == "overlay"
    assert out["version"] == "0.2.0"
    assert out["backend_root"] == str(prev / "backend")


def test_bad_current_and_bad_previous_fall_back_to_baseline(env_root: Path) -> None:
    cur = _make_overlay(env_root, "0.3.0")
    prev = _make_overlay(env_root, "0.2.0")
    _link(env_root, "current", cur)
    _link(env_root, "previous", prev)
    state = env_root / "data" / "updates" / "state"
    state.mkdir(parents=True)
    (state / "bad-0.3.0").touch()
    (state / "bad-0.2.0").touch()
    assert _resolve(env_root)["source"] == "baseline"


def test_ner_model_pointer(env_root: Path) -> None:
    model = env_root / "data" / "models" / "ner" / "current"
    model.mkdir(parents=True)
    # 不完整的模型目录不生效
    (model / "model.int8.onnx").touch()
    assert _resolve(env_root)["ner_dir"] == ""
    # 三件套齐全才切换
    (model / "tokenizer.json").touch()
    (model / "labels.json").touch()
    assert _resolve(env_root)["ner_dir"] == str(model)
