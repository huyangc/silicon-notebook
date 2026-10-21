// 问答「就此追问 / 关联报告」在 page.tsx 一侧的接线守卫。
//
// 组件各自的行为由组件测试钉住（report-followup / linked-report-picker / ask-composer /
// answer-linked-report），但 `Home()` 太大、没有能单独渲染的接缝：page 把**什么**传给了
// 这些组件，只能按源码语义钉。以下任何一种回归，组件测试都照样全绿：
//
//  - `ReportsPanel` 不再收到 `onFollowUp`（按钮整颗消失），或收到的是一个不开会话的函数；
//  - 「就此追问」先切页签、后取范围——`readFollowupScope` 只在报告页签仍是它的 owner
//    时作数，切走之后读回的是 null，高级模式下的范围静默不切；
//  - `AskComposer` 的 `attachment` 写成常量 null，或标签读的不是 askSession 的视图；
//  - 选择器被挪进只在高级模式挂载的分支（自动模式也必须能挂/换/摘，计划 D16）。
//
// 判据全部是 AST 上的语义身份（JSX 元素、属性表达式文本、调用目标与遍历顺序），不用行号
// 与源码偏移——static-source-policy 是硬门。
import test from "node:test";
import assert from "node:assert/strict";

import ts from "typescript";

import { findFunctionIn, ifBranchesIn, jsxElements, parseModule } from "../../test-support/semantic-source.mjs";


const page = await parseModule("page.tsx");


/** page 里名为 `name` 的 JSX 开标签 / 自闭合标签节点。 */
function jsxNodes(name) {
  const found = [];
  function visit(node) {
    if (
      (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node))
      && node.tagName.getText(page) === name
    ) found.push(node);
    ts.forEachChild(node, visit);
  }
  visit(page);
  return found;
}

/** JSX 属性的表达式节点（`attr={expr}`）；没有该属性或不是表达式时为 null。 */
function attributeExpression(element, name) {
  for (const property of element.attributes.properties) {
    if (
      ts.isJsxAttribute(property)
      && property.name.getText(page) === name
      && property.initializer
      && ts.isJsxExpression(property.initializer)
      && property.initializer.expression
    ) return property.initializer.expression;
  }
  return null;
}

/** 节点内的调用，按 AST 遍历（即书写）顺序列出调用目标与实参文本。 */
function orderedCalls(node) {
  const calls = [];
  function visit(child) {
    if (ts.isCallExpression(child)) {
      calls.push({
        target: child.expression.getText(page),
        args: child.arguments.map((argument) => argument.getText(page)),
      });
    }
    ts.forEachChild(child, visit);
  }
  visit(node);
  return calls;
}

function firstIndex(calls, target) {
  return calls.findIndex((call) => call.target === target);
}

/** 从 `node` 往上的祖先链（不含自身）。 */
function ancestors(node) {
  const chain = [];
  for (let current = node.parent; current; current = current.parent) chain.push(current);
  return chain;
}

function jsxTagOf(node) {
  return ts.isJsxElement(node) ? node.openingElement.tagName.getText(page) : null;
}


test("ReportsPanel 的 onFollowUp 接的是 page 里真实的 followUpReport 处理函数", () => {
  const panels = jsxElements(page, "ReportsPanel");
  assert.equal(panels.length, 1, "page.tsx 应当恰好挂一处 ReportsPanel");
  assert.equal(
    panels[0].bindings?.onFollowUp,
    "followUpReport",
    "ReportsPanel 没有接 followUpReport:报告详情里的「就此追问」会整颗消失或什么都不做",
  );
  const handler = findFunctionIn(page, "Home", "followUpReport");
  const targets = new Set(orderedCalls(handler).map((call) => call.target));
  for (const required of [
    "reportWorkspace.readFollowupScope",
    "followupScopeSelections",
    "askSession.beginReportFollowup",
    "switchChatMode",
    "sourceLibrary.applySourceScopeSelection",
    "setBaseScopeSelection",
    "setAskComposerFocusSignal",
  ]) {
    assert.ok(targets.has(required), `followUpReport 缺少 ${required}`);
  }
});


test("「就此追问」先取报告的检索范围、再开会话切页签,最后把焦点送进输入框", () => {
  const handler = findFunctionIn(page, "Home", "followUpReport");
  const calls = orderedCalls(handler);
  const read = firstIndex(calls, "reportWorkspace.readFollowupScope");
  const begin = firstIndex(calls, "askSession.beginReportFollowup");
  const switchTab = calls.findIndex(
    (call) => call.target === "switchChatMode" && call.args[0] === '"ask"',
  );
  const focus = firstIndex(calls, "setAskComposerFocusSignal");
  assert.ok(switchTab >= 0, "followUpReport 没有切到问答页签(switchChatMode(\"ask\"))");
  assert.ok(
    read < begin && read < switchTab,
    "readFollowupScope 必须排在开会话与切页签之前:它只在报告页签仍是 owner 时作数,"
      + "切走之后读回 null,高级模式下的范围会静默不切",
  );
  assert.ok(switchTab < focus, "聚焦输入框要在切到问答页签之后(输入框此时才挂载)");
  // 取范围只在高级模式:自动模式没有勾选框,有效范围恒为全选。
  const advanced = ifBranchesIn(handler).find((branch) => branch.condition === "isAdvanced(uiMode)");
  assert.ok(advanced, "followUpReport 没有按 isAdvanced(uiMode) 分支");
  assert.ok(
    advanced.thenCalls.includes("reportWorkspace.readFollowupScope"),
    "取报告范围不在 isAdvanced(uiMode) 分支里:自动模式会无谓地取范围,或高级模式不取",
  );
});


test("AskComposer 的 attachment 就是 askSession 关联报告视图上的标签,不另加条件", () => {
  const composers = jsxNodes("AskComposer");
  assert.equal(composers.length, 1, "page.tsx 应当恰好挂一处 AskComposer");
  let attachment = attributeExpression(composers[0], "attachment");
  assert.ok(attachment, "AskComposer 没有 attachment:关联报告标签无处显示");
  while (ts.isParenthesizedExpression(attachment)) attachment = attachment.expression;
  // 「什么时候出现」只在 LinkedReportChip 里判(报告与说明都为空时它渲染 null)。page 再包
  // 一层条件就是第二份判据:写成只看 note,用选择器挂上的报告就不显示、也摘不掉,提交却
  // 照样带着它——而条件表达式里 chip 自己的 report 属性会让文本匹配恒真,所以这里要求
  // attachment **就是** chip 元素本身。
  const chip = ts.isJsxSelfClosingElement(attachment)
    ? attachment
    : ts.isJsxElement(attachment) ? attachment.openingElement : null;
  assert.ok(
    chip && chip.tagName.getText(page) === "LinkedReportChip",
    "AskComposer 的 attachment 不是直接的 <LinkedReportChip>(包了条件、写成常量或换了元素)",
  );
  assert.equal(jsxNodes("LinkedReportChip").length, 1, "page.tsx 应当恰好挂一处 LinkedReportChip");
  const chips = [chip];
  const bound = (name) => attributeExpression(chips[0], name)?.getText(page);
  assert.equal(bound("report"), "askSession.linkedReport");
  assert.equal(bound("verdict"), "askSession.linkedReportCheck");
  assert.equal(bound("note"), "askSession.linkedReportNote");
  const onRemove = attributeExpression(chips[0], "onRemove");
  assert.ok(onRemove, "LinkedReportChip 没有 onRemove");
  assert.ok(
    orderedCalls(onRemove).some((call) => call.target === "askSession.unlinkReport"),
    "标签的移除键没有调 askSession.unlinkReport",
  );
});


test("「关联报告」选择器挂在 AskComposer 里,两种界面模式都挂", () => {
  const pickers = jsxNodes("LinkedReportPicker");
  assert.equal(pickers.length, 1, "page.tsx 应当恰好挂一处 LinkedReportPicker");
  const chain = ancestors(pickers[0]);
  assert.ok(
    chain.some((node) => jsxTagOf(node) === "AskComposer"),
    "LinkedReportPicker 不在 AskComposer 的下排控件里",
  );
  assert.ok(
    !chain.some((node) => jsxTagOf(node) === "AskModePicker"),
    "LinkedReportPicker 被挪进了 AskModePicker:自动模式下它不挂载,标签就没法更换",
  );
  for (const node of chain) {
    if (ts.isJsxElement(node) && jsxTagOf(node) === "AskComposer") break;
    if (ts.isBinaryExpression(node) || ts.isConditionalExpression(node)) {
      assert.doesNotMatch(
        node.getText(page),
        /\bisAdvanced\(/,
        "LinkedReportPicker 的挂载条件里有 isAdvanced(...):自动模式也必须能挂/换/摘(计划 D16)",
      );
    }
  }
  const bound = (name) => attributeExpression(pickers[0], name)?.getText(page);
  assert.equal(bound("linked"), "askSession.linkedReport");
  const onSelect = attributeExpression(pickers[0], "onSelect");
  assert.ok(onSelect, "LinkedReportPicker 没有 onSelect");
  const selectCalls = orderedCalls(onSelect).map((call) => call.target);
  assert.ok(selectCalls.includes("askSession.linkReport"), "选择器选中后没有调 askSession.linkReport");
  // 选中后弹出层卸载,焦点要送进输入框,不能落在 body。
  assert.ok(selectCalls.includes("setAskComposerFocusSignal"), "选择器选中后没有把焦点送进输入框");
});


test("在途轮次显示提交时冻结的那份报告", () => {
  const tags = jsxNodes("LinkedReportTurnTag");
  assert.equal(tags.length, 1, "page.tsx 应当恰好挂一处 LinkedReportTurnTag");
  assert.equal(
    attributeExpression(tags[0], "report")?.getText(page),
    "askSession.pendingLinkedReport",
    "在途轮次的报告标签读的不是 pendingLinkedReport(提交那一刻冻结的那份)",
  );
});
