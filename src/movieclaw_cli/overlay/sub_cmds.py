"""精选命令：mclaw sub create——订阅创建的完整工作流。

页面上「点一次订阅按钮」背后是三个接口的编排，这里把流程知识固化成
一条命令（docs/design/cli.md §7）：

    prepare（建档 + 歧义检测）→ dispatch-preview（投递预检回显）→ create

歧义不是对话而是数据（§5.4）：命中多个候选时输出候选清单 JSON，
退出码 7，Agent 重跑并指定 --tmdb 即可，每次调用自身无状态。
"""

from __future__ import annotations

import sys

import click

from movieclaw_cli.core.errors import CliError, ExitCode
from movieclaw_cli.core.output import emit
from movieclaw_cli.overlay.groups import output_option


def _parse_seasons(raw: str | None) -> list[int]:
    if not raw:
        return []
    try:
        return [int(part) for part in raw.replace("，", ",").split(",") if part.strip()]
    except ValueError as exc:
        raise CliError(
            f"--seasons 格式不正确：{raw}",
            exit_code=ExitCode.USAGE,
            hint="用逗号分隔的季号，如 --seasons 1,2,3",
        ) from exc


@click.command(name="create", short_help="创建订阅（prepare 消歧 → 投递预检 → 创建，一步完成）")
@click.option("--kind", type=click.Choice(["movie", "tv"]), required=True, help="媒体类型")
@click.option("--tmdb", "tmdb_id", type=int, help="TMDB 条目 ID（明确指定，跳过消歧）")
@click.option("--title", help="按标题订阅（豆瓣入口语义；命中多个候选时返回清单待选）")
@click.option("--year", type=int, help="配合 --title 收敛年份")
@click.option("--douban-id", help="豆瓣条目 ID（配合 --title，留存来源身份）")
@click.option("--seasons", help="剧集：要订阅的季，逗号分隔（如 1,2）；缺省全部缺失季")
@click.option(
    "--follow/--no-follow",
    "follow_future",
    default=False,
    show_default=True,
    help="持续追新（未来新季自动纳入）",
)
@click.option("--rule-set", "rule_set_id", type=int, help="规则组 id；缺省用默认规则组")
@click.option("--library", "library_id", type=int, help="入库目标库 id；缺省走收藏范围路由")
@output_option
@click.pass_obj
def sub_create(
    settings,
    output_override: str | None,
    kind: str,
    tmdb_id: int | None,
    title: str | None,
    year: int | None,
    douban_id: str | None,
    seasons: str | None,
    follow_future: bool,
    rule_set_id: int | None,
    library_id: int | None,
):
    """创建订阅（完整工作流）。

    示例：

        mclaw sub create --kind tv --tmdb 693134 --seasons 1,2 --follow

        mclaw sub create --kind movie --title "沙丘2"   # 歧义时返回候选清单（退出码 7）

    创建前会做投递预检：目标库、落点、能否自动入库的结论直接回显；
    配置有问题（如没有默认下载器）会先警告再创建，不会静默吞掉。
    """
    if tmdb_id is None and not title:
        raise click.UsageError("必须提供 --tmdb 或 --title 其中之一")

    api = settings.make_api()
    try:
        # 1) prepare：建档 + 歧义检测
        if tmdb_id is not None:
            payload = {"source": "tmdb", "kind": kind, "tmdb_id": tmdb_id}
        else:
            payload = {
                "source": "douban",
                "kind": kind,
                "title": title,
                "year": year,
                "douban_id": douban_id,
            }
        prepared = api.request("POST", "/subscriptions/prepare", json_body=payload) or {}
        status = prepared.get("status")
        if status == "not_found":
            raise CliError(
                f"没有找到可订阅的条目：{title or tmdb_id}",
                hint="检查标题拼写，或用 mclaw media search 先确认条目、拿到 TMDB ID",
            )
        if status == "ambiguous":
            candidates = [
                {
                    "tmdb_id": c.get("tmdb_id"),
                    "title": c.get("title"),
                    "original_title": c.get("original_title"),
                    "year": c.get("year"),
                }
                for c in prepared.get("candidates") or []
            ]
            emit(candidates, output=output_override or settings.output, quiet=False)
            raise CliError(
                f"「{title}」命中 {len(candidates)} 个候选，需要指定其中一个",
                exit_code=ExitCode.AMBIGUOUS,
                hint="从上面的候选里选定后重跑：mclaw sub create --kind "
                f"{kind} --tmdb <tmdb_id>" + (" --seasons ..." if kind == "tv" else ""),
            )
        media = prepared.get("media") or {}
        resolved_tmdb = media.get("tmdb_id") or tmdb_id
        if prepared.get("existing_subscription_id"):
            raise CliError(
                f"「{media.get('title')}」已在订阅中"
                f"（订阅 id={prepared['existing_subscription_id']}）",
                hint=f"查看：mclaw sub show {prepared['existing_subscription_id']}；"
                "改季/追新用 mclaw sub update",
            )

        # 2) 投递预检：把「下载完成后能不能进库」的结论提前亮出来
        preview = (
            api.request(
                "GET",
                "/subscriptions/dispatch-preview",
                params={
                    "kind": kind,
                    **({"library_id": library_id} if library_id else {}),
                    **({"tmdb_id": resolved_tmdb} if resolved_tmdb else {}),
                },
            )
            or {}
        )
        route_bits = [
            f"投递路由：{preview.get('mode')}",
            f"目标库：{preview.get('library_name') or '（无）'}",
            f"下载器：{preview.get('downloader_name') or '（无）'}",
        ]
        if preview.get("path"):
            route_bits.append(f"落点：{preview['path']}")
        print("；".join(route_bits), file=sys.stderr)
        if preview.get("route_reason"):
            print(f"选库理由：{preview['route_reason']}", file=sys.stderr)
        if not preview.get("ok"):
            print(
                f"⚠ 预检警告：{preview.get('warning') or '当前配置可能无法自动入库'}",
                file=sys.stderr,
            )

        # 3) 创建
        body = {
            "kind": kind,
            "tmdb_id": resolved_tmdb,
            "selected_seasons": _parse_seasons(seasons),
            "follow_future": follow_future,
        }
        if rule_set_id is not None:
            body["rule_set_id"] = rule_set_id
        if library_id is not None:
            body["library_id"] = library_id
        if douban_id:
            body["douban_id"] = douban_id
        created = api.request("POST", "/subscriptions", json_body=body)
        if api.last_message:
            print(api.last_message, file=sys.stderr)
    finally:
        api.close()
    emit(created, output=output_override or settings.output, quiet=settings.quiet)
