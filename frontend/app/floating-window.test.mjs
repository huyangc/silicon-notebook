import test from "node:test";
import assert from "node:assert/strict";

import {
  DEFAULT_MIN_VISIBLE,
  DEFAULT_MIN_WIDTH,
  DEFAULT_MIN_HEIGHT,
  VIEWPORT_MAX_RATIO,
  DEFAULT_WINDOW_RECT,
  clampToViewport,
  nextRectOnDrag,
  nextRectOnResize,
  serializeWindowRect,
  parseWindowRect,
  clampRectSizeToViewport,
  isFloatingDisabledWidth,
  FLOATING_DISABLED_MAX_WIDTH,
} from "./floating-window-logic.ts";

// --- 常量：地基默认值 ---------------------------------------------------------

test("DEFAULT_WINDOW_RECT: 偏移 0、宽高 null（跟随 CSS 默认尺寸、居中不偏移）", () => {
  assert.deepStrictEqual(DEFAULT_WINDOW_RECT, { x: 0, y: 0, width: null, height: null });
});

test("DEFAULT_MIN_VISIBLE: 48px（header 拖出视口后仍保证可抓回的最小余量）", () => {
  assert.strictEqual(DEFAULT_MIN_VISIBLE, 48);
});

// --- clampToViewport ----------------------------------------------------------
//
// rect 的 x/y 是「相对默认居中位置」的偏移量；clamp 的实现先换算出卡片在视口坐标
// 系下的绝对 left/top（= 居中基准 + 偏移），钳制这个绝对位置，再换算回偏移量返回。
//
// 水平方向：header 横跨卡片整个宽度，抓哪一段都行，用「卡片宽度」做对称钳制——
// 至少 minVisible px 的卡片宽度留在视口内（可能是左边、也可能是右边那一小条）。
//
// 垂直方向刻意不对称、且不依赖卡片高度：拖动手柄只在卡片顶部（header），所以
// 只保证「顶部」这一条边离视口上下边界最多 minVisible px——不管卡片多高，往上
// 拖到底也只裁掉 minVisible px（header 仍大部分可见），不会像「用卡片高度做对称
// 钳制」那样把整个 header 都拖出屏幕、只剩底部一角留在视口里（那样就真的抓不
// 回来了，是本函数要专门防的经典坑）。
//
// 视口 1200x800、卡片 880x600 时：naturalLeft=(1200-880)/2=160，
// naturalTop=(800-600)/2=100（后续用例复用这组几何关系）。

const VIEWPORT = { width: 1200, height: 800 };
const CARD = { width: 880, height: 600 };

test("clampToViewport: 卡片在视口内、offset 为 0 → 原样返回（无需钳制）", () => {
  const rect = { x: 0, y: 0, ...CARD };
  assert.deepStrictEqual(clampToViewport(rect, VIEWPORT), { x: 0, y: 0 });
});

test("clampToViewport: 向右拖出视口 → 钳制到只剩 minVisible px 露出（左边缘留在视口内）", () => {
  const rect = { x: 2000, y: 0, ...CARD };
  const result = clampToViewport(rect, VIEWPORT);
  assert.strictEqual(result.x, 992); // naturalLeft(160)+x = 1200-48 → x=992
  assert.strictEqual(result.y, 0);
});

test("clampToViewport: 向左拖出视口 → 钳制到只剩 minVisible px 露出（右边缘留在视口内）", () => {
  const rect = { x: -2000, y: 0, ...CARD };
  const result = clampToViewport(rect, VIEWPORT);
  assert.strictEqual(result.x, -992); // naturalLeft(160)+x = -832 → x=-992
});

test("clampToViewport: 向下拖出视口 → 钳制到 header（卡片顶部）仍留 minVisible px 在视口内", () => {
  const rect = { x: 0, y: 2000, ...CARD };
  const result = clampToViewport(rect, VIEWPORT);
  assert.strictEqual(result.y, 652); // naturalTop(100)+y = 800-48 → y=652
});

test("clampToViewport: 向上拖出视口 → 最多裁掉 minVisible px（header 不会被整个拖出屏幕）", () => {
  const rect = { x: 0, y: -2000, ...CARD };
  const result = clampToViewport(rect, VIEWPORT);
  assert.strictEqual(result.y, -148); // naturalTop(100)+y = -48 → y=-148
});

test("clampToViewport: 上下两个方向的钳制不对称——上边界不随卡片高度变化", () => {
  const shortCard = { width: 880, height: 200 };
  const tallCard = { width: 880, height: 900 };
  const up = { x: 0, y: -5000 };
  const shortResult = clampToViewport({ ...up, ...shortCard }, VIEWPORT);
  const tallResult = clampToViewport({ ...up, ...tallCard }, VIEWPORT);
  // naturalTop 不同（矮卡片 300、高卡片 -50），但钳制后的绝对 top 都是 -48：
  // shortResult.y = -48 - 300 = -348；tallResult.y = -48 - (-50) = 2。
  assert.strictEqual(shortResult.y, -348);
  assert.strictEqual(tallResult.y, 2);
});

test("clampToViewport: 自定义 minVisible 生效", () => {
  const rect = { x: 2000, y: 0, ...CARD };
  const result = clampToViewport(rect, VIEWPORT, { minVisible: 100 });
  assert.strictEqual(result.x, 940); // naturalLeft(160)+x = 1200-100 → x=940
});

test("clampToViewport: 卡片比视口还大也不产生 NaN/无穷（只是自动居中重叠，不额外偏移）", () => {
  const rect = { x: 0, y: 0, width: 880, height: 700 };
  const smallViewport = { width: 375, height: 600 };
  const result = clampToViewport(rect, smallViewport);
  assert.ok(Number.isFinite(result.x));
  assert.ok(Number.isFinite(result.y));
  assert.strictEqual(result.x, 0);
  assert.strictEqual(result.y, 2); // 见文件头注释：naturalTop=-50 < -48，自动纠正 2px
});

// --- nextRectOnDrag -------------------------------------------------------------

test("nextRectOnDrag: 从起点累加 delta（不是逐次增量叠加，避免漂移误差）", () => {
  const start = { x: 10, y: 20, ...CARD };
  const bigViewport = { width: 1920, height: 1080 };
  const result = nextRectOnDrag(start, 30, -5, bigViewport);
  assert.deepStrictEqual(result, { x: 40, y: 15 });
});

test("nextRectOnDrag: 拖动途中内部自动 clamp（不会算出跑到视口外的位置）", () => {
  const start = { x: 0, y: 0, ...CARD };
  const result = nextRectOnDrag(start, 2000, 0, VIEWPORT);
  assert.strictEqual(result.x, 992); // 与 clampToViewport 的右拖用例结果一致
});

test("nextRectOnDrag: 同一起点、增量 delta 不改变 start 对象本身（纯函数不产生副作用）", () => {
  const start = { x: 10, y: 20, ...CARD };
  const snapshot = { ...start };
  nextRectOnDrag(start, 500, 500, VIEWPORT);
  assert.deepStrictEqual(start, snapshot);
});

// --- nextRectOnResize -------------------------------------------------------------

test("nextRectOnResize: 向右下拖动手柄 → 宽高按 delta 增加", () => {
  const start = { x: 0, y: 0, width: 880, height: 600 };
  const bigViewport = { width: 1920, height: 1080 };
  const result = nextRectOnResize(start, 40, 25, bigViewport);
  assert.deepStrictEqual(result, { width: 920, height: 625 });
});

test("nextRectOnResize: 向左上拖动手柄（负 delta）→ 缩小，但不低于默认 minWidth/minHeight", () => {
  const start = { x: 0, y: 0, width: 880, height: 600 };
  const bigViewport = { width: 1920, height: 1080 };
  const result = nextRectOnResize(start, -1000, -1000, bigViewport);
  assert.deepStrictEqual(result, { width: DEFAULT_MIN_WIDTH, height: DEFAULT_MIN_HEIGHT });
});

test("nextRectOnResize: 自定义 minWidth/minHeight 生效", () => {
  const start = { x: 0, y: 0, width: 500, height: 400 };
  const bigViewport = { width: 1920, height: 1080 };
  const result = nextRectOnResize(start, -1000, -1000, bigViewport, { minWidth: 420, minHeight: 300 });
  assert.deepStrictEqual(result, { width: 420, height: 300 });
});

test("nextRectOnResize: 封顶到视口的 VIEWPORT_MAX_RATIO（留一点边距，不贴边/不溢出）", () => {
  const start = { x: 0, y: 0, width: 880, height: 600 };
  const result = nextRectOnResize(start, 5000, 5000, VIEWPORT);
  // 上限=视口 * VIEWPORT_MAX_RATIO：既放开 Y 方向（不再被卡片 CSS 的
  // max-height:80vh 压死），又不让卡片撑到贴边/溢出、把 resize 角拖出视口。
  assert.deepStrictEqual(result, {
    width: Math.round(VIEWPORT.width * VIEWPORT_MAX_RATIO),
    height: Math.round(VIEWPORT.height * VIEWPORT_MAX_RATIO),
  });
});

// --- serializeWindowRect / parseWindowRect ---------------------------------------
//
// 容错是硬要求：sessionStorage 里的值可能是上一版本的旧格式、被用户手改、或者
// 干脆是别的键写串了——反序列化任何异常输入都必须回退默认值，绝不能抛出让浮窗
// 渲染直接崩溃。

test("serializeWindowRect/parseWindowRect: round-trip 还原原值（数值 width/height）", () => {
  const rect = { x: 12, y: -34, width: 900, height: 650 };
  assert.deepStrictEqual(parseWindowRect(serializeWindowRect(rect)), rect);
});

test("serializeWindowRect/parseWindowRect: round-trip 还原原值（width/height 为 null）", () => {
  const rect = { x: 0, y: 0, width: null, height: null };
  assert.deepStrictEqual(parseWindowRect(serializeWindowRect(rect)), rect);
});

test("parseWindowRect: raw 为 null → 回退默认", () => {
  assert.deepStrictEqual(parseWindowRect(null), DEFAULT_WINDOW_RECT);
});

test("parseWindowRect: raw 为 undefined → 回退默认", () => {
  assert.deepStrictEqual(parseWindowRect(undefined), DEFAULT_WINDOW_RECT);
});

test("parseWindowRect: raw 为空字符串 → 回退默认（不尝试 JSON.parse）", () => {
  assert.deepStrictEqual(parseWindowRect(""), DEFAULT_WINDOW_RECT);
});

test("parseWindowRect: 坏 JSON（语法错误）→ 回退默认，不抛异常", () => {
  assert.doesNotThrow(() => {
    const result = parseWindowRect("{not valid json");
    assert.deepStrictEqual(result, DEFAULT_WINDOW_RECT);
  });
});

test("parseWindowRect: JSON 合法但顶层是数字 → 回退默认", () => {
  assert.deepStrictEqual(parseWindowRect("42"), DEFAULT_WINDOW_RECT);
});

test("parseWindowRect: JSON 合法但顶层是字符串 → 回退默认", () => {
  assert.deepStrictEqual(parseWindowRect('"hello"'), DEFAULT_WINDOW_RECT);
});

test("parseWindowRect: JSON 合法但顶层是数组 → 回退默认", () => {
  assert.deepStrictEqual(parseWindowRect("[1,2,3]"), DEFAULT_WINDOW_RECT);
});

test("parseWindowRect: JSON 合法但顶层是 null → 回退默认", () => {
  assert.deepStrictEqual(parseWindowRect("null"), DEFAULT_WINDOW_RECT);
});

test("parseWindowRect: 缺字段（空对象）→ 回退默认", () => {
  assert.deepStrictEqual(parseWindowRect("{}"), DEFAULT_WINDOW_RECT);
});

test("parseWindowRect: 缺 width/height 字段（只有 x/y）→ 回退默认（不猜测拼凑）", () => {
  assert.deepStrictEqual(parseWindowRect('{"x":1,"y":2}'), DEFAULT_WINDOW_RECT);
});

test("parseWindowRect: x 是非数字字符串 → 回退默认", () => {
  const raw = JSON.stringify({ x: "abc", y: 0, width: null, height: null });
  assert.deepStrictEqual(parseWindowRect(raw), DEFAULT_WINDOW_RECT);
});

test("parseWindowRect: x 是 null（类型不符，x/y 不允许 null）→ 回退默认", () => {
  const raw = JSON.stringify({ x: null, y: 0, width: null, height: null });
  assert.deepStrictEqual(parseWindowRect(raw), DEFAULT_WINDOW_RECT);
});

test("parseWindowRect: width 是非法值（NaN 无法出现在 JSON 中，用字符串模拟同类非法输入）→ 回退默认", () => {
  const raw = JSON.stringify({ x: 0, y: 0, width: "wide", height: null });
  assert.deepStrictEqual(parseWindowRect(raw), DEFAULT_WINDOW_RECT);
});

test("parseWindowRect: width 是 0 或负数（退化尺寸）→ 回退默认", () => {
  const zero = JSON.stringify({ x: 0, y: 0, width: 0, height: 400 });
  const negative = JSON.stringify({ x: 0, y: 0, width: -10, height: 400 });
  assert.deepStrictEqual(parseWindowRect(zero), DEFAULT_WINDOW_RECT);
  assert.deepStrictEqual(parseWindowRect(negative), DEFAULT_WINDOW_RECT);
});

test("parseWindowRect: 支持调用方自定义 fallback（不是硬编码用 DEFAULT_WINDOW_RECT）", () => {
  const customFallback = { x: 1, y: 2, width: 500, height: 400 };
  assert.deepStrictEqual(parseWindowRect("{bad json", customFallback), customFallback);
  assert.deepStrictEqual(parseWindowRect(null, customFallback), customFallback);
});

// --- 窄屏停用浮窗几何（内联样式会盖过 @media 整屏规则，造成横向溢出）-------------

test("isFloatingDisabledWidth: 720px 及以下停用浮窗几何（与 CSS 整屏断点同一个值）", () => {
  assert.strictEqual(FLOATING_DISABLED_MAX_WIDTH, 720);
  assert.strictEqual(isFloatingDisabledWidth(600), true);
  assert.strictEqual(isFloatingDisabledWidth(720), true);
});

test("isFloatingDisabledWidth: 宽于断点则照常启用拖动/缩放几何", () => {
  assert.strictEqual(isFloatingDisabledWidth(721), false);
  assert.strictEqual(isFloatingDisabledWidth(1280), false);
});

test("clampRectSizeToViewport: 1280 下 resize 到 1041 → 回到 721 视口必须收窄（回归锁）", () => {
  // 实测路径：1280 桌面 resize 到 1041px → 切 720（几何停用、看着正常）→ 切 721，
  // 旧实现把 1041px 原样贴回，卡片 right=1065、在 721 视口里溢出 344px。
  const remembered = { x: 24, y: 10, width: 1041, height: 800 };
  const clamped = clampRectSizeToViewport(remembered, { width: 721, height: 900 });
  assert.ok(clamped.width <= 721, `width ${clamped.width} 仍超出视口`);
  assert.strictEqual(clamped.width, Math.round(721 * 0.96));
});

test("clampRectSizeToViewport: 视口装得下就原样返回（同一对象，不触发多余 setState）", () => {
  const rect = { x: 0, y: 0, width: 600, height: 400 };
  assert.strictEqual(clampRectSizeToViewport(rect, { width: 1280, height: 900 }), rect);
});

test("clampRectSizeToViewport: 没被 resize 过（null 宽高）保持 null，跟随 CSS", () => {
  const rect = { x: 5, y: 5, width: null, height: null };
  assert.deepStrictEqual(clampRectSizeToViewport(rect, { width: 400, height: 300 }), rect);
});
