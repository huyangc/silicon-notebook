// 引用浮层定位(纯函数,单测于 citation-popover.test.mjs)。
// 锚定被点击的引用徽章 rect:默认在其下方;下方放不下且上方有空间则翻到上方;
// 水平对齐徽章左缘,并把整框夹在视口内(留 margin),避免溢出屏幕。

export type Rect = { top: number; bottom: number; left: number };
export type Size = { width: number; height: number };

export function placeCitationPopover(
  anchor: Rect,
  pop: Size,
  viewport: Size,
  gap = 6,
  margin = 8,
): { top: number; left: number } {
  // 水平:左缘对齐徽章,右侧不越界(先夹右上界,再夹左下界)。
  const maxLeft = Math.max(margin, viewport.width - pop.width - margin);
  const left = Math.max(margin, Math.min(anchor.left, maxLeft));

  // 垂直:默认下方;下方溢出且上方够 → 翻上方;最后整体夹进视口。
  let top = anchor.bottom + gap;
  const overflowsBottom = top + pop.height > viewport.height - margin;
  const roomAbove = anchor.top - gap - pop.height >= margin;
  if (overflowsBottom && roomAbove) top = anchor.top - gap - pop.height;
  const maxTop = Math.max(margin, viewport.height - pop.height - margin);
  top = Math.max(margin, Math.min(top, maxTop));

  return { top, left };
}
