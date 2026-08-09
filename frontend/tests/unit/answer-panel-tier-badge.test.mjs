// codex 评审 PR#304 第 3 轮 P2 #2:answer-panel.tsx 的 SelectedReferenceDetail
// 引用徽章此前只把库名放进 title(hover 提示)——触屏/键盘用户完全看不到是哪个
// 库，可见文字仍是泛化的 tier 标签。修复把库名并入可见文字。
//
// SelectedReferenceDetail 是一个含 JSX 的 .tsx 组件，不能被 `node --test`
// 直接 import（含 TS/JSX 语法会 SyntaxError，同 knowhow-citation.test.mjs
// 顶部注释记录的既有限制）——这里用同一形状的表达式镜像着测，并 import 真实
// 的 TIER/label（不是重新发明一份假映射表），保证词表本身漂移时这条测试也会
// 跟着漂移。真实 JSX 把库名包进单独的 <span class="tier-badge-source-name">
// 只是为了 CSS 省略号截断，拼接后的可见文本内容与这里断言的字符串一致——这正是
// 无障碍工具（屏幕阅读器/触屏长按）看到的文本，不依赖鼠标 hover 才能触发的
// title 属性。
import test from "node:test";
import assert from "node:assert/strict";

import { TIER, label } from "../../app/vocabulary.ts";

function tierBadgeVisibleText(tier, sourceName) {
  return sourceName
    ? `来自「${sourceName}」（${label(TIER, tier, "未知来源")}）`
    : label(TIER, tier, "未知来源");
}

test("已知库名(personal):可见文字带库名,与既有 title 文案同形状", () => {
  assert.equal(tierBadgeVisibleText("personal", "模拟笔记"), "来自「模拟笔记」（个人知识库）");
});

test("已知库名(base):可见文字带库名", () => {
  assert.equal(tierBadgeVisibleText("base", "工艺基础"), "来自「工艺基础」（公共知识库）");
});

test("库名缺失(undefined,如跨二级挂载解不出名字):优雅退回原有泛化文案,不出现 undefined", () => {
  const text = tierBadgeVisibleText("personal", undefined);
  assert.equal(text, "个人知识库");
  assert.ok(!text.includes("undefined"));
});

test("库名为空字符串(notebookNames 命中但值是空串):同样退回泛化文案,不渲染空括号", () => {
  const text = tierBadgeVisibleText("base", "");
  assert.equal(text, "公共知识库");
  assert.ok(!text.includes("「」"));
});

test("未知 tier 值:label() 兜底到「未知来源」,不吐出原始 tier 字符串", () => {
  const text = tierBadgeVisibleText("nonsense-tier", undefined);
  assert.equal(text, "未知来源");
  assert.ok(!text.includes("nonsense-tier"));
});

test("未知 tier 值即便带库名,tier 一段仍走兜底,不拼出裸枚举值", () => {
  const text = tierBadgeVisibleText("nonsense-tier", "某个库");
  assert.equal(text, "来自「某个库」（未知来源）");
});
