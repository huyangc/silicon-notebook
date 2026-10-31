"use client";

import { useLayoutEffect, useRef, useState } from "react";

/** 收起来源栏时会话栏从这颗按钮展开,恢复来源栏时缩回它——页面上同一时刻只有其中一个。 */
const HISTORY_TOGGLE_SELECTOR = ".chat-session-toggle";
const MORPH_MS = 280;
const COLLAPSE_MS = 560;
const MORPH_EASING = "cubic-bezier(0.2, 0.8, 0.2, 1)";

export type MorphRect = { left: number; top: number; width: number; height: number };

export function rectOf(element: Element | null | undefined): MorphRect | null {
  if (!element) return null;
  const { left, top, width, height } = element.getBoundingClientRect();
  return width > 0 && height > 0 ? { left, top, width, height } : null;
}

export function canAnimate(element: Element | null): element is HTMLElement {
  if (!element || typeof (element as HTMLElement).animate !== "function") return false;
  return !window.matchMedia?.("(prefers-reduced-motion: reduce)")?.matches;
}

/** 把 `from` 矩形摆到 `to` 矩形上的 transform(变换原点在左上角)。 */
export function placeOver(from: MorphRect, to: MorphRect): string {
  return `translate(${to.left - from.left}px, ${to.top - from.top}px) scale(${to.width / from.width}, ${to.height / from.height})`;
}

/**
 * 「历史」按钮 ⇄ 左侧会话栏 的形变动画。
 *
 * 切换来源栏之前调用 `capture()` 记下起点(会话栏在 → 记会话栏;不在 → 记历史按钮),
 * `sessionSidebar` 翻转后的 layout 相位里再量终点:
 * - 展开:会话栏本体从按钮的位置与大小长到自己的位置;
 * - 收回:会话栏已经卸载,在原位置放一块同样大小的影子,缩进重新出现的历史按钮,
 *   按钮落定时轻跳一下,示意会话去了哪儿。
 * 只有经 `capture()` 发起的切换才有动画;切问答页签、窗口变窄等其它原因直接切换。
 */
export function useSessionSidebarMorph(sessionSidebar: boolean) {
  const sidebarRef = useRef<HTMLElement | null>(null);
  const restoreTargetRef = useRef<MorphRect | null>(null);
  const ghostRef = useRef<HTMLDivElement | null>(null);
  const fromRef = useRef<MorphRect | null>(null);
  const shownRef = useRef(sessionSidebar);
  const [ghost, setGhost] = useState<MorphRect | null>(null);

  function capture() {
    fromRef.current = sessionSidebar
      ? rectOf(sidebarRef.current)
      : rectOf(document.querySelector(HISTORY_TOGGLE_SELECTOR));
  }

  useLayoutEffect(() => {
    if (shownRef.current === sessionSidebar) return;
    shownRef.current = sessionSidebar;
    // Snapshot the destination before animating its parent: DOM rects read later
    // include the temporary transform from the old top-right history button.
    restoreTargetRef.current = sessionSidebar
      ? rectOf(sidebarRef.current?.querySelector(".sources-restore-button"))
      : null;
    const from = fromRef.current;
    fromRef.current = null;
    if (!from) return;
    if (!sessionSidebar) {
      setGhost(from);
      return;
    }
    const sidebar = sidebarRef.current;
    const to = rectOf(sidebar);
    if (!to || !canAnimate(sidebar)) return;
    sidebar.animate(
      [
        { transformOrigin: "top left", transform: placeOver(to, from), opacity: 0.2 },
        { transformOrigin: "top left", transform: "none", opacity: 1 },
      ],
      { duration: MORPH_MS, easing: MORPH_EASING },
    );
  }, [sessionSidebar]);

  useLayoutEffect(() => {
    if (!ghost) return;
    const shadow = ghostRef.current;
    const toggle = document.querySelector<HTMLElement>(HISTORY_TOGGLE_SELECTOR);
    const to = rectOf(toggle);
    if (!to || !canAnimate(shadow)) {
      setGhost(null);
      return;
    }
    const morph = shadow.animate(
      [
        { transform: "none", opacity: 1 },
        { transform: placeOver(ghost, to), opacity: 0.75 },
      ],
      { duration: COLLAPSE_MS, easing: "cubic-bezier(0.4, 0, 0.2, 1)", fill: "forwards" },
    );
    morph.onfinish = () => {
      setGhost(null);
      if (toggle && typeof toggle.animate === "function") {
        toggle.animate(
          [
            { transform: "scale(1)", boxShadow: "0 0 0 0 rgba(15, 109, 122, 0.35)" },
            { transform: "scale(1.12)", boxShadow: "0 0 0 8px rgba(15, 109, 122, 0.12)", offset: 0.4 },
            { transform: "scale(1)", boxShadow: "0 0 0 12px rgba(15, 109, 122, 0)" },
          ],
          { duration: 420, easing: "ease-out" },
        );
      }
    };
    return () => { morph.onfinish = null; morph.cancel(); };
  }, [ghost]);

  // Rapidly toggling back must not leave an old shadow flying over the new sidebar.
  useLayoutEffect(() => { if (sessionSidebar) setGhost(null); }, [sessionSidebar]);

  return { sidebarRef, restoreTargetRef, ghostRef, ghost, capture };
}
