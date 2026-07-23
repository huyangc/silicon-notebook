import test from "node:test";
import assert from "node:assert/strict";

import {
  COMPLETION_CONFIDENCE_LABELS,
  canAcceptCompletionSuggestion,
  completableKnowhowColumns,
  completionSavePlan,
  completionReferenceLabel,
} from "./knowhow-complete-logic.ts";

const columns = [
  { id: "c-anchor", name: "问题", role: "anchor", position: 0 },
  { id: "c-filled", name: "现象", role: "attribute", position: 1 },
  { id: "c-empty", name: "处理方法", role: "procedure", position: 2 },
  { id: "c-space", name: "工具", role: "entity", position: 3 },
];

test("completableKnowhowColumns: 排除 anchor、已有内容和纯空白，只保留精确空串列", () => {
  const row = {
    id: "r1", position: 0, projectionStatus: "synced",
    cells: { "c-anchor": "供电异常", "c-filled": "无法启动", "c-empty": "", "c-space": "  \n" },
  };
  assert.deepStrictEqual(
    completableKnowhowColumns(row, columns, "c-anchor").map((column) => column.id),
    ["c-empty"],
  );
});

test("completionSavePlan: 共享空格冻结完整组员、空基线与 anchor 原值", () => {
  const rows = [
    { id: "r1", position: 0, projectionStatus: "synced", cells: { "c-anchor": " 时钟 ", "c-empty": "" } },
    { id: "r2", position: 1, projectionStatus: "synced", cells: { "c-anchor": "时钟", "c-empty": "" } },
  ];
  const plan = completionSavePlan("r1", "c-empty", rows, "c-anchor");
  assert.deepStrictEqual(plan.targetRowIds, ["r1", "r2"]);
  assert.deepStrictEqual([...plan.expectedByRowId.entries()], [["r1", ""], ["r2", ""]]);
  assert.equal(plan.anchorGuard.anchorColumnId, "c-anchor");
  assert.deepStrictEqual([...plan.anchorGuard.expectedAnchorByRowId.entries()], [["r1", " 时钟 "], ["r2", "时钟"]]);
});

test("completionSavePlan: 非共享列只冻结当前行，不携带整组 anchor 守卫", () => {
  const rows = [
    { id: "r1", position: 0, projectionStatus: "synced", cells: { "c-anchor": "时钟", "c-empty": "" } },
    { id: "r2", position: 1, projectionStatus: "synced", cells: { "c-anchor": "时钟", "c-empty": "已有内容" } },
  ];
  const plan = completionSavePlan("r1", "c-empty", rows, "c-anchor");
  assert.deepStrictEqual(plan.targetRowIds, ["r1"]);
  assert.deepStrictEqual([...plan.expectedByRowId.entries()], [["r1", ""]]);
  assert.equal(plan.anchorGuard, undefined);
});

test("completionSavePlan: trim 后共享但兄弟 raw 为纯空白时退回当前行单格计划", () => {
  const rows = [
    { id: "r1", position: 0, projectionStatus: "synced", cells: { "c-anchor": "时钟", "c-empty": "" } },
    { id: "r2", position: 1, projectionStatus: "synced", cells: { "c-anchor": "时钟", "c-empty": "  \n" } },
  ];
  const plan = completionSavePlan("r1", "c-empty", rows, "c-anchor");
  assert.deepStrictEqual(plan.targetRowIds, ["r1"]);
  assert.deepStrictEqual([...plan.expectedByRowId.entries()], [["r1", ""]]);
  assert.equal(plan.anchorGuard, undefined);
});

test("置信度文案固定为易读中文", () => {
  assert.deepStrictEqual(COMPLETION_CONFIDENCE_LABELS, {
    high: "高置信度", medium: "中置信度", low: "低置信度",
  });
});

test("canAcceptCompletionSuggestion: 放弃项或空建议不可接受", () => {
  const base = { columnId: "c1", confidence: "low", basedOnRowIds: [], basis: "" };
  assert.equal(canAcceptCompletionSuggestion({ ...base, suggestionMd: "建议", abstainReason: "" }), true);
  assert.equal(canAcceptCompletionSuggestion({ ...base, suggestionMd: null, abstainReason: "证据不足" }), false);
  assert.equal(canAcceptCompletionSuggestion({ ...base, suggestionMd: "建议", abstainReason: "证据冲突" }), false);
});

test("completionReferenceLabel: 已知 id 映射行标题，未知 id 不泄漏内部标识", () => {
  const rows = [{
    id: "internal-r2", position: 1, projectionStatus: "synced",
    cells: { "c-anchor": "参考：时钟漂移", "c-filled": "抖动" },
  }];
  assert.equal(completionReferenceLabel("internal-r2", rows, columns), "参考：时钟漂移");
  assert.equal(completionReferenceLabel("secret-id", rows, columns), "表内相关记录");
});
