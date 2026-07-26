"use client";

import { useCallback, useSyncExternalStore } from "react";

/**
 * 媒体查询订阅 —— 全站「移动端 / 桌面端」结构分叉的唯一判定来源。
 *
 * 为什么需要 JS 判定而不是只写 CSS 断点：绝大多数排版差异（内边距、字号、
 * 网格列数）都该用 Tailwind 的 `max-md:` / `md:` 变体在原地解决，改动最小；
 * 但有两类事情 CSS 做不到，必须靠 JS：
 *   1. **结构分叉**——侧栏在桌面是常驻两栏的一列，在手机是覆盖式抽屉，
 *      DOM 位置与层级完全不同；
 *   2. **只能存在一份的重资源**——侧栏面板是真实 WebGL 液态玻璃
 *      （见 components/glass-panel.tsx），同时渲染「桌面版 + 抽屉版」两份
 *      会白白吃掉一个 WebGL 上下文（浏览器上限很低），必须二选一。
 *
 * 用 useSyncExternalStore 而非 useEffect + useState：订阅外部数据源的标准做法，
 * 服务端快照恒为 false（按桌面渲染），客户端首帧即读到真实值，不会闪一帧错版式。
 */
export function useMediaQuery(query: string): boolean {
  const subscribe = useCallback(
    (onChange: () => void) => {
      const mql = window.matchMedia(query);
      mql.addEventListener("change", onChange);
      return () => mql.removeEventListener("change", onChange);
    },
    [query],
  );
  return useSyncExternalStore(
    subscribe,
    () => window.matchMedia(query).matches,
    () => false, // SSR：按桌面渲染
  );
}

/** 移动端断点：与 Tailwind 的 md(768px) 对齐——小于它走单栏 + 抽屉式导航。 */
export const MOBILE_QUERY = "(max-width: 767px)";

/** 是否处于移动端版式（< 768px）。 */
export function useIsMobile(): boolean {
  return useMediaQuery(MOBILE_QUERY);
}
