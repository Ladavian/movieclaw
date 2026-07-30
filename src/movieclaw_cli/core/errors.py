"""CLI 错误模型与退出码契约（docs/design/cli.md §5.8）。

退出码是脚本与 Agent 的机器接口，一经发布不可变更含义：

    0  成功
    1  业务错误（服务端 4xx/5xx，message 中文透传）
    2  用法错误（参数不合法，click 框架层产生）
    3  认证失败 / 会话过期（提示 mclaw login）
    4  无法连接服务器（网络 / 地址错误）
    5  需要确认但未提供 --yes（非交互场景）
    6  长任务失败或 --wait 超时
    7  歧义待消解（候选清单已随输出返回）
"""

from __future__ import annotations

from enum import IntEnum
from typing import Any


class ExitCode(IntEnum):
    OK = 0
    BUSINESS = 1
    USAGE = 2
    AUTH = 3
    NETWORK = 4
    NEED_CONFIRM = 5
    TASK_FAILED = 6
    AMBIGUOUS = 7


class CliError(Exception):
    """带退出码与修正提示的 CLI 错误。

    「错误即帮助」：Agent 靠试错学习，message 说清哪里错了，
    hint 给出下一步该怎么做（docs/design/cli.md §4.2）。
    """

    def __init__(
        self,
        message: str,
        *,
        exit_code: ExitCode = ExitCode.BUSINESS,
        hint: str | None = None,
        code: str | None = None,
        details: Any = None,
    ) -> None:
        super().__init__(message)
        self.message = message
        self.exit_code = exit_code
        self.hint = hint
        self.code = code
        self.details = details
