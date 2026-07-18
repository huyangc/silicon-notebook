// 浮窗（拖动 + resize + 位置记忆）—— 纯逻辑，无 React（供 floating-window.test.mjs
// 直接 import；use-floating-window.ts 这个 hook 只调用本文件导出的函数，不重复
// 实现任何几何/校验逻辑）。这是「地基」：本文件只管算数字，不碰 DOM/window——
// 真正的 pointer 事件绑定、sessionStorage 读写、rAF 节流都在 hook 里。
//
// 设计前提——「偏移量」而非「绝对坐标」：本特性铺给的 7 个弹窗现在都是
// `.kh-modal-overlay { display:grid; place-items:center }` 或等价的 flex 居中
// 布局（见 knowhow-panel.tsx .kh-modal-overlay / knowhow-import.tsx
// .knowhow-import-backdrop），卡片本身从不设置 top/left。WindowRect.x/y 表示在
// 这个「CSS 居中位置」之上再叠加的偏移（配合 hook 侧的 `transform: translate`
// 实现），而不是视口绝对坐标——这样卡片在不同弹窗、不同默认宽度下都能共用同一套
// 偏移量语义，浏览器窗口变化时 CSS 居中会自动跟着重算，我们只需要记住「偏移了
// 多少」。width/height 为 null 表示「没被 resize 过，跟随 CSS 默认尺寸」——多数
// 弹窗默认宽度不同（880/760…）、高度还可能是 auto，本文件不替各弹窗瞎猜一个
// 数字，未 resize 前一律交还 CSS。

export type WindowRect = { x: number; y: number; width: number | null; height: number | null };

export type Viewport = { width: number; height: number };

// clampToViewport / nextRectOnDrag / nextRectOnResize 都在「已知渲染尺寸」的
// 前提下工作——width/height 在这里永远是具体像素数（由 hook 用
// cardRef.getBoundingClientRect() 量出来，null 早就在那一步被解析成真实尺寸），
// 不复用会出现 null 的 WindowRect：把「尺寸未知」的可能性挡在纯几何计算之外，
// 换来这几个函数本身不用到处判 null。
export type ResolvedRect = { x: number; y: number; width: number; height: number };

export type ClampOptions = { minVisible?: number };
export type ResizeOptions = { minWidth?: number; minHeight?: number };

// header 拖出视口后至少留多少 px 在视口内、仍可点中拖回——「拖出屏幕再也抓不
// 回来」是浮窗最经典的坑，这个默认值就是防它的唯一参数。48px 大致是本仓库
// modal header 一行内容（icon-button 尺寸 20px + 内边距）的量级，够点。
export const DEFAULT_MIN_VISIBLE = 48;

// resize 收缩下限：小于这个尺寸卡片内容基本没法用（表单/工具栏挤在一起），
// 而不是随便挑的数字——360 大致是 iPhone SE 竖屏宽度，240 是常见最小可用面板高度。
export const DEFAULT_MIN_WIDTH = 360;
export const DEFAULT_MIN_HEIGHT = 240;

// resize 放大上限占视口的比例——按用户屏幕分辨率自适应（乘视口尺寸），而不是
// 硬编码某个 px 上限。留 4% 边距（0.96）而非贴满视口：一是卡片撑到 100% 会
// 顶到屏幕边、右下角 resize 手柄贴边不好抓（甚至被浏览器滚动条盖住）；二是
// 与卡片自身 24px overlay padding 的观感一致。想真占满屏请用「全屏」按钮
// （那条路走 .kh-modal-card--fullscreen，与 resize 是两回事）。
export const VIEWPORT_MAX_RATIO = 0.96;

// 「未偏移、未 resize」的初始态——居中、跟随 CSS 默认尺寸。parseWindowRect 在
// 任何解析失败时都回退到这个值（或调用方显式传入的等价 fallback）。
export const DEFAULT_WINDOW_RECT: WindowRect = { x: 0, y: 0, width: null, height: null };

// 窄屏（与各弹窗 CSS 的 @media (max-width: 720px) 整屏规则同一个断点）不启用浮窗
// 几何：CSS 把卡片改成整屏铺满，而本 hook 输出的是**内联** transform/width/height，
// 内联样式优先级高于媒体查询——用户在桌面拖动/缩放过一次后，sessionStorage 里记下
// 的桌面尺寸会被贴到窄屏卡片上，实测 600px 视口里 width 仍是 880px、右侧溢出
// 280px（关闭按钮、视图切换、主操作按钮全被裁掉）。窄屏一律不输出几何、也不接
// 拖动/缩放，交给 CSS 全权接管。
export const FLOATING_DISABLED_MAX_WIDTH = 720;

export function isFloatingDisabledWidth(viewportWidth: number): boolean {
  return viewportWidth <= FLOATING_DISABLED_MAX_WIDTH;
}

function clampNumber(value: number, min: number, max: number): number {
  const lo = Math.min(min, max);
  const hi = Math.max(min, max);
  return Math.min(Math.max(value, lo), hi);
}

// 把 rect 的绝对位置钳制回视口内，返回新的偏移量 {x,y}（width/height 不变、
// 也不在返回值里——clamp 只移动位置，不改尺寸）。
//
// 水平方向：header 横跨卡片整个宽度，从左边抓、右边抓都行，用卡片宽度做对称
// 钳制——保证至少 minVisible px 的卡片宽度留在视口内（可能是左边一条，也可能是
// 右边一条）。
//
// 垂直方向刻意不对称、且不依赖卡片高度：拖动手柄只在卡片顶部（header 所在
// 位置），所以只保证卡片「顶部」这条边离视口上/下边界最多 minVisible px——不管
// 卡片多高，往上拖到底也只裁掉 minVisible px（header 依然大部分可见）。如果改用
// 「卡片高度」做对称钳制（min = minVisible-height），往上拖到底会把整个 header
// 都拖出屏幕、只剩卡片底部一角留在视口里——那样才是真的抓不回来，是本函数要
// 专门防的坑。
export function clampToViewport(rect: ResolvedRect, viewport: Viewport, opts?: ClampOptions): { x: number; y: number } {
  const minVisible = opts?.minVisible ?? DEFAULT_MIN_VISIBLE;
  const naturalLeft = (viewport.width - rect.width) / 2;
  const naturalTop = (viewport.height - rect.height) / 2;
  const left = naturalLeft + rect.x;
  const top = naturalTop + rect.y;
  const clampedLeft = clampNumber(left, minVisible - rect.width, viewport.width - minVisible);
  const clampedTop = clampNumber(top, -minVisible, viewport.height - minVisible);
  return { x: clampedLeft - naturalLeft, y: clampedTop - naturalTop };
}

// 拖动中：从「拖动开始时的 rect」累加本次拖动的总位移（不是逐个 pointermove
// 增量累加）——增量累加在长时间拖动后容易因浮点误差漂移，累加一次性 delta
// （currentPointer - startPointer）更稳，hook 侧按这个约定调用。内部即时 clamp，
// 拖动过程中卡片就不会被算到视口外面去（不用等 pointerup 才纠正，体验上也
// 不会有「先冲出去、松手再弹回来」的跳动）。
export function nextRectOnDrag(
  start: ResolvedRect,
  deltaX: number,
  deltaY: number,
  viewport: Viewport,
  opts?: ClampOptions,
): { x: number; y: number } {
  const raw: ResolvedRect = { x: start.x + deltaX, y: start.y + deltaY, width: start.width, height: start.height };
  return clampToViewport(raw, viewport, opts);
}

// resize 中：单一右下角手柄语义——左上角（x/y）不动，宽高按 delta 增减，夹在
// [minWidth,minHeight] 与视口尺寸之间（不会被 resize 撑到比屏幕还大）。
export function nextRectOnResize(
  start: ResolvedRect,
  deltaX: number,
  deltaY: number,
  viewport: Viewport,
  opts?: ResizeOptions,
): { width: number; height: number } {
  const minWidth = opts?.minWidth ?? DEFAULT_MIN_WIDTH;
  const minHeight = opts?.minHeight ?? DEFAULT_MIN_HEIGHT;
  // 上限=视口 * VIEWPORT_MAX_RATIO（按分辨率自适应，留一点边距，见常量注释）。
  const maxWidth = Math.round(viewport.width * VIEWPORT_MAX_RATIO);
  const maxHeight = Math.round(viewport.height * VIEWPORT_MAX_RATIO);
  const width = clampNumber(start.width + deltaX, minWidth, Math.max(minWidth, maxWidth));
  const height = clampNumber(start.height + deltaY, minHeight, Math.max(minHeight, maxHeight));
  return { width, height };
}

// --- sessionStorage 值的序列化/反序列化：必须容错 -------------------------------
//
// sessionStorage 里的值可能是上一版本的旧格式、被用户手改、或者干脆是别的键
// 写串了——parseWindowRect 对任何异常输入（坏 JSON、缺字段、字段类型不对、
// 退化尺寸）都回退默认值，绝不抛异常。这是本文件里唯一「调用方数据不可信」的
// 入口，所以校验刻意写得严格：宁可多回退几次丢一点用户的窗口位置记忆，也不能
// 让一个脏值让浮窗渲染直接崩掉。

function isFiniteNumber(value: unknown): value is number {
  return typeof value === "number" && Number.isFinite(value);
}

export function serializeWindowRect(rect: WindowRect): string {
  return JSON.stringify(rect);
}

export function parseWindowRect(raw: string | null | undefined, fallback: WindowRect = DEFAULT_WINDOW_RECT): WindowRect {
  if (!raw) return fallback;
  let parsed: unknown;
  try {
    parsed = JSON.parse(raw);
  } catch {
    return fallback;
  }
  if (parsed === null || typeof parsed !== "object" || Array.isArray(parsed)) return fallback;

  const candidate = parsed as Record<string, unknown>;
  const { x, y, width, height } = candidate;

  // x/y 是「偏移量」，类型上永远是具体数字（不像 width/height 允许 null 表示
  // 「跟随 CSS 默认」）——出现 null/字符串/缺失都视为整条记录不可信。
  if (!isFiniteNumber(x) || !isFiniteNumber(y)) return fallback;

  // width/height 允许显式 null（未 resize 过），但一旦存在就必须是正的有限数——
  // 0/负数是退化尺寸，宁可回退默认也不要把一个不可见的卡片渲染出来。
  if (width !== null && (!isFiniteNumber(width) || width <= 0)) return fallback;
  if (height !== null && (!isFiniteNumber(height) || height <= 0)) return fallback;

  return { x, y, width: width === null ? null : width, height: height === null ? null : height };
}
