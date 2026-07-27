/**
 * effort-picker.tsx
 *
 * 「档位」共享控件:一个 chip 按钮 + 向上弹出的调档 popover(标题 + 当前档名 +
 * 均布圆点滑块 + 该档一句说明 + 可选补充块)。
 *
 * 两处复用同一套界面:
 * - 深度报告生成区的「研究深度」(report-view.tsx)
 * - 问答输入行「逐步推理」的「检索档位」(page.tsx)
 *
 * 两者档名与档数本来就一致(概览/标准/深入/详尽/穷尽),此前却是两套完全不同的控件:
 * 报告是滑块 popover,问答是一排平铺 chip 外挂一个「阈值」折叠块。控件在这里收敛为
 * 一处实现,调用方只提供档位表与当前值。
 */
"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";

import { clampPopoverLeft } from "./effort-picker-logic";


/** popover 与容器内缘之间保留的留白。 */
const POPOVER_MARGIN = 8;


/**
 * 最近的水平裁剪祖先与视口的交集。问答那侧真正裁掉 popover 的是
 * `.workspace-panel { overflow: hidden }`,它比视口窄得多,只夹视口不够。
 */
function clippingBounds(node: HTMLElement): { left: number; right: number } {
  let left = 0;
  // 视口只是兜底上界。隐藏/离屏的渲染环境会把 innerWidth 报成 0(内嵌预览面板实测如此),
  // 那样会把可用区间压成负数、把 popover 钉到荒谬的位置 —— 拿不到可信宽度时干脆不设上界,
  // 让真正裁剪的祖先说了算。
  const viewport = typeof window === "undefined" ? 0 : window.innerWidth;
  let right = viewport > 0 ? viewport : Number.POSITIVE_INFINITY;
  for (let el = node.parentElement; el; el = el.parentElement) {
    const style = window.getComputedStyle(el);
    // 真实浏览器会把 `overflow: hidden` 简写展开进 overflowX;jsdom 不一定,所以简写兜底。
    // 目标那条正是简写(`.workspace-panel { overflow: hidden }`),只读 overflowX 会漏掉它。
    const overflowX = style.overflowX || style.overflow;
    if (!overflowX || overflowX === "visible") continue;
    const box = el.getBoundingClientRect();
    if (box.width <= 0) continue;
    left = Math.max(left, box.left);
    right = Math.min(right, box.right);
  }
  return { left, right };
}


export type EffortOption = {
  /** 稳定 id,用于 value / onChange;不进入界面文案。 */
  id: string;
  /** 档名,如「标准」。chip、popover 标题右侧与滑块无障碍值都用它。 */
  label: string;
  /** 选中该档时显示的一句中性说明(不用快/聪明这类措辞)。 */
  hint: string;
};


type EffortPickerProps = {
  /** chip 上的前缀词,如「深度」「档位」。 */
  chipLabel: string;
  /** popover 标题,同时作为控件与滑块的无障碍名,如「研究深度」「检索档位」。 */
  title: string;
  options: readonly EffortOption[];
  /** 当前档 id;不在 options 内(如历史值已下线)时回落到第一档。 */
  value: string;
  onChange: (id: string) => void;
  disabled?: boolean;
  /** 紧凑尺寸:嵌在问答输入控制行时与相邻的 .mode-tab 同高。 */
  compact?: boolean;
};


export function EffortPicker({
  chipLabel,
  title,
  options,
  value,
  onChange,
  disabled = false,
  compact = false,
}: EffortPickerProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement | null>(null);
  const popoverRef = useRef<HTMLDivElement | null>(null);
  // null = 用 CSS 的默认位(右对齐控件);量出越界才写具体 left。
  const [popoverLeft, setPopoverLeft] = useState<number | null>(null);

  // 打开时把 popover 夹回可视容器内。用 layout effect 是为了在绘制前完成,不让它先
  // 在越界位置闪一帧;宽度变化会改变控件在折行里的落位,所以 resize 时重算。
  useLayoutEffect(() => {
    if (!open) {
      setPopoverLeft(null);
      return;
    }
    const sync = () => {
      const root = rootRef.current;
      const popover = popoverRef.current;
      if (!root || !popover) return;
      const anchor = root.getBoundingClientRect();
      const box = popover.getBoundingClientRect();
      // 尚未布局(如 jsdom 默认全零)时不要写入一个凭空算出的位置——保持 CSS 默认位。
      if (box.width <= 0) return;
      const bounds = clippingBounds(root);
      setPopoverLeft(clampPopoverLeft({
        anchorLeft: anchor.left,
        anchorRight: anchor.right,
        popoverWidth: box.width,
        containerLeft: bounds.left,
        containerRight: bounds.right,
        margin: POPOVER_MARGIN,
      }));
    };
    sync();
    window.addEventListener("resize", sync);
    return () => window.removeEventListener("resize", sync);
  }, [open]);

  // 点外部 / Esc 关闭(镜像 page.tsx 的菜单收起写法)。
  useEffect(() => {
    if (!open) return;
    function handlePointerDown(event: PointerEvent) {
      const target = event.target;
      if (target instanceof Node && rootRef.current?.contains(target)) return;
      setOpen(false);
    }
    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") setOpen(false);
    }
    window.addEventListener("pointerdown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown);
    return () => {
      window.removeEventListener("pointerdown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown);
    };
  }, [open]);

  // 转入禁用态(提交中 / 会话加载中)必须收起:chip 已不可点,留着的 popover 会浮在
  // 屏幕上且再也关不掉。
  useEffect(() => {
    if (disabled) setOpen(false);
  }, [disabled]);

  if (options.length === 0) return null;
  const found = options.findIndex((option) => option.id === value);
  const index = found >= 0 ? found : 0;
  const current = options[index];

  return (
    <div className={`effort-picker${compact ? " compact" : ""}`} ref={rootRef}>
      <button
        type="button"
        className={`effort-picker-chip${open ? " open" : ""}`}
        disabled={disabled}
        aria-haspopup="dialog"
        aria-expanded={open}
        aria-label={`${title}：${current.label}`}
        onClick={() => setOpen((shown) => !shown)}
      >
        <span className="effort-picker-chip-label">{chipLabel}</span>
        <span className="effort-picker-chip-value">{current.label}</span>
      </button>
      {open && (
        <div
          className="effort-picker-popover"
          role="dialog"
          aria-label={title}
          ref={popoverRef}
          style={popoverLeft === null ? undefined : { left: popoverLeft, right: "auto" }}
        >
          <div className="effort-picker-popover-head">
            <span className="effort-picker-popover-title">{title}</span>
            <span className="effort-picker-popover-current">{current.label}</span>
          </div>
          <div className="effort-picker-slider">
            <div className="effort-picker-slider-track" aria-hidden>
              {options.map((option) => (
                <span key={option.id} className="effort-picker-slider-dot" />
              ))}
            </div>
            <input
              type="range"
              className="effort-picker-slider-input"
              min={0}
              max={options.length - 1}
              step={1}
              value={index}
              aria-label={title}
              aria-valuetext={current.label}
              onChange={(event) => onChange(options[Number(event.target.value)].id)}
            />
          </div>
          <p className="effort-picker-popover-hint">{current.hint}</p>
        </div>
      )}
    </div>
  );
}
