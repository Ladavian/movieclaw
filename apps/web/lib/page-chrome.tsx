"use client";

import { createContext, useContext } from "react";

import type { SearchSubmitOptions } from "@/components/search-command";
import type { SearchScope } from "@/lib/categories";

/** 提交搜索的回调，签名与 AppShell 的 handleSearch 一致。 */
export type PageSearchHandler = (
  keyword: string,
  scope: SearchScope,
  options?: SearchSubmitOptions,
) => void;

/**
 * 页面顶栏的归属协商 —— 解决移动端「两条顶栏叠在一起」的问题。
 *
 * 移动端外壳自带一条全局顶栏（汉堡 + 字标 + 搜索），而详情类页面又各自带一条
 * PageNav（返回 + 标题 + 页面操作）。两者都吸在顶部，窄屏上就摞成了两层，
 * 白白吃掉两倍高度，右上角还并排出现两颗互不相干的图标。
 *
 * 收口办法：让页面顶栏「认领」这一行——PageNav 挂载即向外壳登记，外壳在移动端
 * 据此撤掉自己那条，并把搜索入口交给 PageNav 摆在页面操作旁边。于是无论哪种
 * 页面，窄屏上永远只有一条顶栏。
 *
 * 为什么用「组件自登记」而不是在外壳里列一张路由表：全站的移动端让位逻辑
 * （见 globals.css 的 .app-shell > main）一贯是「新增路由自动继承、无需登记」，
 * 路由表会随着页面增加而漏。谁渲染了 PageNav，谁就自动接管顶栏。
 *
 * 桌面端不受影响：外壳的全局顶栏本来就只在 < 768px 渲染，PageNav 那颗搜索键
 * 也只在移动端渲染（SearchCommand 自带全局 ⌘K 监听，多挂一份会让一次快捷键
 * 把面板开了又关，因此必须条件渲染而不是 CSS 隐藏）。
 */
export interface PageChromeValue {
  /**
   * 登记「本页自带顶栏」。在 effect 里调用，返回注销函数。
   * 用计数而非布尔：路由切换时新旧页面会短暂共存，计数才不会被先卸载的那个清零。
   */
  registerPageNav: () => () => void;
  /** 全局搜索入口：移动端由 PageNav 代为呈现（外壳那条已经撤掉） */
  onSearch: PageSearchHandler;
}

const PageChromeContext = createContext<PageChromeValue | null>(null);

export const PageChromeProvider = PageChromeContext.Provider;

/** 读取顶栏协商上下文；不在 AppShell 内（如独立的 /login、/setup）时返回 null。 */
export function usePageChrome(): PageChromeValue | null {
  return useContext(PageChromeContext);
}
