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
/** T2-d 前端归一函数的模块——四个面都要 import 它,knowhow 格子编辑器刻意不接。 */
const NORMALIZER_MODULE_SUFFIX = "inference-list-markers";
const KNOWHOW_CELL_EDITOR = "knowhow-cell-editor.tsx";
const NORMALIZER_IDENTIFIER = "normalizeInferenceListMarkers";

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

function moduleSpecifiers(sourceFile) {
  return sourceFile.statements
    .filter((statement) => (
      ts.isImportDeclaration(statement)
      && ts.isStringLiteral(statement.moduleSpecifier)
    ))
    .map((statement) => statement.moduleSpecifier.text);
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

/** 非自闭合的 `<ReactMarkdown>…</ReactMarkdown>` 元素(children 里放的是要渲染的 markdown 表达式)。 */
function reactMarkdownElements(sourceFile) {
  const found = [];
  function visit(node) {
    if (ts.isJsxElement(node) && node.openingElement.tagName.getText(sourceFile) === "ReactMarkdown") {
      found.push(node);
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return found;
}

/** 元素的子节点里是否真的有 `name(...)` 调用(任意深度;只认裸标识符调用,不认文本匹配)。 */
function childrenCall(element, name) {
  let hit = false;
  function visit(node) {
    if (hit) return;
    if (ts.isCallExpression(node) && ts.isIdentifier(node.expression) && node.expression.text === name) {
      hit = true;
      return;
    }
    ts.forEachChild(node, visit);
  }
  for (const child of element.children) visit(child);
  return hit;
}

test("T2-d:四个面都 import 并在 <ReactMarkdown> 子表达式里调用了 normalizeInferenceListMarkers,knowhow 格子编辑器没有", async () => {
  const scanned = [];
  const violations = [];
  let knowhowCellEditorScanned = false;
  let knowhowCellEditorImportsNormalizer = false;

  for (const { path, module } of await appSourceModules()) {
    const specifiers = moduleSpecifiers(module);
    const importsNormalizer = specifiers.some((specifier) => specifier.endsWith(NORMALIZER_MODULE_SUFFIX));
    if (MODEL_TEXT_SURFACES.includes(path)) {
      scanned.push(path);
      if (!importsNormalizer) {
        violations.push(`${path}: 没有 import 以 ${NORMALIZER_MODULE_SUFFIX} 结尾的模块`);
      }
      // 只查 import 不够:tsconfig 没开 noUnusedLocals,「留着 import、把调用改回单层」
      // 对守卫和 tsc 都是绿的(评审 P2-2)。每个 <ReactMarkdown> 的子表达式里都必须
      // 真的有 normalizeInferenceListMarkers(...) 这个调用。
      const elements = reactMarkdownElements(module);
      if (elements.length === 0) {
        violations.push(`${path}: 没有任何 <ReactMarkdown> 元素`);
      }
      for (const element of elements) {
        if (!childrenCall(element, NORMALIZER_IDENTIFIER)) {
          violations.push(`${path}: <ReactMarkdown> 的子表达式里没有 ${NORMALIZER_IDENTIFIER}(...) 调用`);
        }
      }
    }
    if (path === KNOWHOW_CELL_EDITOR) {
      knowhowCellEditorScanned = true;
      knowhowCellEditorImportsNormalizer = importsNormalizer;
    }
  }

  for (const surface of MODEL_TEXT_SURFACES) {
    assert.ok(scanned.includes(surface), `没扫到渲染面 ${surface}: ${scanned.join(", ")}`);
  }
  assert.deepEqual(violations, []);
  assert.ok(knowhowCellEditorScanned, `没扫到 ${KNOWHOW_CELL_EDITOR}`);
  assert.equal(
    knowhowCellEditorImportsNormalizer,
    false,
    `${KNOWHOW_CELL_EDITOR} 不该 import ${NORMALIZER_MODULE_SUFFIX}(用户内容,不做推断标记归一)`,
  );
});
