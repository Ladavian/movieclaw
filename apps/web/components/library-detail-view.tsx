"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { createPortal } from "react-dom";

import * as DropdownMenu from "@radix-ui/react-dropdown-menu";
import type { Route } from "next";
import Link from "next/link";

import { MoreIcon, XIcon } from "@/components/icons";
import { PAGE_NAV_BUTTON_CLASS, PageNav } from "@/components/page-nav";
import { usePageTitle } from "@/lib/use-page-title";
import {
  LIBRARY_KIND_META,
  LibraryFormDialog,
  effectiveLibraryId,
  libraryCardAction,
} from "@/components/library-view";
import { LibraryOrganizeDialog } from "@/components/library-organize-dialog";
import { PosterCardVisual, type PosterVisualItem } from "@/components/poster-card";
import {
  type LibraryItem,
  type MediaLibrary,
  type MissingItem,
  type ReviewGroup,
  type MetadataRefreshProgress,
  type UnidentifiedGroup,
  claimFilesBatch,
  clearMissing,
  clearUnidentified,
  getMetadataRefreshProgress,
  ignoreFile,
  listIdentityReview,
  listIgnored,
  listLibraries,
  listLibraryItems,
  listMissing,
  listUnidentified,
  redownloadMissing,
  restoreIgnored,
  resolveIdentityReview,
  SCAN_PHASE_HINTS,
  SCAN_PHASE_LABELS,
  type ScanPhase,
  type ScanProgress,
  startLibraryMetadataRefresh,
  startLibraryScan,
  stopLibraryMetadataRefresh,
  stopLibraryScan,
} from "@/lib/api/libraries";
import {
  fetchMediaDetail,
  searchTmdbMedia,
  type MediaDetailData,
  type MediaSearchItem,
} from "@/lib/api/discover";
import { listSubscriptions, type Subscription } from "@/lib/api/subscriptions";
import { formatBytes } from "@/lib/format";
import { formatRelativeTime } from "@/lib/time";
import { cachedImageUrl, imageUrl } from "@/lib/image-proxy";
import {
  subscriptionProgressNote,
  subscriptionStatusMeta,
} from "@/lib/subscription-ui";

/**
 * 长任务状态胶囊的文案：阶段名 + 分子分母 + 一句"在等什么"。
 *
 * 分子分母**只在分母已知时**显示：分母为 0 是"还不知道有多少"（如盘点
 * 阶段），硬写成 0/0 比不写更误导。阶段换了分母就跟着换，因此不会出现
 * 进度走满后界面还停在同一句话上干等的情况。
 */
function busyText(progress: ScanProgress | null): string {
  if (!progress) return "正在处理…"; // 理论上取不到（接口保证同生同灭），保底不留白
  const counter = progress.total > 0 ? ` ${progress.processed}/${progress.total}` : "";
  return `${SCAN_PHASE_LABELS[progress.phase]}${counter} · ${SCAN_PHASE_HINTS[progress.phase]}`;
}

/**
 * 单库页（/library/[id]）：库头部 + **真实库存**海报墙（Emby 进库后的浏览视图）。
 *
 * 三个分区：
 * 1. 库存（library_file 台账聚合）：已在磁盘上的作品，格下标注集数/规格/大小；
 * 2. 待识别：扫描认不出身份的文件，按条目目录成组，点候选或填 TMDB ID 整组认领；
 * 3. 追踪中：入库目标是本库、但还没有文件落地的订阅（弱化展示，点进订阅详情）。
 */
export function LibraryDetailView({ libraryId }: { libraryId: number }) {
  const [libraries, setLibraries] = useState<MediaLibrary[] | null>(null);
  const [items, setItems] = useState<LibraryItem[]>([]);
  const [unidentified, setUnidentified] = useState<UnidentifiedGroup[]>([]);
  const [review, setReview] = useState<ReviewGroup[]>([]);
  const [ignored, setIgnored] = useState<UnidentifiedGroup[]>([]);
  const [missing, setMissing] = useState<MissingItem[]>([]);
  const [subscriptions, setSubscriptions] = useState<Subscription[]>([]);
  const [failed, setFailed] = useState(false);
  const [notice, setNotice] = useState<string | null>(null);
  const [editing, setEditing] = useState<MediaLibrary | null>(null);
  // 整理文件名对话框的目标库；null = 关闭
  const [organizeTarget, setOrganizeTarget] = useState<MediaLibrary | null>(null);
  // 整库元数据刷新进度（进行中每 3 秒轮询，结束自动刷新库存）
  const [metaRefresh, setMetaRefresh] = useState<MetadataRefreshProgress | null>(null);
  // 工单抽屉：从哪个胶囊点进来就落在哪个 tab；null = 关闭
  const [issueTab, setIssueTab] = useState<IssueTab | null>(null);

  // 轮询乱序守卫：扫描期间后端响应时间抖动大，上一轮的慢响应可能晚于
  // 下一轮到达，不作废就会用旧快照覆盖新状态（进度回跳、胶囊闪烁）
  const reloadSeq = useRef(0);
  const reload = useCallback(() => {
    const seq = ++reloadSeq.current;
    Promise.all([
      listLibraries(),
      listLibraryItems(libraryId).catch(() => []),
      listUnidentified(libraryId).catch(() => []),
      listIdentityReview(libraryId).catch(() => []),
      listIgnored(libraryId).catch(() => []),
      listMissing(libraryId).catch(() => []),
      listSubscriptions().catch(() => []),
    ])
      .then(([libs, libraryItems, unknown, reviewGroups, ignoredGroups, missingItems, subs]) => {
        if (seq !== reloadSeq.current) return;
        setFailed(false);
        setLibraries(libs);
        setItems(libraryItems);
        setUnidentified(unknown);
        setReview(reviewGroups);
        setIgnored(ignoredGroups);
        setMissing(missingItems);
        setSubscriptions(subs);
        // 整库刷新可能是别处（首页卡片/其他设备）发起的：库列表响应里带着
        // 状态，据此补种进度面板——否则只有挂载时那一次探测，之后发起的
        // 刷新这个页面永远看不见。已有进行中的状态时不覆盖（专用轮询更新鲜）
        const remote = libs.find((l) => l.id === libraryId)?.metadata_refresh;
        if (remote?.refreshing) setMetaRefresh((prev) => (prev?.refreshing ? prev : remote));
      })
      // 瞬时失败（网络抖动/后端忙）不清已有数据：failed 只决定顶部提示条，
      // 页面继续用上一份快照展示，下一轮轮询成功即自动恢复
      .catch(() => {
        if (seq === reloadSeq.current) setFailed(true);
      });
  }, [libraryId]);

  useEffect(() => {
    reload();
  }, [reload]);

  // 进入页面先探一次元数据刷新状态（可能是别的入口/上次会话发起的）
  useEffect(() => {
    getMetadataRefreshProgress(libraryId)
      .then(setMetaRefresh)
      .catch(() => {});
  }, [libraryId]);

  // 刷新进行中每 2 秒轮询状态，结束自动重拉库存（海报/档案已更新）。
  // 2 秒是"阶段文字跟得上"与"别把接口打太密"的折中：单部片的一个阶段
  // 常在数秒内走完，轮询再慢就只能看到最后一个阶段
  const refreshingMeta = Boolean(metaRefresh?.refreshing);
  useEffect(() => {
    if (!refreshingMeta) return;
    const timer = setInterval(() => {
      getMetadataRefreshProgress(libraryId)
        .then((p) => {
          setMetaRefresh(p);
          if (!p.refreshing) reload();
        })
        // 瞬时失败保留旧状态、下一轮继续：这里一旦清空，refreshingMeta 变
        // false 会把本轮询连根停掉，后台还在跑的刷新从此在界面上失踪
        // （曾是线上实况）。刷新真结束时成功响应会带 refreshing=false 收尾
        .catch(() => {});
    }, 2000);
    return () => clearInterval(timer);
  }, [refreshingMeta, libraryId, reload]);

  const library = libraries?.find((l) => l.id === libraryId) ?? null;
  usePageTitle(library?.name);

  // 扫描/整理期间轮询，结束自动展示最新库存与文件名
  const busy = Boolean(library?.scanning || library?.organizing);
  // 写入中暂缓入账的文件数（watchdog 已发现、等拷贝/下载落定后自动补扫入库）
  const importing = busy ? 0 : (library?.last_scan?.deferred ?? 0);
  // busy 刚结束后保持快轮询一小段再降速：监控去抖触发的连环扫描之间隔着
  // 几秒空档，一采样到空档就降去 30 秒档的话，下一轮扫描的开始要很久
  // 才被发现，状态看起来就是"时隐时现"
  const [recentlyBusy, setRecentlyBusy] = useState(false);
  useEffect(() => {
    if (busy) {
      setRecentlyBusy(true);
      return;
    }
    if (!recentlyBusy) return;
    const timer = setTimeout(() => setRecentlyBusy(false), 12_000);
    return () => clearTimeout(timer);
  }, [busy, recentlyBusy]);
  useEffect(() => {
    // 忙时快轮询；有文件入库中 / 元数据刷新中中速跟进（刷新会边跑边换海报，
    // 让墙上的图逐步更新）；空闲低频兜底——后台自发的扫描（实时监控/定时
    // 对账）页面开着不动也能感知到
    const interval =
      busy || recentlyBusy ? 3000 : importing > 0 || refreshingMeta ? 10_000 : 30_000;
    const timer = setInterval(reload, interval);
    return () => clearInterval(timer);
  }, [busy, recentlyBusy, importing, refreshingMeta, reload]);

  // 待识别的文件总数（清单按条目目录分组，一组可能是一部剧的几十集）
  const unidentifiedFiles = unidentified.reduce((n, g) => n + g.file_count, 0);

  // 整库刷新中"正在处理哪几部、各在什么阶段"：海报墙据此点亮对应的格，
  // 用户不必在进度面板与墙之间对片名
  const refreshPhaseById = useMemo(
    () => new Map((metaRefresh?.active ?? []).map((a) => [a.media_item_id, a.phase])),
    [metaRefresh],
  );

  // 扫描的补探阶段：把「还有文件没读出规格」的条目排到墙前面并点亮——
  // 头部胶囊只有一个总数（22/25），用户看不出具体是哪几部还在处理。
  // 仅在补探阶段生效：平时按标题排，阶段结束墙自动落回原序
  const probing = Boolean(
    library?.scanning && library.scan_progress?.phase === "probing",
  );
  const displayItems = useMemo(() => {
    if (!probing) return items;
    return [...items].sort(
      (a, b) => Number(b.probe_pending_count > 0) - Number(a.probe_pending_count > 0),
    );
  }, [items, probing]);

  // 追踪中：目标是本库、且尚未在库存中出现的订阅
  const pending = useMemo(() => {
    if (!libraries || !library) return [];
    const ownedMedia = new Set(items.map((i) => i.media_item_id));
    return subscriptions.filter(
      (s) =>
        effectiveLibraryId(s, libraries) === library.id &&
        !ownedMedia.has(s.media.media_item_id ?? -1) &&
        s.progress.imported === 0,
    );
  }, [libraries, library, subscriptions, items]);

  // 只有一次都没加载成功过才整页报错；已有数据在手时，瞬时失败只在页内
  // 挂提示条（stale-while-error）——为一次网络抖动把整面海报墙换成错误屏，
  // 用户看到的就是"页面闪没了"
  // 兜底态（加载中/失败/不存在）也渲染 PageNav（库名未知，末项留空）：向外壳
  // 登记「本页自带顶栏」，否则移动端全局顶栏（☰ + logo）会先显示再消失、顶部闪一下。
  const fallbackTrail = [{ label: "媒体库", href: "/library" }, { label: "" }];

  if (failed && libraries === null) {
    return (
      <div className="flex-1">
        <PageNav items={fallbackTrail} />
        <CenteredNote>
          <p className="text-[13.5px] text-[var(--text-muted)]">媒体库加载失败</p>
          <button
            type="button"
            onClick={reload}
            className="btn-glass px-4 py-2 text-[13px] font-medium text-[var(--text)]"
          >
            重试
          </button>
        </CenteredNote>
      </div>
    );
  }

  if (libraries === null) {
    return (
      <div className="flex-1">
        <PageNav items={fallbackTrail} />
        <CenteredNote>
          <span className="size-4 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
          <p className="text-[13px] text-[var(--text-muted)]">正在加载媒体库…</p>
        </CenteredNote>
      </div>
    );
  }

  if (library === null) {
    return (
      <div className="flex-1">
        <PageNav items={fallbackTrail} />
        <CenteredNote>
          <p className="text-[13.5px] text-[var(--text-muted)]">这个媒体库不存在（可能已被删除）</p>
          <Link href={"/library" as Route} className="btn-glass px-4 py-2 text-[13px] font-medium">
            返回媒体库
          </Link>
        </CenteredNote>
      </div>
    );
  }

  const meta = LIBRARY_KIND_META[library.kind];
  const { stats } = library;

  // 库操作全部收进 ⋯ 菜单，顶栏只留这一个入口；运行状态看头部下方的胶囊
  const actionsMenu = (
    <LibraryActionsMenu
      scanning={Boolean(library.scanning)}
      scanPhase={library.scan_progress?.phase ?? null}
      scanPercent={
        library.scan_progress && library.scan_progress.total > 0
          ? Math.min(
              100,
              Math.round(
                (library.scan_progress.processed / library.scan_progress.total) * 100,
              ),
            )
          : null
      }
      onToggleScan={() => {
        setNotice(null);
        void (library.scanning ? stopLibraryScan(library.id) : startLibraryScan(library.id))
          .then(() => reload())
          .catch((e) => setNotice((e as Error).message));
      }}
      organizing={Boolean(library.organizing)}
      organizePercent={
        library.organize_progress && library.organize_progress.total > 0
          ? Math.min(
              100,
              Math.round(
                (library.organize_progress.processed / library.organize_progress.total) *
                  100,
              ),
            )
          : null
      }
      refreshingMeta={refreshingMeta}
      metaProgress={
        metaRefresh && metaRefresh.total > 0
          ? `${metaRefresh.processed}/${metaRefresh.total}`
          : null
      }
      busy={busy}
      onOrganize={() => {
        setNotice(null);
        setOrganizeTarget(library);
      }}
      onToggleMetaRefresh={() => {
        setNotice(null);
        void (
          refreshingMeta
            ? stopLibraryMetadataRefresh(libraryId)
            : startLibraryMetadataRefresh(libraryId)
        )
          .then(() =>
            getMetadataRefreshProgress(libraryId)
              .then(setMetaRefresh)
              .catch(() => {}),
          )
          .catch((e) => setNotice((e as Error).message));
      }}
      onEdit={() => setEditing(library)}
    />
  );

  return (
    <div className="scroll-thin scroll-safe flex-1 overflow-y-auto pb-10">
      {/* 顶栏：返回媒体库 + 吸顶库名 + 库操作 ⋯（无 pt——顶边与侧栏卡片顶边齐平） */}
      <PageNav
        items={[{ label: "媒体库", href: "/library" }, { label: library.name }]}
        actions={actionsMenu}
      />
      {/* —— 库头部 —— */}
      <div className="px-6 max-md:px-4">
        <div className="flex items-center gap-2.5">
          <h2 className="text-on-image truncate text-[26px] font-bold leading-tight tracking-[-0.02em] text-white max-md:text-[20px]">
            {library.name}
          </h2>
          {library.is_default && (
            <span className="shrink-0 rounded-full border border-white/[0.14] bg-white/[0.12] px-2 py-0.5 text-[11px] font-semibold text-white/90">
              默认
            </span>
          )}
        </div>
        <p
          className="text-on-image mt-1.5 truncate text-[13px] text-[var(--text-muted)] max-md:text-[12px]"
          title={library.root_paths.join("\n")}
        >
          {meta.label}库 · {stats.item_count} 部作品 · {stats.file_count} 个文件 ·{" "}
          {formatBytes(stats.total_size_bytes)}
          {library.primary_root ? ` · ${library.primary_root}` : ""}
        </p>
        {library.last_scan && !busy && (
          <p className="mt-1 text-[12px] text-white/45">
            最近扫描 {formatRelativeTime(library.last_scan.finished_at)}
            {library.last_scan.cancelled ? "（手动停止，未扫完）" : ""} · 新入账{" "}
            {library.last_scan.scanned}（识别 {library.last_scan.identified} / 待识别{" "}
            {library.last_scan.unidentified}）
            {library.last_scan.retried > 0
              ? ` · 重试识别 ${library.last_scan.retried} 个待识别文件`
              : ""}
            {library.last_scan.marked_missing > 0
              ? ` · 标记丢失 ${library.last_scan.marked_missing}`
              : ""}
            {library.last_scan.deferred > 0
              ? ` · ${library.last_scan.deferred} 个写入中暂缓（稍后自动补扫）`
              : ""}
            {library.last_scan.errors.length > 0
              ? ` · ${library.last_scan.errors[0]}`
              : ""}
          </p>
        )}
        {/* —— 健康状态胶囊：工单收进抽屉，海报墙保持干净 —— */}
        {(missing.length > 0 ||
          unidentified.length > 0 ||
          review.length > 0 ||
          importing > 0 ||
          refreshingMeta ||
          busy) && (
          <div className="mt-2.5 flex flex-wrap items-center gap-2">
            {busy && (
              <span className="flex items-center gap-1.5 rounded-full border border-[#7dd3fc]/35 bg-[#7dd3fc]/[0.12] px-3 py-1 text-[12px] font-semibold text-[#7dd3fc]">
                <span className="size-3 animate-spin rounded-full border-[1.5px] border-[#7dd3fc]/30 border-t-[#7dd3fc]" />
                {busyText(
                  library.scanning ? library.scan_progress : library.organize_progress,
                )}
              </span>
            )}
            {/* 刷新元数据的按钮已收进 ⋯ 菜单；它是全量重刷、耗时长，
                进度另用下方的面板完整展示（到哪部了、在做什么） */}
            {importing > 0 && (
              <span className="flex items-center gap-1.5 rounded-full border border-[#7dd3fc]/35 bg-[#7dd3fc]/[0.12] px-3 py-1 text-[12px] font-semibold text-[#7dd3fc]">
                <span className="size-1.5 animate-pulse rounded-full bg-[#7dd3fc]" />
                已发现 {importing} 个新文件 · 写入完成后自动入库
              </span>
            )}
            {missing.length > 0 && (
              <button
                type="button"
                onClick={() => setIssueTab("missing")}
                className="flex items-center gap-1.5 rounded-full border border-white/[0.14] bg-white/[0.06] px-3 py-1 text-[12px] font-semibold text-white/75 transition hover:bg-white/[0.12] hover:text-white"
              >
                <span className="size-1.5 rounded-full bg-white/40" />
                {missing.length} 个条目缺失
              </button>
            )}
            {unidentified.length > 0 && (
              <button
                type="button"
                onClick={() => setIssueTab("unidentified")}
                className="flex items-center gap-1.5 rounded-full border border-[#f5c451]/35 bg-[#f5c451]/[0.12] px-3 py-1 text-[12px] font-semibold text-[#f5c451] transition hover:bg-[#f5c451]/[0.22]"
              >
                <span className="size-1.5 rounded-full bg-[#f5c451]" />
                {unidentifiedFiles} 个文件待识别
                {unidentified.length > 1 ? ` · ${unidentified.length} 组` : ""}
              </button>
            )}
            {review.length > 0 && (
              <button
                type="button"
                onClick={() => setIssueTab("review")}
                className="flex items-center gap-1.5 rounded-full border border-[#c4b5fd]/35 bg-[#c4b5fd]/[0.12] px-3 py-1 text-[12px] font-semibold text-[#c4b5fd] transition hover:bg-[#c4b5fd]/[0.22]"
              >
                <span className="size-1.5 rounded-full bg-[#c4b5fd]" />
                {review.length} 个条目待复核身份
              </button>
            )}
          </div>
        )}
        {/* —— 整库刷新进度：全量重刷每部都要重下图，慢，状态给全 —— */}
        {refreshingMeta && metaRefresh && (
          <MetadataRefreshPanel
            state={metaRefresh}
            onStop={() => {
              setNotice(null);
              void stopLibraryMetadataRefresh(libraryId)
                .then(() =>
                  getMetadataRefreshProgress(libraryId)
                    .then(setMetaRefresh)
                    .catch(() => {}),
                )
                .catch((e) => setNotice((e as Error).message));
            }}
          />
        )}
        {failed && (
          <p className="mt-3 rounded-lg border border-amber-400/25 bg-amber-500/10 px-3.5 py-2 text-[12.5px] text-amber-200">
            与后端通信失败，正在自动重试；下方显示的是最近一次成功加载的数据
          </p>
        )}
        {notice && (
          <p className="mt-3 rounded-lg border border-red-400/25 bg-red-500/10 px-3.5 py-2 text-[12.5px] text-red-200">
            {notice}
          </p>
        )}
      </div>

      {/* —— 工单抽屉：缺失 / 待识别 / 身份复核 / 已忽略，从头部胶囊进入 —— */}
      <IssueDrawer
        open={issueTab}
        onClose={() => setIssueTab(null)}
        onSwitchTab={setIssueTab}
        libraryId={libraryId}
        missing={missing}
        unidentified={unidentified}
        review={review}
        ignored={ignored}
        movie={library.kind === "movie"}
        onChanged={reload}
      />

      {/* —— 库存海报墙 —— */}
      {items.length === 0 && unidentified.length === 0 ? (
        <p className="mt-16 text-center text-[13.5px] leading-7 text-[var(--text-muted)]">
          这个库还没有内容。
          <br />
          点右上角 ⋯ 里的「扫描库」把根路径下已有的影片识别入库；订阅的内容下载完成后也会自动进来。
        </p>
      ) : (
        <div className="mt-6 grid gap-x-4 gap-y-7 px-6 [grid-template-columns:repeat(auto-fill,minmax(148px,1fr))] max-md:mt-4 max-md:gap-x-3 max-md:gap-y-5 max-md:px-4 max-md:[grid-template-columns:repeat(auto-fill,minmax(140px,1fr))]">
          {displayItems.map((item) => (
            <InventoryCell
              key={item.media_item_id}
              item={item}
              libraryId={libraryId}
              workingLabel={
                refreshPhaseById.get(item.media_item_id) ??
                (probing && item.probe_pending_count > 0 ? "正在读取规格" : undefined)
              }
            />
          ))}
        </div>
      )}

      {/* —— 追踪中（订阅已指向本库、文件未落地）—— */}
      {pending.length > 0 && (
        <div className="mt-10 px-6 max-md:mt-7 max-md:px-4">
          <h3 className="text-on-image text-[15px] font-semibold text-white/85">
            追踪中
            <span className="ml-2 text-[12px] font-normal text-[var(--text-faint)]">
              已订阅、资源到位后自动入库
            </span>
          </h3>
          <div className="mt-4 grid gap-x-4 gap-y-7 [grid-template-columns:repeat(auto-fill,minmax(148px,1fr))] max-md:gap-x-3 max-md:gap-y-5 max-md:[grid-template-columns:repeat(auto-fill,minmax(140px,1fr))]">
            {pending.map((sub) => (
              <PendingCell key={sub.id} sub={sub} />
            ))}
          </div>
        </div>
      )}

      <LibraryFormDialog
        state={editing}
        onClose={() => setEditing(null)}
        onSaved={() => {
          setEditing(null);
          reload();
        }}
      />

      <LibraryOrganizeDialog
        library={organizeTarget}
        onClose={() => setOrganizeTarget(null)}
        onChanged={reload}
      />
    </div>
  );
}

/* —— 库操作折叠菜单：扫描库 / 整理文件名 / 刷新元数据 / 编辑库 全收进 ⋯，头部不再摆一排按钮。
 *  三个长任务在菜单里就能看进度并原地停止，运行状态另由头部胶囊常驻呈现。 */

interface LibraryActionsMenuProps {
  scanning: boolean;
  /** 扫描类任务的当前阶段；没在跑为 null——决定停止入口给不给 */
  scanPhase: ScanPhase | null;
  /** 扫描进度百分比；无进度信息时为 null */
  scanPercent: number | null;
  onToggleScan: () => void;
  organizing: boolean;
  /** 整理进度百分比；无进度信息时为 null */
  organizePercent: number | null;
  refreshingMeta: boolean;
  /** 元数据刷新进度文案 "已处理/总数"；无进度信息时为 null */
  metaProgress: string | null;
  /** 扫描或整理进行中：编辑库须锁定（任务正按当前根路径读写台账） */
  busy: boolean;
  onOrganize: () => void;
  onToggleMetaRefresh: () => void;
  onEdit: () => void;
}

function LibraryActionsMenu({
  scanning,
  scanPhase,
  scanPercent,
  onToggleScan,
  organizing,
  organizePercent,
  refreshingMeta,
  metaProgress,
  busy,
  onOrganize,
  onToggleMetaRefresh,
  onEdit,
}: LibraryActionsMenuProps) {
  // 与站点配置一致用 Radix DropdownMenu：Portal 到 body + 碰撞检测，
  // 不会被头部容器裁切；开合/外部点击/键盘导航全交给 Radix。
  const itemClass =
    "glass-row nav-item cursor-pointer px-3 py-2 text-[13px] font-medium outline-none " +
    "data-[highlighted]:!bg-[var(--glass-fill-hover)] data-[highlighted]:!text-[var(--text)] " +
    "data-[disabled]:pointer-events-none data-[disabled]:opacity-40";
  const running = scanning || organizing || refreshingMeta;
  // 重识别占的是同一把库级锁，但它在改身份锚、不接受中途停止（后端会
  // 拒绝）。菜单据此把入口置灰并如实标出在做什么，不给按了没反应的按钮
  const stoppable = scanning && scanPhase !== "reidentifying";

  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          aria-label="更多操作"
          // 长在顶栏里，与返回键并排：共用顶栏控件形状，不再是页面里的胶囊按钮
          className={`${PAGE_NAV_BUTTON_CLASS} relative data-[state=open]:bg-black/55 data-[state=open]:text-white`}
        >
          <MoreIcon className="size-[18px] max-md:size-[22px]" />
          {/* 收起的长任务在跑：触发按钮点一个小点，不至于被菜单藏住 */}
          {running && (
            <span className="absolute right-1 top-1 size-1.5 animate-pulse rounded-full bg-[#7dd3fc]" />
          )}
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={6}
          collisionPadding={12}
          className="menu-surface z-50 min-w-[11rem] !rounded-xl p-1"
        >
          {/* 扫描中切换为「停止扫描」（增量幂等：已入账的保留，剩余下次继续） */}
          <DropdownMenu.Item
            onSelect={onToggleScan}
            disabled={(busy && !scanning) || (scanning && !stoppable)}
            className={itemClass}
          >
            {!scanning
              ? "扫描库"
              : stoppable
                ? `停止扫描${scanPercent === null ? "" : ` ${scanPercent}%`}`
                : `${SCAN_PHASE_LABELS[scanPhase ?? "ingesting"]}…`}
          </DropdownMenu.Item>
          <DropdownMenu.Item
            onSelect={onOrganize}
            disabled={busy && !organizing}
            className={itemClass}
          >
            {organizing
              ? `整理中…${organizePercent === null ? "" : ` ${organizePercent}%`}`
              : "整理文件名"}
          </DropdownMenu.Item>
          <DropdownMenu.Item onSelect={onToggleMetaRefresh} className={itemClass}>
            {refreshingMeta
              ? `停止刷新${metaProgress === null ? "" : ` ${metaProgress}`}`
              : "刷新元数据"}
          </DropdownMenu.Item>
          <DropdownMenu.Item onSelect={onEdit} disabled={busy} className={itemClass}>
            编辑库
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

/**
 * 整库元数据刷新的进度面板。
 *
 * 全量重刷（每部片都重拉档案 + 重下图片 + 覆盖媒体目录）在大库上要跑很久，
 * 只给一个百分比等于让用户干等：这里把**到哪几部了、每部在做什么**都摊开，
 * 外加失败计数和停止入口。并发 3 路，所以 active 是列表。
 */
function MetadataRefreshPanel({
  state,
  onStop,
}: {
  state: MetadataRefreshProgress;
  onStop: () => void;
}) {
  const percent =
    state.total > 0 ? Math.min(100, Math.round((state.processed / state.total) * 100)) : 0;
  return (
    <div className="mt-3 rounded-xl border border-[#7dd3fc]/25 bg-[#7dd3fc]/[0.07] px-4 py-3">
      <div className="flex items-center justify-between gap-3">
        <div className="flex min-w-0 items-center gap-2 text-[12.5px] font-semibold text-[#7dd3fc]">
          <span className="size-3 shrink-0 animate-spin rounded-full border-[1.5px] border-[#7dd3fc]/30 border-t-[#7dd3fc]" />
          <span className="truncate">
            {state.stopping ? "正在停止刷新" : "正在刷新元数据"}
            {state.total > 0 ? ` ${state.processed}/${state.total}` : ""}
            {state.failed > 0 ? ` · 失败 ${state.failed}` : ""}
          </span>
        </div>
        <button
          type="button"
          onClick={onStop}
          disabled={state.stopping}
          className="shrink-0 text-[12px] font-medium text-white/70 transition hover:text-white disabled:opacity-50"
        >
          {state.stopping ? "收尾中…" : "停止"}
        </button>
      </div>
      {/* 进度条：全量刷新常以分钟计，一条能看出"在动"的进度很重要 */}
      <div className="mt-2 h-1 overflow-hidden rounded-full bg-white/[0.08]">
        <div
          className="h-full rounded-full bg-[#7dd3fc] transition-[width] duration-500"
          style={{ width: `${percent}%` }}
        />
      </div>
      {state.active.length > 0 && (
        <div className="mt-2.5 space-y-1">
          {state.active.map((a) => (
            <p key={a.media_item_id} className="flex gap-2 text-[12px] text-white/70">
              <span className="min-w-0 flex-1 truncate">{a.title}</span>
              <span className="shrink-0 text-white/45">{a.phase}</span>
            </p>
          ))}
        </div>
      )}
      <p className="mt-2 text-[11.5px] text-white/40">
        全量重刷：重拉 TMDB 档案、按当前尺寸重下图片、覆盖媒体目录镜像；
        你手动选定的图不受影响
      </p>
    </div>
  );
}

/** 库存格：真实拥有的作品。点击进**媒体库条目详情**（本地刮削信息 +
 *  片源规格 + 条目操作），不再复用发现页的 TMDB 详情；格下标注库存概况。 */
function InventoryCell({
  item,
  libraryId,
  workingLabel,
}: {
  item: LibraryItem;
  libraryId: number;
  /** 这一格正被后台处理（整库刷新的阶段 / 扫描补探）时的文案；不在处理为 undefined */
  workingLabel?: string;
}) {
  const visual: PosterVisualItem = {
    id: String(item.tmdb_id),
    source: "tmdb",
    type: item.kind,
    title: item.title,
    year: item.year ?? undefined,
    rating: 0,
    // 海报可能是本地刮削资产的相对路径（断网可用），也可能是 TMDB 图床地址
    posterUrl: imageUrl(item.poster_url),
  };
  // 格下只说规模与规格；单部占多大盘在这里没有决策价值，总大小看库头部
  const parts: string[] = [];
  if (item.kind === "tv" && item.seasons.length > 0) {
    parts.push(
      item.seasons.length === 1
        ? `第 ${item.seasons[0]} 季 · ${item.episode_count} 集`
        : `${item.seasons.length} 季 · ${item.episode_count} 集`,
    );
  }
  if (item.resolutions.length > 0) parts.push(item.resolutions.join("/"));
  // 文件全部缺失的"死条目"：海报置灰，一眼与在位内容区分
  const dead = item.file_count > 0 && item.missing_count >= item.file_count;
  // 库存墙里的东西本来就都在库里，「已入库」不值一行；只有缺失这类异常才点灯
  const abnormal = dead || item.missing_count > 0;
  if (dead) parts.splice(0, parts.length, "文件已全部缺失");
  else if (item.missing_count > 0) parts.push(`${item.missing_count} 个文件缺失`);
  return (
    <div>
      {/* 后台正在处理的那一格自己点亮：进度面板/胶囊列的是总数或片名，
          海报墙上也要能一眼看到"正在弄这部"，否则用户得在两处之间对片名 */}
      <div className="relative">
        <div className={dead ? "opacity-50 grayscale" : undefined}>
          <PosterCardVisual
            item={visual}
            href={`/library/${libraryId}/item/${item.media_item_id}` as Route}
            action={libraryCardAction(item)}
          />
        </div>
        {workingLabel && (
          <>
            <span className="pointer-events-none absolute inset-0 rounded-xl ring-2 ring-[#7dd3fc] ring-offset-0" />
            <span className="pointer-events-none absolute inset-x-0 bottom-0 flex items-center gap-1.5 rounded-b-xl bg-[rgba(7,12,20,0.82)] px-2 py-1.5 text-[10.5px] font-medium text-[#7dd3fc] backdrop-blur-sm">
              <span className="size-2.5 shrink-0 animate-spin rounded-full border-[1.5px] border-[#7dd3fc]/30 border-t-[#7dd3fc]" />
              <span className="truncate">{workingLabel}</span>
            </span>
          </>
        )}
      </div>
      {parts.length > 0 && (
        <p className="text-on-image mt-1.5 flex items-center gap-1.5 truncate text-[11px] text-[var(--text-muted)]">
          {abnormal && (
            <span
              className={`size-1.5 shrink-0 rounded-full ${dead ? "bg-white/30" : "bg-[#f5c451]"}`}
            />
          )}
          <span className="truncate">{parts.join(" · ")}</span>
        </p>
      )}
    </div>
  );
}

/** 追踪中格：订阅状态行沿用订阅页语言，点击进订阅详情。 */
function PendingCell({ sub }: { sub: Subscription }) {
  const meta = subscriptionStatusMeta[sub.status];
  const visual: PosterVisualItem = {
    id: String(sub.media.tmdb_id),
    source: "tmdb",
    title: sub.media.title,
    year: sub.media.year ?? undefined,
    rating: 0,
    posterUrl: sub.media.poster_url ? cachedImageUrl(sub.media.poster_url) : "",
  };
  return (
    <div className="opacity-80 transition hover:opacity-100">
      {/* 已是订阅产物，悬浮层不再给「订阅影片」重复入口 */}
      <PosterCardVisual item={visual} href={`/subscriptions/${sub.id}` as Route} action="none" />
      <p className="text-on-image mt-1.5 flex items-center gap-1.5 truncate text-[11px] text-[var(--text-muted)]">
        <span
          className="size-1.5 shrink-0 rounded-full"
          style={{ backgroundColor: meta.color }}
        />
        <span className="truncate">
          {meta.label} · {subscriptionProgressNote(sub)}
        </span>
      </p>
    </div>
  );
}

/* —— 工单抽屉：缺失 / 待识别 / 身份复核 / 已忽略，右侧滑出，海报墙不再被工单铺满 ——
   「已忽略」不是待办，是**已处理**的归档：默认不打扰，只在有内容时露出 tab，
   给识别器变强之后反悔的机会。 */

type IssueTab = "missing" | "unidentified" | "review" | "ignored";

function IssueDrawer({
  open,
  onClose,
  onSwitchTab,
  libraryId,
  missing,
  unidentified,
  review,
  ignored,
  movie,
  onChanged,
}: {
  open: IssueTab | null;
  onClose: () => void;
  onSwitchTab: (tab: IssueTab) => void;
  libraryId: number;
  missing: MissingItem[];
  unidentified: UnidentifiedGroup[];
  review: ReviewGroup[];
  ignored: UnidentifiedGroup[];
  movie: boolean;
  onChanged: () => void;
}) {
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);

  // 打开/切 tab 时清空过滤词；Escape 关闭
  useEffect(() => {
    setQuery("");
  }, [open]);
  useEffect(() => {
    if (open === null) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  if (open === null || typeof document === "undefined") return null;

  const keyword = query.trim().toLowerCase();
  const missingShown = keyword
    ? missing.filter((m) => m.title.toLowerCase().includes(keyword))
    : missing;
  const unidentifiedShown = keyword
    ? unidentified.filter(
        (g) =>
          g.label.toLowerCase().includes(keyword) ||
          g.files.some((f) => f.file_path.toLowerCase().includes(keyword)),
      )
    : unidentified;
  const reviewShown = keyword
    ? review.filter(
        (g) =>
          g.label.toLowerCase().includes(keyword) ||
          g.current.title.toLowerCase().includes(keyword) ||
          g.suggestion.title.toLowerCase().includes(keyword),
      )
    : review;
  const ignoredShown = keyword
    ? ignored.filter(
        (g) =>
          g.label.toLowerCase().includes(keyword) ||
          g.files.some((f) => f.file_path.toLowerCase().includes(keyword)),
      )
    : ignored;
  const missingFileTotal = missing.reduce((n, item) => n + item.files.length, 0);
  const unidentifiedFileTotal = unidentified.reduce((n, g) => n + g.file_count, 0);
  const ignoredFileTotal = ignored.reduce((n, g) => n + g.file_count, 0);
  // 「放错库了」的文件数：认领解决不了（TMDB 的 movie/tv 是两套 id 空间），
  // 得挪文件或删库重建，单独给一条修复引导
  const kindMismatchFiles = unidentified.reduce(
    (n, g) => (g.code === "kind_mismatch" ? n + g.file_count : n),
    0,
  );

  const tabClass = (active: boolean) =>
    `rounded-full px-3.5 py-1.5 text-[12.5px] font-semibold transition ${
      active ? "bg-white/[0.14] text-white" : "text-[var(--text-muted)] hover:text-white"
    }`;

  return createPortal(
    <div className="fixed inset-0 z-50" role="dialog" aria-modal="true" aria-label="库工单">
      <button
        type="button"
        aria-label="关闭"
        onClick={onClose}
        className="absolute inset-0 cursor-default bg-black/50 backdrop-blur-[2px]"
      />
      <div className="absolute right-0 top-0 flex h-full w-full max-w-[600px] flex-col border-l border-white/10 bg-[rgba(16,18,26,0.94)] shadow-[-24px_0_70px_rgba(0,0,0,0.55)] backdrop-blur-2xl max-md:max-w-none">
        {/* 头部：tab + 关闭 */}
        <div className="flex items-center justify-between gap-3 border-b border-white/[0.08] px-5 py-3.5">
          <div className="flex items-center gap-1.5">
            <button
              type="button"
              onClick={() => onSwitchTab("missing")}
              className={tabClass(open === "missing")}
            >
              缺失 {missing.length > 0 ? missing.length : ""}
            </button>
            <button
              type="button"
              onClick={() => onSwitchTab("unidentified")}
              className={tabClass(open === "unidentified")}
            >
              待识别 {unidentifiedFileTotal > 0 ? unidentifiedFileTotal : ""}
            </button>
            <button
              type="button"
              onClick={() => onSwitchTab("review")}
              className={tabClass(open === "review")}
            >
              身份复核 {review.length > 0 ? review.length : ""}
            </button>
            {/* 已忽略是归档不是待办：没有内容就不占位，除非正停在这个 tab 上 */}
            {(ignoredFileTotal > 0 || open === "ignored") && (
              <button
                type="button"
                onClick={() => onSwitchTab("ignored")}
                className={tabClass(open === "ignored")}
              >
                已忽略 {ignoredFileTotal > 0 ? ignoredFileTotal : ""}
              </button>
            )}
          </div>
          <button
            type="button"
            onClick={onClose}
            aria-label="关闭抽屉"
            className="btn-glass flex size-8 items-center justify-center !rounded-full"
          >
            <XIcon className="size-4" />
          </button>
        </div>

        {/* 工具行：过滤 + 批量操作 */}
        <div className="flex items-center gap-2.5 border-b border-white/[0.08] px-5 py-3">
          <input
            type="text"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder={open === "missing" ? "按片名过滤…" : "按文件名过滤…"}
            className="min-w-0 flex-1 rounded-lg border border-white/[0.08] bg-white/[0.04] px-3 py-1.5 text-[12.5px] text-[var(--text)] outline-none placeholder:text-white/30 focus:border-[var(--accent)]/60"
          />
          {open === "missing" && missing.length > 0 && (
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                if (
                  !window.confirm(
                    `清理全部 ${missingFileTotal} 条缺失记录？（只删台账，不动磁盘）`,
                  )
                )
                  return;
                setBusy(true);
                void clearMissing(libraryId)
                  .then(onChanged)
                  .catch(() => {})
                  .finally(() => setBusy(false));
              }}
              className="btn-glass shrink-0 px-3 py-1.5 text-[12px] font-medium disabled:opacity-50"
            >
              全部清理
            </button>
          )}
          {open === "unidentified" && unidentified.length > 0 && (
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                if (
                  !window.confirm(
                    `忽略全部 ${unidentifiedFileTotal} 个待识别文件？之后扫描不再过问它们（不动磁盘；可在「已忽略」里恢复）`,
                  )
                )
                  return;
                setBusy(true);
                void clearUnidentified(libraryId)
                  .then(onChanged)
                  .catch(() => {})
                  .finally(() => setBusy(false));
              }}
              className="btn-glass shrink-0 px-3 py-1.5 text-[12px] font-medium disabled:opacity-50"
            >
              全部忽略
            </button>
          )}
        </div>

        {/* 说明行 */}
        <p className="px-5 pt-3 text-[11.5px] leading-5 text-[var(--text-muted)]">
          {open === "missing"
            ? "文件已不在磁盘；「重新下载」交给订阅管线补回，「清理记录」只删台账（都不动磁盘）；文件回归会自动恢复。"
            : open === "unidentified"
              ? "同一目录的文件聚成一组，认领一次整组生效；点候选可先核对海报简介再确认。状态标签悬停看完整原因。"
              : open === "review"
                ? "识别器升级后重新核对了这些条目，新结论与现有身份不一致。身份没有被自动改动——由你拍板：采纳新结论改挂条目，或维持现状。拍板后不再提醒。"
                : "这些文件你选择过「忽略」，之后每次扫描都会直接跳过，不再占用待识别清单（磁盘文件一直都在）。识别器在持续变强，当初认不出的现在未必认不出——「恢复」即可让它重新参与识别。"}
        </p>

        {/* 「放错库了」修复引导：认领解决不了这一类，不给引导用户会在认领里打转 */}
        {open === "unidentified" && kindMismatchFiles > 0 && (
          <p className="mx-5 mt-3 rounded-lg bg-[#f5c451]/[0.1] px-3 py-2 text-[11.5px] leading-5 text-[#f5c451]">
            有 {kindMismatchFiles} 个文件的实际类型与本库不符（
            {movie ? "剧集文件在电影库" : "电影文件在剧集库"}
            ），认领无法解决。个别文件放错了：把文件移到对应类型的库即可；整库类型建错了：
            删除本库并以正确类型重建（删库不会动磁盘文件），重新扫描即可恢复。
          </p>
        )}

        {/* 列表区：独立滚动 */}
        <div className="scroll-thin min-h-0 flex-1 space-y-2 overflow-y-auto px-5 py-4">
          {open === "missing" &&
            missingShown.map((item) => (
              <MissingRow
                key={item.media_item_id}
                libraryId={libraryId}
                item={item}
                onChanged={onChanged}
              />
            ))}
          {open === "unidentified" &&
            unidentifiedShown.map((group) => (
              <UnidentifiedGroupRow
                key={group.key}
                group={group}
                movie={movie}
                onChanged={onChanged}
              />
            ))}
          {open === "review" &&
            reviewShown.map((group) => (
              <ReviewGroupRow key={group.key} group={group} onChanged={onChanged} />
            ))}
          {open === "ignored" &&
            ignoredShown.map((group) => (
              <IgnoredGroupRow key={group.key} group={group} onChanged={onChanged} />
            ))}
          {((open === "missing" && missingShown.length === 0) ||
            (open === "unidentified" && unidentifiedShown.length === 0) ||
            (open === "review" && reviewShown.length === 0) ||
            (open === "ignored" && ignoredShown.length === 0)) && (
            <p className="mt-12 text-center text-[13px] text-[var(--text-muted)]">
              {keyword
                ? "没有匹配的条目"
                : open === "ignored"
                  ? "没有忽略过的文件"
                  : "没有需要处理的了 🎉"}
            </p>
          )}
        </div>
      </div>
    </div>,
    document.body,
  );
}

function MissingRow({
  libraryId,
  item,
  onChanged,
}: {
  libraryId: number;
  item: MissingItem;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState<string | null>(null);

  const act = (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    void fn()
      .then(onChanged)
      .catch((e) => setError((e as Error).message))
      .finally(() => setBusy(false));
  };

  // 剧集：按季聚合缺失集数做摘要；电影：单文件
  const summary =
    item.kind === "tv"
      ? Array.from(
          item.files.reduce((m, f) => {
            m.set(f.season_number, (m.get(f.season_number) ?? 0) + 1);
            return m;
          }, new Map<number, number>()),
        )
          .sort(([a], [b]) => a - b)
          .map(([s, n]) => (s === 0 ? `特别篇 ${n} 集` : `第 ${s} 季缺 ${n} 集`))
          .join("、")
      : `${item.files.length} 个文件`;

  return (
    <div className="rounded-xl bg-white/[0.03] px-3.5 py-2.5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-white/85">
          {item.title}
          {item.year ? ` (${item.year})` : ""}
          <span className="ml-2 text-[12px] font-normal text-[var(--text-muted)]">{summary}</span>
        </span>
        {done ? (
          <span className="text-[12px] text-[#4ade80]">{done}</span>
        ) : (
          <span className="flex shrink-0 items-center gap-2">
            <button
              type="button"
              disabled={busy}
              onClick={() =>
                act(() =>
                  redownloadMissing(libraryId, item.media_item_id).then(({ requeued }) => {
                    setDone(`已交给订阅管线（${requeued} 个工单排队）`);
                  }),
                )
              }
              className="btn-accent rounded-full px-3 py-1.5 text-[12px] font-semibold disabled:opacity-50"
            >
              重新下载
            </button>
            <button
              type="button"
              disabled={busy}
              onClick={() => {
                const warning = item.subscription_id
                  ? `「${item.title}」有正在追踪的订阅，只清记录的话订阅可能把它重新下回来。仍要清理这 ${item.files.length} 条缺失记录？`
                  : `清理「${item.title}」的 ${item.files.length} 条缺失记录？（只删台账，不动磁盘）`;
                if (!window.confirm(warning)) return;
                act(() => clearMissing(libraryId, item.media_item_id));
              }}
              className="btn-glass px-3 py-1.5 text-[12px] font-medium disabled:opacity-50"
            >
              清理记录
            </button>
          </span>
        )}
      </div>
      {item.subscription_id && !done && (
        <p className="mt-1 text-[11.5px] text-[#f5c451]/80">该条目有订阅在追踪</p>
      )}
      {error && <p className="mt-1 text-[11.5px] text-red-300">{error}</p>}
    </div>
  );
}

/* —— 待识别组：整组认领或整组忽略 ——
   一部剧几十集是同一件事，逐集处理既刷屏又折磨人。认领一律走「详情确认
   面板」：点候选/搜索结果先看海报、简介、季数再确认——只看名字不足以在
   同名双版本（正片 46 集 vs 送审版 51 集）之间下判断。候选全不对时走
   TMDB 搜索（按片名检索、支持直接粘 ID），不再让用户手抄 TMDB ID。
   TMDB 真没有条目（no_match）也不是死路：卡片上写明「扫描会自动重试」
   并引导去 TMDB 社区补录——补录收录后回来搜索认领即可。 */

/** 进入确认面板的最小信息：来自候选 chip 或搜索结果，详情异步补全。 */
interface ClaimSeed {
  tmdbId: number;
  title: string;
  year?: number | null;
  posterUrl?: string;
  episodeCount?: number | null;
  reasons?: string[];
}

/** 组名 → 搜索词：去掉 [tmdbid=…] 等标记块与尾部年份括号。 */
function searchSeedFromLabel(label: string): string {
  return label
    .replace(/[[{][^\]}]*[\]}]/g, " ")
    .replace(/\((?:18|19|20)\d{2}\)\s*$/, "")
    .replace(/\s+/g, " ")
    .trim();
}

/* 失败分类 → 标签：整句原因塞进 title，清单上只留一个能扫读的短标签。
   配色只分两级——**黄色专供"系统故障、重扫可自愈"**（TMDB 不可达），
   它有确定解法、不需要用户动脑；其余是正常待办不是错误，用中性色，
   否则满屏黄字等于没有信号。 */
const UNIDENTIFIED_BADGE: Record<
  string,
  { label: (g: UnidentifiedGroup) => string; tone: "warn" | "muted" }
> = {
  tmdb_unreachable: { label: () => "TMDB 不可达", tone: "warn" },
  // 库类型选错也是"有确定解法但需要动手"的一类：认领单个文件没用，得换库
  kind_mismatch: { label: () => "放错库了", tone: "warn" },
  ambiguous: { label: (g) => `${g.candidates.length} 个候选待定`, tone: "muted" },
  no_match: { label: () => "TMDB 无匹配", tone: "muted" },
  unparsable: { label: () => "认不出片名", tone: "muted" },
};

/** 组卡片的次要动作（忽略 / 查看文件）：收进 ⋯，不跟主动作抢视线。 */
function GroupMoreMenu({
  busy,
  expanded,
  canExpand,
  onIgnore,
  onToggleFiles,
}: {
  busy: boolean;
  expanded: boolean;
  canExpand: boolean;
  onIgnore: () => void;
  onToggleFiles: () => void;
}) {
  const itemClass =
    "glass-row nav-item cursor-pointer px-3 py-2 text-[13px] font-medium outline-none " +
    "data-[highlighted]:!bg-[var(--glass-fill-hover)] data-[highlighted]:!text-[var(--text)] " +
    "data-[disabled]:pointer-events-none data-[disabled]:opacity-40";
  return (
    <DropdownMenu.Root>
      <DropdownMenu.Trigger asChild>
        <button
          type="button"
          aria-label="更多操作"
          // 常显但压暗：纯 hover 显示的控件在触屏上根本点不到（NAS 常用平板访问）
          className="btn-glass shrink-0 !rounded-full px-1.5 py-1 opacity-45 transition hover:opacity-100 group-hover/card:opacity-100 focus-visible:opacity-100 data-[state=open]:opacity-100"
        >
          <MoreIcon className="size-4" />
        </button>
      </DropdownMenu.Trigger>
      <DropdownMenu.Portal>
        <DropdownMenu.Content
          align="end"
          sideOffset={6}
          collisionPadding={12}
          // Escape 只关菜单：Radix 在 document 上处理，不拦住就会继续冒泡到
          // 抽屉挂在 window 的监听，把整个抽屉一起关掉（菜单照常关闭——
          // stopPropagation 不影响 Radix 自己的 dismiss）
          onEscapeKeyDown={(e) => e.stopPropagation()}
          className="menu-surface z-[60] min-w-[9rem] !rounded-xl p-1"
        >
          {canExpand && (
            <DropdownMenu.Item onSelect={onToggleFiles} className={itemClass}>
              {expanded ? "收起文件" : "查看文件"}
            </DropdownMenu.Item>
          )}
          <DropdownMenu.Item onSelect={onIgnore} disabled={busy} className={itemClass}>
            忽略
          </DropdownMenu.Item>
        </DropdownMenu.Content>
      </DropdownMenu.Portal>
    </DropdownMenu.Root>
  );
}

function StatusBadge({ group }: { group: UnidentifiedGroup }) {
  // 旧数据没有 code：退回按有无候选粗分，不至于空着
  const meta =
    (group.code && UNIDENTIFIED_BADGE[group.code]) ??
    (group.candidates.length > 0
      ? UNIDENTIFIED_BADGE.ambiguous
      : UNIDENTIFIED_BADGE.no_match);
  return (
    <span
      title={group.reason ?? undefined}
      className={`shrink-0 rounded-md px-1.5 py-0.5 text-[11px] ${
        meta.tone === "warn"
          ? "bg-[#f5c451]/[0.16] text-[#f5c451]"
          : "bg-white/[0.07] text-[var(--text-muted)]"
      }`}
    >
      {meta.label(group)}
    </span>
  );
}

function UnidentifiedGroupRow({
  group,
  movie,
  onChanged,
}: {
  group: UnidentifiedGroup;
  movie: boolean;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);
  // 卡片内联面板：确认（点候选/搜索结果后看详情再定）或搜索；同时只开一个
  const [panel, setPanel] = useState<
    { view: "confirm"; seed: ClaimSeed } | { view: "search" } | null
  >(null);

  const fileIds = group.files.map((f) => f.id);
  const act = (fn: () => Promise<unknown>) => {
    setBusy(true);
    setError(null);
    void fn()
      .then(onChanged)
      .catch((e) => setError((e as Error).message))
      .finally(() => setBusy(false));
  };

  const selectedId = panel?.view === "confirm" ? panel.seed.tmdbId : null;
  const unreachable = group.code === "tmdb_unreachable";
  const searchOpen = panel?.view === "search";

  const ignoreGroup = () => {
    if (
      group.file_count > 1 &&
      !window.confirm(
        `忽略「${group.label}」的全部 ${group.file_count} 个文件？之后扫描不再过问（不动磁盘；可在「已忽略」里恢复）。适合自录、花絮这类 TMDB 本就不会有条目的内容——只是暂时认不出的建议先搜索认领`,
      )
    )
      return;
    act(() => Promise.all(fileIds.map((id) => ignoreFile(id))));
  };

  return (
    <div className="group/card rounded-xl bg-white/[0.03] px-3.5 py-2.5">
      {/* 头行一行说完"是谁、什么状态、多大、能做什么"：整句原因收进标签的
          title，次要动作收进 ⋯ 菜单——组多起来时这一行就是全部扫读内容 */}
      <div className="flex items-center gap-2">
        <span
          className="min-w-0 flex-1 truncate text-[13px] font-medium text-white/85"
          title={`${group.label}\n${group.key}`}
        >
          {group.label}
        </span>
        <StatusBadge group={group} />
        <span className="shrink-0 text-[11px] text-[var(--text-faint)]">
          {group.file_count} 个 · {formatBytes(group.total_size_bytes)}
        </span>
        {/* TMDB 不可达有确定解法，直接给按钮；其余给搜索入口 */}
        {unreachable ? (
          <button
            type="button"
            disabled={busy}
            onClick={() => act(() => startLibraryScan(group.library_id))}
            className="btn-glass shrink-0 px-2.5 py-1 text-[12px] font-medium disabled:opacity-40"
          >
            重新扫描
          </button>
        ) : (
          <button
            type="button"
            disabled={busy}
            onClick={() => setPanel((p) => (p?.view === "search" ? null : { view: "search" }))}
            className={`shrink-0 rounded-full px-2.5 py-1 text-[12px] font-medium transition disabled:opacity-40 ${
              searchOpen ? "bg-white/[0.14] text-white" : "btn-glass"
            }`}
          >
            搜索
          </button>
        )}
        <GroupMoreMenu
          busy={busy}
          expanded={expanded}
          canExpand={group.file_count > 1}
          onIgnore={ignoreGroup}
          onToggleFiles={() => setExpanded((v) => !v)}
        />
      </div>

      {/* no_match 的两条真实出口写在卡片上：条目常常是晚几周才被社区补上
          （待识别行每次扫描本就自动重试），等不及可以自己去补录。不写清楚，
          用户会以为搜不到就只剩忽略一条路 */}
      {group.code === "no_match" && (
        <p className="mt-1 text-[11px] leading-relaxed text-[var(--text-faint)]">
          每次扫描会自动重试识别。TMDB 是社区维护的数据库，确认缺这个条目可以
          <a
            href={`https://www.themoviedb.org/${movie ? "movie" : "tv"}/new`}
            target="_blank"
            rel="noreferrer"
            className="underline decoration-white/20 underline-offset-2 transition hover:text-white/80"
          >
            去 TMDB 补录 ↗
          </a>
          ，收录后回来搜索认领；自录、花絮这类本就不会有条目的内容用「忽略」即可。
        </p>
      )}

      {/* 候选：机器给出的可能匹配。点击先进详情确认面板，不直接认领 */}
      {group.candidates.length > 0 && (
        <div className="mt-1.5 flex flex-wrap gap-1.5">
          {group.candidates.map((c) => (
            <button
              key={c.tmdb_id}
              type="button"
              disabled={busy}
              onClick={() =>
                setPanel(
                  selectedId === c.tmdb_id
                    ? null
                    : {
                        view: "confirm",
                        seed: {
                          tmdbId: c.tmdb_id,
                          title: c.title,
                          year: c.year,
                          episodeCount: c.episode_count,
                          reasons: c.reasons,
                        },
                      },
                )
              }
              className={`rounded-lg border px-2.5 py-1 text-[12px] transition disabled:opacity-40 ${
                selectedId === c.tmdb_id
                  ? "border-[var(--accent)] bg-[var(--accent)]/[0.28] text-white"
                  : "border-[var(--accent)]/40 bg-[var(--accent)]/[0.12] text-white/90 hover:bg-[var(--accent)]/[0.24]"
              }`}
            >
              {c.title}
              {c.year ? ` (${c.year})` : ""}
              {c.episode_count ? (
                <span className="ml-1.5 text-[11px] text-[var(--text-muted)]">
                  {c.episode_count} 集
                </span>
              ) : null}
            </button>
          ))}
        </div>
      )}

      {error && <p className="mt-1.5 text-[11.5px] text-red-300">{error}</p>}

      {panel?.view === "search" && (
        <ClaimSearchPanel
          movie={movie}
          initialQuery={searchSeedFromLabel(group.label)}
          onPick={(seed) => setPanel({ view: "confirm", seed })}
        />
      )}
      {panel?.view === "confirm" && (
        <ClaimConfirmPanel
          key={panel.seed.tmdbId}
          seed={panel.seed}
          movie={movie}
          fileCount={group.file_count}
          busy={busy}
          onConfirm={() => act(() => claimFilesBatch(fileIds, panel.seed.tmdbId))}
          onCancel={() => setPanel(null)}
        />
      )}

      {expanded && (
        <ul className="mt-2 space-y-1 border-t border-white/[0.06] pt-2">
          {group.files.map((f) => (
            <li
              key={f.id}
              className="truncate font-mono text-[11px] text-[var(--text-faint)]"
              title={f.file_path}
            >
              {f.file_path}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/* —— 身份复核组行：现身份 vs 建议身份并排对比，采纳或维持一键拍板 —— */

function ReviewGroupRow({ group, onChanged }: { group: ReviewGroup; onChanged: () => void }) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const act = (accept: boolean) => {
    setBusy(true);
    setError(null);
    void resolveIdentityReview(group.file_ids, accept)
      .then(onChanged)
      .catch((e) => setError((e as Error).message))
      .finally(() => setBusy(false));
  };

  const side = (info: ReviewGroup["current"], tone: "current" | "suggestion") => (
    <div className="flex min-w-0 flex-1 items-center gap-2.5">
      {info.poster_url ? (
        // eslint-disable-next-line @next/next/no-img-element
        <img
          src={cachedImageUrl(info.poster_url)}
          alt=""
          className="h-[54px] w-9 shrink-0 rounded-md object-cover"
        />
      ) : (
        <div className="h-[54px] w-9 shrink-0 rounded-md bg-white/[0.06]" />
      )}
      <div className="min-w-0">
        <p className="text-[10.5px] font-semibold uppercase tracking-wide text-[var(--text-faint)]">
          {tone === "current" ? "现身份" : "新识别结论"}
        </p>
        <p
          className={`truncate text-[13px] font-medium ${
            tone === "suggestion" ? "text-[#c4b5fd]" : "text-white/85"
          }`}
          title={info.title}
        >
          {info.title}
          {info.year ? ` (${info.year})` : ""}
        </p>
        {info.tmdb_id != null && (
          <p className="text-[11px] text-[var(--text-faint)]">tmdb {info.tmdb_id}</p>
        )}
      </div>
    </div>
  );

  return (
    <div className="rounded-xl bg-white/[0.03] px-3.5 py-3">
      <div className="flex items-baseline gap-2">
        <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-white/85" title={group.key}>
          {group.label}
        </span>
        <span className="shrink-0 text-[11px] text-[var(--text-faint)]">
          {group.file_count} 个文件 · {formatBytes(group.total_size_bytes)}
        </span>
      </div>

      {/* 两个身份并排：看清是谁 vs 该是谁 */}
      <div className="mt-2.5 flex items-center gap-3">
        {side(group.current, "current")}
        <span className="shrink-0 text-[15px] text-[var(--text-faint)]">→</span>
        {side(group.suggestion, "suggestion")}
      </div>

      <div className="mt-2.5 flex flex-wrap items-center gap-2">
        <button
          type="button"
          disabled={busy}
          onClick={() => act(true)}
          className="rounded-full border border-[#c4b5fd]/45 bg-[#c4b5fd]/[0.14] px-3.5 py-1.5 text-[12px] font-semibold text-[#c4b5fd] transition hover:bg-[#c4b5fd]/[0.26] disabled:opacity-40"
        >
          采纳新结论
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={() => act(false)}
          className="btn-glass px-3.5 py-1.5 text-[12px] font-medium disabled:opacity-40"
        >
          维持现状
        </button>
        {error && <span className="text-[11.5px] text-red-300">{error}</span>}
      </div>
    </div>
  );
}

/* —— 已忽略组：只有一个动作「恢复」——归档视图不该再摆认领/搜索那一套 —— */

function IgnoredGroupRow({
  group,
  onChanged,
}: {
  group: UnidentifiedGroup;
  onChanged: () => void;
}) {
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [expanded, setExpanded] = useState(false);

  return (
    <div className="rounded-xl bg-white/[0.02] px-3.5 py-2.5">
      <div className="flex flex-wrap items-center gap-x-3 gap-y-1.5">
        <span
          className="min-w-0 flex-1 truncate text-[13px] text-white/70"
          title={group.key}
        >
          {group.label}
        </span>
        <span className="shrink-0 text-[11px] text-[var(--text-faint)]">
          {group.file_count} 个文件 · {formatBytes(group.total_size_bytes)}
        </span>
        <button
          type="button"
          disabled={busy}
          onClick={() => {
            setBusy(true);
            setError(null);
            void restoreIgnored(group.files.map((f) => f.id))
              .then(onChanged)
              .catch((e) => setError((e as Error).message))
              .finally(() => setBusy(false));
          }}
          className="btn-glass shrink-0 px-3 py-1.5 text-[12px] font-medium disabled:opacity-40"
        >
          恢复
        </button>
        {group.file_count > 1 && (
          <button
            type="button"
            onClick={() => setExpanded((v) => !v)}
            className="text-[11.5px] text-[var(--text-muted)] transition hover:text-white/80"
          >
            {expanded ? "收起文件" : "查看文件"}
          </button>
        )}
      </div>
      {error && <p className="mt-1 text-[11.5px] text-red-300">{error}</p>}
      {expanded && (
        <ul className="mt-2 space-y-1 border-t border-white/[0.06] pt-2">
          {group.files.map((f) => (
            <li
              key={f.id}
              className="truncate font-mono text-[11px] text-[var(--text-faint)]"
              title={f.file_path}
            >
              {f.file_path}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

/* —— 认领确认面板：海报 + 简介 + 季数 + 演职员，看清楚是谁再点认领 —— */

function ClaimConfirmPanel({
  seed,
  movie,
  fileCount,
  busy,
  onConfirm,
  onCancel,
}: {
  seed: ClaimSeed;
  movie: boolean;
  fileCount: number;
  busy: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}) {
  const [detail, setDetail] = useState<MediaDetailData | null>(null);
  const [failed, setFailed] = useState(false);
  const kind = movie ? "movie" : "tv";

  useEffect(() => {
    let cancelled = false;
    setDetail(null);
    setFailed(false);
    fetchMediaDetail(kind, String(seed.tmdbId))
      .then((d) => {
        if (!cancelled) setDetail(d);
      })
      .catch(() => {
        if (!cancelled) setFailed(true);
      });
    return () => {
      cancelled = true;
    };
  }, [kind, seed.tmdbId]);

  const item = detail?.item;
  const posterUrl = item?.posterUrl || seed.posterUrl;
  const title = item?.title ?? seed.title;
  const year = item?.year ?? seed.year;
  const credits = detail
    ? [
        detail.info.directors.length > 0 ? `导演 ${detail.info.directors.slice(0, 2).join(" / ")}` : "",
        // cast 是 MediaCastMember 对象数组，取 name 拼接；直接 join 会渲染成 [object Object]
        detail.info.cast.length > 0
          ? `主演 ${detail.info.cast
              .slice(0, 4)
              .map((member) => member.name)
              .join(" / ")}`
          : "",
      ]
        .filter(Boolean)
        .join(" · ")
    : "";

  return (
    <div className="mt-2.5 rounded-xl border border-white/[0.08] bg-white/[0.03] p-3">
      <div className="flex gap-3">
        {posterUrl ? (
          <img
            src={posterUrl}
            alt={title}
            className="h-[104px] w-[70px] shrink-0 rounded-lg bg-white/[0.05] object-cover"
          />
        ) : (
          <div className="h-[104px] w-[70px] shrink-0 rounded-lg bg-white/[0.05]" />
        )}
        <div className="min-w-0 flex-1">
          <div className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
            <span className="text-[13.5px] font-semibold text-white/95">{title}</span>
            {year ? <span className="text-[12px] text-[var(--text-muted)]">{year}</span> : null}
            {item?.extent ? (
              <span className="text-[12px] text-[var(--text-muted)]">{item.extent}</span>
            ) : null}
            {seed.episodeCount ? (
              <span className="text-[12px] text-[var(--text-muted)]">
                对应季 {seed.episodeCount} 集
              </span>
            ) : null}
            {item && item.rating > 0 ? (
              <span className="text-[12px] text-[#f5c451]">★ {item.rating.toFixed(1)}</span>
            ) : null}
          </div>
          {seed.reasons && seed.reasons.length > 0 && (
            <p className="mt-0.5 text-[11px] text-[var(--text-faint)]">
              与本地文件的佐证：{seed.reasons.join("、")}
            </p>
          )}
          {item?.overview ? (
            <p className="mt-1 line-clamp-3 text-[11.5px] leading-[1.55] text-[var(--text-muted)]">
              {item.overview}
            </p>
          ) : failed ? (
            <p className="mt-1 text-[11.5px] text-[var(--text-faint)]">
              详情加载失败（不影响认领，可打开 TMDB 页面核对）
            </p>
          ) : (
            <p className="mt-1 text-[11.5px] text-[var(--text-faint)]">正在加载详情…</p>
          )}
          {credits && (
            <p className="mt-1 truncate text-[11px] text-[var(--text-faint)]" title={credits}>
              {credits}
            </p>
          )}
        </div>
      </div>
      <div className="mt-2.5 flex items-center gap-3 border-t border-white/[0.06] pt-2.5">
        <a
          href={`https://www.themoviedb.org/${kind}/${seed.tmdbId}`}
          target="_blank"
          rel="noreferrer"
          className="text-[11.5px] text-[var(--text-muted)] underline decoration-white/20 underline-offset-2 transition hover:text-white/80"
        >
          在 TMDB 打开核对 ↗
        </a>
        <span className="flex-1" />
        {movie && fileCount > 1 && (
          <span className="text-[11px] text-[var(--text-faint)]">整组视为同一部片的多个版本</span>
        )}
        <button type="button" disabled={busy} onClick={onCancel} className="btn-glass px-3 py-1.5 text-[12px] font-medium disabled:opacity-40">
          取消
        </button>
        <button
          type="button"
          disabled={busy}
          onClick={onConfirm}
          className="btn-accent rounded-full px-3.5 py-1.5 text-[12px] font-semibold disabled:opacity-40"
        >
          认领{fileCount > 1 ? `全部 ${fileCount} 个文件` : ""}
        </button>
      </div>
    </div>
  );
}

/* —— TMDB 搜索面板：候选全不对时按片名自己检索（比手抄 TMDB ID 体面）。
   输入预填从组名剥出的片名；直接粘一串数字则按 ID 认领。 —— */

function ClaimSearchPanel({
  movie,
  initialQuery,
  onPick,
}: {
  movie: boolean;
  initialQuery: string;
  onPick: (seed: ClaimSeed) => void;
}) {
  const [query, setQuery] = useState(initialQuery);
  const [results, setResults] = useState<MediaSearchItem[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const kind = movie ? "movie" : "tv";
  const idOnly = /^\d{1,10}$/.test(query.trim());

  const search = () => {
    const q = query.trim();
    if (!q || searching || idOnly) return;
    setSearching(true);
    setError(null);
    searchTmdbMedia(q)
      // multi 搜索混排两种类型，认领只关心本库的类型
      .then((items) => setResults(items.filter((it) => it.type === kind)))
      .catch((e) => {
        setResults(null);
        setError((e as Error).message);
      })
      .finally(() => setSearching(false));
  };

  return (
    <div className="mt-2.5 rounded-xl border border-white/[0.08] bg-white/[0.03] p-3">
      <div className="flex items-center gap-2">
        <input
          type="text"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") search();
          }}
          placeholder="片名关键词，或直接粘 TMDB ID"
          autoFocus
          className="min-w-0 flex-1 rounded-lg border border-white/[0.08] bg-white/[0.04] px-3 py-1.5 text-[12.5px] text-[var(--text)] outline-none placeholder:text-white/30 focus:border-[var(--accent)]/60"
        />
        {idOnly ? (
          <button
            type="button"
            onClick={() => onPick({ tmdbId: Number(query.trim()), title: `TMDB #${query.trim()}` })}
            className="btn-accent shrink-0 rounded-full px-3.5 py-1.5 text-[12px] font-semibold"
          >
            查看该 ID
          </button>
        ) : (
          <button
            type="button"
            disabled={searching || !query.trim()}
            onClick={search}
            className="btn-glass shrink-0 px-3.5 py-1.5 text-[12px] font-medium disabled:opacity-40"
          >
            {searching ? "搜索中…" : "搜索"}
          </button>
        )}
      </div>

      {error && <p className="mt-2 text-[11.5px] text-red-300">{error}</p>}
      {results !== null && results.length === 0 && !error && (
        <p className="mt-2 text-[11.5px] leading-relaxed text-[var(--text-muted)]">
          没有找到{movie ? "电影" : "剧集"}结果，换个关键词试试（TMDB 对中文名支持有限时可试英文/原名）。
          确认 TMDB 没收录的话，可以
          <a
            href={`https://www.themoviedb.org/${movie ? "movie" : "tv"}/new`}
            target="_blank"
            rel="noreferrer"
            className="underline decoration-white/20 underline-offset-2 transition hover:text-white/80"
          >
            去 TMDB 补录该条目 ↗
          </a>
          ——它是维基式社区库，收录后回来搜索或粘 ID 即可认领
        </p>
      )}
      {results !== null && results.length > 0 && (
        <ul className="mt-2 max-h-72 space-y-1 overflow-y-auto scroll-thin">
          {results.map((it) => (
            <li key={it.id}>
              <button
                type="button"
                onClick={() =>
                  onPick({
                    tmdbId: Number(it.id),
                    title: it.title,
                    year: it.year ?? null,
                    posterUrl: it.posterUrl || undefined,
                  })
                }
                className="flex w-full items-center gap-2.5 rounded-lg px-2 py-1.5 text-left transition hover:bg-white/[0.06]"
              >
                {it.posterUrl ? (
                          <img
                    src={it.posterUrl}
                    alt={it.title}
                    className="h-12 w-8 shrink-0 rounded bg-white/[0.05] object-cover"
                  />
                ) : (
                  <div className="h-12 w-8 shrink-0 rounded bg-white/[0.05]" />
                )}
                <span className="min-w-0 flex-1 truncate text-[12.5px] text-white/90">
                  {it.title}
                  {it.year ? (
                    <span className="ml-1.5 text-[11.5px] text-[var(--text-muted)]">{it.year}</span>
                  ) : null}
                </span>
                {it.rating > 0 && (
                  <span className="shrink-0 text-[11px] text-[var(--text-faint)]">
                    ★ {it.rating.toFixed(1)}
                  </span>
                )}
              </button>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

function CenteredNote({ children }: { children: React.ReactNode }) {
  return (
    <div className="mt-16 flex flex-col items-center gap-3 text-center">{children}</div>
  );
}
