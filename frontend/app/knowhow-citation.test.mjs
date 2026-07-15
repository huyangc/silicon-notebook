// Task 12（引用跳转，前端）：验证「ask 引用 → 表格行抽屉」这条链路的纯逻辑
// 部分——从 answer-formatting.ts 的 buildAnswerReferences（引用透传）到
// knowhow-model.ts 的 mapCitationKnowhowRef（wire snake_case → camelCase，
// T4 已实现，此处只验证与 buildAnswerReferences 拼接后的端到端形状），不涉及
// 任何 JSX/组件渲染（KnowhowPanel/AnswerView 的挂载态跳转由 tsc + 后续浏览器
// QA 验证，见任务简报）。
import test from "node:test";
import assert from "node:assert/strict";

import { buildAnswerReferences } from "./answer-formatting.ts";
import { mapCitationKnowhowRef } from "./knowhow-model.ts";

function knowhowCitation(overrides = {}) {
  return {
    label: "时序修复表 · 过冲问题 · 修复方法",
    source_id: "src-hidden",
    element_id: "el-cell-1",
    location_label: "过冲问题 › 修复方法",
    quoted_span: "增大去耦电容",
    tier: "personal",
    knowhow: { table_id: "tbl-1", row_id: "row-1" },
    ...overrides,
  };
}

test("buildAnswerReferences 原样透传命中 knowhow 格子的引用（无 anchor 标记时的回退列表）", () => {
  const references = buildAnswerReferences("没有引用标记的回答。", [], [knowhowCitation()]);

  assert.equal(references.length, 1);
  assert.deepEqual(references[0].citation?.knowhow, { table_id: "tbl-1", row_id: "row-1" });
});

test("mapCitationKnowhowRef 把透传出来的引用 knowhow 字段映射为 camelCase 跳转参数", () => {
  const references = buildAnswerReferences("没有引用标记的回答。", [], [knowhowCitation()]);
  const ref = mapCitationKnowhowRef(references[0].citation?.knowhow);

  assert.deepEqual(ref, { tableId: "tbl-1", rowId: "row-1" });
});

test("非 knowhow 引用透传后 knowhow 字段缺席，映射结果为 null（按钮不出现的判定依据）", () => {
  const plainCitation = knowhowCitation();
  delete plainCitation.knowhow;
  const references = buildAnswerReferences("没有引用标记的回答。", [], [plainCitation]);

  assert.equal(references[0].citation?.knowhow, undefined);
  assert.equal(mapCitationKnowhowRef(references[0].citation?.knowhow), null);
});

test("后端显式传 knowhow: null（非 knowhow 引用的另一种线上形状）也映射为 null", () => {
  const references = buildAnswerReferences(
    "没有引用标记的回答。",
    [],
    [knowhowCitation({ knowhow: null })],
  );

  assert.equal(mapCitationKnowhowRef(references[0].citation?.knowhow), null);
});

test("混合引用列表：只有命中 knowhow 格子的那条能映射出跳转参数", () => {
  const references = buildAnswerReferences("没有引用标记的回答。", [], [
    { label: "普通来源", source_id: "src-plain", element_id: "el-1", location_label: "p.1", quoted_span: "q1" },
    knowhowCitation({ element_id: "el-cell-2", knowhow: { table_id: "tbl-2", row_id: "row-2" } }),
  ]);

  assert.equal(mapCitationKnowhowRef(references[0].citation?.knowhow), null);
  assert.deepEqual(mapCitationKnowhowRef(references[1].citation?.knowhow), { tableId: "tbl-2", rowId: "row-2" });
});

test("既有 anchor 标记命中时，引用列表走 anchor 分支而非 citation——已知边界：本任务只富化 citation 落点，anchor 从不带 knowhow", () => {
  const anchors = [
    { key: "k1", object_id: "ko-1", object_type: "claim", label: "结论", tier: "personal" },
  ];
  const references = buildAnswerReferences("结论 [k1]。", anchors, [knowhowCitation()]);

  assert.equal(references.length, 1);
  assert.equal(references[0].anchor?.key, "k1");
  assert.equal(references[0].citation, undefined); // knowhow 引用被 anchor 分支整体让位，不出现
});
