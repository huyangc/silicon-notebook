// 档位控件 popover 的水平夹取算法(纯函数,不碰 DOM —— 与 floating-window-logic.ts 同一
// 分工:算术在这里单测,量尺寸与写样式留在组件里)。
//
// 为什么需要它:popover 默认右对齐到控件(CSS 的 `right: 0`),而控件在问答输入行里的落位
// 随宽度变化 —— 模式标签折行时它会落到行首,右对齐的 232px popover 就向左伸出面板,被
// `.workspace-panel { overflow: hidden }` 裁掉。两个静态锚点都救不了,各自在另一段宽度失效:
//
//   right: 0  → 面板 < ~700px：折行后 chip 在行首，向左溢出
//   left: 0   → 面板 ~700–930px：恰好不折行时 chip 贴右缘，向右溢出
//
// 所以只能在打开时量一次再夹回容器内。保持"优先右对齐"是为了让宽屏下的观感与报告那侧
// 完全一致 —— 只有真的放不下时才移动。

export type PopoverClampInput = {
  /** 控件根元素(popover 的定位上下文)的左右边界，视口坐标。 */
  anchorLeft: number;
  anchorRight: number;
  /** popover 自身宽度。 */
  popoverWidth: number;
  /** 可视容器(最近的裁剪祖先与视口的交集)的左右边界，视口坐标。 */
  containerLeft: number;
  containerRight: number;
  /** 容器内缘留白，避免贴边。 */
  margin: number;
};


/**
 * 返回 popover 应当采用的 `left` 偏移（相对控件根元素左边，px）。
 *
 * 优先右对齐控件（与 CSS 默认位一致）；越出容器就夹回内缘。容器窄到连 popover 都放不下时
 * 贴左内缘 —— 此时无论如何都会溢出，靠左至少保住起始那段内容可读。
 */
export function clampPopoverLeft(input: PopoverClampInput): number {
  const {
    anchorLeft, anchorRight, popoverWidth, containerLeft, containerRight, margin,
  } = input;
  const preferred = anchorRight - popoverWidth;
  const min = containerLeft + margin;
  const max = containerRight - margin - popoverWidth;
  const clamped = max < min ? min : Math.min(Math.max(preferred, min), max);
  return clamped - anchorLeft;
}
