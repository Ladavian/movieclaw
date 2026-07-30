"""精选命令：mclaw lib organize——整理文件名的「预览→确认→执行→等待」工作流。

页面上这是一个四段式对话框；CLI 把它固化为一条命令：任何正式执行都
**强制先预览并回显影响面**（改名 N 项 / 跳过 M 项），再要求 --yes。
--dry-run 只看计划不动磁盘。

本命令整体覆盖生成层的 `lib organize preview/start` 两条命令
（同名让位机制，docs/design/cli.md §7）。
"""

from __future__ import annotations

import sys

import click

from movieclaw_cli.core.errors import CliError, ExitCode
from movieclaw_cli.core.output import emit
from movieclaw_cli.gen.tree_builder import wait_long_task
from movieclaw_cli.overlay.groups import output_option

# 进度轮询的端点描述（与生成层 lib.scan/organize 用的是同一个 wait 实现）
_PROGRESS_OPS = {
    "lib.show": {
        "operation_id": "lib.show",
        "path": "/api/v1/libraries/{library_id}",
        "params": [{"name": "library_id", "in": "path"}],
    }
}


@click.command(name="organize", short_help="整理文件名（预览影响面 → --yes 确认 → 执行并等待）")
@click.argument("library_id", type=int)
@click.option("--dry-run", is_flag=True, help="只输出整理计划，不动磁盘")
@click.option("--yes", is_flag=True, help="确认执行（正式整理必须显式给出）")
@click.option("--wait/--no-wait", default=True, show_default=True, help="等待整理完成")
@click.option(
    "--wait-timeout", type=float, default=3600.0, show_default=True, help="--wait 的最长等待秒数"
)
@output_option
@click.pass_obj
def lib_organize(
    settings,
    output_override: str | None,
    library_id: int,
    dry_run: bool,
    yes: bool,
    wait: bool,
    wait_timeout: float,
):
    """按 Emby/Plex 规范批量改名归位库内文件。

    示例：

        mclaw lib organize 1 --dry-run     # 只看计划

        mclaw lib organize 1 --yes         # 执行（先回显影响面）

    源文件绝不删除；执行与扫描互斥（扫描进行中会被服务端拒绝）。
    """
    api = settings.make_api()
    try:
        preview = api.request("POST", f"/libraries/{library_id}/organize/preview") or {}
        renames = preview.get("renames") or []
        skips = preview.get("skips") or []
        print(
            f"整理计划：共 {preview.get('total')} 个文件——"
            f"改名 {len(renames)} 项，已规范 {preview.get('already_ok')} 项，"
            f"跳过 {len(skips)} 项",
            file=sys.stderr,
        )
        if dry_run:
            emit(preview, output=output_override or settings.output, quiet=settings.quiet)
            return
        if not renames:
            print("没有需要整理的文件", file=sys.stderr)
            return
        if not (yes or settings.yes):
            emit(preview, output=output_override or settings.output, quiet=settings.quiet)
            raise CliError(
                f"即将改名 {len(renames)} 个文件，需要确认",
                exit_code=ExitCode.NEED_CONFIRM,
                hint="核对上面的整理计划后重跑并加 --yes；只看计划用 --dry-run",
            )
        started = api.request("POST", f"/libraries/{library_id}/organize")
        if api.last_message:
            print(api.last_message, file=sys.stderr)
        emit(started, output=output_override or settings.output, quiet=settings.quiet)
        if wait:
            op = {"long_task": {"progress_op": "lib.show", "progress_field": "organize_progress"}}
            wait_long_task(op, _PROGRESS_OPS, api, {"library_id": library_id}, wait_timeout)
    finally:
        api.close()
