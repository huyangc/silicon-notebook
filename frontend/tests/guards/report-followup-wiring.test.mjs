// 问答「就此追问 / 关联报告」在 page.tsx 一侧的接线守卫。
//
// 组件各自的行为由组件测试钉住（report-followup / linked-report-picker / ask-composer /
// answer-linked-report），但 `Home()` 太大、没有能单独渲染的接缝：page 把**什么**传给了
// 这些组件，只能按源码语义钉。以下任何一种回归，组件测试都照样全绿：
//
//  - `ReportsPanel` 不再收到 `onFollowUp`（按钮整颗消失），或收到的是一个不开会话的函数；
//  - 「就此追问」先切页签、后取范围——`readFollowupScope` 只在报告页签仍是它的 owner
//    时作数，切走之后读回的是 null，高级模式下的范围静默不切；
//  - 标签读的不是 askSession 的视图，或被包了第二份出现条件；
//  - 「关联报告」分组被挪回问答输入框（来源类控件与回答方式/档位混在一行）；
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


/** 祖先链上是否有某个 className 含 `cls` 的 JSX 元素(字面量 className)。 */
function insideClass(chain, cls) {
  return chain.some((node) => {
    if (!ts.isJsxElement(node)) return false;
    const attr = node.openingElement.attributes.properties.find(
      (property) => ts.isJsxAttribute(property) && property.name.getText(page) === "className",
    );
    return Boolean(attr?.initializer && ts.isStringLiteral(attr.initializer)
      && attr.initializer.text.split(/\s+/).includes(cls));
  });
}


test("「关联报告」分组住在来源栏、不住在问答输入框,两种界面模式都挂", () => {
  const pickers = jsxNodes("LinkedReportPicker");
  assert.equal(pickers.length, 1, "page.tsx 应当恰好挂一处 LinkedReportPicker");
  const chain = ancestors(pickers[0]);
  // 分区:挂哪份报告回答「读什么」,与来源同栏;输入框下排只放「怎么答」(回答方式、检索档位)。
  assert.ok(insideClass(chain, "sources-panel"), "LinkedReportPicker 不在来源栏(.sources-panel)里");
  assert.ok(
    !chain.some((node) => jsxTagOf(node) === "AskComposer" || jsxTagOf(node) === "AskModePicker"),
    "LinkedReportPicker 又回到了问答输入框里:来源类控件与回答方式/档位混在同一行",
  );
  assert.equal(
    attributeExpression(jsxNodes("AskComposer")[0], "attachment"),
    null,
    "AskComposer 又收到了 attachment:关联报告标签应当住在来源栏的分组里",
  );
  for (const node of chain) {
    if (ts.isJsxElement(node) && jsxTagOf(node) === "aside") break;
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
  // 选中后清单卸载,焦点要送进输入框,不能落在 body。
  assert.ok(selectCalls.includes("setAskComposerFocusSignal"), "选择器选中后没有把焦点送进输入框");
});


test("分组里的标签就是 askSession 关联报告视图上的那枚,不另加条件", () => {
  const chips = jsxNodes("LinkedReportChip");
  assert.equal(chips.length, 1, "page.tsx 应当恰好挂一处 LinkedReportChip");
  // 「什么时候出现」只在 LinkedReportChip 里判(报告与说明都为空时它渲染 null)。page 再包
  // 一层条件就是第二份判据:写成只看 note,用选择器挂上的报告就不显示、也摘不掉,提交却
  // 照样带着它——所以要求标签是选择器的**直接** JSX 子元素。
  const parent = chips[0].parent;
  assert.ok(
    ts.isJsxElement(parent) && jsxTagOf(parent) === "LinkedReportPicker",
    "LinkedReportChip 不是 LinkedReportPicker 的直接子元素(包了条件、或挪出了分组)",
  );
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


test("「就此追问」在来源栏收着时把它展开:挂上的标签住在那里", () => {
  const handler = findFunctionIn(page, "Home", "followUpReport");
  // 读 ref:高级模式在 await 取范围之后才判,闭包里的 sourcesCollapsed 可能是点击那一帧的旧值。
  const branch = ifBranchesIn(handler).find((item) => item.condition === "sourcesCollapsedRef.current");
  assert.ok(branch, "followUpReport 没有按 sourcesCollapsedRef.current 分支(读闭包会拿到 await 之前的旧值)");
  const expand = orderedCalls(handler).find((call) => call.target === "setSourcesCollapsedWithMorph");
  assert.ok(
    branch.thenCalls.includes("setSourcesCollapsedWithMorph") && expand?.args[0] === "false",
    "来源栏收着时 followUpReport 没有展开它(setSourcesCollapsedWithMorph(false)):「就此追问」挂上的标签看不见",
  );
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
