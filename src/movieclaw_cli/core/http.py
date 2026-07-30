"""HTTP 客户端封装（docs/design/cli.md §8.3）。

职责：认证注入（会话 Cookie，P1 起兼容 Bearer Token）、统一超时、
`ApiResponse{success,code,message,data}` 信封拆解、错误 → 中文 CliError
（带退出码与 hint）映射。业务命令层只拿到拆好信封的 data。
"""

from __future__ import annotations

import os
import sys
from typing import Any

import httpx

from movieclaw_cli.core import config as cfg
from movieclaw_cli.core.errors import CliError, ExitCode

SESSION_COOKIE_NAME = "movieclaw_session"
API_PREFIX = "/api/v1"


class Api:
    """面向单个 movieclaw 服务器的同步 HTTP 客户端。"""

    def __init__(
        self,
        server: str,
        *,
        timeout: float = 30.0,
        debug: bool = False,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.server = server
        self.debug = debug
        cookies = {}
        if cookie := cfg.load_session_cookie(server):
            cookies[SESSION_COOKIE_NAME] = cookie
        headers = {"Accept": "application/json"}
        # P1 的 API Token 通道：环境变量存在即带上，老服务器会忽略该头
        if token := os.environ.get(cfg.ENV_TOKEN):
            headers["Authorization"] = f"Bearer {token}"
        self._client = httpx.Client(
            base_url=server,
            timeout=timeout,
            cookies=cookies,
            headers=headers,
            transport=transport,
        )

    def close(self) -> None:
        self._client.close()

    def request(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> Any:
        """发起请求并拆信封，返回 data 字段。错误统一抛 CliError。"""
        data, _ = self.request_raw(method, path, params=params, json_body=json_body)
        return data

    def request_raw(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: Any = None,
    ) -> tuple[Any, httpx.Response]:
        """同 request，但额外返回原始响应（登录要读 Set-Cookie）。"""
        url = f"{API_PREFIX}{path}"
        if self.debug:
            print(f"[debug] {method} {self.server}{url} params={params}", file=sys.stderr)
        try:
            response = self._client.request(method, url, params=params, json=json_body)
        except httpx.ConnectError as exc:
            raise CliError(
                f"无法连接 movieclaw 服务器：{self.server}",
                exit_code=ExitCode.NETWORK,
                hint="确认服务已启动、地址正确（含端口）；"
                "地址来源优先级：--server > MOVIECLAW_SERVER > 当前上下文",
            ) from exc
        except httpx.TimeoutException as exc:
            raise CliError(
                f"请求超时（{self.server}{url}）",
                exit_code=ExitCode.NETWORK,
                hint="可用 --timeout 调大超时时间",
            ) from exc
        if self.debug:
            print(f"[debug] -> {response.status_code}", file=sys.stderr)
        return self._parse(response), response

    def _parse(self, response: httpx.Response) -> Any:
        if response.status_code == 401:
            raise CliError(
                "未登录或会话已过期",
                exit_code=ExitCode.AUTH,
                hint="请先执行 mclaw login",
            )
        if response.status_code == 204:
            return None
        try:
            payload = response.json()
        except ValueError as exc:
            raise CliError(
                f"服务器返回了无法解析的响应（HTTP {response.status_code}）",
                exit_code=ExitCode.BUSINESS,
                hint="确认 --server 指向的是 movieclaw 服务而非其他程序",
            ) from exc
        # 统一信封：success 字段存在即为 ApiResponse/ErrorResponse
        if isinstance(payload, dict) and "success" in payload:
            if payload.get("success"):
                return payload.get("data")
            raise CliError(
                payload.get("message") or f"请求失败（HTTP {response.status_code}）",
                exit_code=ExitCode.BUSINESS,
                code=payload.get("code"),
                details=payload.get("details"),
            )
        # 非信封 JSON（如 /health）原样返回
        if response.is_error:
            raise CliError(
                f"请求失败（HTTP {response.status_code}）",
                exit_code=ExitCode.BUSINESS,
                details=payload,
            )
        return payload
