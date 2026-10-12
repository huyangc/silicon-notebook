// 守卫:关闭按钮的悬停提示 / 可访问名不许再写英文 Close。
//
// 来由(技术债 C2,2026-10-12):8 个文件里 17 个「×」关闭按钮都写着 `title="Close"`,
// 用户悬停看到的是英文——界面文案一律中文(AGENTS.md「User-facing copy」)。全部
// 改成「关闭」之后,这条守卫拦住复发。
//
// ── 为什么放在这里而不是 scripts/check_ui_vocabulary.py ─────────────────
// 那个脚本按设计只看「含中文字符的文本单元」,纯 ASCII 的 "Close" 永远不会触发它;
// 而要说清「这是一个 title / aria-label 属性的值」需要 JSX 的上下文,正是
// raw-enum-fallback.test.mjs 选 TypeScript AST、留在前端守卫里的同一个理由。
//
// ── 匹配面 ─────────────────────────────────────────────────────────────
// 只看 JSX 属性 `title` 与 `aria-label` 的**值**里出现的字符串字面量(含
// `{"…"}`、模板串、条件表达式的各个分支),整词、大小写不敏感地找 close。
// 标识符(`onClose`、`requestClose`、`PanelRightClose`)、className、其它属性、
// 以及属性之外的普通字符串都不在匹配面上——它们不是给用户看的提示文案。
// `title={closeLabel}` 这种先算好再传的写法看不见,与 raw-enum-fallback 一样如实承认。
import test from "node:test";
import assert from "node:assert/strict";

import ts from "typescript";

import { appSourceModules, parseText } from "../../test-support/semantic-source.mjs";

const COPY_ATTRIBUTES = new Set(["title", "aria-label"]);
const ENGLISH_CLOSE = /\bclose\b/i;

function literalTexts(node) {
  const texts = [];
  function visit(current) {
    if (
      ts.isStringLiteral(current)
      || ts.isNoSubstitutionTemplateLiteral(current)
      || ts.isTemplateHead(current)
      || ts.isTemplateMiddle(current)
      || ts.isTemplateTail(current)
    ) {
      texts.push(current.text);
    }
    ts.forEachChild(current, visit);
  }
  visit(node);
  return texts;
}

/**
 * 每个 `title` / `aria-label` 属性一条:{ element, name, texts }。按「元素名 + 属性名 + 字面量」
 * 认身份,不报行号——前端守卫一律用语义身份(static-source-policy 的约定)。
 */
function copyAttributes(sourceFile) {
  const found = [];
  function visit(node) {
    if (ts.isJsxAttribute(node) && node.initializer) {
      const name = node.name.getText(sourceFile);
      if (COPY_ATTRIBUTES.has(name)) {
        const element = node.parent.parent.tagName.getText(sourceFile);
        found.push({ element, name, texts: literalTexts(node.initializer) });
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return found;
}

function englishCloseOffenders(sourceFile) {
  return copyAttributes(sourceFile)
    .flatMap(({ element, name, texts }) => texts
      .filter((text) => ENGLISH_CLOSE.test(text))
      .map((text) => `${sourceFile.fileName} <${element} ${name}> ${JSON.stringify(text)}`));
}

test("no title / aria-label in production code says Close in English", async () => {
  const offenders = [];
  for (const item of await appSourceModules()) {
    offenders.push(...englishCloseOffenders(item.module));
  }
  assert.deepEqual(offenders, [], "关闭按钮的 title / aria-label 写「关闭」，不写英文 Close");
});

test("the scan actually reaches the close buttons it protects", async () => {
  // 非空转哨兵:如果属性名比对或遍历坏了,上一条会因为什么都没扫到而恒绿。
  // C2 改过的 17 个「×」按钮都带 title="关闭",扫描必须看得见它们。
  let closeTitles = 0;
  for (const item of await appSourceModules()) {
    for (const attribute of copyAttributes(item.module)) {
      if (attribute.name === "title" && attribute.texts.includes("关闭")) closeTitles += 1;
    }
  }
  assert.ok(closeTitles >= 17, `只扫到 ${closeTitles} 个 title="关闭"`);
});

test("the predicate catches every way of writing English Close into the two attributes", () => {
  const source = [
    'export const a = <button title="Close">×</button>;',
    'export const b = <IconButton aria-label="Close dialog" />;',
    'export const c = <span title={"close"}>×</span>;',
    "export const d = <a title={`Close ${name}`}>×</a>;",
    'export const e = <em title={busy ? "关闭" : "Close"}>×</em>;',
  ].join("\n");
  const offenders = englishCloseOffenders(parseText(source, "probe.tsx"));
  assert.deepEqual(offenders, [
    'probe.tsx <button title> "Close"',
    'probe.tsx <IconButton aria-label> "Close dialog"',
    'probe.tsx <span title> "close"',
    'probe.tsx <a title> "Close "',
    'probe.tsx <em title> "Close"',
  ]);
});

test("the predicate leaves identifiers, other attributes and Chinese copy alone", () => {
  const source = [
    'export const a = <button onClick={onClose} title="关闭">×</button>;',
    'export const b = <Modal onClose={() => requestClose("button")} aria-label="关闭对话框" />;',
    "export const c = <PanelRightClose className=\"close-button\" data-close=\"Close\" />;",
    "export const d = <button title={closeLabel}>×</button>;",
    'export const e = <button title="Closed issues">×</button>;',
    'export const f = "Close";',
  ].join("\n");
  assert.deepEqual(englishCloseOffenders(parseText(source, "probe.tsx")), []);
});
