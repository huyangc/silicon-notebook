// Task 12（引用跳转，前端）：验证「ask 引用 → 表格行抽屉」这条链路的纯逻辑
// 部分——从 answer-formatting.ts 的 buildAnswerReferences（引用透传）到
// knowhow-model.ts 的 mapCitationKnowhowRef（wire snake_case → camelCase，
// T4 已实现，此处只验证与 buildAnswerReferences 拼接后的端到端形状），不涉及
// 任何 JSX/组件渲染（KnowhowPanel/AnswerView 的挂载态跳转由 tsc + 后续浏览器
// QA 验证，见任务简报）。
//
// Task 12b（引用跳转扩面）：T12 曾在本文件最后一条用例里钉住一条「已知边界」
// ——anchor 分支从不带 knowhow，跳转按钮只在 citation 回退列表里出现。T12b
// 把这条边界推翻了：`AnswerAnchorLike` 现在也带可选 `knowhow` 字段（后端
// evidence_context.py 的 knowledge_context/parse_anchors 填充），
// `buildAnswerReferences` 本身不用改——它把整个 anchor 对象原样透传进
// `reference.anchor`，字段是否存在只取决于调用方传入的数据形状。真正变化的
// 是 answer-panel.tsx 的 `SelectedReferenceDetail`：算 knowhowRef 的表达式从
// `citation?.knowhow` 改成 `citation?.knowhow ?? anchor?.knowhow`——这是一个
// TSX 组件内部的一行表达式，`.test.mjs` 不能 import TSX（T12 报告踩过的坑：
// 含 TS 类型语法会让 `node --test` 直接 SyntaxError），所以下面用同一形状的
// 内联表达式镜像着测，而不是导入组件。
import test from "node:test";
import assert from "node:assert/strict";

import { buildAnswerReferences } from "../../app/answer-formatting.ts";
import { mapCitationKnowhowRef } from "../../app/knowhow-model.ts";

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

test("既有 anchor 标记命中时，引用列表走 anchor 分支而非 citation；命中的 anchor 本身没有 knowhow 时不映射出跳转参数", () => {
  const anchors = [
    { key: "k1", object_id: "ko-1", object_type: "claim", label: "结论", tier: "personal" },
  ];
  const references = buildAnswerReferences("结论 [k1]。", anchors, [knowhowCitation()]);

  assert.equal(references.length, 1);
  assert.equal(references[0].anchor?.key, "k1");
  assert.equal(references[0].citation, undefined); // knowhow 引用被 anchor 分支整体让位，不出现
  // Task 12b：anchor 分支赢了，但这个 anchor 自己没有 knowhow 字段——
  // citation 侧的 knowhow 已经让位、不该被误用，两者合起来映射为 null。
  assert.equal(
    mapCitationKnowhowRef(references[0].citation?.knowhow ?? references[0].anchor?.knowhow),
    null,
  );
});

test("Task 12b：anchor 命中 [k] 标记且自带 knowhow 时，buildAnswerReferences 原样透传该字段", () => {
  const anchors = [
    {
      key: "k1", object_id: "ko-kh-1", object_type: "修复方法", label: "增大去耦电容",
      tier: "personal", knowhow: { table_id: "tbl-1", row_id: "row-1" },
    },
  ];
  const references = buildAnswerReferences("修复方法见 [k1]。", anchors, []);

  assert.equal(references.length, 1);
  assert.deepEqual(references[0].anchor?.knowhow, { table_id: "tbl-1", row_id: "row-1" });
  assert.deepEqual(
    mapCitationKnowhowRef(references[0].citation?.knowhow ?? references[0].anchor?.knowhow),
    { tableId: "tbl-1", rowId: "row-1" },
  );
});

test("Task 12b：只有 anchor 命中 knowhow（citation 字段缺席）——回落表达式取到 anchor 那一侧", () => {
  // 镜像 answer-panel.tsx SelectedReferenceDetail 的
  // `citation?.knowhow ?? anchor?.knowhow`：citation 未定义时 `?.` 短路成
  // undefined，`??` 继续取 anchor 那一侧。
  const reference = {
    id: "anchor:k1", displayLabel: "[1]",
    anchor: {
      key: "k1", object_id: "ko-kh-1", object_type: "修复方法", label: "增大去耦电容",
      knowhow: { table_id: "tbl-1", row_id: "row-1" },
    },
  };
  const ref = mapCitationKnowhowRef(reference.citation?.knowhow ?? reference.anchor?.knowhow);
  assert.deepEqual(ref, { tableId: "tbl-1", rowId: "row-1" });
});

test("Task 12b：citation 与 anchor 都带 knowhow 时，回落表达式里 citation 优先", () => {
  // buildAnswerReferences 的真实输出里两者不会同时出现在同一条 reference 上
  // （有 [k] 命中就整体走 anchor 分支，citation 字段留空）——这里直接构造一个
  // "两者都有"的 reference，测的是 SelectedReferenceDetail 那行表达式自身的
  // 优先级语义（citation 在前，`??` 只在左侧为 null/undefined 时才看右侧）。
  const reference = {
    id: "x", displayLabel: "[1]",
    anchor: {
      key: "k1", object_id: "ko-1", object_type: "claim", label: "l",
      knowhow: { table_id: "tbl-anchor", row_id: "row-anchor" },
    },
    citation: {
      label: "l", source_id: "s", element_id: "e", location_label: "p", quoted_span: "q",
      knowhow: { table_id: "tbl-citation", row_id: "row-citation" },
    },
  };
  const ref = mapCitationKnowhowRef(reference.citation?.knowhow ?? reference.anchor?.knowhow);
  assert.deepEqual(ref, { tableId: "tbl-citation", rowId: "row-citation" });
});

test("Task 12b：citation 与 anchor 都没有 knowhow——回落表达式映射为 null", () => {
  const reference = {
    id: "anchor:k1", displayLabel: "[1]",
    anchor: { key: "k1", object_id: "ko-2", object_type: "concept", label: "Cascode" },
  };
  const ref = mapCitationKnowhowRef(reference.citation?.knowhow ?? reference.anchor?.knowhow);
  assert.equal(ref, null);
});

test("Task 12b 评审修复（grounded 主路径）：chunk 型 anchor 自带 knowhow、citation 被 anchor 分支整体遮蔽时，按钮判定仍解析出跳转参数", () => {
  // 评审复现的关键形态：LLM 按 answer_prompt 要求给每句有据的话标 [k] →
  // buildAnswerReferences 的 anchor 优先全有全无让 citation（哪怕带
  // knowhow）整体让位——此时 knowhow 必须来自 chunk 型 anchor 本身（后端
  // chunk_context 的评审修复），否则「在表格中查看」在最主流的问答形态里
  // 永远不出现。
  const anchors = [
    {
      key: "k1", object_id: "chunk-kh-1", object_type: "chunk",
      label: "时序修复表 › 过冲问题 › 修复方法", tier: "personal",
      knowhow: { table_id: "tbl-1", row_id: "row-1" },
    },
  ];
  const references = buildAnswerReferences("修复方法见 [k1]。", anchors, [knowhowCitation()]);

  assert.equal(references.length, 1);
  assert.equal(references[0].anchor?.object_type, "chunk");
  assert.equal(references[0].citation, undefined); // citation 被 anchor 分支遮蔽
  assert.deepEqual(
    mapCitationKnowhowRef(references[0].citation?.knowhow ?? references[0].anchor?.knowhow),
    { tableId: "tbl-1", rowId: "row-1" },
  );
});
