"use client";

// 非根弹窗(浮层面板)的键盘与焦点约定,与 model-service-panel / 扩展弹窗那套同款:
//   - 打开时焦点进入面板(调用方指定落在哪个控件上,破坏性按钮不当默认落点);
//   - Tab / Shift+Tab 只在面板内循环;
//   - Escape 交给 `onEscape`(调用方决定此刻能不能关);
//   - 焦点在面板开着期间丢到 body 上(比如刚获得焦点的按钮被禁用/卸载)时收回到落点;
//   - 可选 `restoreFocus`:挂载时记下打开它的元素,卸载时把焦点还回去(那个元素还在、
//     且不在 inert 子树里时)。
// Tab 陷阱只管「焦点在本层或落到本层外的普通内容」:别的层已经处理过这次按键
// (defaultPrevented),或焦点正在本层之外的另一个对话框里(比如盖在上面的根弹窗、
// 传送到 body 的嵌套弹窗),都不插手,由那一层自己管。
// 使用方:退出共享面板与它的目标笔记本选择器、Knowhow 面板(S7)。

import { useEffect, useLayoutEffect, useRef, type RefObject } from "react";

const FOCUSABLE =
  'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

export const focusableIn = (container: HTMLElement): HTMLElement[] =>
  Array.from(container.querySelectorAll<HTMLElement>(FOCUSABLE));

export function useDialogFocus({
  containerRef,
  active,
  initialFocusRef,
  onEscape,
  restoreFocus = false,
}: {
  containerRef: RefObject<HTMLElement | null>;
  /** 这一层此刻是不是最上层(下面还压着另一层时置 false,不抢焦点也不设陷阱)。 */
  active: boolean;
  initialFocusRef?: RefObject<HTMLElement | null>;
  onEscape?: () => void;
  /** 卸载时把焦点还给挂载那一刻的焦点元素(打开本层的按钮)。 */
  restoreFocus?: boolean;
}) {
  // 打开本层的元素:必须在下面那个 layout effect 把焦点移进来之前记下(同一组件内
  // effect 按声明顺序执行)。
  const returnFocusRef = useRef<HTMLElement | null>(null);
  useLayoutEffect(() => {
    if (!restoreFocus) return undefined;
    const opener = document.activeElement;
    // 子组件在同一次提交里先拿到焦点（autoFocus）时，那不是「打开本层的元素」。
    const inside = opener instanceof Node && containerRef.current?.contains(opener);
    returnFocusRef.current = opener instanceof HTMLElement && opener !== document.body && !inside ? opener : null;
    return undefined;
  }, []); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => {
    if (!restoreFocus) return undefined;
    return () => {
      const opener = returnFocusRef.current;
      returnFocusRef.current = null;
      // 卸载发生在关掉本层的那次提交之后:同一次提交里撤掉的 inert 已经生效。
      if (opener && opener.isConnected && !opener.closest("[inert]")) opener.focus();
    };
  }, []); // eslint-disable-line react-hooks/exhaustive-deps

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
      if (event.key !== "Tab" || event.defaultPrevented) return;
      const container = containerRef.current;
      if (!container) return;
      const current = document.activeElement;
      const otherLayer = current instanceof Element ? current.closest('dialog, [role="dialog"]') : null;
      if (otherLayer && !container.contains(otherLayer)) return;
      const focusable = focusableIn(container);
      if (focusable.length === 0) {
        event.preventDefault();
        container.focus();
        return;
      }
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
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
