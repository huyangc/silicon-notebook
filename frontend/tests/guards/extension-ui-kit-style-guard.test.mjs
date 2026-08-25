import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

import ts from "typescript";

import { appSourceModules } from "../../test-support/semantic-source.mjs";


const CSS = (await readFile(new URL("../../app/globals.css", import.meta.url), "utf8"))
  .replace(/\/\*[\s\S]*?\*\//g, "");

const modules = await appSourceModules();
const source = modules.find((entry) => entry.path === "features/extension-sdk/ui.tsx")?.module;
assert.ok(source, "没找到 features/extension-sdk/ui.tsx（改名或删除？守卫失效）");

function addTokens(target, classList) {
  for (const token of classList.split(/\s+/)) {
    if (token) target.add(token);
  }
}

function collectClassTokens() {
  const tokens = new Set();
  function collectFromExpression(node) {
    if (ts.isStringLiteral(node) || ts.isNoSubstitutionTemplateLiteral(node)) {
      addTokens(tokens, node.text);
      return;
    }
    if (ts.isTemplateExpression(node)) {
      addTokens(tokens, node.head.text);
      for (const span of node.templateSpans) {
        collectFromExpression(span.expression);
        addTokens(tokens, span.literal.text);
      }
      return;
    }
    if (ts.isConditionalExpression(node)) {
      collectFromExpression(node.whenTrue);
      collectFromExpression(node.whenFalse);
      return;
    }
    if (ts.isParenthesizedExpression(node)) collectFromExpression(node.expression);
  }
  function visit(node) {
    if (
      ts.isJsxAttribute(node)
      && node.name.getText(source) === "className"
      && node.initializer
    ) {
      if (ts.isStringLiteral(node.initializer)) addTokens(tokens, node.initializer.text);
      else if (ts.isJsxExpression(node.initializer) && node.initializer.expression) {
        collectFromExpression(node.initializer.expression);
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(source);
  return [...tokens].sort();
}

function hasClassRule(token) {
  return new RegExp(`\\.${token.replace(/[-]/g, "\\-")}(?![\\w-])`).test(CSS);
}

test("extension-sdk/ui.tsx 里出现的每个 className token 都在 globals.css 里有对应规则", () => {
  const tokens = collectClassTokens();
  assert.ok(tokens.length > 0, "ui.tsx 没解析出任何 className token（守卫失效）");
  const missing = tokens.filter((token) => !hasClassRule(token));
  assert.deepEqual(
    missing,
    [],
    `ui.tsx 挂了 globals.css 里不存在的类：${missing.join("、")}`,
  );
});
