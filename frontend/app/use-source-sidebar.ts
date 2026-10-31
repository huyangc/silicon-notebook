"use client";

import { useEffect, useLayoutEffect, useRef, useState, type CSSProperties, type KeyboardEvent, type PointerEvent as ReactPointerEvent, type RefObject } from "react";
import { canAnimate, placeOver, rectOf, type MorphRect } from "./use-session-sidebar-morph";

// UI geometry, shared by pointer and keyboard collapse. Restore always returns to the CSS default.
const COLLAPSE_EDGE_PX = 64;
const KEYBOARD_STEP_PX = 24;

export function useSourceSidebar({ active, collapsed, ownerKey, onCollapse, restoreTargetRef }: {
  active: boolean; collapsed: boolean; ownerKey: string; onCollapse: () => void;
  restoreTargetRef?: RefObject<MorphRect | null>;
}) {
  const panelRef = useRef<HTMLElement | null>(null);
  const ghostRef = useRef<HTMLDivElement | null>(null);
  const fromRef = useRef<MorphRect | null>(null);
  const focusRestore = useRef(false);
  const [ghost, setGhost] = useState<MorphRect | null>(null);
  const [width, setWidth] = useState<number | null>(null);
  const [dragging, setDragging] = useState(false);
  const drag = useRef<{ id: number; x: number; width: number; previous: number | null; current: number; handle: HTMLElement } | null>(null);

  function stopDrag(rollback = false) {
    const current = drag.current;
    drag.current = null;
    if (current?.handle.hasPointerCapture?.(current.id)) current.handle.releasePointerCapture(current.id);
    if (rollback && current) setWidth(current.previous);
    setDragging(false);
  }

  function captureCollapse() {
    fromRef.current = active ? rectOf(panelRef.current) : null;
    focusRestore.current = Boolean(panelRef.current?.contains(document.activeElement));
    stopDrag();
  }

  useEffect(() => {
    stopDrag(true);
    setWidth(null);
    fromRef.current = null;
    setGhost(null);
    return () => {
      const current = drag.current;
      drag.current = null;
      if (current?.handle.hasPointerCapture?.(current.id)) current.handle.releasePointerCapture(current.id);
    };
  }, [active, ownerKey]);

  useEffect(() => {
    stopDrag();
    setWidth(null);
    if (!collapsed) setGhost(null);
  }, [collapsed]);

  useLayoutEffect(() => {
    const from = fromRef.current;
    fromRef.current = null;
    if (collapsed && from) {
      setGhost(from);
      if (focusRestore.current) document.querySelector<HTMLElement>(".sources-restore-button")?.focus({ preventScroll: true });
    }
  }, [collapsed]);

  useLayoutEffect(() => {
    if (!ghost) return;
    const shadow = ghostRef.current;
    const target = document.querySelector(".sources-restore-button");
    // The non-Ask rail has a full-height hit area; land on its visible icon.
    const to = restoreTargetRef?.current ?? rectOf(target?.matches(".sources-reveal-rail") ? target.querySelector("svg") : target);
    if (!to || !canAnimate(shadow)) { setGhost(null); return; }
    const animation = shadow.animate([
      { transform: "none", opacity: 0.9 },
      { transform: placeOver(ghost, to), opacity: 0.2 },
    ], { duration: 360, easing: "cubic-bezier(0.4, 0, 0.2, 1)", fill: "forwards" });
    animation.onfinish = () => setGhost(null);
    return () => { animation.onfinish = null; animation.cancel(); };
  }, [ghost, restoreTargetRef]);

  function shrink(next: number) {
    if (next <= COLLAPSE_EDGE_PX) { onCollapse(); return; }
    setWidth(next);
  }

  function onPointerDown(event: ReactPointerEvent<HTMLDivElement>) {
    if (!active || collapsed || event.button !== 0 || event.isPrimary === false) return;
    const rect = rectOf(panelRef.current);
    if (!rect) return;
    event.preventDefault();
    event.currentTarget.focus({ preventScroll: true });
    drag.current = { id: event.pointerId, x: event.clientX, width: rect.width, previous: width, current: rect.width, handle: event.currentTarget };
    event.currentTarget.setPointerCapture(event.pointerId);
    setDragging(true);
  }

  function onPointerMove(event: ReactPointerEvent<HTMLDivElement>) {
    const current = drag.current;
    if (!current || current.id !== event.pointerId) return;
    // Rightward movement never grows the column, even after it has already shrunk.
    current.current = Math.min(current.current, current.width + event.clientX - current.x);
    shrink(current.current);
  }

  return {
    panelRef, ghostRef, ghost, captureCollapse, dragging,
    style: width === null ? undefined : { "--sources-width": `${width}px` } as CSSProperties,
    resizeProps: {
      role: "separator" as const, tabIndex: 0, "aria-orientation": "vertical" as const,
      "aria-label": "向左拖动缩小来源栏，拖到边缘收起", "aria-valuemin": COLLAPSE_EDGE_PX,
      "aria-valuenow": width === null ? undefined : Math.round(width),
      "aria-valuetext": width === null ? "默认宽度" : `${Math.round(width)} 像素`,
      onPointerDown, onPointerMove,
      onPointerUp: (event: ReactPointerEvent<HTMLDivElement>) => { if (drag.current?.id === event.pointerId) stopDrag(); },
      onPointerCancel: () => stopDrag(true), onLostPointerCapture: () => stopDrag(true),
      onKeyDown: (event: KeyboardEvent<HTMLDivElement>) => {
        if (event.key === "Escape") { stopDrag(true); return; }
        if (!active || collapsed) return;
        if (event.key === "Home" || event.key === "Enter") { event.preventDefault(); onCollapse(); }
        else if (event.key === "ArrowLeft") {
          event.preventDefault();
          const current = rectOf(panelRef.current);
          if (current) shrink(current.width - KEYBOARD_STEP_PX);
        }
      },
    },
  };
}
