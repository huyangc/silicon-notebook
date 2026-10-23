// 「说到的动作就是可点的字」第二批（重问 / 内容审核 / 报告范围 / 挂载公共知识库 / 文档上限）
// 在 page.tsx 一侧的接线守卫。
//
// 每句提示的渲染、回调与权限取舍由各自的组件测试钉住（answer-skipped-libraries、
// memory-panel-inline-actions、promotion-target-hint、report-scope-hint、source-list-panel）。
// `Home()` 没有能单独渲染的接缝，宿主把回调接到了哪里只能按源码语义钉。以下回归组件测试
// 照样全绿：
//
//  - 「再问一次」不再禁用于输入框有草稿时（提交会清空输入框，草稿被静默丢掉）；
//  - 「内容审核」对非管理员也给（点了 403：提案队列只对管理员开放，铃铛也只给管理员列）；
//  - 报告那句的「全选」只全选了本库来源（参考库还被清空着，范围依旧为空——来源栏「全选」
//    必须一并管参考库那条事故的翻版）；
//  - 「挂载公共知识库」打开了设置却没停在「参考库」一节；
//  - 「关闭并管理来源」关了弹窗却没把焦点交给来源栏（来源栏收着时也没展开）。
//
// 判据是 AST 上的语义身份，不用行号与源码偏移。
import test from "node:test";
import assert from "node:assert/strict";
import ts from "typescript";

import { findFunctionIn, parseModule } from "../../test-support/semantic-source.mjs";


const page = await parseModule("page.tsx");


/** page 里名为 `name` 的 JSX 标签上 `attribute={…}` 的表达式文本（每个出现处一项）。 */
function jsxAttributeTexts(name, attribute) {
  const found = [];
  function visit(node) {
    if (
      (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node))
      && node.tagName.getText(page) === name
    ) {
      const property = node.attributes.properties.find(
        (item) => ts.isJsxAttribute(item) && item.name.getText(page) === attribute,
      );
      found.push(property?.initializer?.expression?.getText(page) ?? "");
    }
    ts.forEachChild(node, visit);
  }
  visit(page);
  return found;
}

/** 对象字面量 `text` 里名为 `key` 的属性值文本。 */
function objectPropertyText(text, key) {
  const source = ts.createSourceFile("x.tsx", `(${text})`, ts.ScriptTarget.Latest, true, ts.ScriptKind.TSX);
  let found = null;
  function visit(node) {
    if (ts.isPropertyAssignment(node) && node.name.getText(source) === key) {
      found = node.initializer.getText(source);
      return;
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
  return found;
}

const focusBases = /presentNotebookEditor\(\s*[\w.]+,\s*\{\s*focusBases:\s*true\s*\}\s*\)/;


test("「再问一次」重提这一轮的问题，在途或输入框有草稿时禁用", () => {
  const [onReask] = jsxAttributeTexts("AnswerView", "onReask").filter(Boolean);
  assert.ok(onReask, "笔记本问答的 AnswerView 没有接 onReask");
  assert.match(onReask, /askSession\.submit\(\s*turn\.question\s*\)/);
  const [reason] = jsxAttributeTexts("AnswerView", "reaskDisabledReason").filter(Boolean);
  assert.ok(reason, "没有 reaskDisabledReason:在途时也能重问");
  assert.match(reason, /askInFlight/);
  assert.match(reason, /question\.trim\(\)/, "输入框有草稿时没有禁用:提交会清空输入框");
});


test("「内容审核」只对管理员给，打开的是提案队列", () => {
  const handlers = jsxAttributeTexts("MemoryPanel", "onOpenPromotionQueue");
  assert.equal(handlers.length, 2, `全局与笔记本内两处记忆页都应接上,实际 ${handlers.length}`);
  for (const handler of handlers) {
    assert.match(handler, /^currentUser\?\.role === "admin"\s*\?/, `内容审核没有按管理员收起:${handler}`);
    assert.match(handler, /promotionQueue\.openPromoQueue\(\)/);
    assert.match(handler, /:\s*undefined$/);
  }
});


test("每个「挂载公共知识库」都打开笔记本设置并停在「参考库」一节", () => {
  const handlers = [
    ...jsxAttributeTexts("MemoryPanel", "onMountBases"),
    ...jsxAttributeTexts("KnowledgeBrowser", "onMountBases"),
  ];
  assert.equal(handlers.length, 3, `应有三处,实际 ${handlers.length}`);
  for (const handler of handlers) assert.match(handler, focusBases, `没有停在「参考库」一节:${handler}`);
  for (const access of jsxAttributeTexts("MemoryPanel", "mountBasesAccess")) {
    assert.equal(access, "mountBasesAccessFor");
  }
  // 由谁来挂只走 workspaceCapabilities 一处判据,镜像谁都挂不了。
  const accessFor = findFunctionIn(page, "Home", "mountBasesAccessFor").getText(page);
  assert.match(accessFor, /workspaceCapabilities\(/);
  assert.match(accessFor, /canMountBases\) return "self"/);
  assert.match(accessFor, /mirrored \? "none" : "owner"/);
});


test("报告那句的「全选」与来源栏「全选」是同一个动作（本库来源与参考库一起）", () => {
  const [hint] = jsxAttributeTexts("ReportsPanel", "creationBlockedHint");
  assert.ok(hint, "ReportsPanel 没有接 creationBlockedHint");
  const selectAll = objectPropertyText(hint, "onSelectAll");
  assert.ok(selectAll, "creationBlockedHint 没有 onSelectAll");
  assert.match(selectAll, /sourceLibrary\.selectAllSources\(\)/);
  assert.match(selectAll, /setBaseScopeSelection\(\s*defaultBaseScopeSelection\(\)\s*\)/, "只全选了本库来源,参考库仍是空的");
  assert.match(objectPropertyText(hint, "onMountBases") ?? "", focusBases);
  assert.equal(objectPropertyText(hint, "canAddSources"), "!readOnlyWorkspace");
  assert.equal(objectPropertyText(hint, "canMountBases"), "capabilities.canMountBases");
});


test("「关闭并管理来源」关掉弹窗、展开来源栏、把焦点交给来源搜索框", () => {
  let onClick = null;
  function visit(node) {
    if (ts.isJsxElement(node) && node.children.some((child) => ts.isJsxText(child) && child.getText(page).includes("关闭并管理来源"))) {
      const property = node.openingElement.attributes.properties.find(
        (item) => ts.isJsxAttribute(item) && item.name.getText(page) === "onClick",
      );
      onClick = property?.initializer?.expression?.getText(page) ?? "";
    }
    ts.forEachChild(node, visit);
  }
  visit(page);
  assert.equal(onClick, "closeSourceModalToManageSources");
  const body = findFunctionIn(page, "Home", "closeSourceModalToManageSources").getText(page);
  assert.match(body, /rootModals\.requestClose\("source-add"/);
  assert.match(body, /if \(sourcesCollapsedRef\.current\) setSourcesCollapsedWithMorph\(false\)/);
  assert.match(body, /setSourceSearchFocusRequested\(true\)/);
  assert.deepEqual(jsxAttributeTexts("SourceListPanel", "focusSearchRequested"), ["sourceSearchFocusRequested"]);
  assert.match(jsxAttributeTexts("SourceListPanel", "onSearchFocused")[0], /setSourceSearchFocusRequested\(false\)/);
});
