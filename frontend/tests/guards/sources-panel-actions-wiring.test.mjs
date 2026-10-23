// 来源栏与问答区「说到的动作就是可点的字」在 page.tsx 一侧的接线守卫。
//
// 组件各自的渲染与回调由组件测试钉住（kg-readiness-hint / welcome-guidance），但 `Home()`
// 太大、没有能单独渲染的接缝：「挂载已整理的参考库」打开的是不是笔记本设置、是不是停在
// 「参考库」一节，只能按源码语义钉。以下回归组件测试照样全绿：
//
//  - 某处 onMountBases 不再传 `{ focusBases: true }`（弹窗打开了，却停在顶上，用户还得自己找）；
//  - 「参考库」一节的 ref 回调不再消费标志（之后每次打开设置都会被拽到这一节）。
//
// 判据是 AST 上的语义身份，不用行号与源码偏移。工具排 `:empty` 的 CSS 规则由
// extension-ui-layout-guard.test.mjs 钉住（它是登记过可以读样式表的那一份）。
import test from "node:test";
import assert from "node:assert/strict";
import ts from "typescript";

import { findFunctionIn, parseModule } from "../../test-support/semantic-source.mjs";


const page = await parseModule("page.tsx");


/** page 里名为 `name` 的 JSX 开标签 / 自闭合标签上 `attribute={…}` 的表达式文本。 */
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


test("每个「挂载参考库」动作都打开笔记本设置并停在「参考库」一节", () => {
  const handlers = [
    ...jsxAttributeTexts("KgReadinessHint", "onMountBases"),
    ...jsxAttributeTexts("KgUnavailableHint", "onMountBases"),
    ...jsxAttributeTexts("WelcomeGuidance", "onMountBases"),
  ];
  assert.equal(handlers.length, 3, `应有三处挂载参考库的行内动作,实际 ${handlers.length}`);
  for (const handler of handlers) {
    assert.match(
      handler,
      /presentNotebookEditor\(\s*currentNotebook\.id,\s*\{\s*focusBases:\s*true\s*\}\s*\)/,
      `挂载参考库没有打开笔记本设置的「参考库」一节:${handler}`,
    );
  }
});


test("「参考库」一节只在被要求时滚到眼前,滚完就把标志清掉", () => {
  // 打开时按本次的 options 重置标志:别的打开路径(顶栏「设置」、卡片菜单)不会误滚。
  const present = findFunctionIn(page, "Home", "presentNotebookEditor").getText(page);
  assert.match(present, /notebookEditorBasesFocusRef\.current = Boolean\(options\.focusBases\)/);

  let consumer = null;
  function visit(node) {
    if (
      ts.isJsxAttribute(node)
      && node.name.getText(page) === "ref"
      && node.initializer?.expression?.getText(page).includes("notebookEditorBasesFocusRef")
    ) consumer = node.initializer.expression.getText(page);
    ts.forEachChild(node, visit);
  }
  visit(page);
  assert.ok(consumer, "「参考库」一节没有消费 notebookEditorBasesFocusRef 的 ref 回调");
  assert.match(consumer, /notebookEditorBasesFocusRef\.current = false/, "ref 回调滚完没有清掉标志:之后每次渲染都会再滚");
  assert.match(consumer, /scrollIntoView\(/);
});

