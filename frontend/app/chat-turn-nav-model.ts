// 「本次会话提问导航」的纯逻辑：DOM id 约定 + 滚动高亮命中判定。
// 与 React 组件拆开，便于 node --test 直接跑（本文件不含 JSX）。

/** 第 index 轮对话包裹元素的 DOM id，供导航栏定位滚动目标。 */
export function chatTurnDomId(index: number): string {
  return `chat-turn-${index}`;
}

/**
 * 给定每一轮相对滚动视口顶部的 top 偏移（像素、可为负），返回应高亮的轮次
 * 下标：顶部触发线以上、最靠下的那一轮——也就是「此刻正在读」的那条提问。
 * 各轮自上而下堆叠、tops 单调递增，故遇到第一条尚未越线的即可停止扫描。
 * 纯函数、无 DOM 依赖，便于测试。
 */
export function activeTurnFromTops(tops: number[], triggerLine = 140): number {
  let active = 0;
  for (let i = 0; i < tops.length; i++) {
    if (tops[i] - triggerLine <= 0) active = i;
    else break;
  }
  return active;
}

/**
 * 放大镜（Dock 式鱼眼）：给定每个刻度的中心 y（相对导轨）与指针 y，返回每个刻度
 * 的缩放系数。越靠近指针越大（高斯衰减，峰值 = max），指针离开（null）时全部归 1。
 * transform-origin 取中心/右缘，故缩放不改变中心 y——命中判定可继续用原始中心。
 * 纯函数、无 DOM 依赖，便于测试。
 */
export function magnifyScales(
  centers: number[],
  pointerY: number | null,
  opts: { max?: number; sigma?: number } = {},
): number[] {
  const max = opts.max ?? 2.25;
  const sigma = opts.sigma ?? 34;
  if (pointerY == null || !Number.isFinite(pointerY)) return centers.map(() => 1);
  return centers.map((c) => 1 + (max - 1) * Math.exp(-((c - pointerY) ** 2) / (2 * sigma * sigma)));
}

/** 离指针最近的刻度下标（并列取靠前的一个）；空数组返回 -1。 */
export function nearestIndex(centers: number[], pointerY: number): number {
  let best = -1;
  let bestDist = Number.POSITIVE_INFINITY;
  for (let i = 0; i < centers.length; i++) {
    const d = Math.abs(centers[i] - pointerY);
    if (d < bestDist) {
      bestDist = d;
      best = i;
    }
  }
  return best;
}
