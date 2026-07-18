// useFloatingWindow —— 浮窗拖动 + resize + 位置记忆的可复用 hook（地基，本身
// 不渲染任何 UI）。所有几何计算/校验都委托给 floating-window-logic.ts 的纯
// 函数，本文件只负责：Pointer Events 绑定与清理、measure 真实 DOM 尺寸、rAF
// 合帧节流、sessionStorage 读写。消费方（7 个 knowhow 弹窗，下一个 task 接入）
// 把 `style` 贴到卡片元素、`cardRef` 挂到同一个元素、`dragHandleProps` 展开到
// header、`resizeHandleProps` 展开到右下角的一个小手柄元素上。
//
// 为什么本 hook 自己拿一个 cardRef、而不是让调用方传入卡片的实际尺寸：
// 这几个弹窗的卡片宽度各不相同（880/760…），高度大多是 `max-height` 限制下的
// auto（随内容撑开），根本没有一个"默认尺寸"数字可以硬编码或者让调用方传入——
// 唯一靠谱的尺寸来源是 DOM 自己的 getBoundingClientRect()。这也是本 hook 的
// 返回值比任务简报里给的示意类型多出 cardRef 字段的原因。
"use client";

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type CSSProperties,
  type PointerEvent as ReactPointerEvent,
  type RefObject,
} from "react";
import {
  DEFAULT_MIN_HEIGHT,
  DEFAULT_MIN_VISIBLE,
  DEFAULT_MIN_WIDTH,
  DEFAULT_WINDOW_RECT,
  clampToViewport,
  nextRectOnDrag,
  nextRectOnResize,
  parseWindowRect,
  serializeWindowRect,
  type ResolvedRect,
  type Viewport,
  type WindowRect,
} from "./floating-window-logic.ts";

export type UseFloatingWindowOptions = {
  /** 每个弹窗一个键，沿用 "knowhow.xxx.window" 风格（镜像
   * knowhow-cell-editor.tsx 的 FULLSCREEN_STORAGE_KEY 命名习惯）。 */
  storageKey: string;
  /** 默认 true——个别弹窗（如内容天然定高的向导步骤）可传 false 只保留拖动。 */
  resizable?: boolean;
  minWidth?: number;
  minHeight?: number;
  /** 全屏时传 true：拖动/resize 完全禁用，style 不再施加任何偏移/尺寸覆盖，
   * 让全屏态的 CSS（.kh-modal-card--fullscreen 等）完全接管。 */
  disabled?: boolean;
};

export type FloatingWindowHandleProps = {
  onPointerDown: (event: ReactPointerEvent<HTMLElement>) => void;
  /** touchAction:"none" 防止触屏/触控板把拖动手势当成页面滚动；drag 手柄额外带
   * grab/grabbing 光标。展开到目标元素的 style 上，与元素自身样式合并。 */
  style: CSSProperties;
};

export type UseFloatingWindowResult = {
  /** 挂到卡片根元素（.kh-modal-card / .knowhow-import-card 等）—— style 生效
   * 的同一个元素，也是量测真实渲染尺寸的来源。 */
  cardRef: RefObject<HTMLDivElement | null>;
  /** 贴给卡片：transform 偏移 + （resize 过才有的）width/height 覆盖。 */
  style: CSSProperties;
  /** 展开到 header（拖动手柄）上。 */
  dragHandleProps: FloatingWindowHandleProps;
  /** 展开到右下角 resize 手柄元素上。resizable=false 时 onPointerDown 是
   * no-op——消费方不需要按 resizable 条件渲染手柄本身也不会有副作用，但通常
   * 仍建议按 resizable 决定是否渲染，避免出现一个不响应的手柄。 */
  resizeHandleProps: FloatingWindowHandleProps;
  isDragging: boolean;
  /** 回默认位置/尺寸（居中、跟随 CSS 默认），同时清掉 sessionStorage 里的记忆。 */
  reset: () => void;
};

function readViewport(): Viewport {
  if (typeof window === "undefined") return { width: 0, height: 0 };
  return { width: window.innerWidth, height: window.innerHeight };
}

function readStoredRect(storageKey: string): WindowRect {
  if (typeof window === "undefined") return DEFAULT_WINDOW_RECT;
  try {
    return parseWindowRect(window.sessionStorage.getItem(storageKey));
  } catch {
    // 隐私模式/存储被禁用等——静默回退默认，不影响浮窗本身可用。
    return DEFAULT_WINDOW_RECT;
  }
}

function writeStoredRect(storageKey: string, rect: WindowRect): void {
  if (typeof window === "undefined") return;
  try {
    window.sessionStorage.setItem(storageKey, serializeWindowRect(rect));
  } catch {
    // 隐私模式/配额溢出等——静默，只是记不住这次的位置，不影响拖动/resize 本身。
  }
}

type DragOrigin = {
  pointerId: number;
  startClientX: number;
  startClientY: number;
  size: { width: number; height: number };
  startX: number;
  startY: number;
};

type ResizeOrigin = {
  pointerId: number;
  startClientX: number;
  startClientY: number;
  size: { width: number; height: number };
  x: number;
  y: number;
};

export function useFloatingWindow(options: UseFloatingWindowOptions): UseFloatingWindowResult {
  const {
    storageKey,
    resizable = true,
    minWidth = DEFAULT_MIN_WIDTH,
    minHeight = DEFAULT_MIN_HEIGHT,
    disabled = false,
  } = options;

  const [rect, setRect] = useState<WindowRect>(() => readStoredRect(storageKey));
  const [isDragging, setIsDragging] = useState(false);
  const [isResizing, setIsResizing] = useState(false);

  const cardRef = useRef<HTMLDivElement | null>(null);

  // rect 状态的镜像 ref：window 级 pointermove 监听器在一次拖动/resize 会话期间
  // 只挂载一次（下面的 effect 以 isDragging/isResizing 为依赖，不是每次 rect
  // 变化都重新绑定），靠闭包读 state 会拿到绑定那一刻的旧值（stale closure）；
  // 改读 ref 就总是最新值，不需要为了"读到最新 rect"而频繁重新绑定/解绑监听器。
  const rectRef = useRef(rect);
  const disabledRef = useRef(disabled);
  const resizableRef = useRef(resizable);
  const minWidthRef = useRef(minWidth);
  const minHeightRef = useRef(minHeight);

  useEffect(() => {
    rectRef.current = rect;
  }, [rect]);
  useEffect(() => {
    disabledRef.current = disabled;
  }, [disabled]);
  useEffect(() => {
    resizableRef.current = resizable;
  }, [resizable]);
  useEffect(() => {
    minWidthRef.current = minWidth;
  }, [minWidth]);
  useEffect(() => {
    minHeightRef.current = minHeight;
  }, [minHeight]);

  const rafRef = useRef<number | null>(null);
  const pendingRectRef = useRef<WindowRect | null>(null);
  const dragOriginRef = useRef<DragOrigin | null>(null);
  const resizeOriginRef = useRef<ResizeOrigin | null>(null);

  // storageKey 理论上每个弹窗实例固定不变，但防御性地支持它变化时重新读取对应
  // 键的记忆值（而不是继续沿用上一个键读到的 rect）。
  useEffect(() => {
    setRect(readStoredRect(storageKey));
  }, [storageKey]);

  // 持久化：rect 变化就写 sessionStorage。写入频率已经被 scheduleRectUpdate 的
  // rAF 合帧天然限制在"每帧最多一次"，这里不需要再单独加一层节流/防抖。
  useEffect(() => {
    writeStoredRect(storageKey, rect);
  }, [storageKey, rect]);

  // 卸载时清理还没跑到的 rAF——避免 setState 在组件卸载之后才触发（React 会警告，
  // 且那次更新本来就没有意义了）。
  useEffect(() => {
    return () => {
      if (rafRef.current !== null) {
        window.cancelAnimationFrame(rafRef.current);
        rafRef.current = null;
      }
    };
  }, []);

  const measureSize = useCallback((): { width: number; height: number } | null => {
    const node = cardRef.current;
    if (!node) return null;
    const box = node.getBoundingClientRect();
    return { width: box.width, height: box.height };
  }, []);

  // pointermove 在真实拖动中高频触发（远超 60fps 的输入采样率在触控板上很常见）。
  // 用 rAF 把同一帧内的多次更新合并成最多一次 setState：拖动本身仍然每帧都跟手
  // （不是"跳帧"意义上的节流），但避免了每个 pointermove 都同步触发一次 React
  // 渲染 + sessionStorage 写入。持久化 effect 挂在 rect 上，而 rect 的提交已经
  // 被这里限制到"每帧最多一次"，两个要求（视觉流畅 / 别刷爆 storage）用同一个
  // 节流机制就都满足了。
  const scheduleRectUpdate = useCallback((updater: (prev: WindowRect) => WindowRect) => {
    pendingRectRef.current = updater(pendingRectRef.current ?? rectRef.current);
    if (rafRef.current !== null) return;
    rafRef.current = window.requestAnimationFrame(() => {
      rafRef.current = null;
      const next = pendingRectRef.current;
      pendingRectRef.current = null;
      if (next) setRect(next);
    });
  }, []);

  // --- 拖动：header 上的 pointerdown 起手，window 级 pointermove/up 驱动 -------

  const onDragPointerDown = useCallback(
    (event: ReactPointerEvent<HTMLElement>) => {
      if (disabledRef.current) return;
      if (event.pointerType === "mouse" && event.button !== 0) return; // 只响应鼠标主键；触屏/触控笔不受此限
      // header 上总跟着关闭/全屏/编辑等按钮（或 KnowhowRowContext 那类
      // role="button" 条目）——pointerdown 命中它们时必须放行，不能当成拖动
      // 起手。这不是"更保险"式的防御性编程：已用一个独立最小复现页面实测
      // 验证过，下面的 setPointerCapture 一旦在 header 上调用成功，浏览器会
      // 把随后的 pointerup retarget 到 header 自己身上（不再是原始按钮），
      // 原生 click 事件因此完全不会在按钮上触发——不加这道判断，header 挂
      // 上 dragHandleProps 之后，header 里的每一个按钮都会失灵。下面这份
      // selector 是一份具体标签/属性的白名单（同一浏览器复现也确认过
      // contenteditable 后代会重现一模一样的 retargeting 失败，故一并列入），
      // 不是"是否可交互"的语义判定——closest() 只放行列在这份名单里的元素，
      // 以后 header 里长出名单之外的新交互元素类型，得手动把它加进来才会
      // 被放行，不会自动识别。
      const interactiveTarget = (event.target as HTMLElement | null)?.closest(
        'button, a, input, textarea, select, [role="button"], [contenteditable]:not([contenteditable="false"])',
      );
      if (interactiveTarget) return;
      const size = measureSize();
      if (!size) return;
      event.preventDefault(); // 防止拖动触发文本选中/原生图片拖拽 ghost
      event.stopPropagation(); // 别冒泡到 overlay 的背景点击关闭处理
      try {
        event.currentTarget.setPointerCapture(event.pointerId);
      } catch {
        // Pointer Capture 不可用时静默降级——仍靠下面挂在 window 上的监听工作。
      }
      dragOriginRef.current = {
        pointerId: event.pointerId,
        startClientX: event.clientX,
        startClientY: event.clientY,
        size,
        startX: rectRef.current.x,
        startY: rectRef.current.y,
      };
      setIsDragging(true);
    },
    [measureSize],
  );

  useEffect(() => {
    if (!isDragging) return;
    const origin = dragOriginRef.current;
    if (!origin) return;

    function handleMove(event: PointerEvent) {
      if (!origin || event.pointerId !== origin.pointerId) return;
      const deltaX = event.clientX - origin.startClientX;
      const deltaY = event.clientY - origin.startClientY;
      const start: ResolvedRect = { x: origin.startX, y: origin.startY, width: origin.size.width, height: origin.size.height };
      const next = nextRectOnDrag(start, deltaX, deltaY, readViewport(), { minVisible: DEFAULT_MIN_VISIBLE });
      scheduleRectUpdate((prev) => ({ ...prev, x: next.x, y: next.y }));
    }

    function endDrag() {
      dragOriginRef.current = null;
      setIsDragging(false);
    }

    window.addEventListener("pointermove", handleMove);
    window.addEventListener("pointerup", endDrag);
    window.addEventListener("pointercancel", endDrag);
    // 拖动中禁止选中文本——卡片内容（markdown 正文等）本身可选中，快速拖动时
    // 鼠标很容易"顺带"划选一片文字，体验上很糟。
    const previousUserSelect = document.body.style.userSelect;
    document.body.style.userSelect = "none";

    return () => {
      window.removeEventListener("pointermove", handleMove);
      window.removeEventListener("pointerup", endDrag);
      window.removeEventListener("pointercancel", endDrag);
      document.body.style.userSelect = previousUserSelect;
    };
    // 组件卸载时这个 cleanup 也会跑（React 对所有 effect 一视同仁），满足
    // "监听挂在 window 上、结束时必须清理（含组件卸载时）"的要求。
  }, [isDragging, scheduleRectUpdate]);

  // --- resize：右下角手柄上的 pointerdown 起手，逻辑与拖动镜像 -----------------

  const onResizePointerDown = useCallback(
    (event: ReactPointerEvent<HTMLElement>) => {
      if (disabledRef.current || !resizableRef.current) return;
      if (event.pointerType === "mouse" && event.button !== 0) return;
      const size = measureSize();
      if (!size) return;
      event.preventDefault();
      event.stopPropagation();
      try {
        event.currentTarget.setPointerCapture(event.pointerId);
      } catch {
        // 同上——静默降级。
      }
      resizeOriginRef.current = {
        pointerId: event.pointerId,
        startClientX: event.clientX,
        startClientY: event.clientY,
        size,
        x: rectRef.current.x,
        y: rectRef.current.y,
      };
      setIsResizing(true);
    },
    [measureSize],
  );

  useEffect(() => {
    if (!isResizing) return;
    const origin = resizeOriginRef.current;
    if (!origin) return;

    function handleMove(event: PointerEvent) {
      if (!origin || event.pointerId !== origin.pointerId) return;
      const deltaX = event.clientX - origin.startClientX;
      const deltaY = event.clientY - origin.startClientY;
      const viewport = readViewport();
      const start: ResolvedRect = { x: origin.x, y: origin.y, width: origin.size.width, height: origin.size.height };
      const nextSize = nextRectOnResize(start, deltaX, deltaY, viewport, {
        minWidth: minWidthRef.current,
        minHeight: minHeightRef.current,
      });
      // resize 手柄只在右下角、左上角(x,y)语义上不动，但 resize 后卡片变大仍
      // 可能把 header 顶出视口（比如卡片本来贴着屏幕底部，一变高顶部就没了）——
      // 用新尺寸再 clamp 一次位置，双保险。
      const reclamped = clampToViewport(
        { x: origin.x, y: origin.y, width: nextSize.width, height: nextSize.height },
        viewport,
        { minVisible: DEFAULT_MIN_VISIBLE },
      );
      scheduleRectUpdate((prev) => ({
        ...prev,
        x: reclamped.x,
        y: reclamped.y,
        width: nextSize.width,
        height: nextSize.height,
      }));
    }

    function endResize() {
      resizeOriginRef.current = null;
      setIsResizing(false);
    }

    window.addEventListener("pointermove", handleMove);
    window.addEventListener("pointerup", endResize);
    window.addEventListener("pointercancel", endResize);
    const previousUserSelect = document.body.style.userSelect;
    document.body.style.userSelect = "none";

    return () => {
      window.removeEventListener("pointermove", handleMove);
      window.removeEventListener("pointerup", endResize);
      window.removeEventListener("pointercancel", endResize);
      document.body.style.userSelect = previousUserSelect;
    };
  }, [isResizing, scheduleRectUpdate]);

  // --- 浏览器视口 resize：把窗口重新 clamp 回视野内 ----------------------------
  //
  // 用户拖窗口变小之后，之前记忆的偏移量可能让卡片（或至少 header）跑到新视口
  // 外面——不重新 clamp 的话，刷新前的这个会话里就再也够不着了。

  useEffect(() => {
    if (typeof window === "undefined") return;

    function handleViewportResize() {
      if (disabledRef.current) return;
      const size = measureSize();
      if (!size) return;
      const viewport = readViewport();
      const current = rectRef.current;
      const clamped = clampToViewport(
        { x: current.x, y: current.y, width: size.width, height: size.height },
        viewport,
        { minVisible: DEFAULT_MIN_VISIBLE },
      );
      if (clamped.x === current.x && clamped.y === current.y) return; // 常见情况：本来就在界内，跳过一次无意义的 setState
      scheduleRectUpdate((prev) => ({ ...prev, x: clamped.x, y: clamped.y }));
    }

    window.addEventListener("resize", handleViewportResize);
    return () => window.removeEventListener("resize", handleViewportResize);
  }, [measureSize, scheduleRectUpdate]);

  // --- 派生输出 -----------------------------------------------------------------

  const style = useMemo<CSSProperties>(() => {
    if (disabled) return {};
    return {
      transform: `translate3d(${rect.x}px, ${rect.y}px, 0)`,
      ...(rect.width !== null ? { width: `${rect.width}px` } : {}),
      // 被 resize 过就把内联 height 顶上去，同时用 maxHeight:none 顶开卡片 CSS
      // 的 max-height（各弹窗多是 max-height:80vh）——否则内联 height 会被
      // max-height 压回 80vh，用户拉不高（Y 方向卡死的真凶）。resize 逻辑已
      // 封顶到 VIEWPORT_MAX_RATIO，放开 max-height 也不会撑出屏幕。
      ...(rect.height !== null ? { height: `${rect.height}px`, maxHeight: "none" } : {}),
    };
  }, [rect, disabled]);

  const dragHandleStyle = useMemo<CSSProperties>(
    () => ({ touchAction: "none", cursor: disabled ? undefined : isDragging ? "grabbing" : "grab" }),
    [disabled, isDragging],
  );

  const resizeHandleStyle = useMemo<CSSProperties>(() => ({ touchAction: "none" }), []);

  const reset = useCallback(() => {
    pendingRectRef.current = null;
    if (rafRef.current !== null) {
      window.cancelAnimationFrame(rafRef.current);
      rafRef.current = null;
    }
    setRect(DEFAULT_WINDOW_RECT);
  }, []);

  return {
    cardRef,
    style,
    dragHandleProps: { onPointerDown: onDragPointerDown, style: dragHandleStyle },
    resizeHandleProps: { onPointerDown: onResizePointerDown, style: resizeHandleStyle },
    isDragging,
    reset,
  };
}
