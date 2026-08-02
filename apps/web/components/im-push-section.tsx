"use client";

import { useCallback, useEffect, useState } from "react";

import Link from "next/link";

import { ChatIcon } from "@/components/icons";
import { useLlmConfigured } from "@/components/llm-gate";
import { WeixinBindingSection } from "@/components/weixin-binding-section";
import {
  type ChannelPushConfig,
  type ImAccount,
  type ImBinding,
  type ImChannelId,
  getChannelPushConfig,
  getImBindingStatus,
  listImAccounts,
  sendChannelPushTest,
  startImBinding,
  unbindImAccount,
  updateChannelPushConfig,
} from "@/lib/api/channels";
import { useBackdrop } from "@/lib/backdrop";
import { formatRelativeTime } from "@/lib/time";
import { useVisiblePolling } from "@/lib/use-visible-polling";
import { LiquidGlassButton } from "@/vendor/liquid-glass";

/** 平台文案（组件唯一的平台差异落点）。 */
const CHANNEL_META: Record<
  ImChannelId,
  { label: string; tokenHint: string; howTo: string }
> = {
  telegram: {
    label: "Telegram",
    tokenHint: "从 @BotFather 创建 bot 后获得的 token",
    howTo: "在 Telegram 搜索 @BotFather 创建 bot 并复制 token；国内网络请先在「设置 → 网络」为 Telegram 开启代理。",
  },
  discord: {
    label: "Discord",
    tokenHint: "Discord 开发者后台 Bot 页面的 token",
    howTo: "在 discord.com/developers 创建应用 → Bot → Reset Token 复制；国内网络请先在「设置 → 网络」为 Discord 开启代理。",
  },
};

/**
 * 「消息推送」设置分区：Telegram 与 Discord 的配对码绑定 + 测试推送。
 *
 * 交互（与微信绑定同节奏，但无二维码）：
 * 1. 填 bot token → 发起绑定 → 面板显示 6 位配对码；
 * 2. 用户去平台私聊 bot 发配对码，前端每 2 秒轮询状态；
 * 3. confirmed → 刷新列表（发码人即白名单与推送目标）。
 */
export function ImPushSection() {
  const llmConfigured = useLlmConfigured();
  const [pushMsg, setPushMsg] = useState<string | null>(null);
  const [pushBusy, setPushBusy] = useState(false);

  async function handleTestPush() {
    setPushBusy(true);
    setPushMsg(null);
    try {
      const { sent } = await sendChannelPushTest();
      setPushMsg(`✅ 已推送到 ${sent} 个账号，去客户端看看吧`);
    } catch (e) {
      setPushMsg(`⚠️ ${(e as Error).message}`);
    } finally {
      setPushBusy(false);
    }
  }

  return (
    <div className="space-y-8">
      <p className="text-sub text-[var(--text-muted)]">
        绑定 Telegram / Discord bot 后：订阅投递、入库完成等事件会自动推送；直接给 bot
        发消息即可让 AI 助手理解并执行（搜片、订阅、查进度……）。发送
        <span className="mx-1 rounded bg-white/[0.08] px-1.5 py-0.5 text-caption">/reset</span>
        重置会话，
        <span className="mx-1 rounded bg-white/[0.08] px-1.5 py-0.5 text-caption">/stop</span>
        取消正在进行的处理。
      </p>

      {llmConfigured === false && (
        <div className="rounded-xl border border-[#f5c451]/30 bg-[#f5c451]/10 px-4 py-3 text-body text-[#f5c451]">
          绑定需要先完成 AI 模型配置——对话完全由模型驱动。请先前往
          <Link
            href="/settings/llm"
            className="mx-1 font-medium underline underline-offset-2 hover:opacity-80"
          >
            设置 → AI 模型
          </Link>
          接入模型供应商。
        </div>
      )}

      {/* 微信绑定（扫码流程自成一体，整块复用原组件） */}
      <div className="space-y-3">
        <h3 className="text-body font-semibold text-[var(--text)]">微信</h3>
        <WeixinBindingSection hideIntro />
      </div>

      <ChannelBindingBlock channel="telegram" disabled={llmConfigured === false} />
      <ChannelBindingBlock channel="discord" disabled={llmConfigured === false} />

      <PushContentBlock />

      {/* 测试推送：验证已绑定通道能收到系统事件 */}
      <div className="css-glass !rounded-xl p-4">
        <div className="flex items-center justify-between gap-3">
          <div>
            <p className="text-body font-medium text-[var(--text)]">测试推送</p>
            <p className="mt-0.5 text-caption text-[var(--text-faint)]">
              向所有已绑定通道（含微信）发送一条测试消息
            </p>
          </div>
          <button
            type="button"
            disabled={pushBusy}
            onClick={() => void handleTestPush()}
            className="btn-glass shrink-0 rounded-full px-4 py-1.5 text-sub font-medium disabled:opacity-40"
          >
            发送测试
          </button>
        </div>
        {pushMsg && <p className="mt-2 text-caption text-[var(--text-muted)]">{pushMsg}</p>}
      </div>
    </div>
  );
}

/** 推送内容开关：控制哪些系统事件会推送（保存即生效，对所有通道含微信生效）。 */
function PushContentBlock() {
  const { backdrop } = useBackdrop();
  const [config, setConfig] = useState<ChannelPushConfig | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    getChannelPushConfig()
      .then(setConfig)
      .catch((e) => setError((e as Error).message));
  }, []);

  async function handleToggle(key: keyof ChannelPushConfig, value: boolean) {
    if (config == null) return;
    const next = { ...config, [key]: value };
    setConfig(next); // 乐观更新，失败回滚
    setError(null);
    try {
      await updateChannelPushConfig(next);
    } catch (e) {
      setConfig(config);
      setError((e as Error).message);
    }
  }

  const rows: { key: keyof ChannelPushConfig; label: string; desc: string }[] = [
    { key: "push_dispatch", label: "开始下载", desc: "订阅命中资源并提交下载器时" },
    { key: "push_imported", label: "入库完成", desc: "下载完成整理进媒体库时" },
  ];

  return (
    <div className="space-y-3">
      <h3 className="text-body font-semibold text-[var(--text)]">推送内容</h3>
      {error && (
        <div className="rounded-xl border border-[#ff6b6b]/30 bg-[#ff6b6b]/10 px-4 py-3 text-body text-[#ff6b6b]">
          {error}
        </div>
      )}
      {config == null ? (
        <div className="h-[104px] animate-pulse rounded-xl bg-white/[0.04]" />
      ) : (
        <div className="css-glass !rounded-xl">
          {rows.map((row, i) => (
            <div
              key={row.key}
              className={`flex items-center gap-3.5 p-4 ${i > 0 ? "border-t border-white/[0.06]" : ""}`}
            >
              <div className="min-w-0 flex-1">
                <p className="text-body font-medium text-[var(--text)]">{row.label}</p>
                <p className="mt-0.5 text-caption text-[var(--text-faint)]">{row.desc}</p>
              </div>
              {/* 与网络设置同款的受控液态玻璃开关 */}
              <LiquidGlassButton
                backgroundImage={backdrop}
                variant="dark"
                checked={config[row.key]}
                aria-label={`${row.label}推送`}
                onCheckedChange={(v: boolean) => void handleToggle(row.key, v)}
                className="!min-h-0 !w-auto !gap-0 !bg-transparent !p-0"
              >
                <span className="sr-only">{config[row.key] ? "已开启" : "已关闭"}</span>
              </LiquidGlassButton>
            </div>
          ))}
        </div>
      )}
    </div>
  );
}

/** 单平台的绑定块：账号列表 + token 输入 + 配对码流程。 */
function ChannelBindingBlock({
  channel,
  disabled,
}: {
  channel: ImChannelId;
  disabled: boolean;
}) {
  const meta = CHANNEL_META[channel];
  const [accounts, setAccounts] = useState<ImAccount[] | null>(null);
  const [binding, setBinding] = useState<ImBinding | null>(null);
  const [token, setToken] = useState("");
  const [showForm, setShowForm] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [justConfirmed, setJustConfirmed] = useState(false);

  const loadAccounts = useCallback(async () => {
    try {
      setAccounts(await listImAccounts(channel));
    } catch (e) {
      setError((e as Error).message);
      setAccounts([]);
    }
  }, [channel]);

  useEffect(() => {
    void loadAccounts();
  }, [loadAccounts]);

  async function handleStart() {
    if (!token.trim()) return;
    setBusy(true);
    setError(null);
    setJustConfirmed(false);
    try {
      setBinding(await startImBinding(channel, token.trim()));
      setToken("");
      setShowForm(false);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  // 配对进行中每 2s 轮询；confirmed/终态停止
  useVisiblePolling(
    () => {
      if (binding == null) return;
      void getImBindingStatus(channel, binding.challenge_id)
        .then((snap) => {
          setBinding(snap.status === "confirmed" ? null : snap);
          if (snap.status === "confirmed") {
            setJustConfirmed(true);
            void loadAccounts();
          }
        })
        .catch(() => {
          /* 轮询失败静默重试（过期由状态自己表达） */
        });
    },
    binding?.status === "pending" ? 2000 : null,
  );

  async function handleUnbind(accountId: string) {
    if (!window.confirm(`确定解绑该 ${meta.label} bot？解绑后需重新配对才能使用。`)) return;
    setBusy(true);
    setError(null);
    try {
      await unbindImAccount(channel, accountId);
      await loadAccounts();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const hasAccounts = (accounts?.length ?? 0) > 0;

  return (
    <div className="space-y-3">
      <div className="flex items-center justify-between">
        <h3 className="text-body font-semibold text-[var(--text)]">{meta.label}</h3>
        {!disabled && binding == null && (
          <button
            type="button"
            disabled={busy}
            onClick={() => setShowForm((v) => !v)}
            className="btn-glass rounded-full px-3.5 py-1 text-sub font-medium disabled:opacity-40"
          >
            {showForm ? "收起" : hasAccounts ? "新增绑定" : "绑定 bot"}
          </button>
        )}
      </div>

      {error && (
        <div className="rounded-xl border border-[#ff6b6b]/30 bg-[#ff6b6b]/10 px-4 py-3 text-body text-[#ff6b6b]">
          {error}
        </div>
      )}
      {justConfirmed && (
        <div className="rounded-xl border border-[#4ade80]/30 bg-[#4ade80]/10 px-4 py-3 text-body text-[#4ade80]">
          绑定完成，{meta.label} 通道已启动。现在就可以给 bot 发消息试试。
        </div>
      )}

      {/* 已绑定账号列表 */}
      {accounts == null ? (
        <div className="h-[72px] animate-pulse rounded-xl bg-white/[0.04]" />
      ) : hasAccounts ? (
        <div className="css-glass !rounded-xl">
          {accounts.map((account, i) => (
            <div
              key={account.account_id}
              className={`flex items-center gap-3.5 p-4 ${i > 0 ? "border-t border-white/[0.06]" : ""}`}
            >
              <span className="icon-chip size-10 !rounded-xl">
                <ChatIcon className="size-5" />
              </span>
              <div className="min-w-0 flex-1">
                <div className="flex items-center gap-2">
                  <p className="truncate text-body font-semibold text-[var(--text)]">
                    {account.bound_user_id ?? account.account_id}
                  </p>
                  <ImAccountStatusPill account={account} />
                </div>
                <p className="mt-0.5 truncate text-caption text-[var(--text-faint)]">
                  {account.status === "stale"
                    ? (account.last_error ?? "凭据已失效，请重新绑定")
                    : `机器人 ${account.account_id} · 绑定于 ${formatRelativeTime(account.bound_at)}`}
                </p>
              </div>
              <button
                type="button"
                disabled={busy}
                onClick={() => void handleUnbind(account.account_id)}
                className="btn-glass shrink-0 px-3 py-1.5 text-sub font-medium !text-[#ff6b6b] disabled:opacity-40"
              >
                解绑
              </button>
            </div>
          ))}
        </div>
      ) : binding == null && !showForm ? (
        <p className="text-caption text-[var(--text-faint)]">{meta.howTo}</p>
      ) : null}

      {/* token 输入表单 */}
      {showForm && binding == null && (
        <div className="css-glass !rounded-xl p-4">
          <p className="text-caption text-[var(--text-faint)]">{meta.howTo}</p>
          <div className="mt-3 flex gap-2">
            <input
              value={token}
              onChange={(e) => setToken(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && void handleStart()}
              autoFocus
              placeholder={meta.tokenHint}
              className="flex-1 rounded-xl border border-white/[0.08] bg-white/[0.04] px-3.5 py-2 text-ui text-[var(--text)] outline-none focus:border-[var(--accent)]/60"
            />
            <button
              type="button"
              disabled={busy || !token.trim()}
              onClick={() => void handleStart()}
              className="btn-accent rounded-xl px-4 py-2 text-sub font-semibold disabled:opacity-40"
            >
              绑定
            </button>
          </div>
        </div>
      )}

      {/* 配对码卡片 */}
      {binding != null && (
        <div className="css-glass flex flex-col items-center gap-3 !rounded-2xl px-6 py-8 text-center">
          {binding.status === "pending" ? (
            <>
              <p className="text-body font-medium text-[var(--text)]">
                私聊 @{binding.bot_name} 发送配对码
              </p>
              <p className="select-all font-mono text-[32px] font-bold tracking-[0.3em] text-[var(--accent)]">
                {binding.pair_code}
              </p>
              <p className="text-caption text-[var(--text-faint)]">
                10 分钟内有效 · 发码人将成为唯一可对话的用户与推送目标
              </p>
            </>
          ) : (
            <>
              <p className="text-body font-medium text-[var(--text)]">绑定未完成</p>
              <p className="text-sub text-[var(--text-muted)]">{binding.message}</p>
              <button
                type="button"
                onClick={() => {
                  setBinding(null);
                  setShowForm(true);
                }}
                className="btn-accent rounded-full px-4 py-1.5 text-sub font-semibold"
              >
                重新发起
              </button>
            </>
          )}
        </div>
      )}
    </div>
  );
}

function ImAccountStatusPill({ account }: { account: ImAccount }) {
  const meta =
    account.status === "stale"
      ? { label: "需重新绑定", color: "#ff6b6b" }
      : account.running
        ? { label: "运行中", color: "#4ade80" }
        : { label: "未运行", color: "#c0c4cc" };
  return (
    <span
      className="flex shrink-0 items-center gap-1.5 rounded-full px-2 py-0.5 text-caption font-medium"
      style={{ background: `${meta.color}1f`, color: meta.color }}
    >
      <span className="size-1.5 rounded-full" style={{ background: meta.color }} />
      {meta.label}
    </span>
  );
}
