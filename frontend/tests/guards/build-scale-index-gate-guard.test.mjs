// 「构建索引」入口必须过 `readOnlyIndexes` 写门(PR-A T3 质量评审发现的既有缺口)。
//
// 背景:AnswerView 的 `onBuildScaleIndex` 触发的 POST 走 scale_index:write(admin 档,
// `backend/app/api/deps.py::_CAPABILITY_LEVELS`)。page.tsx 此前把它无条件下发,
// 纯只读成员在「内容较多、尚未建索引」的回答横幅里会看到一颗可点的构建按钮,
// 点击必然 403。修法与「添加来源」同口径:只读时传 `undefined`——AnswerView 对
// 缺席的承接方会保留横幅诊断、只收起按钮(answer-panel.tsx 里已有注释钉住)。
//
// ⚠ 门的名字在跨环境同步 PR-4 之后从 `readOnlyWorkspace` 换成了 `readOnlyIndexes`,
// 不是改名而是**换了一条判据**:检索索引不在同步闭包里,后端围栏
// (`_CAPABILITY_MIRROR_FENCE["scale_index:write"] = False`)刻意放行它,镜像库必须
// 还能重建自己的索引。对纯只读成员两者恒同值,所以这条守卫要钉的东西没变:钉回
// `readOnlyWorkspace` 会让镜像库的索引永远停在导入那一刻且无从修复。
//
// 这类「多画了一颗必失败的按钮」不会被任何功能测试自然抓到,所以用 AST 钉住:
// page.tsx 里每一处 `onBuildScaleIndex` JSX 传值都必须是以 `readOnlyIndexes`
// 为条件的三元,且只读那一支是 `undefined`。改回无条件传 handler 即红。
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

const buildAttributes = collect(
  page,
  (node) =>
    ts.isJsxAttribute(node) && node.name.getText(page) === "onBuildScaleIndex",
);

function isUndefinedIdentifier(node) {
  return ts.isIdentifier(node) && node.text === "undefined";
}

function isReadOnlyIndexesIdentifier(node) {
  return ts.isIdentifier(node) && node.text === "readOnlyIndexes";
}

/**
 * 传值是否是「只读即 undefined」的写门三元。两种极性都算:
 *   `readOnlyIndexes ? undefined : handler`
 *   `!readOnlyIndexes ? handler : undefined`
 */
function gatedByReadOnlyIndexes(expression) {
  if (!ts.isConditionalExpression(expression)) return false;
  const { condition, whenTrue, whenFalse } = expression;
  if (isReadOnlyIndexesIdentifier(condition)) {
    return isUndefinedIdentifier(whenTrue);
  }
  if (
    ts.isPrefixUnaryExpression(condition) &&
    condition.operator === ts.SyntaxKind.ExclamationToken &&
    isReadOnlyIndexesIdentifier(condition.operand)
  ) {
    return isUndefinedIdentifier(whenFalse);
  }
  return false;
}

test("守卫非空转:page.tsx 里确实在传 onBuildScaleIndex(否则守卫什么都没在钉)", () => {
  assert.ok(
    buildAttributes.length >= 1,
    "page.tsx 里找不到 onBuildScaleIndex 传值——prop 被改名或入口被挪走," +
      "同步更新这条守卫,别让它空转。",
  );
});

test("每一处 onBuildScaleIndex 传值都带 readOnlyIndexes 三元,只读支为 undefined", () => {
  const offenders = [];
  for (const attribute of buildAttributes) {
    const initializer = attribute.initializer;
    const expression =
      initializer && ts.isJsxExpression(initializer)
        ? initializer.expression
        : undefined;
    if (!expression || !gatedByReadOnlyIndexes(expression)) {
      offenders.push(
        attribute.getText(page).replace(/\s+/g, " ").slice(0, 120),
      );
    }
  }
  assert.deepEqual(
    offenders,
    [],
    "onBuildScaleIndex 没过 readOnlyIndexes 写门——构建索引走 scale_index:write(admin 档)," +
      "只读成员点了必 403。写成 `readOnlyIndexes ? undefined : (...)`," +
      "AnswerView 对 undefined 会保留横幅说明、只收起按钮。",
  );
});
