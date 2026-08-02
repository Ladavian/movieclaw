"use client";

import { useCallback, useEffect, useRef, useState } from "react";

import Link from "next/link";

import { useConfirm } from "@/components/feedback";
import { ChatIcon } from "@/components/icons";
import { useLlmConfigured } from "@/components/llm-gate";
import {
  type WeixinAccount,
  type WeixinBindingSnapshot,
  getWeixinBindingStatus,
  listWeixinAccounts,
  startWeixinBinding,
  submitWeixinVerifyCode,
  unbindWeixinAccount,
} from "@/lib/api/channels";
import { formatRelativeTime } from "@/lib/time";
import { useVisiblePolling } from "@/lib/use-visible-polling";

/**
 * 「微信绑定」设置分区。
 *
 * 交互状态机(与产品确认的流程):
 * - 进入页面先拉账号列表:非空 → 只展示列表(不出二维码);为空 → 自动发起
 *   绑定并展示二维码;
 * - 出码后每 2 秒 poll 绑定状态(后端只读内存快照,毫秒级返回):
 *   need_verify_code → 二维码旁弹出配对码输入框;
 *   confirmed → 二维码消失、提示完成、刷新列表(此时通道已在收发);
 *   expired/failed → 展示原因 + 「重新生成」按钮;
 * - 已有绑定后,除非点「新增绑定」,二维码不再出现。
 *
 * 前置门禁:微信侧对话完全由 AI 模型驱动,未接入模型供应商时不自动出码、
 * 隐藏绑定入口,并展示引导提醒(判定与 useLlmConfigured / 后端同口径)。
 */
export function WeixinBindingSection() {
  // null = 探测中(不出码也不提醒,避免闪烁);false = 未配置(拦下并引导)
  const llmConfigured = useLlmConfigured();
  const confirm = useConfirm();
  const [accounts, setAccounts] = useState<WeixinAccount[] | null>(null);
  const [binding, setBinding] = useState<WeixinBindingSnapshot | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);
  const [verifyCode, setVerifyCode] = useState("");
  const [justConfirmed, setJustConfirmed] = useState(false);
  // 空列表只自动出码一次:失败/过期后由用户手动点按钮,不无限自动重试
  const autoStartedRef = useRef(false);

  const loadAccounts = useCallback(async () => {
    setError(null);
    try {
      return await listWeixinAccounts().then((rows) => {
        setAccounts(rows);
        return rows;
      });
    } catch (e) {
      setError((e as Error).message);
      setAccounts([]);
      return [];
    }
  }, []);

  const beginBinding = useCallback(async () => {
    setError(null);
    setJustConfirmed(false);
    setVerifyCode("");
    setBusy(true);
    try {
      const start = await startWeixinBinding();
      setBinding({
        challenge_id: start.challenge_id,
        status: "pending",
        message: start.message,
        qrcode_url: start.qrcode_url,
        qrcode_image: start.qrcode_image,
        account: null,
      });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }, []);

  // 进入页面:先拉账号列表
  useEffect(() => {
    void loadAccounts();
  }, [loadAccounts]);

  // 列表为空且模型已配置时自动发起绑定(仅首次);未配置模型不出码
  useEffect(() => {
    if (llmConfigured !== true || accounts == null) return;
    if (accounts.length === 0 && !autoStartedRef.current) {
      autoStartedRef.current = true;
      void beginBinding();
    }
  }, [llmConfigured, accounts, beginBinding]);

  // 绑定进行中每 2s poll 状态;终态停止
  const polling =
    binding != null &&
    (binding.status === "pending" ||
      binding.status === "scanned" ||
      binding.status === "need_verify_code");
  useVisiblePolling(
    () => {
      if (binding == null) return;
      void getWeixinBindingStatus(binding.challenge_id)
        .then((snap) => {
          setBinding(snap);
          if (snap.status === "confirmed" || snap.status === "already_bound") {
            setJustConfirmed(snap.status === "confirmed");
            setBinding(null); // 二维码消失
            void loadAccounts();
          }
        })
        .catch(() => {
          /* poll 失败静默重试(challenge 过期由状态自己表达) */
        });
    },
    polling ? 2000 : null,
  );

  async function handleVerifySubmit() {
    if (binding == null || !verifyCode.trim()) return;
    setBusy(true);
    setError(null);
    try {
      await submitWeixinVerifyCode(binding.challenge_id, verifyCode.trim());
      setVerifyCode("");
      // 提交后立即把本地状态推回 scanned,等待下一次 poll 的服务端判定
      setBinding({ ...binding, status: "scanned", message: "正在校验配对码…" });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  async function handleUnbind(accountId: string) {
    if (
      !(await confirm({
        title: "解绑该微信账号？",
        description: "解绑后需重新扫码才能使用。",
        confirmLabel: "解绑",
        tone: "danger",
      }))
    ) {
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await unbindWeixinAccount(accountId);
      await loadAccounts();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }

  const loading = accounts == null;
  const hasAccounts = (accounts?.length ?? 0) > 0;

  return (
    <div className="space-y-5">
      {error && (
        <div className="rounded-xl border border-[#ff6b6b]/30 bg-[#ff6b6b]/10 px-4 py-3 text-body text-[#ff6b6b]">
          {error}
        </div>
      )}

      <p className="text-sub text-[var(--text-muted)]">
        绑定后，用你的微信给 AI 助手发消息即可对话（仅扫码人本人可用）。发送
        <span className="mx-1 rounded bg-white/[0.08] px-1.5 py-0.5 text-caption">/reset</span>
        重置会话，
        <span className="mx-1 rounded bg-white/[0.08] px-1.5 py-0.5 text-caption">/stop</span>
        取消正在进行的处理。
      </p>

      {justConfirmed && (
        <div className="rounded-xl border border-[#4ade80]/30 bg-[#4ade80]/10 px-4 py-3 text-body text-[#4ade80]">
          绑定完成，微信通道已启动。现在就可以在微信里发消息试试。
        </div>
      )}

      {/* 前置门禁:未接入 AI 模型时给出引导,并隐藏所有绑定入口 */}
      {llmConfigured === false && (
        <div className="rounded-xl border border-[#f5c451]/30 bg-[#f5c451]/10 px-4 py-3 text-body text-[#f5c451]">
          微信绑定需要先完成 AI 模型配置——微信里的对话完全由模型驱动。请先前往
          <Link
            href="/settings/llm"
            className="mx-1 font-medium underline underline-offset-2 hover:opacity-80"
          >
            设置 → AI 模型
          </Link>
          接入模型供应商，再回来扫码绑定。
        </div>
      )}

      {loading ? (
        <div className="h-[104px] animate-pulse rounded-xl bg-white/[0.04]" />
      ) : (
        <>
          {/* 已绑定账号列表 */}
          {hasAccounts && (
            <div className="css-glass !rounded-xl">
              {accounts!.map((account, i) => (
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
                      <AccountStatusPill account={account} />
                    </div>
                    <p className="mt-0.5 truncate text-caption text-[var(--text-faint)]">
                      {account.status === "stale"
                        ? (account.last_error ?? "凭据已失效，请重新扫码绑定")
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
          )}

          {/* 绑定进行中:二维码卡片 */}
          {binding != null && <BindingCard binding={binding} onRetry={() => void beginBinding()} />}

          {/* 配对码输入(need_verify_code) */}
          {binding?.status === "need_verify_code" && (
            <div className="css-glass !rounded-xl p-4">
              <p className="text-body font-medium text-[var(--text)]">{binding.message}</p>
              <div className="mt-3 flex gap-2">
                <input
                  value={verifyCode}
                  onChange={(e) => setVerifyCode(e.target.value)}
                  onKeyDown={(e) => e.key === "Enter" && void handleVerifySubmit()}
                  inputMode="numeric"
                  autoFocus
                  placeholder="手机微信上显示的数字"
                  className="flex-1 rounded-xl border border-white/[0.08] bg-white/[0.04] px-3.5 py-2 text-ui text-[var(--text)] outline-none focus:border-[var(--accent)]/60"
                />
                <button
                  type="button"
                  disabled={busy || !verifyCode.trim()}
                  onClick={() => void handleVerifySubmit()}
                  className="btn-accent rounded-xl px-4 py-2 text-sub font-semibold disabled:opacity-40"
                >
                  确认
                </button>
              </div>
            </div>
          )}

          {/* 已有绑定且当前没在走绑定流程:提供「新增绑定」入口(模型未配置时隐藏) */}
          {hasAccounts && binding == null && llmConfigured !== false && (
            <button
              type="button"
              disabled={busy}
              onClick={() => void beginBinding()}
              className="btn-glass rounded-full px-4 py-1.5 text-sub font-medium disabled:opacity-40"
            >
              新增绑定
            </button>
          )}

          {/* 空态且绑定发起失败(网关不可达等):手动重试入口(模型未配置时隐藏,由上方提醒引导) */}
          {!hasAccounts && binding == null && llmConfigured !== false && (
            <div className="css-glass flex flex-col items-center gap-3 !rounded-2xl px-6 py-12 text-center">
              <span className="icon-chip size-12 !rounded-2xl">
                <ChatIcon className="size-6" />
              </span>
              <div>
                <p className="text-body font-medium text-[var(--text)]">
                  {justConfirmed ? "还没有其他绑定" : "还没有绑定微信"}
                </p>
                <p className="mt-1 text-sub text-[var(--text-muted)]">
                  生成二维码后用手机微信扫描，即可把 AI 助手接入微信。
                </p>
              </div>
              <button
                type="button"
                disabled={busy}
                onClick={() => void beginBinding()}
                className="btn-accent mt-1 rounded-full px-4 py-1.5 text-sub font-semibold disabled:opacity-40"
              >
                生成绑定二维码
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}

/** 绑定二维码卡片:二维码 + 实时状态;过期/失败给重试按钮。 */
function BindingCard({
  binding,
  onRetry,
}: {
  binding: WeixinBindingSnapshot;
  onRetry: () => void;
}) {
  const terminal = binding.status === "expired" || binding.status === "failed";
  return (
    <div className="css-glass flex flex-col items-center gap-4 !rounded-2xl px-6 py-8 text-center">
      {!terminal && (
        <div className="rounded-2xl bg-white p-3">
          {/* 服务端渲染的 SVG data URL;用原生 img 展示内联数据,无需 next/image 优化 */}
          <img src={binding.qrcode_image} alt="微信绑定二维码" className="size-48" />
        </div>
      )}
      <div>
        <p className="text-body font-medium text-[var(--text)]">
          {binding.status === "scanned" ? "已扫码" : terminal ? "绑定未完成" : "等待扫码"}
        </p>
        <p className="mt-1 text-sub text-[var(--text-muted)]">{binding.message}</p>
      </div>
      {terminal ? (
        <button
          type="button"
          onClick={onRetry}
          className="btn-accent rounded-full px-4 py-1.5 text-sub font-semibold"
        >
          重新生成二维码
        </button>
      ) : (
        <p className="text-caption text-[var(--text-faint)]">
          打开手机微信，扫一扫上方二维码
        </p>
      )}
    </div>
  );
}

function AccountStatusPill({ account }: { account: WeixinAccount }) {
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
