"""精选命令：mclaw logs tail——系统日志的 tail / follow。

服务端只有「按天读全量/tail」的接口，follow 用轮询模拟：按行数差值
只打印新增部分（与页面「自动刷新」同语义）。
"""

from __future__ import annotations

import sys
import time

import click

from movieclaw_cli.core.errors import CliError


@click.command(name="tail", short_help="查看系统日志尾部，-f 持续跟随")
@click.option("--day", help="日期（YYYY-MM-DD）；缺省取最新一天")
@click.option("--lines", type=int, default=50, show_default=True, help="初始输出的行数")
@click.option("-f", "--follow", is_flag=True, help="持续跟随新日志（Ctrl-C 退出）")
@click.option("--interval", type=float, default=3.0, show_default=True, help="跟随的轮询间隔（秒）")
@click.pass_obj
def logs_tail(settings, day: str | None, lines: int, follow: bool, interval: float):
    """查看服务端日志。

    示例：

        mclaw logs tail --lines 100

        mclaw logs tail -f                 # 类 tail -f，Ctrl-C 退出
    """
    api = settings.make_api()
    try:
        if not day:
            days = api.request("GET", "/system/logs") or {}
            items = days.get("days") or days.get("items") or []
            if not items:
                raise CliError("还没有任何日志文件", hint="服务运行后会按天产生日志")
            first = items[0]
            day = first.get("day") if isinstance(first, dict) else first

        data = api.request("GET", f"/system/logs/{day}", params={"tail": lines}) or {}
        for line in data.get("lines") or []:
            print(line)
        seen_total = data.get("total_lines") or 0
        if not follow:
            return
        # follow：每轮取大尾巴，按 total_lines 差值只补打新增部分
        try:
            while True:
                time.sleep(interval)
                data = api.request("GET", f"/system/logs/{day}", params={"tail": 2000}) or {}
                total = data.get("total_lines") or 0
                fetched = data.get("lines") or []
                if total > seen_total:
                    fresh = min(total - seen_total, len(fetched))
                    for line in fetched[-fresh:]:
                        print(line)
                    seen_total = total
        except KeyboardInterrupt:
            print("（已停止跟随）", file=sys.stderr)
    finally:
        api.close()
