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

function collectClassTokens(module) {
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
      && node.name.getText(module) === "className"
      && node.initializer
    ) {
      if (ts.isStringLiteral(node.initializer)) addTokens(tokens, node.initializer.text);
      else if (ts.isJsxExpression(node.initializer) && node.initializer.expression) {
        collectFromExpression(node.initializer.expression);
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(module);
  return [...tokens].sort();
}

function hasClassRule(token) {
  // 以 `-` 结尾的 token 是模板串的静态前缀(`extension-alert--${tone}`):按前缀
  // 判「存在以它开头的类」;完整 token 按精确类名判。
  if (token.endsWith("-")) {
    return new RegExp(`\\.${token.replace(/[-]/g, "\\-")}[\\w-]`).test(CSS);
  }
  return new RegExp(`\\.${token.replace(/[-]/g, "\\-")}(?![\\w-])`).test(CSS);
}

test("extension-sdk/ui.tsx 里出现的每个 className token 都在 globals.css 里有对应规则", () => {
  const tokens = collectClassTokens(source);
  assert.ok(tokens.length > 0, "ui.tsx 没解析出任何 className token（守卫失效）");
  const missing = tokens.filter((token) => !hasClassRule(token));
  assert.deepEqual(
    missing,
    [],
    `ui.tsx 挂了 globals.css 里不存在的类：${missing.join("、")}`,
  );
});

test("全部生产模块里的 extension-* className token 同样必须在 globals.css 里存在", () => {
  // 核心 UI 也在复用 kit 命名空间的类(如笔记本设置里的 extension-alert)——
  // 只对账 ui.tsx 时,将来重命名 kit 类会让核心那处静默失去样式(评审 P2)。
  const offenders = [];
  let scanned = 0;
  for (const entry of modules) {
    const tokens = collectClassTokens(entry.module)
      .filter((token) => token.startsWith("extension-"));
    if (tokens.length === 0) continue;
    scanned += 1;
    for (const token of tokens) {
      if (!hasClassRule(token)) offenders.push(`${entry.path}: ${token}`);
    }
  }
  assert.ok(scanned > 0, "没有任何模块用到 extension-* 类(守卫失效?)");
  assert.deepEqual(
    offenders,
    [],
    `extension-* 类在 globals.css 里没有对应规则：${offenders.join("、")}`,
  );
});
