// 来源栏几条只能在样式表上钉住的布局约束（jsdom 不做 flex 布局，rect 恒为 0）。
//
//  - 工具排（补全论文信息 + workspace.side_panel 入口）两样都不在时是空 div，不隐藏就白占
//    `.sources-body` 的一格 gap；
//  - 「关联报告」清单占文档流：`.sources-body` 不滚、`.workspace-panel` 裁剪溢出，所以这一组
//    与清单都必须能收缩（min-height:0），清单在剩下的高度里自己滚——否则矮窗口里展开它会把
//    选项、分页连同下面的来源控件推出来源栏、被裁掉。
//
// 样式表经 PostCSS 解析，按选择器与声明判，不做文本位置查询。
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import postcss from "postcss";

const stylesheet = postcss.parse(await readFile(new URL("../../app/globals.css", import.meta.url), "utf8"));


/** 选择器列表里含 `selector` 的规则上，`prop` 的全部取值。 */
function declared(selector, prop) {
  const values = [];
  stylesheet.walkRules((rule) => {
    if (!rule.selector.split(",").map((part) => part.trim()).includes(selector)) return;
    rule.walkDecls(prop, (decl) => values.push(decl.value));
  });
  return values;
}


test("工具排两样都不在时不占间距", () => {
  assert.ok(declared(".sources-tool-row:empty", "display").includes("none"), ".sources-tool-row:empty 必须 display: none");
});


test("「关联报告」这一组与它的清单能收缩，清单在剩余高度里自己滚", () => {
  assert.ok(declared(".scope-group.linked-report-picker", "min-height").includes("0"), "这一组必须 min-height: 0,否则按内容高度撑出来源栏");
  assert.ok(declared(".linked-report-picker-panel", "min-height").includes("0"), "清单必须 min-height: 0 才能收缩");
  assert.ok(declared(".linked-report-picker-panel", "overflow-y").includes("auto"), "清单收缩后必须自己滚");
});
