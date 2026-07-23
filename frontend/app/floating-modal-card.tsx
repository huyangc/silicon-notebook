"use client";

import type { ReactNode } from "react";

import { useFloatingWindow, type UseFloatingWindowResult } from "./use-floating-window";

/**
 * 让「居中浮动弹窗」可拖动的最小包装：把 useFloatingWindow 收进来，只接管**卡片
 * 元素**（挂 cardRef + 施加 transform 偏移的 style）。背景遮罩、背景点击关闭、
 * header/body 的具体结构都留在调用点原样不动——用 render-prop 把 `floating` 交回
 * 去，调用方只需把 `floating.dragHandleProps` 展开到自己的标题栏(拖动手柄)上。
 *
 * 为什么用 render-prop 而不是把 overlay/header 也包进来：page.tsx 里十几个弹窗共享
 * `.utility-modal > .utility-modal-card > .source-modal-header` 的骨架，但遮罩类名
 * (utility-modal / source-modal / utility-modal-top)、是否背景点击关闭、标题栏里的
 * 操作按钮各不相同。只包卡片、把手柄交回调用点，改动面最小、也不动各弹窗既有的
 * 关闭/无障碍语义。默认 resizable=false（本轮只做「可拖动」，不给这些弹窗加缩放
 * 手柄）；已经自带拖动+缩放的 knowhow 系弹窗不走这里、保持原样。
 *
 * ⚠️ useFloatingWindow 桌面态始终给卡片施加 `translate3d(...)`（静止时也是
 * translate3d(0,0,0)），卡片因此成为其 `position:fixed` 后代的**包含块**。故被包裹
 * 弹窗体内不要放 `position:fixed` 的下拉/浮层/portal，否则会相对卡片而非视口定位；
 * 需要脱离卡片定位的浮层请渲染到弹窗子树之外（与 source-detail / knowhow 系既有
 * 可拖动弹窗同一约束）。
 */
type FloatingModalCardProps = {
  /** 每个弹窗一个稳定键，沿用 "xxx.window" 风格；用于本会话内记住拖动后的位置。 */
  storageKey: string;
  /** 卡片自身的类名（如 "utility-modal-card"、"utility-modal-card narrow"）。 */
  className: string;
  resizable?: boolean;
  children: (floating: UseFloatingWindowResult) => ReactNode;
};

export function FloatingModalCard({
  storageKey,
  className,
  resizable = false,
  children,
}: FloatingModalCardProps) {
  const floating = useFloatingWindow({ storageKey, resizable });
  return (
    <div ref={floating.cardRef} className={className} style={floating.style}>
      {children(floating)}
    </div>
  );
}
