"""登录 / 登出 / 状态（精选命令）。

login 是一个小工作流：探测初始化状态 → 密码登录 → 保存会话 Cookie 与
上下文。零交互原则（docs/design/cli.md §5.1）：仅在 TTY 下才会交互补问
用户名/密码，非 TTY（Agent/脚本）缺参数直接以用法错误退出。
"""

from __future__ import annotations

import sys

import click

from movieclaw_cli.core import config as cfg
from movieclaw_cli.core.errors import CliError, ExitCode
from movieclaw_cli.core.http import SESSION_COOKIE_NAME
from movieclaw_cli.core.output import emit


@click.command(name="login", short_help="登录服务器并保存会话凭证")
@click.option("--server", help="movieclaw 服务器地址，如 http://192.168.1.10:3000")
@click.option("--username", help="管理员用户名")
@click.option("--password", help="管理员密码（明文入参会留在 shell 历史，交互输入更安全）")
@click.option(
    "--remember/--no-remember",
    default=True,
    show_default=True,
    help="30 天内记住登录（对应 Web 端「记住我」）",
)
@click.pass_obj
def login(settings, server: str | None, username: str | None, password: str | None, remember: bool):
    """登录 movieclaw 并把会话凭证保存到本地。

    示例：

        mclaw login --server http://192.168.1.10:3000

    登录成功后服务器地址会记入当前上下文，之后的命令无需再指定 --server。
    """
    target = cfg.resolve_server(server or settings.server, settings.context)
    interactive = sys.stdin.isatty()
    if not username:
        if not interactive:
            raise click.UsageError("非交互模式下必须提供 --username")
        username = click.prompt("用户名")
    if not password:
        if not interactive:
            raise click.UsageError("非交互模式下必须提供 --password")
        password = click.prompt("密码", hide_input=True)

    api = settings.make_api(server=target)
    try:
        bootstrap = api.request("GET", "/auth/bootstrap")
        if not (bootstrap or {}).get("initialized"):
            raise CliError(
                "系统尚未初始化（还没有管理员账号）",
                hint=f"请先在浏览器打开 {target} 完成初始化引导，再回来登录",
            )
        _, response = api.request_raw(
            "POST",
            "/auth/login",
            json_body={"username": username, "password": password, "remember": remember},
        )
        cookie = response.cookies.get(SESSION_COOKIE_NAME)
        if not cookie:
            raise CliError(
                "登录成功但未收到会话 Cookie，无法保存凭证",
                hint="服务器版本可能过旧，请升级 movieclaw",
            )
        cfg.save_session_cookie(target, cookie)
        cfg.save_context(target)
        click.echo(f"登录成功：{username} @ {target}", err=True)
    finally:
        api.close()


@click.command(name="logout", short_help="退出登录并清除本地凭证")
@click.pass_obj
def logout(settings):
    """退出登录：注销服务端会话并删除本地保存的凭证。"""
    target = cfg.resolve_server(settings.server, settings.context)
    api = settings.make_api(server=target)
    try:
        api.request("POST", "/auth/logout")
    except CliError as exc:
        # 服务器不可达时也要把本地凭证清掉，不能让登出卡死
        if exc.exit_code != ExitCode.NETWORK:
            raise
    finally:
        api.close()
    cfg.delete_session_cookie(target)
    click.echo(f"已退出登录：{target}", err=True)


@click.command(name="status", short_help="查看服务器与登录状态")
@click.option(
    "-o",
    "--output",
    "output_override",
    type=click.Choice(["table", "json", "yaml"]),
    default=None,
    help="输出格式（覆盖全局设置）",
)
@click.pass_obj
def status(settings, output_override: str | None):
    """一眼看部署状态：服务健康、登录身份、CLI 基线 spec 指纹。"""
    from movieclaw_api.export_openapi import spec_hash  # 仅取哈希算法，无重依赖
    from movieclaw_cli.gen.spec_loader import load_baseline

    target = cfg.resolve_server(settings.server, settings.context)
    api = settings.make_api(server=target)
    try:
        health = api.request("GET", "/health")
        try:
            me = api.request("GET", "/auth/me")
            identity = (me or {}).get("nickname") or (me or {}).get("username") or "已登录"
        except CliError as exc:
            if exc.exit_code != ExitCode.AUTH:
                raise
            identity = "未登录（mclaw login）"
    finally:
        api.close()
    emit(
        {
            "server": target,
            "service": (health or {}).get("service"),
            "status": (health or {}).get("status"),
            "environment": (health or {}).get("environment"),
            "identity": identity,
            "cli_spec_hash": spec_hash(load_baseline()),
        },
        output=output_override or settings.output,
        quiet=settings.quiet,
    )
