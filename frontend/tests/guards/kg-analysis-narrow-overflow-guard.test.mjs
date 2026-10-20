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
//
// 判据看的是一个选择器**全部**规则块(包括媒体查询里的、后面追加的覆盖块)的最终效果:
// 只看第一块的话,后面再写一条 `.kg-analysis-table-scroll { overflow-x: visible }` 照样全绿。

const css = readFileSync(
  fileURLToPath(new URL("../../app/globals.css", import.meta.url)),
  "utf8",
).replace(/\/\*[\s\S]*?\*\//g, "");

/**
 * 某个选择器出现过的全部声明块(顶层与 @media 里的都算;选择器列表里含它也算)。
 * 只认最内层的 `选择器 { 声明 }`:@media 的块里还有块,不会被当成声明块。
 */
function blocksFor(selector) {
  const blocks = [];
  for (const match of css.matchAll(/([^{}]+)\{([^{}]*)\}/g)) {
    const selectors = match[1].split(",").map((part) => part.trim());
    if (selectors.includes(selector)) blocks.push(match[2]);
  }
  assert.ok(blocks.length > 0, `globals.css 里找不到 ${selector} —— 版式约定被搬走了?`);
  return blocks;
}

/** 这个选择器在所有块里给某个属性写过的值(按出现顺序,去掉首尾空白与 !important)。 */
function valuesOf(selector, property) {
  const values = [];
  for (const body of blocksFor(selector)) {
    for (const declaration of body.split(";")) {
      const colon = declaration.indexOf(":");
      if (colon < 0) continue;
      if (declaration.substring(0, colon).trim() !== property) continue;
      values.push(declaration.substring(colon + 1).replace(/!important/, "").trim());
    }
  }
  return values;
}

/** 每一处写法都必须是期望值,而且至少写过一处。 */
function assertEverywhere(selector, property, expected, why) {
  const values = valuesOf(selector, property);
  assert.ok(values.length > 0, `${selector} 没有写 ${property}:${why}`);
  assert.deepEqual(
    values.filter((value) => value !== expected),
    [],
    `${selector} 有一处把 ${property} 写成了别的值(后面的块会盖掉前面的):${why}`,
  );
}

test(".kg-analysis-table-scroll:超宽的表在自己的容器里横向滚动,容器不超过父元素", () => {
  assertEverywhere(".kg-analysis-table-scroll", "overflow-x", "auto", "没有横向滚动,超宽的表会把面板一起撑宽");
  assert.deepEqual(valuesOf(".kg-analysis-table-scroll", "overflow"), [], "overflow 简写会盖掉 overflow-x");
  assertEverywhere(".kg-analysis-table-scroll", "max-width", "100%", "容器本身不能比父元素宽");
});

test("诊断数据区不被子元素撑宽", () => {
  assertEverywhere(".kg-findings-diagnostics", "min-width", "0", "flex 子项默认按内容最小宽度撑开");
  assertEverywhere(".kg-findings-diagnostics", "max-width", "100%", "诊断区不能比面板宽");
  assertEverywhere(".kg-findings-diagnostics-body", "min-width", "0", "flex 子项默认按内容最小宽度撑开");
});

test("表格本身仍撑满卡片(滚动条挂在外层,不给 <table> 改 display)", () => {
  assertEverywhere(".kg-analysis-table", "width", "100%", "表格要撑满卡片");
  assert.deepEqual(
    valuesOf(".kg-analysis-table", "display"),
    [],
    "给 <table> 改 display(例如 block)会让它退化成收缩到内容宽度的匿名表格盒",
  );
});

test("俯瞰图按容器宽度缩放", () => {
  assertEverywhere(".kg-analysis-map", "width", "100%", "俯瞰图不能写死宽度");
});

test("判据看全部规则块:后面追加的覆盖块、媒体查询里的块都算", () => {
  // 自检:这份解析认得出媒体查询里的块,也认得出选择器列表里的一员。
  const narrow = blocksFor(".kg-finding-source-group");
  assert.ok(narrow.length >= 2, "顶层一块 + 窄屏媒体查询一块");
  assert.ok(valuesOf(".kg-finding-source-group", "border-radius").includes("12px"));
});
