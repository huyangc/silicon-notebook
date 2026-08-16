// 「Agent 添加」来源徽标守卫。
//
// page.tsx 巨大且没有整页渲染的组件测试基建（无 mock provider/API 的先例），所以渲染
// 门控与文案走 AST 语义扫描（仿 source-poll-refresh-guard.test.mjs：对 `parseModule`
// 产出的节点树做结构判断，而不是裸文本正则）。样式面单独读一次 globals.css 原文
// （仿 effort-picker-style-guard.test.mjs / answer-heading-scope-guard.test.mjs）——CSS
// 没有语义 AST 可消费，jsdom 的 getComputedStyle 也不实现选择器特异性/级联，文本是
// 唯一诚实的输入；因此本文件已按 static-source-policy.test.mjs 的
// `DIRECT_READ_ALLOWLIST` 模式登记，见该文件里紧邻两个先例的条目与理由。
//
// 已知盲区（如实说明，不假装全覆盖）：本守卫钉的是"表达式逐字形态"（门控条件 + 徽标
// 元素本身），不是"徽标在 sources.map 渲染树里的位置"。把整段
// `{source.agent_created && (<span className="source-agent-badge" .../>)}` 原样剪切
// 到 sources.map 之外的任意位置（比如挪到笔记本级别、只渲染一次而非逐行来源），门控
// 表达式与 className 都不变，本守卫测不出来——这类"位置漂移"目前只能靠人工评审或另
// 一层渲染快照兜底，不在本文件范围内。
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import ts from "typescript";

import {
  jsxElements,
  jsxTextValues,
  parseModule,
} from "../../test-support/semantic-source.mjs";

const APP_DIR = path.resolve(path.dirname(fileURLToPath(import.meta.url)), "../../app");
const CSS = await readFile(path.join(APP_DIR, "globals.css"), "utf8");

/** 粗粒度切出顶层规则块(与 effort-picker-style-guard.test.mjs 同一实现,够用——本文件
 *  关心的规则都不在 @media 嵌套内)。 */
function cssRules(css) {
  const rules = [];
  const withoutComments = css.replace(/\/\*[\s\S]*?\*\//g, "");
  const pattern = /([^{}]+)\{([^{}]*)\}/g;
  let match;
  while ((match = pattern.exec(withoutComments)) !== null) {
    const selectorList = match[1].trim();
    if (selectorList.startsWith("@")) continue;
    rules.push({
      selectors: selectorList.split(",").map((s) => s.trim()).filter(Boolean),
      body: match[2],
    });
  }
  return rules;
}

/** page.tsx 里 className="source-agent-badge" 的 <span> JSX 开标签节点(不存在则 null)。 */
function findAgentBadgeSpan(sourceFile) {
  let found = null;
  const visit = (node) => {
    if (
      (ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node))
      && node.tagName.getText(sourceFile) === "span"
    ) {
      const classAttr = node.attributes.properties.find(
        (property) => ts.isJsxAttribute(property) && property.name.getText(sourceFile) === "className",
      );
      if (
        classAttr
        && classAttr.initializer
        && ts.isStringLiteral(classAttr.initializer)
        && classAttr.initializer.text === "source-agent-badge"
      ) {
        found = node;
      }
    }
    if (!found) ts.forEachChild(node, visit);
  };
  visit(sourceFile);
  return found;
}

/** 把一个条件表达式沿顶层 `&&` 拆成合取项的文本列表(不下钻其它运算符——那些是
 *  不同的门控形态,不该被这份"是不是合取项之一"的判定悄悄吸收)。 */
function conjunctsOf(node, sourceFile) {
  if (
    ts.isBinaryExpression(node)
    && node.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken
  ) {
    return [...conjunctsOf(node.left, sourceFile), ...conjunctsOf(node.right, sourceFile)];
  }
  return [node.getText(sourceFile).replace(/\s+/g, " ").trim()];
}

/**
 * 从该 JSX 节点向上找**最近**一层门控此节点渲染的表达式,接受两种形态:
 *   · `a && (<jsx/>)`  —— BinaryExpression 且 `.right === 当前节点`;
 *   · `a ? (<jsx/>) : b` —— ConditionalExpression 且 `.whenTrue === 当前节点`。
 * 必须是"最近"而不是"任意一层祖先"——page.tsx 顶层还有 `isWorkspace &&
 * currentNotebook && (...)` 这样的外层门控包住整块来源列表,若不限定"直接命中
 * right/whenTrue"会把外层无关门控也算进来(实测按祖先枚举会误命中 2 条)。中间插入
 * 的普通 JSX 包裹(比如外包一层 <div>)不影响判定——climb 只按 `.parent` 链走,不关心
 * 中间节点类型,只在每一层检查"这一层的父节点是不是恰好把当前节点当 right/whenTrue"。
 */
function nearestGate(node, sourceFile) {
  let current = node;
  while (current) {
    const parent = current.parent;
    if (parent) {
      if (
        ts.isBinaryExpression(parent)
        && parent.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken
        && parent.right === current
      ) {
        return { conditionNode: parent.left };
      }
      if (ts.isConditionalExpression(parent) && parent.whenTrue === current) {
        return { conditionNode: parent.condition };
      }
    }
    current = parent;
  }
  return null;
}

test("source list gates the Agent-added badge on source.agent_created", async () => {
  const page = await parseModule("page.tsx");
  const span = findAgentBadgeSpan(page);

  // 变异验证①:把整段渲染删掉,span 就找不到了。
  assert.ok(span, "expected a <span className=\"source-agent-badge\"> in page.tsx");

  const gate = nearestGate(span, page);
  // 变异验证②:门控被整段摘掉(徽标变成无条件渲染)——找不到最近门控。
  assert.ok(
    gate,
    "Agent-added badge lost its rendering gate — expected `source.agent_created && (...)` "
    + "or an equivalent ternary directly enclosing the badge span",
  );

  const conjuncts = conjunctsOf(gate.conditionNode, page);
  // 变异验证③:门控条件被换成 `true`、或换成别的字段(如 source.kg_extracted)——
  // source.agent_created 不再是合取项之一。加一个新的合取项(比如
  // `source.agent_created && !deletingSource`)不算违规,因为它仍在合取项列表里。
  assert.ok(
    conjuncts.includes("source.agent_created"),
    "Agent-added badge gate no longer includes `source.agent_created` as a conjunct "
    + `(found: ${conjuncts.join(" && ")}) — the badge must stay gated on the agent_created flag, `
    + "not replaced by another field or hardcoded",
  );
});

test("Agent-added badge renders exactly once with the provenance title", async () => {
  const page = await parseModule("page.tsx");
  const spans = jsxElements(page, "span");
  const badgeSpans = spans.filter(
    (el) => el.attributes.className === "source-agent-badge",
  );

  assert.equal(badgeSpans.length, 1, "expected exactly one source-agent-badge span");
  assert.equal(badgeSpans[0].attributes.title, "由 Agent 通过接入通道添加的来源");

  const visibleCopy = jsxTextValues(page).join(" ");
  assert.match(visibleCopy, /Agent 添加/);
});

test("no span combines the source-agent-badge class with anomaly/danger/warning styling", async () => {
  const page = await parseModule("page.tsx");
  const spans = jsxElements(page, "span");
  // 全文件扫描(不预先按精确相等过滤className),否则"是否混入警示色类"这条断言会
  // 在前置精确过滤下永不可能失败——精确等于 "source-agent-badge" 的字符串里本来就
  // 不可能同时含 anomaly/danger/warning。
  const offenders = spans.filter((element) => {
    const className = String(
      element.attributes?.className ?? element.bindings?.className ?? "",
    );
    return className.includes("source-agent-badge") && /anomaly|danger|warning/i.test(className);
  });

  assert.deepEqual(
    offenders,
    [],
    "no <span> should combine source-agent-badge with an anomaly/danger/warning class "
    + "— this badge is a neutral provenance note, not an AnomalyBadge",
  );
});

test("source-agent-badge shares the neutral pill selector group in globals.css, not warning tokens", () => {
  const rules = cssRules(CSS);
  const groupRule = rules.find((rule) => rule.selectors.includes(".source-agent-badge"));

  assert.ok(groupRule, "expected a `.source-agent-badge` rule in globals.css");
  assert.ok(
    groupRule.selectors.includes(".source-kg-badge"),
    "`.source-agent-badge` must stay in the same selector group as `.source-kg-badge` "
    + `(found selectors: ${groupRule.selectors.join(", ")}) — that's what guarantees the "same neutral `
    + 'pill" instead of two copies that can silently drift apart',
  );
  assert.doesNotMatch(
    groupRule.body,
    /--danger|--warning|danger-bg|warning-bg/,
    "the shared neutral pill rule must not reference any danger/warning design tokens",
  );

  // pointer-events:none 必须**不在**共享选择器组里——那会重新压住 source-agent-badge
  // 的 title tooltip(浏览器对 pointer-events:none 的元素直接跳过 hover/title)。它是
  // .source-kg-badge 独有的既有行为,必须单独留在自己的规则里,不随合并被带进共享组。
  assert.doesNotMatch(
    groupRule.body,
    /pointer-events\s*:\s*none/,
    "pointer-events:none 不能出现在共享选择器组里,否则 source-agent-badge 的 title 悬浮提示会被压住",
  );
  const kgOnlyRule = rules.find(
    (rule) => rule.selectors.length === 1 && rule.selectors[0] === ".source-kg-badge",
  );
  assert.ok(kgOnlyRule, "expected a `.source-kg-badge`-only rule carrying pointer-events: none");
  assert.match(kgOnlyRule.body, /pointer-events\s*:\s*none/);
});
