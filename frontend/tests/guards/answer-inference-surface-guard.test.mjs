// 回归门:渲染模型产出文本的每个 Markdown 面都必须接 `remarkAnswerInference`。
//
// 规格(docs/superpowers/specs/2026-09-03-ask-understanding-echo-and-mcp-clarification-design_zh.md
// §T2-a)要求推断/通识标记在四个面上一致呈现:站内回答、深度报告、以及两个公开分享页。
// 分享页 `c/[token]` / `r/[token]` 各有自己的 `<ReactMarkdown>` 实例,不复用
// `AnswerMarkdown` / `ReportMarkdown`——评审时正是这两处被漏接:站内看到琥珀色标签,
// 分享出去的收件人看到裸文本,而分享页读者恰恰是最没有上下文、最需要标记的人。
//
// 判据与 markdown-single-tilde-guard 同一口径:
//   ① 四个面的每个 `remarkPlugins` 数组字面量里都要有裸标识符 `remarkAnswerInference`;
//   ② 四个面必须都被扫到——重命名/搬走时守卫要响,而不是静默缩水。
import test from "node:test";
import assert from "node:assert/strict";
import ts from "typescript";

import { appSourceModules } from "../../test-support/semantic-source.mjs";

/** 渲染模型产出文本的面。knowhow 格子预览渲染的是用户表格,不是模型答案,不在其列。 */
const MODEL_TEXT_SURFACES = [
  "answer-markdown.tsx",
  "report-view.tsx",
  "r/[token]/page.tsx",
  "c/[token]/page.tsx",
];
const PLUGIN_IDENTIFIER = "remarkAnswerInference";

/** `remarkPlugins={...}` 的表达式节点(每个 `<ReactMarkdown>` 一个)。 */
function remarkPluginsExpressions(sourceFile) {
  const found = [];
  function visit(node) {
    if (
      (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node))
      && node.tagName.getText(sourceFile) === "ReactMarkdown"
    ) {
      for (const attribute of node.attributes.properties) {
        if (
          ts.isJsxAttribute(attribute)
          && attribute.name.getText(sourceFile) === "remarkPlugins"
          && attribute.initializer
          && ts.isJsxExpression(attribute.initializer)
          && attribute.initializer.expression
        ) {
          found.push(attribute.initializer.expression);
        }
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return found;
}

function hasBarePluginEntry(expression) {
  if (!ts.isArrayLiteralExpression(expression)) return false;
  return expression.elements.some((entry) => {
    const bare = ts.isAsExpression(entry) || ts.isParenthesizedExpression(entry)
      ? entry.expression
      : entry;
    return ts.isIdentifier(bare) && bare.text === PLUGIN_IDENTIFIER;
  });
}

test("渲染模型文本的每个 ReactMarkdown 都接了 remarkAnswerInference", async () => {
  const scanned = [];
  const violations = [];
  for (const { path, module } of await appSourceModules()) {
    if (!MODEL_TEXT_SURFACES.includes(path)) continue;
    const expressions = remarkPluginsExpressions(module);
    if (expressions.length === 0) {
      violations.push(`${path}: 没有任何 <ReactMarkdown remarkPlugins={...}>`);
      continue;
    }
    scanned.push(path);
    for (const expression of expressions) {
      if (!ts.isArrayLiteralExpression(expression)) {
        violations.push(`${path}: remarkPlugins 不是数组字面量,守卫读不出插件列表`);
      } else if (!hasBarePluginEntry(expression)) {
        violations.push(`${path}: remarkPlugins 缺 ${PLUGIN_IDENTIFIER}`);
      }
    }
  }

  for (const surface of MODEL_TEXT_SURFACES) {
    assert.ok(scanned.includes(surface), `没扫到渲染面 ${surface}: ${scanned.join(", ")}`);
  }
  assert.deepEqual(violations, []);
});
