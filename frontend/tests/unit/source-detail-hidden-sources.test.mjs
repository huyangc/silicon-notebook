// 来源详情窗对「记忆 / Knowhow 同步行」不提供重新解析、删除与命令目录。
//
// 详情窗能从引用卡的「查看原文」打开任何同库来源，包括自己的记忆投影和 Knowhow 格子
// 所在的同步行；后端对这两类来源的重新解析、删除与七个命令目录端点一律回 404。入口若
// 照常出现，用户点了只会得到一个「找不到」——所以这三处入口必须由同一个判据收起。
//
// 详情窗整块长在 page.tsx 里（路由文件不能导出子组件，也就没法单独渲染），所以这里用
// AST 钉接线：三处入口各自的外层条件里都有 `sourceDetailManageable(sourceDetail.type)`，
// 判据本身另用各类型逐一断言。
import test from "node:test";
import assert from "node:assert/strict";

import ts from "typescript";

import { parseModule } from "../../test-support/semantic-source.mjs";
import { sourceDetailManageable } from "../../app/source-management.ts";

const GATE = "sourceDetailManageable(sourceDetail.type)";

test("hidden source types are not manageable; imported documents are", () => {
  for (const type of ["memory", "knowhow"]) {
    assert.equal(sourceDetailManageable(type), false, type);
  }
  for (const type of ["document", "pdf", "url", "markdown", ""]) {
    assert.equal(sourceDetailManageable(type), true, type);
  }
});

function staticAttribute(opening, name) {
  for (const attribute of opening.attributes.properties) {
    if (
      ts.isJsxAttribute(attribute)
      && attribute.name.getText() === name
      && attribute.initializer
      && ts.isStringLiteral(attribute.initializer)
    ) {
      return attribute.initializer.text;
    }
  }
  return undefined;
}

function openingsWhere(sourceFile, predicate) {
  const found = [];
  function visit(node) {
    if ((ts.isJsxOpeningElement(node) || ts.isJsxSelfClosingElement(node)) && predicate(node)) {
      found.push(node);
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return found;
}

// Every condition that governs whether `node` renders: the left side of each
// enclosing `&&`, and the condition of each enclosing `?:` whose true branch
// holds it.
function governingConditions(node) {
  const conditions = [];
  let child = node;
  let parent = node.parent;
  while (parent) {
    if (
      ts.isBinaryExpression(parent)
      && parent.operatorToken.kind === ts.SyntaxKind.AmpersandAmpersandToken
      && parent.right === child
    ) {
      conditions.push(parent.left.getText());
    }
    if (ts.isConditionalExpression(parent) && parent.whenTrue === child) {
      conditions.push(parent.condition.getText());
    }
    child = parent;
    parent = parent.parent;
  }
  return conditions;
}

test("the detail dialog gates reparse, delete and the command catalog on the source type", async () => {
  const page = await parseModule("page.tsx");
  const targets = {
    "标题行的重新解析/删除": openingsWhere(
      page,
      (node) => node.tagName.getText() === "div"
        && staticAttribute(node, "className") === "source-detail-actions",
    ),
    "降级解析提示及其两个按钮": openingsWhere(
      page,
      (node) => node.tagName.getText() === "section"
        && staticAttribute(node, "aria-label") === "降级解析提示",
    ),
    "命令目录": openingsWhere(
      page,
      (node) => node.tagName.getText() === "CommandCatalogSection",
    ),
  };
  for (const [name, nodes] of Object.entries(targets)) {
    assert.equal(nodes.length, 1, `${name}：page.tsx 里应当恰好一处（入口被改名或删除？守卫失效）`);
    const conditions = governingConditions(nodes[0]);
    assert.ok(
      conditions.some((condition) => condition.includes(GATE)),
      `${name} 的外层条件里没有 ${GATE}：记忆 / Knowhow 来源会显示一个必然失败的入口`
        + `（现有条件：${JSON.stringify(conditions)}）`,
    );
  }
});
