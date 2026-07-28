"use client";

import type { Route } from "next";
import Link from "next/link";

import { BellIcon, CheckIcon, DownloadIcon, PlayIcon, StarIcon } from "@/components/icons";
import { PosterImage } from "@/components/poster-image";
import { useSubscribeEntry } from "@/components/subscribe-entry";
import { useMediaDetail } from "@/lib/media-detail";
import type { MediaItem, MediaType } from "@/lib/media-types";
import { useTapGuard } from "@/lib/use-tap-guard";

/**
 * 海报卡片：发现页海报墙的最小单元（Netflix 式）。
 *
 * 结构分两层：
 *   - 海报区：2:3 竖版海报，常显「评分徽章（右上）+ 最高清晰度徽章（左上）」；
 *     hover 时整卡上浮、海报放大，底部升起渐变信息层（类型 / 简介 / 订阅影片按钮）。
 *   - 文字区：海报下方常显标题与「年份 · 规模」，保证不 hover 也能扫读海报墙。
 *
 * ranked 模式（Top 10 行）：海报左侧叠一个 Netflix 式描边大数字，
 * 数字与海报轻微重叠，靠 z-index 压在海报之下，形成「探出头」的层次。
 */
export interface PosterCardProps {
  item: MediaItem;
  /** Top 10 排名（1 起）；传入即渲染描边大数字变体 */
  rank?: number;
  /** 悬浮层的操作区变体，默认「订阅影片」 */
  action?: PosterCardAction;
  /** 点击目标覆盖：传入即直接链接到该地址（媒体库「最近添加」行跳
   *  库内条目详情），缺省走发现页详情（useMediaDetail.open） */
  href?: Route;
}

/**
 * 悬浮层操作区的形态，按内容与用户的关系选择：
 * - subscribe：还没拥有（发现页/搜索），给「订阅影片」入口；
 * - follow：已在库的在播剧（还会有新集），给「订阅追新」入口；
 * - backfill：已在库的完结剧但已播集有缺口，给「补齐缺集」入口
 *   （订阅创建按 E−H 跳过库里已有的集，只为缺的集生成工单）；
 * - owned：已在媒体库且无后续动作（电影/完结齐全剧），静态「已入库」标识；
 * - none：已订阅但尚未落地（单库页「追踪中」），再给订阅按钮是重复操作，不显示。
 *
 * 前三种都是订阅入口，仅文案/图标不同；该影片已存在订阅时自动切换为
 * 「已订阅」状态徽标（点击进入订阅管理弹层），状态来自 SubscribeEntryProvider
 * 的全站订阅列表，卡片自身不发请求。
 */
export type PosterCardAction = "subscribe" | "follow" | "backfill" | "owned" | "none";

/** 三种订阅入口的文案与图标（已订阅时统一切换为状态徽标，不走这张表）。 */
const SUBSCRIBE_ACTION_META = {
  subscribe: { label: "订阅影片", Icon: PlayIcon },
  follow: { label: "订阅追新", Icon: BellIcon },
  backfill: { label: "补齐缺集", Icon: DownloadIcon },
} as const;

/**
 * 海报卡片的最小视觉契约。搜索结果不含年份和类型，缺失字段保持不显示，
 * 但仍复用与发现页完全相同的海报、评分、悬浮信息层和来源标识。
 */
export interface PosterVisualItem {
  id: string;
  title: string;
  rating: number;
  posterUrl: string;
  source?: "tmdb" | "douban";
  /** 电影/剧集；豆瓣轻量搜索结果没有该字段，订阅入口点击时补拉详情识别 */
  type?: MediaType;
  year?: number;
  genres?: string[];
  extent?: string;
  badges?: string[];
  overview?: string;
}

export function PosterCard({ item, rank, action, href }: PosterCardProps) {
  // 点击整卡（含 hover 信息层）进入该影片的详情页
  const { open } = useMediaDetail();
  if (href) {
    return <PosterCardVisual item={item} rank={rank} action={action} href={href} />;
  }
  return (
    <PosterCardVisual
      item={item}
      rank={rank}
      action={action}
      onClick={() => open(item)}
    />
  );
}

/** 统一海报视觉组件；传入 onClick 时才渲染为可点击按钮。 */
export function PosterCardVisual({
  item,
  rank,
  onClick,
  href,
  action = "subscribe",
}: {
  item: PosterVisualItem;
  rank?: number;
  onClick?: () => void;
  href?: Route;
  action?: PosterCardAction;
}) {
  // 触摸端误触保护：海报墙滑动远多于点击，浏览器原生的 click 抑制拦不住
  // 「滑到边缘」「点一下停惯性」这类手势，统一交给 useTapGuard 判定。
  // Link 分支不传动作，仅靠 preventDefault 拦掉不合格点击的跳转。
  const tapGuard = useTapGuard(onClick);
  const content = <PosterCardContent item={item} rank={rank} action={action} />;
  const interactiveClass =
    "group/card block w-full cursor-pointer rounded-2xl text-left outline-none focus-visible:ring-2 focus-visible:ring-[var(--accent-ring)]";
  if (href) {
    return (
      <Link
        href={href}
        {...tapGuard}
        className={interactiveClass}
        aria-label={`查看《${item.title}》详情`}
      >
        {content}
      </Link>
    );
  }
  if (!onClick) return <div className="group/card block w-full text-left">{content}</div>;
  return (
    <button type="button" {...tapGuard} className={interactiveClass}>
      {content}
    </button>
  );
}

function PosterCardContent({
  item,
  rank,
  action = "subscribe",
}: {
  item: PosterVisualItem;
  rank?: number;
  action?: PosterCardAction;
}) {
  const badges = item.badges ?? [];
  const genres = item.genres ?? [];
  const overview = item.overview ?? "";
  return (
    <>
      <div className="relative">
        {/* Top 10 描边大数字：绝对定位在左下、贴齐海报底边，右缘塞进海报之下。
            位置与位数无关，两位数「10」也不会把布局撑开（多出的部分自然被海报盖住）。 */}
        {rank !== undefined && (
          <span
            aria-hidden="true"
            className="absolute bottom-0 left-0 z-0 select-none text-[108px] font-black leading-[0.78] tracking-[-0.08em] max-md:text-[86px]"
            style={{
              WebkitTextStroke: "2.5px rgba(205, 214, 230, 0.42)",
              color: "rgba(10, 11, 16, 0.55)",
            }}
          >
            {rank}
          </span>
        )}

        {/* 海报区：ranked 模式固定宽度并整体右移，把左侧空间留给探出的大数字 */}
        <div
          className={`relative z-[1] aspect-[2/3] overflow-hidden rounded-2xl bg-[#141824] shadow-[0_10px_28px_rgba(0,0,0,0.4)] ring-1 ring-white/[0.08] transition-all duration-300 ease-out group-hover/card:-translate-y-1.5 group-hover/card:shadow-[0_22px_50px_rgba(0,0,0,0.6)] group-hover/card:ring-white/25 ${
            rank !== undefined ? "ml-11 w-[144px] max-md:ml-9 max-md:w-[118px]" : "w-full"
          }`}
        >
          <PosterImage
            src={item.posterUrl}
            alt={`${item.title} 海报`}
            className="absolute inset-0 size-full transition-transform duration-500 ease-out group-hover/card:scale-[1.06]"
          />

          {/* 左上：资源最高清晰度徽章（无资源信息时不渲染） */}
          {badges[0] && (
            <span className="absolute left-2 top-2 rounded-md bg-black/55 px-1.5 py-0.5 text-[10px] font-bold tracking-wide text-[var(--accent)] backdrop-blur-sm">
              {badges[0]}
            </span>
          )}
          {/* 右上：评分徽章（暂无评分时不渲染，避免展示 0.0） */}
          {item.rating > 0 && (
            <span className="tnum absolute right-2 top-2 flex items-center gap-1 rounded-md bg-black/55 px-1.5 py-0.5 text-[11px] font-semibold text-white backdrop-blur-sm">
              <StarIcon className="size-3 text-[#f5c451]" />
              {item.rating.toFixed(1)}
            </span>
          )}

          {/* 触摸设备的操作入口：没有 hover 就没有下方的信息层，订阅/追新/补齐
              在手机上会彻底点不到。这里在海报右下角常驻一枚圆形操作键——只放
              图标不放文案（卡片在两列网格里只有 150px 宽，塞不下胶囊按钮），
              动作与信息层里的那颗完全一致。仅无悬停设备渲染，桌面端不受影响。
              只为「够得着操作」而设，因此仅订阅类动作才出这枚键：owned 是纯状态
              没有可点的动作，在媒体库里更是句废话（那儿的东西本来就都已入库），
              为它在海报角上常驻一个绿点只是噪声。桌面 hover 层里的标识保持不变。 */}
          {action !== "none" && action !== "owned" && (
            <span className="touch-only absolute bottom-2 right-2 z-[2]">
              <PosterCardActionButton item={item} action={action} compact />
            </span>
          )}

          {/* hover 信息层：底部渐变升起，展示类型 / 简介 / 快捷操作 */}
          <div className="absolute inset-x-0 bottom-0 translate-y-3 bg-gradient-to-t from-black/90 via-black/60 to-transparent px-3 pb-3 pt-10 opacity-0 transition-all duration-300 ease-out group-hover/card:translate-y-0 group-hover/card:opacity-100">
            {genres.length > 0 && (
              <p className="text-[11px] font-medium text-[var(--accent-2)]">
                {genres.join(" · ")}
              </p>
            )}
            {overview && (
              <p className="mt-1 line-clamp-3 text-[11px] leading-4 text-white/75">
                {overview}
              </p>
            )}
            {action !== "none" && (
              <div className="mt-2.5 flex items-center gap-2">
                <PosterCardActionButton item={item} action={action} />
              </div>
            )}
          </div>
        </div>
      </div>

      {/* 文字区：常显标题 + 元信息（压在背景大图上，需 text-on-image 投影保证可读） */}
      <div className={`mt-2 ${rank ? "pl-11 max-md:pl-9" : ""}`}>
        <p className="text-on-image truncate text-[13px] font-semibold text-[var(--text)]">
          {item.title}
        </p>
        {/* year 用真值判断：媒体库条目缺失年份时以 0 占位，不应显示出来 */}
        {(!!item.year || item.extent) && (
          <p className="text-on-image tnum mt-0.5 truncate text-[11px] text-[var(--text-muted)]">
            {item.year || ""}
            {!!item.year && item.extent ? " · " : ""}
            {item.extent}
          </p>
        )}
      </div>
    </>
  );
}

/**
 * 卡片上的操作键（订阅 / 追新 / 补齐 / 已入库标识），两处形态共用一份逻辑：
 *   - 默认（桌面）：hover 信息层里的文字胶囊；
 *   - compact（触摸设备）：海报右下角常驻的圆形图标键。
 *
 * 外层整卡已经是 button / Link，内部不能再嵌 button（HTML 不允许交互元素嵌套），
 * 所以用 role="button" 的 span 承载：preventDefault 拦掉 Link 跳转，
 * stopPropagation 拦掉整卡 onClick，点它就只做订阅这一件事。
 */
function PosterCardActionButton({
  item,
  action,
  compact = false,
}: {
  item: PosterVisualItem;
  action: PosterCardAction;
  /** 紧凑圆形形态：只留图标，给触摸设备的常驻入口用 */
  compact?: boolean;
}) {
  const { open: openSubscribe, subscriptionOf } = useSubscribeEntry();
  // 订阅入口类动作（subscribe/follow/backfill）才需要判断订阅状态；owned/none 不查询
  const subscribeMeta =
    action === "subscribe" || action === "follow" || action === "backfill"
      ? SUBSCRIBE_ACTION_META[action]
      : null;
  const existingSub = subscribeMeta ? subscriptionOf(item) : undefined;
  const trigger = () => void openSubscribe(item);
  // 圆键就贴在海报角上，横滑时拇指最容易蹭到，同样过一遍误触判定。
  // Hook 不能放在下面的提前返回之后，故与 trigger 一起提到最前。
  const tapGuard = useTapGuard(trigger);

  if (!subscribeMeta) {
    // 已入库标识：非交互，与库存格下方的绿点语言一致。
    // 只有 hover 信息层会走到这里——纯状态没有「够不着」的问题，
    // 触摸端不再为它单出常驻圆键（见上方 touch-only 的渲染条件）。
    return (
      <span className="flex h-7 items-center gap-1.5 rounded-full bg-white/[0.14] px-3 text-[11px] font-semibold text-white/90 backdrop-blur-sm">
        <span className="size-1.5 rounded-full bg-[#4ade80]" />
        已入库
      </span>
    );
  }

  const label = existingSub
    ? `管理《${item.title}》的订阅`
    : `${subscribeMeta.label}《${item.title}》`;

  return (
    <span
      role="button"
      tabIndex={0}
      aria-label={label}
      title={compact ? label : undefined}
      onPointerDown={tapGuard.onPointerDown}
      onPointerUp={tapGuard.onPointerUp}
      onPointerCancel={tapGuard.onPointerCancel}
      onClick={(e) => {
        // 无论判定结果如何都先拦住冒泡：这颗键嵌在整卡的 button/Link 里，
        // 放行会让「订阅」顺带把详情页也打开。判定通过与否交给 tapGuard。
        e.preventDefault();
        e.stopPropagation();
        tapGuard.onClick(e);
      }}
      onKeyDown={(e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          e.stopPropagation();
          trigger();
        }
      }}
      className={
        compact
          ? // 32px 圆键：低于这个尺寸在手机上就点不准了（触摸目标下限）
            existingSub
            ? "grid size-8 place-items-center rounded-full bg-black/60 text-white/90 shadow-lg backdrop-blur-sm active:scale-90"
            : "btn-accent grid size-8 place-items-center rounded-full active:scale-90"
          : existingSub
            ? "flex h-7 items-center gap-1.5 rounded-full bg-white/[0.14] px-3 text-[11px] font-semibold text-white/90 backdrop-blur-sm transition-colors hover:bg-white/[0.22]"
            : "btn-accent flex h-7 items-center gap-1 rounded-full px-3 text-[11px] font-semibold"
      }
    >
      {existingSub ? (
        <>
          <CheckIcon className={compact ? "size-4 text-[#4ade80]" : "size-3 text-[#4ade80]"} />
          {!compact && "已订阅"}
        </>
      ) : (
        <>
          <subscribeMeta.Icon className={compact ? "size-4" : "size-3"} />
          {!compact && subscribeMeta.label}
        </>
      )}
    </span>
  );
}
