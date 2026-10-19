// 知识分析看板「回答质量」一节的接线守卫。
//
// 这一节(提问数、👍/👎 与其中全局问答的标注、低分提问与「另有 N 条来自全局问答」)
// 只在 analytics-answer-quality.tsx 里实现,组件测试钉住它的渲染;page.tsx 是没有
// 组件级测试的编排组件,所以这里按语义结构钉住它确实把这一节接进了看板弹窗,而且
// 没有留下第二份内联实现(那份旧实现不认全局评分,会让两处数字口径分叉)。
//
// 判据是语义身份(导入、JSX 祖先链、绑定源码),不碰源码偏移与行号。
import test from "node:test";
import assert from "node:assert/strict";

import ts from "typescript";

import { importsFrom, jsxElements, parseModule } from "../../test-support/semantic-source.mjs";

const page = await parseModule("page.tsx");

/** 每个 AnalyticsAnswerQuality 元素,连同它外层所有条件渲染表达式的源码。 */
function renderSites(sourceFile) {
  const sites = [];
  const guards = [];
  function visit(node) {
    const isGuard = ts.isBinaryExpression(node)
      && node.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken;
    if (isGuard) guards.push(node.left.getText(sourceFile));
    if (
      (ts.isJsxSelfClosingElement(node) || ts.isJsxOpeningElement(node))
      && node.tagName.getText(sourceFile) === "AnalyticsAnswerQuality"
    ) {
      sites.push({ guards: [...guards], node });
    }
    ts.forEachChild(node, visit);
    if (isGuard) guards.pop();
  }
  visit(sourceFile);
  return sites;
}

test("page.tsx 从 analytics-answer-quality 导入回答质量一节", () => {
  assert.deepEqual(importsFrom(page, "./analytics-answer-quality"), [
    { imported: "AnalyticsAnswerQuality", local: "AnalyticsAnswerQuality" },
  ]);
});

test("看板弹窗里恰好渲染一次 <AnalyticsAnswerQuality analytics={analytics} />", () => {
  const sites = renderSites(page);
  assert.equal(sites.length, 1);
  const [site] = sites;
  assert.ok(
    site.guards.some((guard) => guard.includes('rootModals.view("analytics").open')),
    `渲染点不在看板弹窗的条件之内: ${site.guards.join(" | ")}`,
  );
  const [element] = jsxElements(page, "AnalyticsAnswerQuality");
  assert.equal(element.bindings?.analytics, "analytics");
});

test("page.tsx 不再内联一份回答质量的标签", () => {
  const spans = jsxElements(page, "span").filter((element) => (
    element.attributes?.className === "tag"
    && Object.values(element.bindings ?? {}).some((value) => /analytics\.feedback_/.test(value))
  ));
  assert.deepEqual(spans, []);
  const inline = [];
  function visit(node) {
    if (ts.isPropertyAccessExpression(node) && node.getText(page).startsWith("analytics.feedback_")) {
      inline.push(node.getText(page));
    }
    ts.forEachChild(node, visit);
  }
  visit(page);
  assert.deepEqual(inline, []);
});
