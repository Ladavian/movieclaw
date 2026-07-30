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

    def latest_day() -> str:
        days = api.request("GET", "/system/logs") or {}
        items = days.get("days") or []
        if not items:
            raise CliError("还没有任何日志文件", hint="服务运行后会按天产生日志")
        first = items[0]
        return first.get("day") if isinstance(first, dict) else first

    try:
        auto_day = not day
        if not day:
            day = latest_day()

        data = api.request("GET", f"/system/logs/{day}", params={"tail": lines}) or {}
        for line in data.get("lines") or []:
            print(line)
        seen_total = data.get("total_lines") or 0
        if not follow:
            return
        # follow：每轮取大尾巴，按 total_lines 差值只补打新增部分；
        # 未显式指定日期时每轮复核最新日期，跨零点自动切到新一天的日志文件
        try:
            while True:
                time.sleep(interval)
                try:
                    if auto_day and (current := latest_day()) != day:
                        print(f"（已跨天，切换到 {current} 的日志）", file=sys.stderr)
                        day, seen_total = current, 0
                    data = api.request("GET", f"/system/logs/{day}", params={"tail": 2000}) or {}
                except CliError as exc:
                    # 日志文件被超期清理等业务错误：如实说明后停止跟随，不算失败
                    print(f"（跟随中止：{exc.message}）", file=sys.stderr)
                    return
                total = data.get("total_lines") or 0
                fetched = data.get("lines") or []
                if total > seen_total:
                    fresh = total - seen_total
                    if fresh > len(fetched):
                        print(
                            f"（两轮之间新增 {fresh} 行，超出单次拉取上限，"
                            f"中间 {fresh - len(fetched)} 行未显示）",
                            file=sys.stderr,
                        )
                        fresh = len(fetched)
                    for line in fetched[-fresh:]:
                        print(line)
                    seen_total = total
        except KeyboardInterrupt:
            print("（已停止跟随）", file=sys.stderr)
    finally:
        api.close()
