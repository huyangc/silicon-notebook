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

test(".workspace-header 仍是固定高度——上面两条的前提没变", () => {
  // 若哪天它改成 auto,换行就不再是灾难,这两条守卫也该跟着重估(而不是默默失去意义)。
  assert.match(block(".workspace-header"), /height:\s*72px/);
});

test("空转保护:这三条选择器确实都在被检查的那份 CSS 里", () => {
  for (const selector of [".reader-badge-row", ".reader-badge-title", ".reader-badge-chip"]) {
    assert.ok(css.includes(`\n${selector} {`), `${selector} 不在 globals.css 里`);
  }
});
