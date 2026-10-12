// 守卫:控件的悬停提示 / 可访问名(JSX `title`、`aria-label`)不许是纯英文。
//
// 来由(技术债 C2,2026-10-12):8 个文件里 17 个「×」关闭按钮都写着 `title="Close"`,
// 笔记本列表的 ⌕ 按钮写着 `title="Search"`、视图切换组写着 `aria-label="View mode"`——
// 用户悬停看到、读屏器念出来的都是英文,而界面文案一律中文(AGENTS.md「User-facing
// copy」)。全部改成中文之后,这条守卫拦住复发。
//
// ── 判据 ───────────────────────────────────────────────────────────────
// 属性的**结果值**里含 ASCII 字母、且不含任何 CJK 字符,即报。所以:
//   * 「Close」「Search」「View mode」「Close dialog」报;
//   * 「关闭」「图谱 Schema 视图」「KG 视图」不报(中文文案里夹英文专名是词表的事,
//     归 scripts/check_ui_vocabulary.py 管);
//   * 「×」「⌕」这类符号、空串、纯数字不报——它们没有可翻译的英文。
// 目前全前端没有需要放行的例外(产品名、URL、快捷键等);真出现时在 ALLOWED_ENGLISH
// 里逐条登记并写理由,不要放宽判据。
//
// ── 只看「可能成为结果值」的字面量 ─────────────────────────────────────
// 直接字面量、`{"…"}`、模板串(各字面片段拼成一个单元,插值不计)、`+` 拼接(两侧拼成
// 一个单元)、三元的两个分支、`??` / `||` / `&&` 的右侧、括号与 as/satisfies/! 包装。
// **不**进下标与调用实参:`title={LABELS["close"]}`、`aria-label={label(MAP, "close", "关闭")}`
// 里的 "close" 是键名,不是给用户看的字。
//
// ── 为什么放在这里而不是 scripts/check_ui_vocabulary.py ─────────────────
// 那个脚本按设计只看「含中文字符的文本单元」,纯英文永远不会触发它——恰好是这条
// 守卫要抓的东西;而要说清「这是 title / aria-label 的结果值」需要 JSX 的上下文,
// 正是 raw-enum-fallback.test.mjs 选 TypeScript AST、留在前端守卫里的同一个理由。
//
// ── 已知盲区(看不见,如实承认,不假装全覆盖)──────────────────────────
//  * 先算好再传:`title={closeLabel}`、`title={t("close")}`;
//  * 从对象 / 数组取值:`title={item.title}`(page.tsx 的视图切换就是这个形状,它的
//    「卡片视图」「列表视图」写在对象字面量里,这条守卫看不到);
//  * 展开属性:`<button {...{ title: "Close" }} />`;
//  * 组件用自定义 prop 名接文案再转给 aria-label(如 extension-sdk/ui.tsx 的 `ariaLabel`);
//  * 非 JSX 的 `element.setAttribute("title", …)`;
//  * `placeholder`、`alt` 等其它属性不在匹配面上。
// 这些写法新增界面文案时,仍须人工对照 docs/ui-vocabulary.md。
import test from "node:test";
import assert from "node:assert/strict";

import ts from "typescript";

import { appSourceModules, parseText } from "../../test-support/semantic-source.mjs";

const COPY_ATTRIBUTES = new Set(["title", "aria-label"]);
const ASCII_LETTER = /[A-Za-z]/;
const CJK = /[\p{Script=Han}\p{Script=Hiragana}\p{Script=Katakana}\p{Script=Hangul}]/u;
const RESULT_OPERATORS = new Set([
  ts.SyntaxKind.QuestionQuestionToken,
  ts.SyntaxKind.BarBarToken,
  ts.SyntaxKind.AmpersandAmpersandToken,
]);

// 显式例外:属性结果值 → 理由。当前为空;登记时写清为何这个英文值本来就该原样显示。
const ALLOWED_ENGLISH = new Map();

/**
 * 表达式可能产出的、由字面量决定的结果值单元。每个单元是一个可能上屏的完整值
 * (模板串与 `+` 拼接把各字面片段拼成一个单元)。识别不了的形状返回空数组。
 */
function resultUnits(expression) {
  if (!expression) return [];
  if (ts.isStringLiteral(expression) || ts.isNoSubstitutionTemplateLiteral(expression)) {
    return [expression.text];
  }
  if (ts.isTemplateExpression(expression)) {
    return [[expression.head.text, ...expression.templateSpans.map((span) => span.literal.text)].join("")];
  }
  if (
    ts.isParenthesizedExpression(expression)
    || ts.isAsExpression(expression)
    || ts.isSatisfiesExpression(expression)
    || ts.isNonNullExpression(expression)
  ) {
    return resultUnits(expression.expression);
  }
  if (ts.isConditionalExpression(expression)) {
    return [...resultUnits(expression.whenTrue), ...resultUnits(expression.whenFalse)];
  }
  if (ts.isBinaryExpression(expression)) {
    const operator = expression.operatorToken.kind;
    if (RESULT_OPERATORS.has(operator)) return resultUnits(expression.right);
    if (operator === ts.SyntaxKind.PlusToken) {
      const parts = [...resultUnits(expression.left), ...resultUnits(expression.right)];
      return parts.length > 0 ? [parts.join("")] : [];
    }
  }
  return [];
}

function attributeUnits(initializer) {
  if (ts.isStringLiteral(initializer)) return [initializer.text];
  if (ts.isJsxExpression(initializer)) return resultUnits(initializer.expression);
  return [];
}

/**
 * 每个 `title` / `aria-label` 属性一条:{ element, name, units }。按「元素名 + 属性名 + 值」
 * 认身份,不报行号——前端守卫一律用语义身份(static-source-policy 的约定)。
 */
function copyAttributes(sourceFile) {
  const found = [];
  function visit(node) {
    if (ts.isJsxAttribute(node) && node.initializer) {
      const name = node.name.getText(sourceFile);
      if (COPY_ATTRIBUTES.has(name)) {
        const element = node.parent.parent.tagName.getText(sourceFile);
        found.push({ element, name, units: attributeUnits(node.initializer) });
      }
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return found;
}

function isEnglishOnly(unit) {
  return ASCII_LETTER.test(unit) && !CJK.test(unit) && !ALLOWED_ENGLISH.has(unit);
}

function englishCopyOffenders(sourceFile) {
  return copyAttributes(sourceFile).flatMap(({ element, name, units }) => units
    .filter(isEnglishOnly)
    .map((unit) => `${sourceFile.fileName} <${element} ${name}> ${JSON.stringify(unit)}`));
}

test("no title / aria-label in production code is English-only", async () => {
  const offenders = [];
  for (const item of await appSourceModules()) {
    offenders.push(...englishCopyOffenders(item.module));
  }
  assert.deepEqual(offenders, [], "控件的 title / aria-label 写中文（如「关闭」「搜索」），不写纯英文");
});

test("the scan actually reaches the controls it protects", async () => {
  // 非空转哨兵:如果属性名比对、遍历或取值坏了,上一条会因为什么都没扫到而恒绿。
  // C2 改过的 17 个「×」按钮都带 title="关闭",扫描必须看得见它们。
  let closeTitles = 0;
  for (const item of await appSourceModules()) {
    for (const attribute of copyAttributes(item.module)) {
      if (attribute.name === "title" && attribute.units.includes("关闭")) closeTitles += 1;
    }
  }
  assert.ok(closeTitles >= 17, `只扫到 ${closeTitles} 个 title="关闭"`);
});

test("the allow-list stays justified", () => {
  for (const [value, reason] of ALLOWED_ENGLISH) {
    assert.ok(typeof reason === "string" && reason.length > 0, `${value} 缺登记理由`);
  }
});

test("the predicate catches every result position that can carry English copy", () => {
  const source = [
    'export const a = <button title="Close">×</button>;',
    'export const b = <IconButton aria-label="View mode" />;',
    'export const c = <span title={"search"}>⌕</span>;',
    "export const d = <a title={`Close ${name}`}>×</a>;",
    'export const e = <em title={busy ? "关闭" : "Close"}>×</em>;',
    'export const f = <i title={hint ?? "Search"}>⌕</i>;',
    'export const g = <b title={hint || ("Close" as string)}>×</b>;',
    'export const h = <u aria-label={busy && "Saving"} />;',
    'export const i = <s title={"Open " + name} />;',
  ].join("\n");
  assert.deepEqual(englishCopyOffenders(parseText(source, "probe.tsx")), [
    'probe.tsx <button title> "Close"',
    'probe.tsx <IconButton aria-label> "View mode"',
    'probe.tsx <span title> "search"',
    'probe.tsx <a title> "Close "',
    'probe.tsx <em title> "Close"',
    'probe.tsx <i title> "Search"',
    'probe.tsx <b title> "Close"',
    'probe.tsx <u aria-label> "Saving"',
    'probe.tsx <s title> "Open "',
  ]);
});

test("the predicate leaves Chinese copy, symbols, keys, identifiers and other attributes alone", () => {
  const source = [
    'export const a = <button onClick={onClose} title="关闭">×</button>;',
    'export const b = <Modal onClose={() => requestClose("button")} aria-label="图谱 Schema 视图" />;',
    'export const c = <PanelRightClose className="close-button" data-close="Close" placeholder="Search" />;',
    "export const d = <button title={closeLabel}>×</button>;",
    'export const e = <button title={LABELS["close"]}>×</button>;',
    'export const f = <button aria-label={label(MAP, "close", "关闭")}>×</button>;',
    'export const g = <button title="×">×</button>;',
    'export const h = <button title="⌕" aria-label="">⌕</button>;',
    'export const i = <button title="42">42</button>;',
    "export const j = <button title={`${count} 条`}>…</button>;",
    'export const k = "Close";',
  ].join("\n");
  assert.deepEqual(englishCopyOffenders(parseText(source, "probe.tsx")), []);
});
