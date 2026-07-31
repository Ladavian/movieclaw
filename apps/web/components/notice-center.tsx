"use client";

/**
 * 待处理事项（状态化告警中心）的全局入口 + 面板。
 *
 * 产品语义（与后端 system_notice 对齐）：这里只展示"需要用户行动才能推进
 * 的运行时故障"——正常运行时列表为空，入口**整个不渲染**（宁少勿多的
 * 控件密度原则）；一旦亮起就是真问题。问题修好后服务端自动消退，入口
 * 随之消失，用户不需要"清理通知"。
 *
 * 形态：侧栏主导航下方一行红色入口（折叠态为带红点的居中图标），点击
 * 打开居中弹窗列出全部事项；每条可跳转到能修它的页面，或手动忽略。
 * 轮询 30s 一次 + 窗口回焦立即刷一次——查询极轻（active 空表短路）。
 */

import { useCallback, useEffect, useState } from "react";
import type { Route } from "next";
import { useRouter } from "next/navigation";

import { BellIcon, ChevronRightIcon, XIcon } from "@/components/icons";
import { Modal } from "@/components/modal";
import {
  dismissNotice,
  fetchActiveNotices,
  noticeHref,
  type SystemNotice,
} from "@/lib/api/notices";
import { formatRelativeTime } from "@/lib/time";

const POLL_MS = 30_000;

/** 严重度圆点颜色：error 用全站危险色，warning 用琥珀 */
const DOT_COLOR: Record<string, string> = {
  error: "var(--danger)",
  warning: "#f5a623",
};

export function NoticeCenter({ collapsed }: { collapsed: boolean }) {
  const [notices, setNotices] = useState<SystemNotice[]>([]);
  const [open, setOpen] = useState(false);
  const router = useRouter();

  const refresh = useCallback(() => {
    fetchActiveNotices()
      .then(setNotices)
      .catch(() => {
        // 拉取失败（离线/后端重启）不打扰：保留上次结果，下轮轮询自愈
      });
  }, []);

  useEffect(() => {
    refresh();
    const timer = window.setInterval(refresh, POLL_MS);
    const onFocus = () => refresh();
    window.addEventListener("focus", onFocus);
    return () => {
      window.clearInterval(timer);
      window.removeEventListener("focus", onFocus);
    };
  }, [refresh]);

  if (notices.length === 0) return null;

  const goto = (notice: SystemNotice) => {
    setOpen(false);
    router.push(noticeHref(notice) as Route);
  };

  const dismiss = (id: number) => {
    // 乐观移除：dismiss 幂等且失败无害（下轮轮询会拉回真实状态）
    setNotices((prev) => prev.filter((n) => n.id !== id));
    void dismissNotice(id).catch(refresh);
  };

  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        title={collapsed ? `待处理（${notices.length}）` : undefined}
        className={`glass-row nav-item py-2 !text-[var(--danger)] max-md:py-2.5 ${
          collapsed ? "justify-center px-0" : "px-3"
        }`}
      >
        <span className="relative shrink-0">
          <BellIcon className="size-[18px] max-md:size-[22px]" />
          {collapsed && (
            <span className="absolute -right-1 -top-1 size-2 rounded-full bg-[var(--danger)]" />
          )}
        </span>
        {!collapsed && (
          <>
            <span className="flex-1 text-ui font-medium">待处理</span>
            <span className="rounded-full bg-[var(--danger)] px-1.5 py-0.5 text-[11px] font-semibold leading-none text-white">
              {notices.length}
            </span>
          </>
        )}
      </button>

      <Modal
        open={open}
        onClose={() => setOpen(false)}
        label="待处理事项"
        width="lg"
        // dvh 而非 vh：iOS 浏览器里 vh 按「地址栏收起后」的大视口算，76vh 的
        // 弹层会超出实际可视高度（subscribe-dialog 同款约定）
        panelClassName="flex max-h-[76dvh] flex-col"
      >
        <div className="flex items-center gap-2 px-5 pb-3 pt-5">
          <BellIcon className="size-[18px] text-[var(--danger)]" />
          <h2 className="flex-1 text-body font-semibold">待处理事项</h2>
          <span className="text-caption text-[var(--text-faint)]">
            问题修复后会自动消失
          </span>
        </div>
        <div className="scroll-thin flex-1 space-y-2 overflow-y-auto px-5 pb-5">
          {notices.map((notice) => (
            <div key={notice.id} className="glass-row flex-col items-stretch gap-1.5 !rounded-xl p-3">
              <div className="flex items-center gap-2">
                <span
                  className="size-2 shrink-0 rounded-full"
                  style={{ background: DOT_COLOR[notice.severity] ?? DOT_COLOR.error }}
                />
                <span className="flex-1 truncate text-ui font-semibold">{notice.title}</span>
                <span className="shrink-0 text-caption text-[var(--text-faint)]">
                  {formatRelativeTime(notice.updated_at)}
                </span>
              </div>
              <p className="text-caption leading-5 text-[var(--text-muted)]">{notice.message}</p>
              <div className="flex items-center justify-end gap-1.5 pt-0.5">
                <button
                  type="button"
                  onClick={() => dismiss(notice.id)}
                  className="glass-row !w-auto gap-1 rounded-lg px-2 py-1 text-caption text-[var(--text-faint)]"
                >
                  <XIcon className="size-3.5" />
                  忽略
                </button>
                <button
                  type="button"
                  onClick={() => goto(notice)}
                  className="glass-row !w-auto gap-1 rounded-lg px-2 py-1 text-caption font-medium"
                >
                  去处理
                  <ChevronRightIcon className="size-3.5" />
                </button>
              </div>
            </div>
          ))}
        </div>
      </Modal>
    </>
  );
}
