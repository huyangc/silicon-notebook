"use client";

// 非根弹窗(浮层面板)的键盘与焦点约定,与 model-service-panel / 扩展弹窗那套同款:
//   - 打开时焦点进入面板(调用方指定落在哪个控件上,破坏性按钮不当默认落点);
//   - Tab / Shift+Tab 只在面板内循环;
//   - Escape 交给 `onEscape`(调用方决定此刻能不能关);
//   - 焦点在面板开着期间丢到 body 上(比如刚获得焦点的按钮被禁用/卸载)时收回到落点。
// 仓库里没有共享的焦点陷阱 helper(每个弹窗各写一份),这里只服务退出共享面板与它的
// 目标笔记本选择器这两层,不去改别人的弹窗。

import { useEffect, useLayoutEffect, type RefObject } from "react";

const FOCUSABLE =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export const focusableIn = (container: HTMLElement): HTMLElement[] =>
  Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE));

export function useDialogFocus({
  containerRef,
  active,
  initialFocusRef,
  onEscape,
}: {
  containerRef: RefObject<HTMLElement | null>;
  /** 这一层此刻是不是最上层(下面还压着另一层时置 false,不抢焦点也不设陷阱)。 */
  active: boolean;
  initialFocusRef?: RefObject<HTMLElement | null>;
  onEscape?: () => void;
}) {
  const focusInitial = () => {
    const container = containerRef.current;
    if (!container) return;
    const target = initialFocusRef?.current ?? focusableIn(container)[0] ?? container;
    target.focus();
  };

  // 变成最上层的那一刻把焦点移进来。
  useLayoutEffect(() => {
    if (active) focusInitial();
  }, [active]); // eslint-disable-line react-hooks/exhaustive-deps

  // 每次渲染后:面板开着而焦点丢到 body(而不是被用户主动带去别处)时收回。
  useEffect(() => {
    if (!active) return;
    const current = document.activeElement;
    if (!current || current === document.body) focusInitial();
  });

  useEffect(() => {
    if (!active) return undefined;
    function onKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") {
        onEscape?.();
        return;
      }
      if (event.key !== "Tab") return;
      const container = containerRef.current;
      if (!container) return;
      const focusable = focusableIn(container);
      if (focusable.length === 0) {
        event.preventDefault();
        container.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      const current = document.activeElement;
      if (!(current instanceof HTMLElement) || !container.contains(current)) {
        event.preventDefault();
        (event.shiftKey ? last : first).focus();
      } else if (event.shiftKey && current === first) {
        event.preventDefault();
        last.focus();
      } else if (!event.shiftKey && current === last) {
        event.preventDefault();
        first.focus();
      }
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, [active, containerRef, onEscape]);
}
