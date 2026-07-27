"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import Link from "next/link";

import { searchTmdbMedia, type MediaSearchItem } from "@/lib/api/discover";
import {
  getDispatchPreview,
  getPipelineHealth,
  listRuleSets,
  type DispatchPreview,
  type LibraryPipeline,
  type PipelineCheck,
  type PipelineHealth,
  type RuleSet,
} from "@/lib/api/subscriptions";

/**
 * 订阅设定（设置 → 订阅）：订阅相关配置的家。
 *
 * 三个住户：
 * - 链路体检：把「资源搜索 → 下载器 → 投递 → 转移 → 入库」画成流水线，
 *   逐段陈述事实、红黄项给修复去处。全新用户看到的是同一份数据的另一种
 *   读法——开局清单（缺什么、下一步做什么）；
 * - 模拟一单：搜一部片，把路由选库/投递落点/入库方式完整预演一遍，
 *   不真正订阅——让用户主动验证自己对系统的理解；
 * - 规则组：只读清单（在订阅弹窗中选用；编辑器待独立设计，不做半成品）。
 *
 * 体检只读、绝不成为第三个配置入口：修复动作全部跳回原配置页。
 */
export function SubscriptionSettingsSection() {
  return (
    <div className="space-y-10">
      <PipelineHealthPanel />
      <SimulatePanel />
      <RuleSetsPanel />
    </div>
  );
}

/** 状态 → 颜色/文案（节点、检查项、库行共用一套语义）。 */
const STATUS_META = {
  ok: { dot: "bg-[#4ade80]", text: "text-[#4ade80]", label: "正常" },
  warn: { dot: "bg-amber-400", text: "text-amber-300", label: "降级" },
  error: { dot: "bg-[#ff6b6b]", text: "text-[#ff6b6b]", label: "有问题" },
} as const;

type Status = keyof typeof STATUS_META;

/** 修复去处 → 跳转地址与文案。 */
function fixTarget(section: string | null): { href: string; label: string } | null {
  if (section === "sites") return { href: "/settings/sites", label: "去接入站点" };
  if (section === "downloaders") return { href: "/settings/downloaders", label: "去下载器设置" };
  if (section === "import-watch") return { href: "/settings/import-watch", label: "去监听导入" };
  if (section === "libraries") return { href: "/library", label: "去媒体库" };
  return null;
}

function worst(statuses: Status[]): Status {
  if (statuses.includes("error")) return "error";
  if (statuses.includes("warn")) return "warn";
  return "ok";
}

// ---------------------------------------------------------------------------
// 链路体检
// ---------------------------------------------------------------------------

function PipelineHealthPanel() {
  const [health, setHealth] = useState<PipelineHealth | null>(null);
  const [failed, setFailed] = useState(false);
  const [busy, setBusy] = useState(false);

  const reload = useCallback(() => {
    setFailed(false);
    setBusy(true);
    getPipelineHealth()
      .then(setHealth)
      .catch(() => setFailed(true))
      .finally(() => setBusy(false));
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  // 修完配置回到本页自动重检：修复 → 回来 → 变绿的反馈闭环
  useEffect(() => {
    const onVisible = () => {
      if (document.visibilityState === "visible") reload();
    };
    document.addEventListener("visibilitychange", onVisible);
    window.addEventListener("focus", onVisible);
    return () => {
      document.removeEventListener("visibilitychange", onVisible);
      window.removeEventListener("focus", onVisible);
    };
  }, [reload]);

  // 开局清单条件：三个必要件（站点/下载器/媒体库）任一缺失
  const setupNeeded =
    health !== null &&
    (health.site_check.status === "error" ||
      !health.downloader_ok ||
      health.libraries.length === 0);

  return (
    <section>
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-[14px] font-semibold text-white/90">订阅链路体检</h3>
        <button
          type="button"
          onClick={reload}
          disabled={busy}
          className="btn-glass flex items-center gap-1.5 px-3 py-1.5 text-[12px] font-medium disabled:opacity-60"
        >
          {busy && (
            <span className="size-3 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
          )}
          重新体检
        </button>
      </div>
      <p className="mb-4 text-[12.5px] leading-6 text-[var(--text-muted)]">
        逐库预演「资源搜索 → 下载 → 投递 → 入库」的完整链路，与真实投递同一套判定。
        红点表示订阅会卡在那一步（工单不会丢，修好后自动重试）；黄点能转但有降级。
        修改配置后回到本页会自动重检。
      </p>

      {failed && (
        <p className="rounded-xl bg-white/[0.03] px-4 py-5 text-center text-[13px] text-[var(--text-muted)]">
          体检加载失败，请重试
        </p>
      )}
      {health === null && !failed && (
        <p className="flex items-center gap-2.5 rounded-xl bg-white/[0.03] px-4 py-5 text-[13px] text-[var(--text-muted)]">
          <span className="size-4 animate-spin rounded-full border-2 border-white/20 border-t-white/70" />
          正在体检…
        </p>
      )}

      {health !== null && setupNeeded && <SetupChecklist health={health} />}

      {health !== null && !setupNeeded && (
        <div className="space-y-3">
          {/* 公共段：资源搜索 + 下载器（所有库共享，不逐库重复） */}
          <SharedSegmentsRow health={health} />
          {health.libraries.map((pipeline) => (
            <LibraryPipelineCard key={pipeline.library_id} pipeline={pipeline} />
          ))}
        </div>
      )}
    </section>
  );
}

/**
 * 开局清单：三个必要件缺任一时，体检数据换一种读法——"下一步做什么"。
 * 全部配齐后本清单自动让位给正常体检，同一页面从第一天用到第一百天。
 */
function SetupChecklist({ health }: { health: PipelineHealth }) {
  const steps: { done: boolean; label: string; hint: string; href: string; action: string }[] = [
    {
      done: health.site_check.status === "ok",
      label: "接入资源站点",
      hint: "订阅从这里搜索资源",
      href: "/settings/sites",
      action: "去接入",
    },
    {
      done: health.downloader_ok,
      label: "接入下载器",
      hint: "qBittorrent / Transmission，找到的资源交给它下载",
      href: "/settings/downloaders",
      action: "去接入",
    },
    {
      done: health.libraries.length > 0,
      label: "创建媒体库",
      hint: "内容的家：下载完成后按「标题 (年份)」整理进库",
      href: "/library",
      action: "去创建",
    },
  ];
  return (
    <div className="rounded-xl border border-white/[0.08] bg-white/[0.04] p-4">
      <p className="mb-3 text-[13px] font-medium text-white/90">
        把订阅跑起来需要三步（完成后这里会变成链路体检）：
      </p>
      <div className="space-y-2">
        {steps.map((step, i) => (
          <div key={step.label} className="flex items-center gap-3">
            <span
              className={`flex size-5 shrink-0 items-center justify-center rounded-full text-[11px] font-semibold ${
                step.done ? "bg-[#4ade80]/20 text-[#4ade80]" : "bg-white/10 text-white/70"
              }`}
            >
              {step.done ? "✓" : i + 1}
            </span>
            <span className="min-w-0 flex-1">
              <span
                className={`text-[13px] font-medium ${step.done ? "text-white/50 line-through" : "text-white/90"}`}
              >
                {step.label}
              </span>
              <span className="ml-2 text-[11.5px] text-[var(--text-faint)]">{step.hint}</span>
            </span>
            {!step.done && (
              <Link
                href={step.href as never}
                className="btn-glass shrink-0 px-3 py-1.5 text-[12px] font-medium"
              >
                {step.action}
              </Link>
            )}
          </div>
        ))}
      </div>
      <p className="mt-3 text-[11.5px] leading-relaxed text-[var(--text-faint)]">
        可选第四步：「监听导入」让下载区与媒体库分离（PT 保种推荐）——下载器落盘的
        内容自动硬链接进库，源文件继续做种。不配置则直接下载进库根，同样能自动入账。
      </p>
    </div>
  );
}

/** 流水线上的一个节点（步骤条渲染单元）。 */
interface FlowNode {
  key: string;
  label: string;
  sub: string | null; // 节点下的一行小字（下载器名/路径等）
  status: Status;
  checks: PipelineCheck[]; // 该节点聚合的检查项（红黄项列在卡片下方）
}

/** 步骤条：节点胶囊 + 连接线，红黄节点自然显眼。 */
function FlowStepper({ nodes }: { nodes: FlowNode[] }) {
  return (
    <div className="flex flex-wrap items-center gap-y-2">
      {nodes.map((node, i) => (
        <div key={node.key} className="flex items-center">
          {i > 0 && <span className="mx-1.5 h-px w-4 shrink-0 bg-white/20" />}
          <div
            className={`flex items-center gap-2 rounded-lg border px-2.5 py-1.5 ${
              node.status === "ok"
                ? "border-white/[0.08] bg-white/[0.03]"
                : node.status === "warn"
                  ? "border-amber-400/30 bg-amber-500/10"
                  : "border-[#ff6b6b]/35 bg-[#ff6b6b]/10"
            }`}
          >
            <span className={`size-1.5 shrink-0 rounded-full ${STATUS_META[node.status].dot}`} />
            <span className="text-[12px] font-medium text-white/90">{node.label}</span>
            {node.sub && (
              <span
                dir="rtl"
                className="max-w-[180px] truncate font-mono text-[11px] text-[var(--text-faint)]"
                title={node.sub}
              >
                {"‎" + node.sub + "‎"}
              </span>
            )}
          </div>
        </div>
      ))}
    </div>
  );
}

/** 公共段（资源搜索 + 下载器）：所有库共享，单独一行不逐库重复。 */
function SharedSegmentsRow({ health }: { health: PipelineHealth }) {
  const downloaderCheck = health.libraries[0]?.checks.find((c) => c.key === "downloader");
  const nodes: FlowNode[] = [
    {
      key: "sites",
      label: "资源搜索",
      sub: null,
      status: health.site_check.status,
      checks: [health.site_check],
    },
    {
      key: "downloader",
      label: "下载器",
      sub: null,
      status: downloaderCheck?.status ?? (health.downloader_ok ? "ok" : "error"),
      checks: downloaderCheck ? [downloaderCheck] : [],
    },
  ];
  const problems = nodes.flatMap((n) => n.checks).filter((c) => c.status !== "ok");
  return (
    <div className="rounded-xl border border-white/[0.08] bg-white/[0.04] px-4 py-3">
      <div className="flex items-center gap-3">
        <span className="w-24 shrink-0 text-[12px] text-[var(--text-faint)] max-md:hidden">
          公共链路
        </span>
        <FlowStepper nodes={nodes} />
      </div>
      {problems.length > 0 && <ProblemList checks={problems} />}
    </div>
  );
}

/** 一个库的流水线卡片：步骤条 + 红黄项详情（全绿则只有一行）。 */
function LibraryPipelineCard({ pipeline }: { pipeline: LibraryPipeline }) {
  const [showAll, setShowAll] = useState(false);

  const byKey = (keys: string[]) => pipeline.checks.filter((c) => keys.includes(c.key));
  const dispatchChecks = byKey(["dispatch_dir", "mapping"]);
  const transferChecks = byKey(["transfer_disk", "watch_active"]);
  const ingestError = pipeline.checks.some(
    (c) => c.key === "dispatch_dir" && c.fix_section === "libraries" && c.status !== "ok",
  );

  const nodes: FlowNode[] = [
    {
      key: "dispatch",
      label: pipeline.mode === "watch" ? "投递到监听目录" : "投递",
      sub: pipeline.path,
      status: worst(dispatchChecks.map((c) => c.status)),
      checks: dispatchChecks,
    },
  ];
  if (pipeline.mode === "watch") {
    nodes.push({
      key: "transfer",
      label: "转移进库",
      sub: null,
      status: worst(transferChecks.map((c) => c.status)),
      checks: transferChecks,
    });
  }
  nodes.push({
    key: "ingest",
    label: "入库",
    sub: pipeline.library_root,
    status: ingestError ? "error" : "ok",
    checks: [],
  });

  // 卡片下方默认只列红黄项；「查看全部」展开每一段的事实陈述
  const problems = pipeline.checks.filter((c) => c.status !== "ok");
  const listed = showAll ? pipeline.checks.filter((c) => c.key !== "downloader") : problems;
  const meta = STATUS_META[pipeline.status];

  return (
    <div className="rounded-xl border border-white/[0.08] bg-white/[0.04] px-4 py-3">
      <div className="flex items-center gap-3">
        <span className="w-24 shrink-0 max-md:hidden">
          <span className="block truncate text-[13px] font-medium text-white/90">
            {pipeline.library_name}
          </span>
          <span className="block text-[11px] text-[var(--text-faint)]">
            {pipeline.kind === "movie" ? "电影" : "剧集"}
            {pipeline.is_default ? " · 默认" : ""}
          </span>
        </span>
        <div className="min-w-0 flex-1">
          <p className="mb-1.5 text-[12.5px] font-medium text-white/90 md:hidden">
            {pipeline.library_name}
          </p>
          <FlowStepper nodes={nodes} />
        </div>
        <div className="flex shrink-0 flex-col items-end gap-1">
          <span className={`text-[12px] font-medium ${meta.text}`}>{meta.label}</span>
          <button
            type="button"
            onClick={() => setShowAll((v) => !v)}
            className="text-[11px] text-[var(--text-faint)] hover:text-white"
          >
            {showAll ? "收起" : "查看全部"}
          </button>
        </div>
      </div>
      {listed.length > 0 && <ProblemList checks={listed} />}
    </div>
  );
}

/** 检查项明细列表（红黄项默认展示；「查看全部」时含绿项）。 */
function ProblemList({ checks }: { checks: PipelineCheck[] }) {
  return (
    <div className="mt-2.5 space-y-1.5 border-t border-white/[0.06] pt-2.5">
      {checks.map((check) => {
        const fix = check.status !== "ok" ? fixTarget(check.fix_section) : null;
        return (
          <div key={check.key + check.detail} className="flex items-start gap-2.5">
            <span
              className={`mt-[5px] size-1.5 shrink-0 rounded-full ${STATUS_META[check.status].dot}`}
            />
            <p className="min-w-0 flex-1 text-[12px] leading-relaxed text-[var(--text-muted)]">
              <span className="font-medium text-[var(--text)]">{check.label}</span>
              <span className="ml-2">{check.detail}</span>
              {fix && (
                <Link
                  href={fix.href as never}
                  className="ml-2 whitespace-nowrap font-medium text-[var(--accent)] hover:underline"
                >
                  {fix.label} →
                </Link>
              )}
            </p>
          </div>
        );
      })}
    </div>
  );
}

// ---------------------------------------------------------------------------
// 模拟一单：搜一部片，把完整决策过程演一遍（不真正订阅）
// ---------------------------------------------------------------------------

function SimulatePanel() {
  const [query, setQuery] = useState("");
  const [candidates, setCandidates] = useState<MediaSearchItem[] | null>(null);
  const [searching, setSearching] = useState(false);
  const [picked, setPicked] = useState<MediaSearchItem | null>(null);
  const [preview, setPreview] = useState<DispatchPreview | null>(null);
  const timer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // 输入去抖搜索：只取带类型的 TMDB 条目（路由需要 movie/tv 先验）
  useEffect(() => {
    if (timer.current) clearTimeout(timer.current);
    setPicked(null);
    setPreview(null);
    if (query.trim().length < 2) {
      setCandidates(null);
      return;
    }
    timer.current = setTimeout(() => {
      setSearching(true);
      searchTmdbMedia(query.trim())
        .then((items) => setCandidates(items.filter((it) => it.type).slice(0, 6)))
        .catch(() => setCandidates([]))
        .finally(() => setSearching(false));
    }, 400);
    return () => {
      if (timer.current) clearTimeout(timer.current);
    };
  }, [query]);

  const pick = (item: MediaSearchItem) => {
    setPicked(item);
    setPreview(null);
    if (!item.type) return;
    void getDispatchPreview(item.type, null, Number(item.id))
      .then(setPreview)
      .catch(() => setPreview(null));
  };

  return (
    <section>
      <h3 className="mb-2 text-[14px] font-semibold text-white/90">模拟一单</h3>
      <p className="mb-3 text-[12.5px] leading-6 text-[var(--text-muted)]">
        搜一部片，看它订阅后会进哪个库、投递到哪、怎么入库——只做预演，不会真的订阅。
        配完收藏范围想确认「某类片会进哪」，在这里试一下就知道。
      </p>
      <input
        type="text"
        value={query}
        onChange={(e) => setQuery(e.target.value)}
        placeholder="输入片名，如：葬送的芙莉莲"
        autoComplete="off"
        className="w-full rounded-xl border border-white/[0.08] bg-white/[0.04] px-3.5 py-2.5 text-[13px] text-[var(--text)] outline-none focus:border-[var(--accent)]/60"
      />
      {searching && (
        <p className="mt-2 text-[12px] text-[var(--text-faint)]">正在搜索…</p>
      )}
      {candidates !== null && !searching && !picked && (
        <div className="mt-2 flex flex-wrap gap-1.5">
          {candidates.length === 0 && (
            <p className="text-[12px] text-[var(--text-faint)]">没有找到条目，换个关键词试试</p>
          )}
          {candidates.map((item) => (
            <button
              key={item.id}
              type="button"
              onClick={() => pick(item)}
              className="glass-row nav-item !w-auto px-3 py-1.5 text-[12px] font-medium"
            >
              {item.title}
              <span className="ml-1.5 text-[var(--text-faint)]">
                {item.year ?? ""} {item.type === "movie" ? "电影" : "剧集"}
              </span>
            </button>
          ))}
        </div>
      )}
      {picked && (
        <div className="mt-3 rounded-xl border border-white/[0.08] bg-white/[0.04] px-4 py-3">
          <p className="text-[13px] font-medium text-white/90">
            《{picked.title}》{picked.year ? ` (${picked.year})` : ""}
          </p>
          {preview === null ? (
            <p className="mt-2 text-[12px] text-[var(--text-faint)]">正在预演…</p>
          ) : (
            <div className="mt-2 space-y-1.5">
              <SimStep
                n={1}
                text={
                  preview.route_reason ??
                  (preview.library_name ? `入库到「${preview.library_name}」` : "没有可用的媒体库")
                }
              />
              <SimStep
                n={2}
                text={
                  preview.mode === "watch"
                    ? `投递到监听导入目录 ${preview.path}，下载完成后自动整理入库`
                    : preview.mode === "inplace"
                      ? `直接下载到库内目录 ${preview.path?.replace(/\/+$/, "")}/${picked.title}${picked.year ? ` (${picked.year})` : ""}，完成后自动入账`
                      : "落到下载器默认目录（不会自动入库）"
                }
              />
              {preview.ok ? (
                <p className="flex items-center gap-1.5 pt-1 text-[12.5px] font-medium text-[#4ade80]">
                  ✓ 全链路可行：订阅后即可自动下载并整理入库
                </p>
              ) : (
                <p className="rounded-lg border border-amber-400/25 bg-amber-500/10 px-3 py-2 text-[12px] leading-relaxed text-amber-200">
                  {preview.warning}
                </p>
              )}
            </div>
          )}
        </div>
      )}
    </section>
  );
}

function SimStep({ n, text }: { n: number; text: string }) {
  return (
    <p className="flex items-start gap-2.5 text-[12.5px] leading-relaxed text-[var(--text-muted)]">
      <span className="mt-px flex size-4.5 shrink-0 items-center justify-center rounded-full bg-white/10 text-[10.5px] font-semibold text-white/70">
        {n}
      </span>
      <span className="min-w-0 flex-1">{text}</span>
    </p>
  );
}

// ---------------------------------------------------------------------------
// 规则组（只读清单）
// ---------------------------------------------------------------------------

function RuleSetsPanel() {
  const [ruleSets, setRuleSets] = useState<RuleSet[] | null>(null);

  useEffect(() => {
    void listRuleSets()
      .then(setRuleSets)
      .catch(() => setRuleSets([]));
  }, []);

  return (
    <section>
      <h3 className="mb-2 text-[14px] font-semibold text-white/90">规则组</h3>
      <p className="mb-4 text-[12.5px] leading-6 text-[var(--text-muted)]">
        规则组定义「什么样的资源可接受」（分辨率、来源等硬条件与偏好排序），
        在订阅弹窗中按订阅选用；标「默认」的组是新订阅的初始选择。
      </p>
      {ruleSets === null ? (
        <p className="rounded-xl bg-white/[0.03] px-4 py-4 text-[13px] text-[var(--text-muted)]">
          正在加载…
        </p>
      ) : (
        <div className="space-y-1.5">
          {ruleSets.map((rs) => (
            <div
              key={rs.id}
              className="flex items-center gap-3 rounded-xl border border-white/[0.08] bg-white/[0.04] px-4 py-2.5"
            >
              <span className="min-w-0 flex-1 truncate text-[13px] font-medium text-white/90">
                {rs.name}
              </span>
              {rs.is_default && (
                <span className="shrink-0 rounded-full border border-white/[0.14] bg-white/[0.1] px-2 py-0.5 text-[10.5px] font-semibold text-white/80">
                  默认
                </span>
              )}
              <span className="shrink-0 text-[11.5px] text-[var(--text-faint)]">
                {Object.keys(rs.spec ?? {}).length === 0 ? "全不限" : "已配置条件"}
              </span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}
