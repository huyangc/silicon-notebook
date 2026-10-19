import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { fileURLToPath } from "node:url";
import test from "node:test";

// 2026-10-20 本机真跑(375 宽):诊断数据里的「合并收敛率」表比图谱分析面板宽,整个面板
// 能横向滚约 120px——诊断数据收起时也一样,因为折叠里的表仍参与布局。修法是让每一张表在
// 自己的 `.kg-analysis-table-scroll` 里横向滚动,诊断区本身不被子元素撑宽。
//
// jsdom 没有排版引擎,量不到那 120px:结构那一半(每张表的父元素就是滚动容器、俯瞰图不写
// 死宽度)由 tests/component/kg-analysis-view.component.test.tsx 钉,这里钉 CSS 那一半。

const css = readFileSync(
  fileURLToPath(new URL("../../app/globals.css", import.meta.url)),
  "utf8",
);

/** 取一条顶层规则的声明块(只认 `\n选择器 {`,媒体查询里缩进的同名规则不算)。 */
function block(selector) {
  const start = css.indexOf(`\n${selector} {`);
  assert.notEqual(start, -1, `globals.css 里找不到 ${selector} —— 版式约定被搬走了?`);
  const open = css.indexOf("{", start);
  const close = css.indexOf("}", open);
  assert.ok(close > open, `${selector} 的声明块没闭合`);
  return css.slice(open + 1, close);
}

test(".kg-analysis-table-scroll:超宽的表在自己的容器里横向滚动,容器不超过父元素", () => {
  const rule = block(".kg-analysis-table-scroll");
  assert.match(rule, /overflow-x:\s*auto/, "没有横向滚动,超宽的表会把面板一起撑宽");
  assert.match(rule, /max-width:\s*100%/, "容器本身不能比父元素宽");
});

test("诊断数据区不被子元素撑宽", () => {
  const details = block(".kg-findings-diagnostics");
  assert.match(details, /min-width:\s*0/);
  assert.match(details, /max-width:\s*100%/);
  assert.match(block(".kg-findings-diagnostics-body"), /min-width:\s*0/);
});

test("表格本身仍撑满卡片(滚动条挂在外层,不给 <table> 改 display)", () => {
  const rule = block(".kg-analysis-table");
  assert.match(rule, /width:\s*100%/);
  assert.doesNotMatch(rule, /display:\s*block/, "display:block 会让表格退化成收缩到内容宽度的匿名表格盒");
});

test("俯瞰图按容器宽度缩放", () => {
  assert.match(block(".kg-analysis-map"), /width:\s*100%/);
});
