// 「设为公共知识库」会被服务端拒绝(笔记本里还有成员的个人记忆)。按钮的结果要落在
// 按钮旁边(AGENTS.md Interactive feedback),不能只进顶部状态栏:这个动作必须走通用
// 提示弹窗的 `run` 形态(info-modal-actions.tsx 把拒绝原因画在按钮同一行),而不是
// 旧的「先关弹窗、错误交给 reportError」。
import test from "node:test";
import assert from "node:assert/strict";

import { jsxElements, parseModule } from "../../test-support/semantic-source.mjs";

const page = await parseModule("page.tsx");
const text = page.getFullText();

test("the publish action reports its refusal next to itself", () => {
  assert.equal(jsxElements(page, "InfoModalActions").length, 1);
  assert.match(text, /label: tier\.label,[^\n]*run: \(\) => handleTierAction\(\)/);
  assert.doesNotMatch(text, /handleTierAction\(\)\.catch\(reportError\)/);
});
