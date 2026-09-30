// 「退出共享」确认面板的落点算术(纯函数,不碰 DOM)。
//
// 落点规则:
//   - 优先放在入口控件**正下方**(留一条缝);
//   - 下方放不下、上方放得下,就翻到入口上方——不去盖住入口;两边都放不下就贴着视口底边;
//   - 四个边都夹在视口内(留 `margin`),面板比视口还高/宽时贴上/左边(面板自己 max-height
//     + 滚动,见 globals.css)。
// 面板是 fixed 浮层,页面滚动或窗口缩放后由调用方重新量入口再调用本函数,所以「跟着走」。

export type Box = { left: number; top: number; right: number; bottom: number };
export type PanelSize = { width: number; height: number };
export type ViewportSize = { width: number; height: number };

export const PLACEMENT_MARGIN = 8;
export const PLACEMENT_GAP = 8;

const clamp = (value: number, low: number, high: number) => Math.max(low, Math.min(value, high));

export function placePanel(
  anchor: Box,
  panel: PanelSize,
  viewport: ViewportSize,
): { left: number; top: number } {
  const left = clamp(
    anchor.left,
    PLACEMENT_MARGIN,
    Math.max(PLACEMENT_MARGIN, viewport.width - panel.width - PLACEMENT_MARGIN),
  );
  const lowest = Math.max(PLACEMENT_MARGIN, viewport.height - panel.height - PLACEMENT_MARGIN);
  const below = anchor.bottom + PLACEMENT_GAP;
  if (below <= lowest) return { left, top: below };
  const above = anchor.top - PLACEMENT_GAP - panel.height;
  if (above >= PLACEMENT_MARGIN) return { left, top: above };
  return { left, top: clamp(below, PLACEMENT_MARGIN, lowest) };
}
