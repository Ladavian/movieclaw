"use client";

import { useCallback, useEffect, useState } from "react";

import Link from "next/link";

import {
  getPipelineHealth,
  listRuleSets,
  type LibraryPipeline,
  type PipelineCheck,
  type PipelineHealth,
  type RuleSet,
} from "@/lib/api/subscriptions";

/**
 * 订阅设定（设置 → 订阅）：订阅相关配置的家。
 *
 * 当前两个住户：
 * - 链路体检：逐库预演「投递 → 转移 → 入库」。下载器映射、监听导入、库根
 *   路径分散在三个页面，各自保存时都有校验，但正确性是**联合约束**——
 *   这里把每个库的链路逐段陈述事实，红项给修复去处。判定与真实投递同一批
 *   原语，不存在"体检说好、投递却挂"的口径漂移；
 * - 规则组：只读清单（在订阅弹窗中选用；编辑器待独立设计，不做半成品）。
 */
export function SubscriptionSettingsSection() {
  return (
    <div className="space-y-10">
      <PipelineHealthPanel />
      <RuleSetsPanel />
    </div>
  );
}

/** 状态 → 颜色/文案（体检点与库行共用一套语义）。 */
const STATUS_META = {
  ok: { dot: "bg-[#4ade80]", text: "text-[#4ade80]", label: "正常" },
  warn: { dot: "bg-amber-400", text: "text-amber-300", label: "降级" },
  error: { dot: "bg-[#ff6b6b]", text: "text-[#ff6b6b]", label: "有问题" },
} as const;

/** 修复去处 → 跳转地址与文案。 */
function fixTarget(section: string | null): { href: string; label: string } | null {
  if (section === "downloaders") return { href: "/settings/downloaders", label: "去下载器设置" };
  if (section === "import-watch") return { href: "/settings/import-watch", label: "去监听导入" };
  if (section === "libraries") return { href: "/library", label: "去媒体库" };
  return null;
}

function PipelineHealthPanel() {
  const [health, setHealth] = useState<PipelineHealth | null>(null);
  const [failed, setFailed] = useState(false);

  const reload = useCallback(() => {
    setFailed(false);
    setHealth(null);
    getPipelineHealth()
      .then(setHealth)
      .catch(() => setFailed(true));
  }, []);

  useEffect(() => {
    reload();
  }, [reload]);

  return (
    <section>
      <div className="mb-2 flex items-center justify-between">
        <h3 className="text-[14px] font-semibold text-white/90">订阅链路体检</h3>
        <button
          type="button"
          onClick={reload}
          className="btn-glass px-3 py-1.5 text-[12px] font-medium"
        >
          重新体检
        </button>
      </div>
      <p className="mb-4 text-[12.5px] leading-6 text-[var(--text-muted)]">
        逐库预演「投递 → 转移 → 入库」的完整链路，与真实投递同一套判定。
        红项表示订阅会卡在那一步（工单不会丢，修好后自动重试）；黄项能转但有降级。
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
      {health !== null && health.libraries.length === 0 && (
        <p className="rounded-xl bg-white/[0.03] px-4 py-5 text-center text-[13px] text-[var(--text-muted)]">
          还没有媒体库。订阅需要一个入库目标，请先到「媒体库」页创建。
        </p>
      )}
      {health !== null && health.libraries.length > 0 && (
        <div className="space-y-2.5">
          {health.libraries.map((pipeline) => (
            <LibraryPipelineRow key={pipeline.library_id} pipeline={pipeline} />
          ))}
        </div>
      )}
    </section>
  );
}

function LibraryPipelineRow({ pipeline }: { pipeline: LibraryPipeline }) {
  // 有问题的库默认展开（用户来这页就是找红项的），全绿的收起省视线
  const [open, setOpen] = useState(pipeline.status !== "ok");
  const meta = STATUS_META[pipeline.status];

  return (
    <div className="rounded-xl border border-white/[0.08] bg-white/[0.04]">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        className="flex w-full items-center gap-3 px-4 py-3 text-left"
      >
        <span className={`size-2 shrink-0 rounded-full ${meta.dot}`} />
        <span className="min-w-0 flex-1">
          <span className="text-[13.5px] font-medium text-white/90">
            {pipeline.library_name}
          </span>
          <span className="ml-2 text-[11.5px] text-[var(--text-faint)]">
            {pipeline.kind === "movie" ? "电影" : "剧集"}
            {pipeline.is_default ? " · 默认库" : ""}
          </span>
        </span>
        <span className={`shrink-0 text-[12px] font-medium ${meta.text}`}>{meta.label}</span>
      </button>
      {open && (
        <div className="space-y-2 border-t border-white/[0.06] px-4 py-3">
          {pipeline.checks.map((check) => (
            <CheckRow key={check.key} check={check} />
          ))}
        </div>
      )}
    </div>
  );
}

function CheckRow({ check }: { check: PipelineCheck }) {
  const meta = STATUS_META[check.status];
  const fix = check.status !== "ok" ? fixTarget(check.fix_section) : null;
  return (
    <div className="flex items-start gap-2.5">
      <span className={`mt-[5px] size-1.5 shrink-0 rounded-full ${meta.dot}`} />
      <div className="min-w-0 flex-1">
        <p className="text-[12.5px] leading-relaxed text-[var(--text)]">
          <span className="font-medium">{check.label}</span>
          <span className="ml-2 text-[var(--text-muted)]">{check.detail}</span>
        </p>
        {fix && (
          <Link
            href={fix.href as never}
            className="mt-0.5 inline-block text-[12px] font-medium text-[var(--accent)] hover:underline"
          >
            {fix.label} →
          </Link>
        )}
      </div>
    </div>
  );
}

/** 规则组只读清单：在订阅弹窗中选用；spec 编辑器待独立设计。 */
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
