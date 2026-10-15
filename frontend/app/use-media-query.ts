"use client";

import { useEffect, useState } from "react";

/**
 * 订阅一条 CSS 媒体查询。首帧(以及没有 matchMedia 的环境)按 `false` 算,挂载后
 * 立刻对齐真实结果,之后随窗口变化更新。
 */
export function useMediaQuery(query: string): boolean {
  const [matches, setMatches] = useState(false);
  useEffect(() => {
    const list = typeof window.matchMedia === "function" ? window.matchMedia(query) : null;
    if (!list) return;
    const sync = () => setMatches(list.matches);
    sync();
    list.addEventListener("change", sync);
    return () => list.removeEventListener("change", sync);
  }, [query]);
  return matches;
}
