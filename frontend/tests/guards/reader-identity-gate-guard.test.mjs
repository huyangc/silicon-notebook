// `isReader` 只许做**身份显示**,写门一律走 `readOnlyWorkspace`(群组知识共享 P2-T2 评审 P2-3)。
//
// 背景:P2 之前 `isReader = access === "reader"` 同时承担两件事——「这是别人共享给我的
// 库」(身份)和「我不能写」(权限)。P2 之后组管理员的 access 仍是 "reader" 却**有**内容
// 管理权,两件事分了家:
//   · 写门(添加来源 / 建图谱 / 写 knowhow / …)一律用 `readOnlyWorkspace`
//     (= !canWriteNotebook,组管理员为 false);
//   · `isReader` 只留给顶栏那个「身份徽章 vs 可编辑标题」的三元(ReaderNotebookBadge)。
//
// 最危险的回归形态是有人把一颗写门写成 `!isReader`(就像 P2 之前那样)——它不会报错,
// 只会把组管理员挡在一个后端明明放行的入口外面(「添加来源」那类格子),而这种「少画了
// 按钮」的 bug 没有任何功能测试会自然抓到。所以这条守卫用 AST(跳注释/字符串):
//   ① 任何 `!isReader` 一律报红(写门必须改用 readOnlyWorkspace);
//   ② 每一处 `isReader` 引用只能是它的声明、或渲染 ReaderNotebookBadge 的那个三元条件。
import test from "node:test";
import assert from "node:assert/strict";

import ts from "typescript";

import { parseModule } from "../../test-support/semantic-source.mjs";

const page = await parseModule("page.tsx");

/** 深度优先收集满足 predicate 的节点。 */
function collect(root, predicate) {
  const out = [];
  const visit = (node) => {
    if (predicate(node)) out.push(node);
    ts.forEachChild(node, visit);
  };
  visit(root);
  return out;
}

/** 该 `isReader` 三元条件的 whenTrue 子树里有没有引用 ReaderNotebookBadge。 */
function whenTrueRendersBadge(conditional) {
  return collect(
    conditional.whenTrue,
    (node) => ts.isIdentifier(node) && node.text === "ReaderNotebookBadge",
  ).length > 0;
}

const isReaderIdentifiers = collect(
  page,
  (node) => ts.isIdentifier(node) && node.text === "isReader",
);

test("守卫非空转:page.tsx 里确实存在 isReader(否则这条守卫什么都没在钉)", () => {
  assert.ok(
    isReaderIdentifiers.length >= 2,
    `page.tsx 里的 isReader 引用只有 ${isReaderIdentifiers.length} 处——` +
      "少于「声明 + 身份三元」两处,疑似解析落空或变量被改名,守卫已失去意义",
  );
});

test("没有任何 `!isReader` 写门(写门必须走 readOnlyWorkspace)", () => {
  const negations = collect(
    page,
    (node) =>
      ts.isPrefixUnaryExpression(node) &&
      node.operator === ts.SyntaxKind.ExclamationToken &&
      ts.isIdentifier(node.operand) &&
      node.operand.text === "isReader",
  );
  assert.equal(
    negations.length,
    0,
    "发现 `!isReader` —— P2-T2 之后写门一律用 readOnlyWorkspace(组管理员 access 仍是 " +
      "reader 却有写权,按 !isReader 判会把「添加来源」这类入口对他藏起来)。",
  );
});

test("每一处 isReader 只能是声明,或渲染 ReaderNotebookBadge 的身份三元条件", () => {
  const offenders = [];
  for (const id of isReaderIdentifiers) {
    const parent = id.parent;
    // ① 声明:`const isReader = ...`
    if (ts.isVariableDeclaration(parent) && parent.name === id) continue;
    // ② 身份三元:`isReader ? <ReaderNotebookBadge .../> : <input/>`
    if (
      ts.isConditionalExpression(parent) &&
      parent.condition === id &&
      whenTrueRendersBadge(parent)
    ) {
      continue;
    }
    offenders.push(semanticSnippet(id));
  }
  assert.deepEqual(
    offenders,
    [],
    "isReader 出现在了「声明 / 身份徽章三元」之外的位置——它只能做身份显示。" +
      "如果这是一颗写门,改用 readOnlyWorkspace;如果是别的身份分支,把它并进那个三元。",
  );
});

function semanticSnippet(node) {
  // 给出出错处所在语句的一小段文本,便于定位(不含行号,挪动不报红)。
  let cursor = node;
  while (cursor.parent && !ts.isStatement(cursor.parent)) cursor = cursor.parent;
  return cursor.getText().replace(/\s+/g, " ").slice(0, 120);
}
