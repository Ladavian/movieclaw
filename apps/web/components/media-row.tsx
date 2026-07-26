"use client";

import type { Route } from "next";
import Link from "next/link";

import { HScroller } from "@/components/h-scroller";
import { PosterCard, type PosterCardAction } from "@/components/poster-card";
import type { MediaItem, MediaRowData } from "@/lib/media-types";

/**
 * 横滚海报行（Netflix 式分类行）：标题栏 + 横滚容器（HScroller 提供隐藏
 * 滚动条与左右翻页钮）。ranked 行（Top 10）的卡片更宽，为左侧描边大数字留空间。
 */
export function MediaRow({
  row,
  moreHref,
  moreLabel = "查看完整榜单",
  cardAction,
  cardHref,
}: {
  row: MediaRowData;
  moreHref?: Route;
  moreLabel?: string;
  /**
   * 卡片悬浮操作区变体，缺省为「订阅影片」。媒体库「最近添加」行的动作
   * 因条目而异（在播剧追新/完结缺集补齐/齐全已入库），支持传函数逐条目决定。
   */
  cardAction?: PosterCardAction | ((item: MediaItem) => PosterCardAction);
  /**
   * 卡片点击目标覆盖：媒体库「最近添加」行跳库内条目详情（本地刮削信息），
   * 返回 undefined 的条目回落到默认的发现页详情。
   */
  cardHref?: (item: MediaItem) => Route | undefined;
}) {
  return (
    <section className="relative">
      <div className="mb-3 flex items-center justify-between gap-4 px-6 max-md:mb-2 max-md:px-4">
        <h3 className="text-on-image text-[15px] font-semibold tracking-[-0.01em] text-[var(--text)]">
          {row.title}
        </h3>
        {moreHref && (
          <Link
            href={moreHref}
            className="shrink-0 text-xs font-semibold text-[var(--text-muted)] transition hover:text-[var(--text)]"
          >
            {moreLabel}
          </Link>
        )}
      </div>

      <HScroller className="gap-4 px-6 pb-1 pt-1 max-md:gap-3 max-md:px-4">
        {row.items.map((item, i) => (
          <div
            key={`${row.id}-${item.id}`}
            className={`shrink-0 ${row.ranked ? "w-[188px] max-md:w-[156px]" : "w-[152px] max-md:w-[126px] xl:w-[164px]"}`}
          >
            <PosterCard
              item={item}
              rank={row.ranked ? i + 1 : undefined}
              action={typeof cardAction === "function" ? cardAction(item) : cardAction}
              href={cardHref?.(item)}
            />
          </div>
        ))}
      </HScroller>
    </section>
  );
}
