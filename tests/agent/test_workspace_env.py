"""Agent 工作区环境注入测试（docs/design/cli.md §6.2）。

bash 工具的 extra_env 是 mclaw CLI 自动授权的载体：MOVIECLAW_SERVER 与
MOVIECLAW_TOKEN 经此进入工作区子进程。这里验证注入与合并语义。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from movieclaw_agent.tools.bash import make_bash_tool


def test_extra_env_reaches_subprocess(tmp_path: Path) -> None:
    tool = make_bash_tool(
        tmp_path, extra_env={"MOVIECLAW_SERVER": "http://127.0.0.1:8000", "MOVIECLAW_TOKEN": "t1"}
    )
    result = asyncio.run(tool.handler({"command": "echo $MOVIECLAW_SERVER:$MOVIECLAW_TOKEN"}))
    assert "http://127.0.0.1:8000:t1" in result


def test_process_env_still_inherited(tmp_path: Path, monkeypatch) -> None:
    """extra_env 是合并而非替换：PATH 等进程环境必须保留，bash 才能找到命令。"""
    monkeypatch.setenv("MOVIECLAW_TEST_MARKER", "inherited")
    tool = make_bash_tool(tmp_path, extra_env={"MOVIECLAW_TOKEN": "t2"})
    result = asyncio.run(tool.handler({"command": "echo $MOVIECLAW_TEST_MARKER:$MOVIECLAW_TOKEN"}))
    assert "inherited:t2" in result
