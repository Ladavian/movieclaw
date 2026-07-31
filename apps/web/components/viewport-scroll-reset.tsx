"use client";

import { useEffect } from "react";

/**
 * iOS 独立 App（PWA）的窗口滚动归位。
 *
 * 全站是「外壳固定 + 内层容器滚动」，窗口本身永远不该有滚动偏移。但为了修
 * 底部黑条，html/body 被撑高了 --vp-overshoot（见 globals.css，两处是一对），
 * 窗口因此多出一截可滚动区域；iOS 弹出软键盘时会滚动窗口去露出聚焦的输入框，
 * 收起后这段偏移**不会还原**——整页向上错位一截，顶栏叠进状态栏。
 *
 * CSS 治不了（overflow: hidden 挡不住系统发起的滚动），只能在键盘收起后把
 * 窗口滚回原点。两个信号缺一不可：
 * - focusout：点「完成」或点空白处收起键盘（输入框失焦）；
 * - visualViewport resize：下滑手势收起键盘（输入框保持聚焦，只有视口高度变化）。
 *
 * 判定「键盘已收起」= 可视视口高度回到接近布局视口（键盘占高远大于 80px）。
 * 用户捏合放大时（scale > 1）窗口滚动是合法的浏览能力，不做任何干预。
 * 桌面端窗口偏移恒为 0，整个 effect 是空转，无需按端分支。
 */
export function ViewportScrollReset() {
  useEffect(() => {
    const reset = () => {
      const vv = window.visualViewport;
      if (vv && (vv.scale > 1.01 || vv.height < window.innerHeight - 80)) return;
      if (window.scrollY !== 0 || window.scrollX !== 0) window.scrollTo(0, 0);
    };
    // 收起动画期间视口高度尚未恢复，推迟一拍再判定；focusout 时若键盘还没
    // 收完，这次判定会被跳过，随后的 visualViewport resize 会兜住。
    const deferredReset = () => window.setTimeout(reset, 0);
    window.addEventListener("focusout", deferredReset);
    window.visualViewport?.addEventListener("resize", deferredReset);
    return () => {
      window.removeEventListener("focusout", deferredReset);
      window.visualViewport?.removeEventListener("resize", deferredReset);
    };
  }, []);
  return null;
}
