"use client";

// 页面右下角的瞬时提示。
//
// 两点:
//  - 读屏:外层是一个**始终存在**的 `role="status"` 区域,提示文字是往里面塞的——live region
//    必须先在页面里、内容后到,读屏才会念;条件渲染整个节点(旧写法)常常念不出来。
//  - 时长:全站的即逝提示固定 2.2 秒(TOAST_DEFAULT_MS),读一句话刚好。但「已退出共享,已删除
//    N 条记忆」是永久删除唯一的确认,入口控件(卡片)随之消失,2.2 秒读不完;这类提示由调用方
//    给更长的 `lingerMs`(待确认中心的完工提示是 6 秒,这里给 8 秒)。

import { useEffect } from "react";

export const TOAST_DEFAULT_MS = 2200;
export const TOAST_EXIT_MS = 8000;

export function ToastRegion({
  message,
  lingerMs = TOAST_DEFAULT_MS,
  onExpire,
}: {
  message: string;
  lingerMs?: number;
  onExpire: () => void;
}) {
  useEffect(() => {
    if (!message) return undefined;
    const timer = window.setTimeout(onExpire, lingerMs);
    return () => window.clearTimeout(timer);
  }, [message, lingerMs, onExpire]);
  return (
    <div role="status" aria-live="polite" aria-atomic="true">
      {message && <div className="toast">{message}</div>}
    </div>
  );
}
