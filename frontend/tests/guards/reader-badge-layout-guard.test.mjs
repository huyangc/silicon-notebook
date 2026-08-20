import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import test from "node:test";

// 2026-08-20 用户反馈:群组共享库点进去「完全看不到 notebook 名字」。
//
// 数据没问题——`notebook.name` 一直是对的。坏的是版式:顶栏身份行原本用 `.tag-row`
// (`flex-wrap: wrap`),里面挤着标题 + 徽章 + 一句长说明,而 `.workspace-header` 是
// **固定 72px 的单行**。三样东西换成三行、在 72px 里垂直居中,标题那一行就被推到可视区
// **之上**;静态量过:内容高 141px vs 容器 72px,标题 top 在 header top 之上,整份库名
// 一个像素都不在屏幕上。
//
// jsdom 没有排版引擎,组件测试量不到那 141px,所以「不换行」这条只能在 CSS 里钉。
// 组件那侧的结构前提(用 `.reader-badge-row` 而不是 `.tag-row`、行内不塞 `.tool-hint`)
// 由 `tests/component/notebook-reader-actions.component.test.tsx` 钉。

const css = readFileSync(
  fileURLToPath(new URL("../../app/globals.css", import.meta.url)),
  "utf8",
);

/** 取一条规则的声明块(只认顶层的 `选择器 { ... }`,够用且不引依赖)。 */
function block(selector) {
  const start = css.indexOf(`\n${selector} {`);
  assert.notEqual(start, -1, `globals.css 里找不到 ${selector} —— 版式约定被搬走了?`);
  const open = css.indexOf("{", start);
  const close = css.indexOf("}", open);
  assert.ok(close > open, `${selector} 的声明块没闭合`);
  return css.slice(open + 1, close);
}

test(".reader-badge-row 必须不换行(固定 72px 顶栏的前提)", () => {
  const rule = block(".reader-badge-row");
  assert.match(rule, /flex-wrap:\s*nowrap/, "换行会让库名被推出 72px 顶栏之外");
  assert.match(rule, /min-width:\s*0/, "不给 0 下限,标题就压不动、徽章会被顶出去");
});

test(".reader-badge-title 必须可压缩并省略,而不是把徽章挤走", () => {
  const rule = block(".reader-badge-title");
  // `.notebook-title-input` 自己是 width:100%(在 owner 顶栏里独占整行,那里是对的),
  // 这一行里必须显式解开,否则它一个人就吃满整行。
  assert.match(rule, /width:\s*auto/);
  assert.match(rule, /max-width:\s*none/);
  assert.match(rule, /min-width:\s*0/);
  assert.match(rule, /text-overflow:\s*ellipsis/);
  assert.match(rule, /white-space:\s*nowrap/);
});

test(".reader-badge-chip 自己也必须有界(否则长组名把标题重新压成 0)", () => {
  // 组名上限 120 字符,`grantedViaLabel` 还会把多个组名串起来。一个不缩不换行的徽章
  // 能吃掉整条 `.workspace-title`(max-width: min(48vw, 720px)),标题于是又被压到 0 宽
  // ——正是这次改动要根除的那个 bug 换个入口回来(codex #529 R1 P2)。
  const rule = block(".reader-badge-chip");
  assert.match(rule, /max-width:\s*\d/, "徽章没有上限,长组名可以吃满整行");
  assert.match(rule, /text-overflow:\s*ellipsis/, "超出必须省略,不能撑破这一行");
  assert.match(rule, /min-width:\s*0/);
});

test(".workspace-header 仍是固定高度——上面两条的前提没变", () => {
  // 「恒不换行」成立**只因为**它是固定 72px。哪天改成 auto,换行就不再是灾难,上面两条
  // 守卫也该跟着重估(而不是默默失去意义)。
  assert.match(block(".workspace-header"), /height:\s*72px/);
});

test("紧凑桌面宽度不渲染顶栏的「退出共享」——它是把标题挤没的那一个", () => {
  // 它固定 ~98px 且不缩。1000px 视口下顶栏左半只有 288px,扣掉「返回主页」和它之后
  // 标题只剩 37px(26px 字号,一个字都放不下);不渲染它之后回到 105px,与群组共享形态
  // 持平(codex #529 R7 P2)。能力没丢:同一动作在笔记本卡片菜单里一直都在。
  const compact = css.slice(css.indexOf("@media (max-width: 1200px) {"));
  assert.ok(css.includes("@media (max-width: 1200px) {"), "缺紧凑桌面宽度的那道断点");
  assert.match(compact, /\.reader-badge-action\s*\{[^}]*display:\s*none/,
    "紧凑桌面宽度仍渲染顶栏动作,标题会被它挤没");
});

test("窄屏(前提不成立处)必须放开换行,否则标题被挤没", () => {
  // ≤760px 时顶栏已经是 `height: auto`,前提没了,恒单行就只剩代价:320px 上
  // 「返回主页」+ 徽章 +「退出共享」实测只给标题留 43px(26px 字号连一个半字都放不下),
  // 放开换行后标题独占整行 196px(codex #529 R6 P2)。
  const narrow = css.slice(css.lastIndexOf("@media (max-width: 760px) {"));
  assert.match(narrow, /\.workspace-header\s*\{[^}]*height:\s*auto/,
    "窄屏顶栏不再是 auto 高度——下面这条放开换行的前提也就变了,需要重估");
  assert.match(narrow, /\.reader-badge-row\s*\{[^}]*flex-wrap:\s*wrap/,
    "窄屏没有放开换行,标题会被徽章与动作挤没");
  assert.match(narrow, /\.reader-badge-title\s*\{[^}]*flex-basis:\s*100%/,
    "标题没有独占一行,换行也救不了它");
});

test("空转保护:这三条选择器确实都在被检查的那份 CSS 里", () => {
  for (const selector of [".reader-badge-row", ".reader-badge-title", ".reader-badge-chip"]) {
    assert.ok(css.includes(`\n${selector} {`), `${selector} 不在 globals.css 里`);
  }
});
