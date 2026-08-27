import test from "node:test";
import assert from "node:assert/strict";
import { httpErrorStatus } from "../../app/errors.ts";

import {
  rewriteAssetUrls,
  cellSummary,
  composeRowTitle,
  filterKnowhowTables,
  ROLE_LABELS,
  KIND_LABELS,
  fetchKnowhowTables,
  fetchKnowhowTable,
  patchKnowhowTable,
  addKnowhowColumn,
  patchKnowhowColumn,
  deleteKnowhowColumn,
  addKnowhowRow,
  deleteKnowhowRow,
  patchKnowhowCell,
  batchPatchKnowhowCells,
  reformatKnowhowCell,
  createKnowhowTable,
  importKnowhowPreview,
  importKnowhow,
  knowhowTemplateUrl,
  appendKnowhowPreview,
  appendKnowhowCommit,
  optimizeKnowhowCell,
  completeKnowhowRow,
  getCellCode,
  putCellCode,
  deleteCellCode,
  fetchKnowhowRowCodeByColumn,
  mapCitationKnowhowRef,
  uploadNotebookAsset,
  fetchKnowhowHistory,
  fetchKnowhowChange,
  fetchKnowhowHistoryDiff,
  fetchKnowhowCellHistory,
  revertKnowhowTable,
  parseKnowhowRevertFailure,
  KnowhowRevertError,
  createKnowhowMilestone,
  deleteKnowhowMilestone,
  pruneKnowhowHistory,
} from "../../app/knowhow-model.ts";

const healthTables = [
  {
    id: "pending",
    title: "Pending",
    description: "",
    rowCount: 1,
    projectionPending: 1,
    projectionFailed: 0,
    staleCodeCount: 0,
    lastActivityAt: "",
  },
  {
    id: "failed",
    title: "Failed",
    description: "",
    rowCount: 1,
    projectionPending: 0,
    projectionFailed: 1,
    staleCodeCount: 0,
    lastActivityAt: "",
  },
  {
    id: "stale",
    title: "Stale",
    description: "",
    rowCount: 1,
    projectionPending: 0,
    projectionFailed: 0,
    staleCodeCount: 1,
    lastActivityAt: "",
  },
];

test("filterKnowhowTables selects table-level health", () => {
  assert.deepEqual(
    filterKnowhowTables(healthTables, "projection_pending").map((table) => table.id),
    ["pending"],
  );
  assert.deepEqual(
    filterKnowhowTables(healthTables, "projection_failed").map((table) => table.id),
    ["failed"],
  );
  assert.deepEqual(
    filterKnowhowTables(healthTables, "stale_code").map((table) => table.id),
    ["stale"],
  );
});

test("fetchKnowhowTables maps health summaries with safe defaults", () => {
  return withFetchStub([
    {
      id: "t1",
      title: "Health",
      description: null,
      row_count: 2,
      projection_pending: 1,
      projection_failed: 3,
      stale_code_count: 4,
      last_activity_at: "2026-07-20T00:00:00Z",
    },
    { id: "t2", title: "Legacy", description: null },
  ], async () => {
    assert.deepEqual(await fetchKnowhowTables("nb-1"), [
      {
        id: "t1",
        title: "Health",
        description: "",
        rowCount: 2,
        projectionPending: 1,
        projectionFailed: 3,
        staleCodeCount: 4,
        lastActivityAt: "2026-07-20T00:00:00Z",
      },
      {
        id: "t2",
        title: "Legacy",
        description: "",
        rowCount: 0,
        projectionPending: 0,
        projectionFailed: 0,
        staleCodeCount: 0,
        lastActivityAt: "",
      },
    ]);
  });
});

// --- fetch stub helper（镜像 edge-review-queue.test.mjs 的 withFetchStub，
// 加了可参数化的响应体，供本文件校验 mapper 的 snake->camel 转换）------------

function withFetchStub(responseBody, run) {
  const calls = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    if (String(url).endsWith("/stream")) {
      const encoder = new TextEncoder();
      return new Response(new ReadableStream({
        start(controller) {
          controller.enqueue(encoder.encode(`${JSON.stringify({ event: "started", stage: "test", elapsed_ms: 0 })}\n`));
          controller.enqueue(encoder.encode(`${JSON.stringify({ event: "final", stage: "test", result: responseBody })}\n`));
          controller.close();
        },
      }), { status: 200, headers: { "Content-Type": "application/x-ndjson" } });
    }
    return new Response(JSON.stringify(responseBody), {
      status: 200,
      headers: { "Content-Type": "application/json" },
    });
  };
  return Promise.resolve(run(calls)).finally(() => {
    globalThis.fetch = original;
  });
}

function bodyOf(call) {
  return JSON.parse(call.init.body);
}

// --- KIND_LABELS / ROLE_LABELS -------------------------------------------------

test("KIND_LABELS: 恰好三项内容类型，文案与规格①一致（方法步骤/工具/事物/普通）", () => {
  assert.deepStrictEqual(KIND_LABELS, {
    procedure: "方法步骤",
    entity: "工具/事物",
    attribute: "普通",
  });
});

test("KIND_LABELS: 不含 anchor 键（anchor 走表级 anchorColumnId，不进列 kind 下拉）", () => {
  assert.ok(!("anchor" in KIND_LABELS));
});

test("KIND_LABELS: 每项文案都是非空字符串", () => {
  for (const kind of ["procedure", "entity", "attribute"]) {
    assert.ok(typeof KIND_LABELS[kind] === "string" && KIND_LABELS[kind].length > 0, `kind ${kind} 缺少文案`);
  }
});

test("ROLE_LABELS(deprecated): 覆盖全部四个 CellKind 值，含 anchor='行标题'", () => {
  assert.deepStrictEqual(ROLE_LABELS, {
    anchor: "行标题",
    procedure: "方法步骤",
    entity: "工具/事物",
    attribute: "普通",
  });
});

test("ROLE_LABELS(deprecated): 键集合 = KIND_LABELS 键集合 + anchor（旧徽章代码兼容）", () => {
  const roleKeys = Object.keys(ROLE_LABELS).sort();
  const expected = [...Object.keys(KIND_LABELS), "anchor"].sort();
  assert.deepStrictEqual(roleKeys, expected);
});

test("ROLE_LABELS(deprecated): 每个值的文案都是非空字符串", () => {
  for (const role of ["anchor", "procedure", "entity", "attribute"]) {
    assert.ok(typeof ROLE_LABELS[role] === "string" && ROLE_LABELS[role].length > 0, `role ${role} 缺少文案`);
  }
});

// --- composeRowTitle -----------------------------------------------------------

test("composeRowTitle: 两个非空格子按 ' · ' 连接", () => {
  assert.strictEqual(composeRowTitle(["Setup 违例", "调整时钟约束"]), "Setup 违例 · 调整时钟约束");
});

test("composeRowTitle: 默认 maxSegments=3，超过部分不参与拼接", () => {
  assert.strictEqual(composeRowTitle(["A", "B", "C", "D"]), "A · B · C");
});

test("composeRowTitle: 自定义 maxSegments 生效", () => {
  assert.strictEqual(composeRowTitle(["A", "B", "C", "D"], 2), "A · B");
});

test("composeRowTitle: 跳过空/纯空白格子，继续向后找非空格子（不提前中止扫描）", () => {
  assert.strictEqual(composeRowTitle(["", "A", "   ", "B", "C", "D"]), "A · B · C");
});

test("composeRowTitle: 全部为空（含纯空白）时返回空串", () => {
  assert.strictEqual(composeRowTitle(["", "   ", "\n"]), "");
});

test("composeRowTitle: 空数组返回空串", () => {
  assert.strictEqual(composeRowTitle([]), "");
});

test("composeRowTitle: 每段截断到 <=16 字，纯 slice 不加省略号（与后端 textops.compose_row_title 同规则——省略号是 node_name 的记号，不用于本合成标题）", () => {
  const longCell = "一二三四五六七八九十一二三四五六七八九十"; // 20 字
  const out = composeRowTitle([longCell]);
  assert.strictEqual(out.length, 16);
  assert.ok(!out.includes("…"));
  assert.strictEqual(out, "一二三四五六七八九十一二三四五六");
});

test("composeRowTitle: 恰好 16 字时不截断、不加省略号", () => {
  const exact = "一二三四五六七八九十一二三四五六"; // 16 字
  assert.strictEqual(composeRowTitle([exact]), exact);
});

test("composeRowTitle: 只取每格的首行，忽略换行后的内容", () => {
  assert.strictEqual(composeRowTitle(["首行\n第二行被忽略"]), "首行");
});

test("composeRowTitle: 首行为空白但后续行有内容时该格仍视为空（按首行判定，不回退扫描格内后续行）", () => {
  assert.strictEqual(composeRowTitle(["\n真正内容", "B"]), "B");
});

test("composeRowTitle: 每格先 trim 首行再判空/截断", () => {
  assert.strictEqual(composeRowTitle(["  带前后空格的值  "]), "带前后空格的值");
});

// --- anchorColumnId 映射（经 fetchKnowhowTable + mapDetail，覆盖“显式字段优先，
// 回退派生”两条路径，兼容 Task 3 落地前后的线上形状）--------------------------

function wireTable({ columns, rows = [], anchorColumnId } = {}) {
  const table = {
    id: "t1",
    title: "表",
    description: null,
    columns,
    rows,
  };
  if (anchorColumnId !== undefined) table.anchor_column_id = anchorColumnId;
  return table;
}

test("anchorColumnId: Task 3 落地后——显式字段为字符串时直接采用，不派生", () => {
  const wire = wireTable({
    columns: [{ id: "c1", name: "现象", position: 0, kind: "anchor" }],
    anchorColumnId: "c1",
  });
  return withFetchStub(wire, async () => {
    const detail = await fetchKnowhowTable("nb-1", "t1");
    assert.strictEqual(detail.anchorColumnId, "c1");
  });
});

test("anchorColumnId: Task 3 落地后——显式 null 表示确实无行标题列，即使有列也不派生", () => {
  const wire = wireTable({
    columns: [{ id: "c1", name: "备注", position: 0, kind: "attribute" }],
    anchorColumnId: null,
  });
  return withFetchStub(wire, async () => {
    const detail = await fetchKnowhowTable("nb-1", "t1");
    assert.strictEqual(detail.anchorColumnId, null);
  });
});

test("anchorColumnId: Task 3 落地前——字段不存在时从 columns[].kind==='anchor' 派生", () => {
  const wire = wireTable({
    columns: [
      { id: "c1", name: "工具", position: 1, kind: "entity" },
      { id: "c0", name: "现象", position: 0, kind: "anchor" },
    ],
  });
  return withFetchStub(wire, async () => {
    const detail = await fetchKnowhowTable("nb-1", "t1");
    assert.strictEqual(detail.anchorColumnId, "c0");
  });
});

test("anchorColumnId: Task 3 落地前——字段不存在且无列 kind==='anchor' 时派生为 null", () => {
  const wire = wireTable({
    columns: [{ id: "c1", name: "备注", position: 0, kind: "attribute" }],
  });
  return withFetchStub(wire, async () => {
    const detail = await fetchKnowhowTable("nb-1", "t1");
    assert.strictEqual(detail.anchorColumnId, null);
  });
});

test("列映射：兼容旧字段名 role（Task 3 落地前）与新字段名 kind（落地后）", () => {
  const wire = wireTable({
    columns: [
      { id: "c1", name: "旧形状", position: 0, role: "procedure" },
      { id: "c2", name: "新形状", position: 1, kind: "entity" },
    ],
  });
  return withFetchStub(wire, async () => {
    const detail = await fetchKnowhowTable("nb-1", "t1");
    assert.strictEqual(detail.columns[0].role, "procedure");
    assert.strictEqual(detail.columns[1].role, "entity");
  });
});

// --- patchKnowhowTable（payload 组装）-------------------------------------------

test("patchKnowhowTable: PATCH 正确 URL，只提供的字段进请求体", () => {
  return withFetchStub(wireTable({ columns: [] }), async (calls) => {
    await patchKnowhowTable("nb-1", "t1", { title: "新标题" });
    assert.strictEqual(calls[0].init.method, "PATCH");
    assert.match(calls[0].url, /\/notebooks\/nb-1\/knowhow\/t1$/);
    const body = bodyOf(calls[0]);
    assert.deepStrictEqual(body, { title: "新标题" });
  });
});

test("patchKnowhowTable: 未提供的字段不进请求体（omit，而非 null）", () => {
  return withFetchStub(wireTable({ columns: [] }), async (calls) => {
    await patchKnowhowTable("nb-1", "t1", { description: "新描述" });
    const body = bodyOf(calls[0]);
    assert.deepStrictEqual(body, { description: "新描述" });
    assert.ok(!("title" in body));
    assert.ok(!("anchor_column_id" in body));
  });
});

test("patchKnowhowTable: anchorColumnId 显式传 null 时保留在请求体里（清除语义，区别于不改）", () => {
  return withFetchStub(wireTable({ columns: [] }), async (calls) => {
    await patchKnowhowTable("nb-1", "t1", { anchorColumnId: null });
    const body = bodyOf(calls[0]);
    assert.ok("anchor_column_id" in body);
    assert.strictEqual(body.anchor_column_id, null);
  });
});

test("patchKnowhowTable: anchorColumnId 传字符串时原样透传为 anchor_column_id", () => {
  return withFetchStub(wireTable({ columns: [] }), async (calls) => {
    await patchKnowhowTable("nb-1", "t1", { anchorColumnId: "col-9" });
    assert.strictEqual(bodyOf(calls[0]).anchor_column_id, "col-9");
  });
});

// --- addKnowhowColumn / patchKnowhowColumn / deleteKnowhowColumn ----------------

test("addKnowhowColumn: POST 到 .../columns，请求体含 name/kind/position", () => {
  const wireColumn = { id: "c-new", name: "根因分析", position: 2, kind: "procedure" };
  return withFetchStub(wireColumn, async (calls) => {
    const column = await addKnowhowColumn("nb-1", "t1", { name: "根因分析", kind: "procedure", position: 2 });
    assert.match(calls[0].url, /\/notebooks\/nb-1\/knowhow\/t1\/columns$/);
    assert.strictEqual(calls[0].init.method, "POST");
    assert.deepStrictEqual(bodyOf(calls[0]), { name: "根因分析", kind: "procedure", position: 2 });
    assert.deepStrictEqual(column, { id: "c-new", name: "根因分析", position: 2, role: "procedure" });
  });
});

test("addKnowhowColumn: position 缺省时不进请求体", () => {
  const wireColumn = { id: "c-new", name: "备注", position: 3, kind: "attribute" };
  return withFetchStub(wireColumn, async (calls) => {
    await addKnowhowColumn("nb-1", "t1", { name: "备注", kind: "attribute" });
    assert.ok(!("position" in bodyOf(calls[0])));
  });
});

test("patchKnowhowColumn: PATCH 到 .../columns/{col}，只含提供的字段", () => {
  const wireColumn = { id: "c1", name: "改名后", position: 0, kind: "entity" };
  return withFetchStub(wireColumn, async (calls) => {
    const column = await patchKnowhowColumn("nb-1", "t1", "c1", { name: "改名后" });
    assert.match(calls[0].url, /\/notebooks\/nb-1\/knowhow\/t1\/columns\/c1$/);
    assert.strictEqual(calls[0].init.method, "PATCH");
    assert.deepStrictEqual(bodyOf(calls[0]), { name: "改名后" });
    assert.strictEqual(column.role, "entity");
  });
});

test("deleteKnowhowColumn: DELETE 到 .../columns/{col}", () => {
  return withFetchStub(null, async (calls) => {
    await deleteKnowhowColumn("nb-1", "t1", "c1");
    assert.match(calls[0].url, /\/notebooks\/nb-1\/knowhow\/t1\/columns\/c1$/);
    assert.strictEqual(calls[0].init.method, "DELETE");
  });
});

// --- addKnowhowRow / deleteKnowhowRow -------------------------------------------

test("addKnowhowRow: POST 到 .../rows，请求体含 cells/position，响应映射为 KnowhowRow", () => {
  const wireRow = { id: "r-new", position: 1, projection_status: "pending", cells: { c1: "值" } };
  return withFetchStub(wireRow, async (calls) => {
    const row = await addKnowhowRow("nb-1", "t1", { cells: { c1: "值" }, position: 1 });
    assert.match(calls[0].url, /\/notebooks\/nb-1\/knowhow\/t1\/rows$/);
    assert.deepStrictEqual(bodyOf(calls[0]), { cells: { c1: "值" }, position: 1 });
    assert.deepStrictEqual(row, { id: "r-new", position: 1, projectionStatus: "pending", cells: { c1: "值" } });
  });
});

test("deleteKnowhowRow: DELETE 到 .../rows/{row}", () => {
  return withFetchStub(null, async (calls) => {
    await deleteKnowhowRow("nb-1", "t1", "r1");
    assert.match(calls[0].url, /\/notebooks\/nb-1\/knowhow\/t1\/rows\/r1$/);
    assert.strictEqual(calls[0].init.method, "DELETE");
  });
});

// --- patchKnowhowCell ------------------------------------------------------------

test("patchKnowhowCell: PATCH 请求体为 {content_md}，响应 snake_case 映射为 camelCase", () => {
  const wireResult = { row_id: "r1", column_id: "c1", content_md: "新内容", projection_status: "syncing" };
  return withFetchStub(wireResult, async (calls) => {
    const result = await patchKnowhowCell("nb-1", "t1", "r1", "c1", "新内容");
    assert.match(calls[0].url, /\/notebooks\/nb-1\/knowhow\/t1\/rows\/r1\/cells\/c1$/);
    assert.strictEqual(calls[0].init.method, "PATCH");
    assert.deepStrictEqual(bodyOf(calls[0]), { content_md: "新内容" });
    assert.deepStrictEqual(result, {
      rowId: "r1",
      columnId: "c1",
      contentMd: "新内容",
      projectionStatus: "syncing",
    });
  });
});

// 并发防护（P1-b）：expectedBefore 提供时进请求体（后端据此做事务内比对，
// 陈旧则 409）；省略时不进请求体，退回既有 last-write-wins（手动编辑器语义不变）。
test("patchKnowhowCell: 提供 expectedBefore 时请求体带 expected_before（服务端比对基线）", () => {
  const wireResult = { row_id: "r1", column_id: "c1", content_md: "新内容", projection_status: "pending" };
  return withFetchStub(wireResult, async (calls) => {
    await patchKnowhowCell("nb-1", "t1", "r1", "c1", "新内容", "旧内容");
    assert.deepStrictEqual(bodyOf(calls[0]), { content_md: "新内容", expected_before: "旧内容" });
  });
});

test("patchKnowhowCell: 省略 expectedBefore 时请求体不含 expected_before 键（不是 null——手动编辑器 last-write-wins）", () => {
  const wireResult = { row_id: "r1", column_id: "c1", content_md: "x", projection_status: "pending" };
  return withFetchStub(wireResult, async (calls) => {
    await patchKnowhowCell("nb-1", "t1", "r1", "c1", "x");
    assert.strictEqual("expected_before" in bodyOf(calls[0]), false);
  });
});

// --- patchKnowhowCell: origin（knowhow 表版本管理 Task 14，第 7 个位置参数）---
// 后端 KnowhowCellPatch.origin 契约：省略时不进请求体，退回后端默认 "user"
// （既有手动编辑器调用点字节不变）；"从历史恢复此格" 等新调用点需要显式传
// "revert"，时间线上才分得清"手动改的"与"从历史恢复的"。

test("patchKnowhowCell: 省略 origin 时请求体不带该字段", () => {
  const wire = { row_id: "r1", column_id: "c1", content_md: "x", projection_status: "pending" };
  return withFetchStub(wire, async (calls) => {
    await patchKnowhowCell("nb-1", "t1", "r1", "c1", "x");
    assert.deepStrictEqual(bodyOf(calls[0]), { content_md: "x" });
  });
});

test("patchKnowhowCell: 传 origin 时请求体带 origin（从历史恢复要能区分来源）", () => {
  const wire = { row_id: "r1", column_id: "c1", content_md: "x", projection_status: "pending" };
  return withFetchStub(wire, async (calls) => {
    await patchKnowhowCell("nb-1", "t1", "r1", "c1", "x", undefined, "revert");
    assert.deepStrictEqual(bodyOf(calls[0]), { content_md: "x", origin: "revert" });
  });
});

test("patchKnowhowCell: origin 与 expectedBefore 同时提供时请求体两个键都带（批量规整保存路径）", () => {
  const wire = { row_id: "r1", column_id: "c1", content_md: "x", projection_status: "pending" };
  return withFetchStub(wire, async (calls) => {
    await patchKnowhowCell("nb-1", "t1", "r1", "c1", "x", "旧内容", "llm_reformat");
    assert.deepStrictEqual(bodyOf(calls[0]), {
      content_md: "x", expected_before: "旧内容", origin: "llm_reformat",
    });
  });
});

// --- reformatKnowhowCell（P1-a：回带 source_md）------------------------------

test("reformatKnowhowCell: POST 到 .../reformat/stream，响应 source_md 映射为 sourceMd（连同 candidate/source/changed）", () => {
  const wire = { candidate_md: "- a\n- b", source: "llm", changed: true, source_md: "原文快照" };
  return withFetchStub(wire, async (calls) => {
    const result = await reformatKnowhowCell("nb-1", "t1", "r1", "c1");
    assert.match(calls[0].url, /\/notebooks\/nb-1\/knowhow\/t1\/rows\/r1\/cells\/c1\/reformat\/stream$/);
    assert.strictEqual(calls[0].init.method, "POST");
    assert.deepStrictEqual(result, { candidateMd: "- a\n- b", source: "llm", changed: true, sourceMd: "原文快照" });
  });
});

test("reformatKnowhowCell: 可选 AbortSignal 透传到共享请求客户端", () => {
  const wire = { candidate_md: "x", source: "rule", changed: false, source_md: "x" };
  return withFetchStub(wire, async (calls) => {
    const controller = new AbortController();
    await reformatKnowhowCell("nb-1", "t1", "r1", "c1", controller.signal);
    assert.strictEqual(calls[0].init.signal, controller.signal);
  });
});

// --- batchPatchKnowhowCells（followup A：合并格整组单事务批量写，spec §6）--------

test("batchPatchKnowhowCells: PATCH 到 .../cells，请求体为 {column_id,row_ids,content_md}，响应列表按项 snake_case 映射为 camelCase", () => {
  const wireResult = [
    { row_id: "r1", column_id: "c1", content_md: "统一改法", projection_status: "pending" },
    { row_id: "r2", column_id: "c1", content_md: "统一改法", projection_status: "pending" },
  ];
  return withFetchStub(wireResult, async (calls) => {
    const result = await batchPatchKnowhowCells("nb-1", "t1", {
      columnId: "c1",
      rowIds: ["r1", "r2"],
      contentMd: "统一改法",
    });
    assert.match(calls[0].url, /\/notebooks\/nb-1\/knowhow\/t1\/cells$/);
    assert.strictEqual(calls[0].init.method, "PATCH");
    assert.deepStrictEqual(bodyOf(calls[0]), {
      column_id: "c1",
      row_ids: ["r1", "r2"],
      content_md: "统一改法",
    });
    assert.deepStrictEqual(result, [
      { rowId: "r1", columnId: "c1", contentMd: "统一改法", projectionStatus: "pending" },
      { rowId: "r2", columnId: "c1", contentMd: "统一改法", projectionStatus: "pending" },
    ]);
  });
});

test("batchPatchKnowhowCells: 提供 expectedBefore（按 rowIds 平行）时请求体带 expected_before 数组", () => {
  const wireResult = [
    { row_id: "r1", column_id: "c1", content_md: "统一改法", projection_status: "pending" },
    { row_id: "r2", column_id: "c1", content_md: "统一改法", projection_status: "pending" },
  ];
  return withFetchStub(wireResult, async (calls) => {
    await batchPatchKnowhowCells("nb-1", "t1", {
      columnId: "c1",
      rowIds: ["r1", "r2"],
      contentMd: "统一改法",
      expectedBefore: ["旧1", "旧2"],
    });
    assert.deepStrictEqual(bodyOf(calls[0]), {
      column_id: "c1",
      row_ids: ["r1", "r2"],
      content_md: "统一改法",
      expected_before: ["旧1", "旧2"],
    });
  });
});

test("batchPatchKnowhowCells: 省略 expectedBefore 时请求体不含 expected_before 键（手动编辑器合并格 last-write-wins）", () => {
  return withFetchStub([], async (calls) => {
    await batchPatchKnowhowCells("nb-1", "t1", { columnId: "c1", rowIds: ["r1"], contentMd: "x" });
    assert.strictEqual("expected_before" in bodyOf(calls[0]), false);
  });
});

test("batchPatchKnowhowCells: 响应空列表时返回空数组", () => {
  return withFetchStub([], async (calls) => {
    const result = await batchPatchKnowhowCells("nb-1", "t1", {
      columnId: "c1",
      rowIds: [],
      contentMd: "x",
    });
    assert.deepStrictEqual(bodyOf(calls[0]), { column_id: "c1", row_ids: [], content_md: "x" });
    assert.deepStrictEqual(result, []);
  });
});

// 并发防护（P1 round-4）：批量扇出还带上 anchor 基线守卫——anchorColumnId +
// expectedAnchor（按 rowIds 平行），后端在同一事务内重读每行 anchor 列，任一
// 行的 anchor 自建批次以来被移出组就整组 409（离组行不再被冻结扇出误写）。与
// expectedBefore 一样是可选的：手动编辑器省略。
test("batchPatchKnowhowCells: 提供 anchorColumnId + expectedAnchor（按 rowIds 平行）时请求体带 anchor_column_id + expected_anchor", () => {
  return withFetchStub([], async (calls) => {
    await batchPatchKnowhowCells("nb-1", "t1", {
      columnId: "c1",
      rowIds: ["r1", "r2"],
      contentMd: "统一改法",
      expectedBefore: ["旧1", "旧2"],
      anchorColumnId: "c-anchor",
      expectedAnchor: ["示波器", "示波器"],
    });
    assert.deepStrictEqual(bodyOf(calls[0]), {
      column_id: "c1",
      row_ids: ["r1", "r2"],
      content_md: "统一改法",
      expected_before: ["旧1", "旧2"],
      anchor_column_id: "c-anchor",
      expected_anchor: ["示波器", "示波器"],
    });
  });
});

test("batchPatchKnowhowCells: 省略 anchor 守卫时请求体不含 anchor_column_id / expected_anchor 键（手动编辑器合并格）", () => {
  return withFetchStub([], async (calls) => {
    await batchPatchKnowhowCells("nb-1", "t1", {
      columnId: "c1",
      rowIds: ["r1"],
      contentMd: "x",
      expectedBefore: ["旧1"],
    });
    const body = bodyOf(calls[0]);
    assert.strictEqual("anchor_column_id" in body, false);
    assert.strictEqual("expected_anchor" in body, false);
  });
});

// --- batchPatchKnowhowCells: origin（knowhow 表版本管理 Task 14）--------------
// 合并/共享格的批量规整回填走这条端点保存，需要自己的 origin 才能在时间线上
// 被记成 "llm_reformat" 而不是永远静默落回默认的 "user"（design doc §8.1 尾）。

test("batchPatchKnowhowCells: 省略 origin 时请求体不带该字段", () => {
  return withFetchStub([], async (calls) => {
    await batchPatchKnowhowCells("nb-1", "t1", { columnId: "c1", rowIds: ["r1"], contentMd: "x" });
    assert.strictEqual("origin" in bodyOf(calls[0]), false);
  });
});

test("batchPatchKnowhowCells: 传 origin 时请求体带 origin", () => {
  return withFetchStub([], async (calls) => {
    await batchPatchKnowhowCells("nb-1", "t1", {
      columnId: "c1", rowIds: ["r1"], contentMd: "x", origin: "llm_reformat",
    });
    assert.strictEqual(bodyOf(calls[0]).origin, "llm_reformat");
  });
});

// --- createKnowhowTable（建表向导）--------------------------------------------------

test("createKnowhowTable: POST 到 /notebooks/{nb}/knowhow，body 为 {title,columns,anchor_index}（anchorIndex null 也显式保留）", () => {
  const wire = wireTable({
    columns: [{ id: "c0", name: "违例概念", position: 0, kind: "anchor" }],
    anchorColumnId: "c0",
  });
  return withFetchStub(wire, async (calls) => {
    const detail = await createKnowhowTable("nb-1", {
      title: "时序修复",
      columns: [
        { name: "违例概念", kind: "attribute" },
        { name: "修复方法", kind: "procedure" },
      ],
      anchorIndex: 0,
    });
    assert.match(calls[0].url, /\/notebooks\/nb-1\/knowhow$/);
    assert.strictEqual(calls[0].init.method, "POST");
    assert.deepStrictEqual(bodyOf(calls[0]), {
      title: "时序修复",
      columns: [
        { name: "违例概念", kind: "attribute" },
        { name: "修复方法", kind: "procedure" },
      ],
      anchor_index: 0,
    });
    assert.strictEqual(detail.anchorColumnId, "c0");

    // 不设行标题列：anchor_index 显式 null 保留在请求体里（与 undefined 丢键区分）。
    await createKnowhowTable("nb-1", { title: "旅行日志", columns: [{ name: "日期", kind: "attribute" }], anchorIndex: null });
    const body = bodyOf(calls[1]);
    assert.ok("anchor_index" in body);
    assert.strictEqual(body.anchor_index, null);
  });
});

// --- knowhowTemplateUrl -----------------------------------------------------------

test("knowhowTemplateUrl: 拼出 .../knowhow/{t}/template", () => {
  assert.match(knowhowTemplateUrl("nb-1", "t1"), /\/notebooks\/nb-1\/knowhow\/t1\/template$/);
});

test("knowhowTemplateUrl: 不同表/notebook 生成不同 URL", () => {
  assert.notStrictEqual(knowhowTemplateUrl("nb-1", "t1"), knowhowTemplateUrl("nb-2", "t2"));
});

// --- appendKnowhowPreview / appendKnowhowCommit -----------------------------------

test("appendKnowhowPreview: POST multipart 到 .../append，mode=preview，响应映射 camelCase", () => {
  const wirePreview = {
    rows_preview: [["a", "b"]],
    total_rows: 12,
    unmatched_columns: ["未知列"],
    duplicate_titles: [{ row_index: 3, title: "已存在的标题" }],
  };
  return withFetchStub(wirePreview, async (calls) => {
    const file = new Blob(["x"]);
    const preview = await appendKnowhowPreview("nb-1", "t1", file);
    assert.match(calls[0].url, /\/notebooks\/nb-1\/knowhow\/t1\/append$/);
    assert.strictEqual(calls[0].init.method, "POST");
    const form = calls[0].init.body;
    assert.ok(form instanceof FormData);
    assert.strictEqual(form.get("mode"), "preview");
    assert.deepStrictEqual(preview, {
      rowsPreview: [["a", "b"]],
      totalRows: 12,
      unmatchedColumns: ["未知列"],
      duplicateTitles: [{ rowIndex: 3, title: "已存在的标题" }],
    });
  });
});

test("appendKnowhowCommit: 同一端点 mode=commit，响应为 {added}", () => {
  return withFetchStub({ added: 7 }, async (calls) => {
    const file = new Blob(["x"]);
    const result = await appendKnowhowCommit("nb-1", "t1", file);
    assert.match(calls[0].url, /\/notebooks\/nb-1\/knowhow\/t1\/append$/);
    assert.strictEqual(calls[0].init.body.get("mode"), "commit");
    assert.deepStrictEqual(result, { added: 7 });
  });
});

// --- optimizeKnowhowCell -----------------------------------------------------------

test("optimizeKnowhowCell: POST 到 .../optimize/stream，响应 suggestion_md 映射为 suggestionMd", () => {
  return withFetchStub({ suggestion_md: "优化后的正文" }, async (calls) => {
    const result = await optimizeKnowhowCell("nb-1", "t1", "r1", "c1");
    assert.match(calls[0].url, /\/notebooks\/nb-1\/knowhow\/t1\/rows\/r1\/cells\/c1\/optimize\/stream$/);
    assert.strictEqual(calls[0].init.method, "POST");
    assert.deepStrictEqual(result, { suggestionMd: "优化后的正文" });
  });
});

test("optimizeKnowhowCell: 可选 AbortSignal 透传到共享请求客户端", () => {
  return withFetchStub({ suggestion_md: "优化后的正文" }, async (calls) => {
    const controller = new AbortController();
    await optimizeKnowhowCell("nb-1", "t1", "r1", "c1", controller.signal);
    assert.strictEqual(calls[0].init.signal, controller.signal);
  });
});

test("completeKnowhowRow: POST 目标空列并把建议 wire 映射为 camelCase", () => {
  const wire = {
    retrieval_mode: "reasoning",
    retrieval_scope: "active_and_mounted",
    retrieval_status: "succeeded",
    reasoning_trace: [{ step_type: "retrieve", summary: "检索资料", detail: { hits: 2 }, duration_ms: 12 }],
    evidence: [{
      key: "k1", kind: "knowledge", object_type: "Claim", label: "供电规则",
      excerpt_md: "输入电压应稳定。", source_title: "设计规范", location_label: "第 4 节", tier: "base",
    }],
    suggestions: [{
      column_id: "c-fix",
      suggestion_md: "检查供电",
      confidence: "high",
      based_on_row_ids: ["r2"],
      evidence_keys: ["k1"],
      basis: "现象相近",
      abstain_reason: "",
    }],
  };
  return withFetchStub(wire, async (calls) => {
    const result = await completeKnowhowRow("nb-1", "t1", "r1", ["c-fix", "c-tool"]);
    assert.match(calls[0].url, /\/notebooks\/nb-1\/knowhow\/t1\/rows\/r1\/complete\/stream$/);
    assert.strictEqual(calls[0].init.method, "POST");
    assert.deepStrictEqual(bodyOf(calls[0]), { target_column_ids: ["c-fix", "c-tool"] });
    assert.deepStrictEqual(result, {
      retrievalMode: "reasoning",
      retrievalScope: "active_and_mounted",
      retrievalStatus: "succeeded",
      reasoningTrace: [{ step_type: "retrieve", summary: "检索资料", detail: { hits: 2 }, duration_ms: 12 }],
      evidence: [{
        key: "k1", kind: "knowledge", objectType: "Claim", label: "供电规则",
        excerptMd: "输入电压应稳定。", sourceTitle: "设计规范", locationLabel: "第 4 节", tier: "base",
      }],
      suggestions: [{
        columnId: "c-fix",
        suggestionMd: "检查供电",
        confidence: "high",
        basedOnRowIds: ["r2"],
        evidenceKeys: ["k1"],
        basis: "现象相近",
        abstainReason: "",
      }],
    });
  });
});

test("completeKnowhowRow: 未指定目标列时发送空对象，不伪造 null 字段", () =>
  withFetchStub({
    retrieval_mode: "reasoning", retrieval_scope: "active_and_mounted",
    retrieval_status: "no_evidence", reasoning_trace: [], evidence: [], suggestions: [],
  }, async (calls) => {
    await completeKnowhowRow("nb-1", "t1", "r1");
    assert.deepStrictEqual(bodyOf(calls[0]), {});
  }));

test("completeKnowhowRow: 可选 AbortSignal 透传到共享请求客户端", () =>
  withFetchStub({
    retrieval_mode: "reasoning", retrieval_scope: "active_and_mounted",
    retrieval_status: "no_evidence", reasoning_trace: [], evidence: [], suggestions: [],
  }, async (calls) => {
    const controller = new AbortController();
    await completeKnowhowRow("nb-1", "t1", "r1", undefined, controller.signal);
    assert.strictEqual(calls[0].init.signal, controller.signal);
  }));

// --- getCellCode / putCellCode / deleteCellCode（三态覆盖）------------------------

test("getCellCode: GET .../rows/{row}/cells/{col}/code，status=implemented 时正常映射", () => {
  const wireCode = { code_text: "print(1)", language: "python", status: "implemented", updated_at: "2026-07-15T00:00:00Z", updated_by: "a00123456" };
  return withFetchStub(wireCode, async (calls) => {
    const code = await getCellCode("r1", "c1");
    assert.match(calls[0].url, /\/agent\/knowhow\/rows\/r1\/cells\/c1\/code$/);
    assert.deepStrictEqual(code, {
      codeText: "print(1)",
      language: "python",
      status: "implemented",
      updatedAt: "2026-07-15T00:00:00Z",
      updatedBy: "a00123456",
    });
  });
});

test("getCellCode: status=stale 时同样正常映射（知识已更新待重审）", () => {
  const wireCode = { code_text: "old code", language: "tcl", status: "stale", updated_at: "2026-07-01T00:00:00Z" };
  return withFetchStub(wireCode, async () => {
    const code = await getCellCode("r1", "c1");
    assert.strictEqual(code.status, "stale");
  });
});

test("getCellCode: status=none 时无附件，codeText/language 兜底为空串", () => {
  const wireCode = { code_text: null, language: null, status: "none", updated_at: null };
  return withFetchStub(wireCode, async () => {
    const code = await getCellCode("r1", "c1");
    assert.deepStrictEqual(code, { codeText: "", language: "", status: "none", updatedAt: null, updatedBy: null });
  });
});

test("putCellCode: PUT 请求体为 {code_text,language}，响应映射同 getCellCode", () => {
  const wireCode = { code_text: "print(2)", language: "python", status: "implemented", updated_at: "2026-07-15T01:00:00Z", updated_by: "a00123456" };
  return withFetchStub(wireCode, async (calls) => {
    const code = await putCellCode("r1", "c1", "print(2)", "python");
    assert.match(calls[0].url, /\/agent\/knowhow\/rows\/r1\/cells\/c1\/code$/);
    assert.strictEqual(calls[0].init.method, "PUT");
    assert.deepStrictEqual(bodyOf(calls[0]), { code_text: "print(2)", language: "python" });
    assert.strictEqual(code.status, "implemented");
    assert.strictEqual(code.updatedBy, "a00123456");
  });
});

test("deleteCellCode: DELETE .../rows/{row}/cells/{col}/code", () => {
  return withFetchStub(null, async (calls) => {
    await deleteCellCode("r1", "c1");
    assert.match(calls[0].url, /\/agent\/knowhow\/rows\/r1\/cells\/c1\/code$/);
    assert.strictEqual(calls[0].init.method, "DELETE");
  });
});

// --- fetchKnowhowRowCodeByColumn（Task 11 抽屉；GET 行详情只取 code[]，按
// column_id 建索引——一次请求换全行代码状态，见 knowhow-model.ts 头注释）------

test("fetchKnowhowRowCodeByColumn: GET .../agent/knowhow/rows/{row}（无 /code 后缀），按 column_id 建索引", () => {
  const wireRowDetail = {
    title: "过冲问题",
    cells: [{ column_id: "c1", column_name: "修复方法", kind: "procedure", text: "增大电容" }],
    code: [
      { column_id: "c1", language: "python", code_text: "print(1)", status: "implemented", updated_at: "2026-07-15T00:00:00Z", updated_by: "u1" },
      { column_id: "c2", language: "tcl", code_text: "set x 1", status: "stale", updated_at: "2026-07-01T00:00:00Z", updated_by: "agent-1" },
    ],
  };
  return withFetchStub(wireRowDetail, async (calls) => {
    const map = await fetchKnowhowRowCodeByColumn("r1");
    assert.match(calls[0].url, /\/agent\/knowhow\/rows\/r1$/);
    assert.ok(!calls[0].url.includes("/cells/"), "不应带 cells 段——这是行级端点，不是单格端点");
    assert.deepStrictEqual(map, {
      c1: { codeText: "print(1)", language: "python", status: "implemented", updatedAt: "2026-07-15T00:00:00Z", updatedBy: "u1" },
      c2: { codeText: "set x 1", language: "tcl", status: "stale", updatedAt: "2026-07-01T00:00:00Z", updatedBy: "agent-1" },
    });
  });
});

test("fetchKnowhowRowCodeByColumn: code 为空数组时返回空 map（这一行没有任何代码附件）", () => {
  return withFetchStub({ title: "t", cells: [], code: [] }, async () => {
    const map = await fetchKnowhowRowCodeByColumn("r1");
    assert.deepStrictEqual(map, {});
  });
});

test("fetchKnowhowRowCodeByColumn: code 字段整个缺失时也返回空 map（不抛异常）", () => {
  return withFetchStub({ title: "t", cells: [] }, async () => {
    const map = await fetchKnowhowRowCodeByColumn("r1");
    assert.deepStrictEqual(map, {});
  });
});

// --- uploadNotebookAsset（Task 7；补 Task 4 起草本文件时遗漏的 asset 上传
// fetcher——端点是 PR-1 既有的 POST /notebooks/{nb}/assets）-----------------------

test("uploadNotebookAsset: POST multipart 到 /notebooks/{nb}/assets，响应 {id,url} 原样返回（无 snake_case 字段需转换）", () => {
  const wireAsset = { id: "a1", url: "/api/notebooks/nb-1/assets/a1" };
  return withFetchStub(wireAsset, async (calls) => {
    const file = new Blob(["binary-image-bytes"]);
    const asset = await uploadNotebookAsset("nb-1", file);
    assert.match(calls[0].url, /\/notebooks\/nb-1\/assets$/);
    assert.strictEqual(calls[0].init.method, "POST");
    const form = calls[0].init.body;
    assert.ok(form instanceof FormData);
    assert.ok(form.get("file") instanceof Blob, "file 字段应作为二进制内容附着");
    assert.deepStrictEqual(asset, wireAsset);
  });
});

// --- mapCitationKnowhowRef ---------------------------------------------------------

test("mapCitationKnowhowRef: 命中 knowhow 的引用映射 table_id/row_id 为 camelCase", () => {
  assert.deepStrictEqual(mapCitationKnowhowRef({ table_id: "t1", row_id: "r1" }), { tableId: "t1", rowId: "r1" });
});

test("mapCitationKnowhowRef: null 原样返回 null（非 knowhow 引用）", () => {
  assert.strictEqual(mapCitationKnowhowRef(null), null);
});

test("mapCitationKnowhowRef: undefined 归一为 null", () => {
  assert.strictEqual(mapCitationKnowhowRef(undefined), null);
});

// --- rewriteAssetUrls ---------------------------------------------------------

test("rewriteAssetUrls: 单张 asset:// 图片改写为鉴权 API URL", () => {
  const md = "![截图](asset://a1)";
  const out = rewriteAssetUrls(md, "nb-1", "http://api.test/api");
  assert.strictEqual(out, "![截图](http://api.test/api/notebooks/nb-1/assets/a1)");
});

test("rewriteAssetUrls: 多张图片全部改写，非图片文本不受影响", () => {
  const md = "步骤一：\n![图1](asset://a1)\n步骤二：\n![图2](asset://a2)\n完成。";
  const out = rewriteAssetUrls(md, "nb-1", "http://api.test/api");
  assert.strictEqual(
    out,
    "步骤一：\n![图1](http://api.test/api/notebooks/nb-1/assets/a1)\n步骤二：\n![图2](http://api.test/api/notebooks/nb-1/assets/a2)\n完成。",
  );
});

test("rewriteAssetUrls: 无图片时原样返回", () => {
  const md = "这一格只有纯文本说明，没有任何图片引用。";
  assert.strictEqual(rewriteAssetUrls(md, "nb-1", "http://api.test/api"), md);
});

test("rewriteAssetUrls: 非 asset 协议的图片/链接不动", () => {
  const md = "![外链图片](https://example.com/pic.png) 与 [普通链接](https://example.com) 都不应被改写。";
  assert.strictEqual(rewriteAssetUrls(md, "nb-1", "http://api.test/api"), md);
});

test("rewriteAssetUrls: 合法 id(含连字符/下划线)改写", () => {
  const md = "![截图](asset://asset-9f3a_bc-01)";
  const out = rewriteAssetUrls(md, "nb-1", "http://api.test/api");
  assert.strictEqual(out, "![截图](http://api.test/api/notebooks/nb-1/assets/asset-9f3a_bc-01)");
});

test("rewriteAssetUrls: 路径穿越/含斜杠或点的 id 不改写(纵深防御)", () => {
  for (const md of [
    "![x](asset://../../etc/passwd)",
    "![x](asset://a1/b2)",
    "![x](asset://a.b)",
    "![x](asset://%2e%2e/secret)",
  ]) {
    assert.strictEqual(rewriteAssetUrls(md, "nb-1", "http://api.test/api"), md, md);
  }
});

test("rewriteAssetUrls: 空字符串原样返回", () => {
  assert.strictEqual(rewriteAssetUrls("", "nb-1", "http://api.test/api"), "");
});

// --- cellSummary ---------------------------------------------------------------

test("cellSummary: 图片剥离为图示占位文案（保留 alt 线索）", () => {
  const md = "步骤如下：\n![示意图](asset://x1)\n请参考上图完成操作。";
  const out = cellSummary(md);
  assert.ok(!out.includes("!["), "不应残留 markdown 图片语法");
  assert.ok(out.includes("（图示：示意图）"), "应替换为图示占位文案");
});

test("cellSummary: 无 alt 文本的图片使用无冒号占位", () => {
  assert.strictEqual(cellSummary("![](asset://x1)"), "（图示）");
});

test("cellSummary: 多张图片各自剥离", () => {
  const out = cellSummary("![图A](asset://a)中间文字![图B](asset://b)");
  assert.strictEqual(out, "（图示：图A）中间文字（图示：图B）");
});

test("cellSummary: 去除常见 md 记号（加粗/列表项）", () => {
  const out = cellSummary("**加粗文字**\n- 列表项一\n- 列表项二");
  assert.ok(!out.includes("**"), "不应残留加粗记号");
  assert.ok(!out.includes("- "), "不应残留列表符号");
  assert.strictEqual(out, "加粗文字 列表项一 列表项二");
});

test("cellSummary: 去除标题/行内代码记号", () => {
  const out = cellSummary("### 标题\n请运行 `foo --bar` 命令");
  assert.strictEqual(out, "标题 请运行 foo --bar 命令");
});

test("cellSummary: 超过默认 maxLen(80) 时截断并加省略号", () => {
  const longText = "这是一段很长的修复方法说明，".repeat(10); // 140 字符，无空白
  const out = cellSummary(longText);
  assert.ok(out.length <= 80, `长度应 <=80，实际 ${out.length}`);
  assert.ok(out.endsWith("…"), "截断后应以省略号结尾");
  assert.notStrictEqual(out, longText);
});

test("cellSummary: 恰好等于 maxLen 时不截断、不加省略号", () => {
  const exact = "字".repeat(80);
  const out = cellSummary(exact);
  assert.strictEqual(out, exact);
  assert.ok(!out.includes("…"));
});

test("cellSummary: 自定义 maxLen 参数生效", () => {
  const out = cellSummary("一二三四五六七八九十", 5);
  assert.ok(out.length <= 5, `长度应 <=5，实际 ${out.length}`);
  assert.ok(out.endsWith("…"));
});

test("cellSummary: 空格子（空串/纯空白）返回空串", () => {
  assert.strictEqual(cellSummary(""), "");
  assert.strictEqual(cellSummary("   \n   "), "");
});

// --- cellSummary: 下划线标识符（EDA/silicon 场景，innovus 命令/信号名）不应被误伤 ---

test("cellSummary: 下划线标识符原样保留，不被当作斜体剥离", () => {
  assert.strictEqual(cellSummary("用 place_opt_design 修复"), "用 place_opt_design 修复");
});

test("cellSummary: 反引号包裹的下划线标识符去反引号但保留下划线", () => {
  assert.strictEqual(cellSummary("先跑 `place_opt_design` 再查"), "先跑 place_opt_design 再查");
});

test("cellSummary: 句中的下划线标识符存活", () => {
  assert.strictEqual(cellSummary("信号 clk_out_en 需要检查"), "信号 clk_out_en 需要检查");
});

// --- cellSummary: 星号斜体守卫不应误伤空格相邻的乘号场景 ---

test("cellSummary: 空格相邻的星号（乘号）不被当作斜体剥离", () => {
  assert.strictEqual(cellSummary("2 * 3 * 4"), "2 * 3 * 4");
});

test("cellSummary: 真斜体（无内部首尾空格）仍被剥离", () => {
  assert.strictEqual(cellSummary("*强调*"), "强调");
});

// --- importKnowhow 的后端 wire 契约 ------------------------------------------
// 真机 bug（本次修复）：importKnowhow 的 FormData 漏了 anchor_index，且
// columns_json 用的是前端自造的 role 字段。后端 _columns_with_anchor 只读
// column["kind"]（缺失时静默默认 'attribute'，不报错），anchor 只认独立的
// anchor_index（VALID_KINDS 明确排除 'anchor'，不接受内联）。后果：用户在
// 向导里选的行标题列 + 每列内容类型全部被静默丢弃，整张表落库成无 anchor 的
// 记录型表 → forward-fill 跳过 → anchor 分组显示完全不生效。
// 此前 importKnowhow 没有任何测试，前端单测只断言前端自己那套 role 形状，
// 于是前后端测试都全绿、真机却从未工作。这两个用例锁住真实的 wire。

test("importKnowhowPreview: multipart 携带用户选择的属性排列方式", () => {
  return withFetchStub(
    {
      columns: [],
      rows_preview: [],
      total_rows: 0,
      anchor_suggestion: null,
    },
    async (calls) => {
      await importKnowhowPreview(
        "nb-1",
        new Blob(["x"]),
        "rows",
      );
      const form = calls[0].init.body;
      assert.ok(form instanceof FormData);
      assert.strictEqual(form.get("orientation"), "rows");
    },
  );
});

test("importKnowhow: columns_json 用 kind 字段 + anchor_index 独立传（后端 wire 契约）", () => {
  return withFetchStub(wireTable({ columns: [] }), async (calls) => {
    await importKnowhow(
      "nb-1",
      new Blob(["x"]),
      "我的表",
      [
        { name: "违例概念", kind: "attribute" },
        { name: "现象识别方法", kind: "procedure" },
      ],
      0,
      "rows",
    );
    assert.match(calls[0].url, /\/notebooks\/nb-1\/knowhow\/import$/);
    assert.strictEqual(calls[0].init.method, "POST");
    const form = calls[0].init.body;
    assert.ok(form instanceof FormData);
    assert.strictEqual(form.get("title"), "我的表");
    assert.strictEqual(form.get("orientation"), "rows");
    // anchor 只能经 anchor_index 传达——写进 columns_json 的 role 后端不读。
    assert.strictEqual(form.get("anchor_index"), "0");
    // 字段名必须是 kind：写成 role 会让后端静默把每列都当成 attribute。
    assert.deepStrictEqual(JSON.parse(form.get("columns_json")), [
      { name: "违例概念", kind: "attribute" },
      { name: "现象识别方法", kind: "procedure" },
    ]);
  });
});

test("importKnowhow: anchorIndex=null（记录型表）→ 不发 anchor_index，走后端 Form(None) 默认", () => {
  return withFetchStub(wireTable({ columns: [] }), async (calls) => {
    await importKnowhow(
      "nb-1",
      new Blob(["x"]),
      "T",
      [{ name: "日期", kind: "attribute" }],
      null,
      "columns",
    );
    assert.strictEqual(calls[0].init.body.get("anchor_index"), null);
    assert.strictEqual(calls[0].init.body.get("orientation"), "columns");
  });
});

// --- importKnowhowPreview 的 anchor_index wire（P2 修复：向导改行标题列后重取预览）--
// 预览按后端选的锚定列跳过规整；向导初始加载不知道用户会选哪列（猜测列由响应带回），
// 故初始不发 anchor_index（后端退回按 guess 规整）；用户在步骤②改选某列后重取时带上
// 它，后端据此按**所选**列规整，保住「预览即所得」。
// 三态编码（评审残留修复）：anchor 选择有三种状态，「省略参数」只能表达其一——
//   undefined（初始加载，未碰选择器）→ 省略（后端按猜测列跳过，与界面预选一致）；
//   number（选了某列）→ 发该列下标；
//   null（明确清空「不设行标题」）→ 发 -1（后端解读为「无锚定列」→ 不跳过任何列，
//   与 commit 不带 anchor_index 时全列规整对齐）。若 null 也省略，预览会按猜测列
//   跳过而提交却全列规整——预览即所得在猜测列上撒谎。

const WIRE_PREVIEW = { columns: [], rows_preview: [], total_rows: 0, anchor_suggestion: null };

test("importKnowhowPreview: 初始加载（不传 anchorIndex）→ POST /import/preview 不带 anchor_index（后端退回按猜测列规整）", () => {
  return withFetchStub(WIRE_PREVIEW, async (calls) => {
    await importKnowhowPreview("nb-1", new Blob(["x"]));
    assert.match(calls[0].url, /\/notebooks\/nb-1\/knowhow\/import\/preview$/);
    assert.strictEqual(calls[0].init.method, "POST");
    const form = calls[0].init.body;
    assert.ok(form instanceof FormData);
    assert.ok(form.get("file"));
    assert.strictEqual(form.get("orientation"), "columns");
    assert.strictEqual(form.get("anchor_index"), null); // 未发送
  });
});

test("importKnowhowPreview: 用户选了某列（anchorIndex=1）→ FormData 带 anchor_index='1'（后端按所选列规整，预览即所得）", () => {
  return withFetchStub(WIRE_PREVIEW, async (calls) => {
    await importKnowhowPreview("nb-1", new Blob(["x"]), "columns", 1);
    const form = calls[0].init.body;
    assert.strictEqual(form.get("anchor_index"), "1");
  });
});

test("importKnowhowPreview: anchorIndex=0 也如实发送（不能被 falsy 判断吞掉）", () => {
  return withFetchStub(WIRE_PREVIEW, async (calls) => {
    await importKnowhowPreview("nb-1", new Blob(["x"]), "columns", 0);
    assert.strictEqual(calls[0].init.body.get("anchor_index"), "0");
  });
});

test("importKnowhowPreview: 清空行标题（anchorIndex=null）→ 发 anchor_index='-1'（明确无锚定，预览全列规整=commit 行为）", () => {
  return withFetchStub(WIRE_PREVIEW, async (calls) => {
    await importKnowhowPreview("nb-1", new Blob(["x"]), "columns", null);
    assert.strictEqual(calls[0].init.body.get("anchor_index"), "-1");
  });
});

// =============================================================================
// knowhow 表版本管理 Task 14：历史时间线 / 单条详情 / 两版对比 / 单格历史 /
// 回退 / 里程碑 / 清理——wire 契约测试。
//
// 这些端点由 Task 12 落地在 backend/app/api/knowhow_routes.py，四个读端点没有
// 独立 response_model（路由把 store 的 dict 原样返回），字段名逐一对照
// backend/app/repositories/sqlite/knowhow_history_store.py /
// backend/app/services/knowhow/history.py 的真实返回值写就，不是本任务简报
// 定稿前的猜测（差异见任务报告）。每个测试都断言 URL 不含双 /api——PR#207 的
// 教训是后端 TestClient 直打不经前端拼接，看不出这类问题，只有这层契约测试
// 才是真正的防线。
// =============================================================================

// --- fetchKnowhowHistory -----------------------------------------------------

test("fetchKnowhowHistory: GET .../history（无可选参数时 URL 不带查询串），响应字段名映射", () => {
  const wire = {
    head_seq: 53,
    changes: [
      {
        id: "khchg-53", table_id: "t1", seq: 53, kind: "cell_update", actor: "user-1",
        origin: "user", fingerprint: "fp-53", note: "",
        created_at: "2026-07-22T14:30:00",
        payload: { cells: [{ row_id: "r1", column_id: "c1", before: "A", after: "B" }] },
      },
    ],
    milestones: [
      {
        id: "khms-1", table_id: "t1", seq: 40, name: "v1.0", note: "上线里程碑",
        created_by: "user-1", created_at: "2026-07-20T00:00:00", stale: false,
      },
    ],
  };
  return withFetchStub(wire, async (calls) => {
    const page = await fetchKnowhowHistory("nb-1", "t1");
    assert.match(calls[0].url, /\/notebooks\/nb-1\/knowhow\/t1\/history$/);
    assert.doesNotMatch(calls[0].url, /\/api\/api\//, "双 /api 会 404（PR#207）");
    assert.deepStrictEqual(page, {
      headSeq: 53,
      changes: [
        {
          id: "khchg-53", tableId: "t1", seq: 53, kind: "cell_update", actor: "user-1",
          origin: "user", fingerprint: "fp-53", note: "",
          createdAt: "2026-07-22T14:30:00",
          payload: { cells: [{ row_id: "r1", column_id: "c1", before: "A", after: "B" }] },
        },
      ],
      milestones: [
        {
          id: "khms-1", tableId: "t1", seq: 40, name: "v1.0", note: "上线里程碑",
          createdBy: "user-1", createdAt: "2026-07-20T00:00:00", stale: false,
        },
      ],
    });
  });
});

test("fetchKnowhowHistory: beforeSeq/limit/milestonesOnly 三个可选参数各自映射为 before_seq/limit/milestones_only", () => {
  const wire = { head_seq: 10, changes: [], milestones: [] };
  return withFetchStub(wire, async (calls) => {
    await fetchKnowhowHistory("nb-1", "t1", 42, 20, true);
    assert.match(calls[0].url, /\/notebooks\/nb-1\/knowhow\/t1\/history\?/);
    const url = new URL(calls[0].url);
    assert.strictEqual(url.searchParams.get("before_seq"), "42");
    assert.strictEqual(url.searchParams.get("limit"), "20");
    assert.strictEqual(url.searchParams.get("milestones_only"), "true");
  });
});

test("fetchKnowhowHistory: 里程碑数组为空、changes 为空时也能正常映射（新建表首次拉取）", () => {
  return withFetchStub({ head_seq: 0, changes: [], milestones: [] }, async () => {
    const page = await fetchKnowhowHistory("nb-1", "t1");
    assert.deepStrictEqual(page, { headSeq: 0, changes: [], milestones: [] });
  });
});

// --- fetchKnowhowChange -------------------------------------------------------

test("fetchKnowhowChange: GET .../history/{seq}，单条变更详情字段名映射", () => {
  const wire = {
    id: "khchg-12", table_id: "t1", seq: 12, kind: "column_rename", actor: "user-2",
    origin: "user", fingerprint: "fp-12", note: "",
    created_at: "2026-07-15T10:00:00",
    payload: { column_id: "c1", before: "旧名", after: "新名" },
  };
  return withFetchStub(wire, async (calls) => {
    const change = await fetchKnowhowChange("nb-1", "t1", 12);
    assert.match(calls[0].url, /\/notebooks\/nb-1\/knowhow\/t1\/history\/12$/);
    assert.doesNotMatch(calls[0].url, /\/api\/api\//, "双 /api 会 404（PR#207）");
    assert.deepStrictEqual(change, {
      id: "khchg-12", tableId: "t1", seq: 12, kind: "column_rename", actor: "user-2",
      origin: "user", fingerprint: "fp-12", note: "",
      createdAt: "2026-07-15T10:00:00",
      payload: { column_id: "c1", before: "旧名", after: "新名" },
    });
  });
});

// --- fetchKnowhowHistoryDiff ---------------------------------------------------

test("fetchKnowhowHistoryDiff: GET .../history/diff?from=&to=，五键响应全部字段名映射", () => {
  const wire = {
    cells: [{ row_id: "r1", column_id: "c1", before: "A", after: "C" }],
    rows_added: [
      {
        row_id: "r9", position: 3, created_at: "2026-07-21T00:00:00",
        cells: { c1: "新行内容" },
        code: [{ column_id: "c1", code_text: "print(1)", language: "python", cell_content_hash: "h1", updated_by: "user-1" }],
      },
    ],
    rows_removed: [],
    columns: [{ column_id: "c2", before: { name: "旧列名" }, after: { name: "新列名" } }],
    table_meta: { before: { title: "旧标题", description: "" }, after: { title: "新标题", description: "" } },
  };
  return withFetchStub(wire, async (calls) => {
    const diff = await fetchKnowhowHistoryDiff("nb-1", "t1", 10, 20);
    assert.match(calls[0].url, /\/notebooks\/nb-1\/knowhow\/t1\/history\/diff\?from=10&to=20$/);
    assert.doesNotMatch(calls[0].url, /\/api\/api\//, "双 /api 会 404（PR#207）");
    assert.deepStrictEqual(diff, {
      cells: [{ rowId: "r1", columnId: "c1", before: "A", after: "C" }],
      rowsAdded: [
        {
          rowId: "r9", position: 3, createdAt: "2026-07-21T00:00:00",
          cells: { c1: "新行内容" },
          code: [{ columnId: "c1", codeText: "print(1)", language: "python", cellContentHash: "h1", updatedBy: "user-1" }],
        },
      ],
      rowsRemoved: [],
      columns: [{ columnId: "c2", before: { name: "旧列名" }, after: { name: "新列名" } }],
      tableMeta: { before: { title: "旧标题", description: "" }, after: { title: "新标题", description: "" } },
    });
  });
});

test("fetchKnowhowHistoryDiff: table_meta 为 null（区间内表元无净变化）时原样映射为 null", () => {
  const wire = { cells: [], rows_added: [], rows_removed: [], columns: [], table_meta: null };
  return withFetchStub(wire, async () => {
    const diff = await fetchKnowhowHistoryDiff("nb-1", "t1", 10, 20);
    assert.strictEqual(diff.tableMeta, null);
  });
});

// --- fetchKnowhowCellHistory ---------------------------------------------------

test("fetchKnowhowCellHistory: GET .../rows/{row}/cells/{col}/history，数组每项字段名映射", () => {
  const wire = [
    { seq: 8, actor: "user-1", origin: "llm_reformat", created_at: "2026-07-18T00:00:00", before: "旧值", after: "新值" },
    { seq: 3, actor: "user-1", origin: "user", created_at: "2026-07-10T00:00:00", before: null, after: "旧值" },
  ];
  return withFetchStub(wire, async (calls) => {
    const entries = await fetchKnowhowCellHistory("nb-1", "t1", "r1", "c1");
    assert.match(calls[0].url, /\/notebooks\/nb-1\/knowhow\/t1\/rows\/r1\/cells\/c1\/history$/);
    assert.doesNotMatch(calls[0].url, /\/api\/api\//, "双 /api 会 404（PR#207）");
    assert.deepStrictEqual(entries, [
      { seq: 8, actor: "user-1", origin: "llm_reformat", createdAt: "2026-07-18T00:00:00", before: "旧值", after: "新值" },
      { seq: 3, actor: "user-1", origin: "user", createdAt: "2026-07-10T00:00:00", before: null, after: "旧值" },
    ]);
  });
});

test("fetchKnowhowCellHistory: 提供 limit 时 URL 带 ?limit=", () => {
  return withFetchStub([], async (calls) => {
    await fetchKnowhowCellHistory("nb-1", "t1", "r1", "c1", 5);
    assert.match(calls[0].url, /\/history\?limit=5$/);
  });
});

// --- revertKnowhowTable --------------------------------------------------------
// 请求体字段名 target_seq / expected_head_seq 锁定后端 KnowhowRevertRequest
// 契约（app/models/knowhow.py）；响应字段名 seq / target_seq 锁定
// KnowhowHistoryStore.revert_to 的真实返回形状。

test("revertKnowhowTable: POST 到 /revert，请求体锁定字段名", () => {
  return withFetchStub({ seq: 54, target_seq: 12 }, async (calls) => {
    const result = await revertKnowhowTable("nb-1", "t1", 12, 53);
    assert.match(calls[0].url, /\/notebooks\/nb-1\/knowhow\/t1\/revert$/);
    assert.doesNotMatch(calls[0].url, /\/api\/api\//, "双 /api 会 404（PR#207）");
    assert.strictEqual(calls[0].init.method, "POST");
    assert.deepStrictEqual(bodyOf(calls[0]), { target_seq: 12, expected_head_seq: 53 });
    assert.deepStrictEqual(result, { seq: 54, targetSeq: 12 });
  });
});

test("revertKnowhowTable: 回退到当前 head 的 no-op 分支——响应 seq 就是 head 本身，targetSeq 原样回声", () => {
  return withFetchStub({ seq: 53, target_seq: 53 }, async (calls) => {
    const result = await revertKnowhowTable("nb-1", "t1", 53, 53);
    assert.deepStrictEqual(bodyOf(calls[0]), { target_seq: 53, expected_head_seq: 53 });
    assert.deepStrictEqual(result, { seq: 53, targetSeq: 53 });
  });
});

// revertKnowhowTable 的三种失败（409 head 陈旧 / 400 指纹不一致 / 500 后置校验
// 失败）后端返回结构化 detail={"code","message"}（见 knowhow_routes.py
// revert_knowhow_table），不是 user_error() 打过 X-User-Message 头的纯字符串。
// 共享错误层 errors.ts 的可展示通道（pickUserDetail）只认字符串/字符串数组形
// 状的 detail，对象形状会被判定为"抠不出来"而返回空串——即使能抠出来，这条
// 路径也没有 X-User-Message 头，闸1（信任）本来就不会放行，单靠共享层三种错误
// 都会被 throwHumanizedHttpError 兜底成按状态码泛化的通用中文，后端为这三种
// 场景各写的具体中文（"这张表刚被其他人改过，请刷新后重试" 等）永远不会穿透
// 到界面。评审问题 1 的修复：revertKnowhowTable 内部先用
// parseKnowhowRevertFailure 探测这三种结构化失败，命中就抛 KnowhowRevertError
// 把后端原文（.message）与机器码（.code）都带出来；未命中（如目标 seq 不存
// 在的 404，或未来新增而前端还不认识的 code）落回既有 throwHumanizedHttpError
// ——同 knowhow-transfer.ts parseCleanupFailure/KnowhowSourceCleanupError 的
// 既有先例。下面三条各验证一个 code，最后一条验证未知 code 时仍正确落回既有
// 兜底（不会被强行误判命中）。
test("revertKnowhowTable: 409 knowhow_history_stale——抛 KnowhowRevertError，带出后端原文与 code", () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({
        detail: { code: "knowhow_history_stale", message: "这张表刚被其他人改过，请刷新后重试" },
      }),
      { status: 409, headers: { "Content-Type": "application/json" } }, // 无 X-User-Message 头
    );
  return revertKnowhowTable("nb-1", "t1", 12, 50)
    .then(
      () => assert.fail("应当以 409 拒绝"),
      (error) => {
        assert.ok(error instanceof KnowhowRevertError);
        assert.strictEqual(error.code, "knowhow_history_stale");
        assert.strictEqual(error.message, "这张表刚被其他人改过，请刷新后重试");
      },
    )
    .finally(() => {
      globalThis.fetch = original;
    });
});

test("revertKnowhowTable: 400 knowhow_history_inconsistent——抛 KnowhowRevertError，带出后端原文与 code", () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({
        detail: {
          code: "knowhow_history_inconsistent",
          message: "表的当前内容与变更历史对不上，回退已中止",
        },
      }),
      { status: 400, headers: { "Content-Type": "application/json" } },
    );
  return revertKnowhowTable("nb-1", "t1", 12, 50)
    .then(
      () => assert.fail("应当以 400 拒绝"),
      (error) => {
        assert.ok(error instanceof KnowhowRevertError);
        assert.strictEqual(error.code, "knowhow_history_inconsistent");
        assert.strictEqual(error.message, "表的当前内容与变更历史对不上，回退已中止");
      },
    )
    .finally(() => {
      globalThis.fetch = original;
    });
});

test("revertKnowhowTable: 500 knowhow_revert_verify_failed——抛 KnowhowRevertError，带出后端原文与 code", () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({
        detail: {
          code: "knowhow_revert_verify_failed",
          message: "回退结果校验失败，已放弃本次回退，表未被改动",
        },
      }),
      { status: 500, headers: { "Content-Type": "application/json" } },
    );
  return revertKnowhowTable("nb-1", "t1", 12, 50)
    .then(
      () => assert.fail("应当以 500 拒绝"),
      (error) => {
        assert.ok(error instanceof KnowhowRevertError);
        assert.strictEqual(error.code, "knowhow_revert_verify_failed");
        assert.strictEqual(error.message, "回退结果校验失败，已放弃本次回退，表未被改动");
      },
    )
    .finally(() => {
      globalThis.fetch = original;
    });
});

test("revertKnowhowTable: 未知 code 时落回既有兜底（不强行误判命中，仍走共享人话层）", () => {
  const original = globalThis.fetch;
  globalThis.fetch = async () =>
    new Response(
      JSON.stringify({ detail: { code: "some_future_code", message: "以后可能新增的另一种失败" } }),
      { status: 409, headers: { "Content-Type": "application/json" } }, // 无 X-User-Message 头
    );
  return revertKnowhowTable("nb-1", "t1", 12, 50)
    .then(
      () => assert.fail("应当以 409 拒绝"),
      (error) => {
        assert.ok(!(error instanceof KnowhowRevertError));
        // 落回既有共享人话层：状态码穿透可分流，但具体文案被泛化成通用中文
        // ——parseKnowhowRevertFailure 对不认识的 code 没有强行命中。
        assert.strictEqual(httpErrorStatus(error), 409);
        assert.strictEqual(error.message, "操作有冲突，请刷新后重试");
      },
    )
    .finally(() => {
      globalThis.fetch = original;
    });
});

// parseKnowhowRevertFailure 的一条防御性分支单测：code 本身认识（三者之一），
// 但状态码对不上后端 except 分支实际会用的那个值——理论不应发生（后端每个
// code 只从一处 raise，status 恒定），但要确认解析函数不会仅凭 code 已知就
// 误判命中，同 parseCleanupFailure 严格校验 status===409 的既有取向一致。
test("parseKnowhowRevertFailure: code 已知但 status 对不上时返回 null", () => {
  assert.strictEqual(
    parseKnowhowRevertFailure(400, {
      detail: { code: "knowhow_history_stale", message: "这张表刚被其他人改过，请刷新后重试" },
    }),
    null,
  );
});

// --- createKnowhowMilestone / deleteKnowhowMilestone --------------------------
// 请求体字段名 seq / name / note 锁定 KnowhowMilestoneCreate 契约。

test("createKnowhowMilestone: POST 到 .../milestones，请求体字段名锁定，响应映射（create 端点响应无 stale 键，兜底 false）", () => {
  const wire = {
    id: "khms-9", table_id: "t1", seq: 30, name: "v2.0", note: "发布前快照",
    created_by: "user-1", created_at: "2026-07-22T00:00:00",
  };
  return withFetchStub(wire, async (calls) => {
    const milestone = await createKnowhowMilestone("nb-1", "t1", 30, "v2.0", "发布前快照");
    assert.match(calls[0].url, /\/notebooks\/nb-1\/knowhow\/t1\/milestones$/);
    assert.doesNotMatch(calls[0].url, /\/api\/api\//, "双 /api 会 404（PR#207）");
    assert.strictEqual(calls[0].init.method, "POST");
    assert.deepStrictEqual(bodyOf(calls[0]), { seq: 30, name: "v2.0", note: "发布前快照" });
    assert.deepStrictEqual(milestone, {
      id: "khms-9", tableId: "t1", seq: 30, name: "v2.0", note: "发布前快照",
      createdBy: "user-1", createdAt: "2026-07-22T00:00:00", stale: false,
    });
  });
});

test("createKnowhowMilestone: note 省略时默认空串，仍然进请求体（后端字段非 Optional，省略与显式空串对后端等价）", () => {
  const wire = {
    id: "khms-10", table_id: "t1", seq: 31, name: "v2.1", note: "",
    created_by: "user-1", created_at: "2026-07-22T00:00:00",
  };
  return withFetchStub(wire, async (calls) => {
    await createKnowhowMilestone("nb-1", "t1", 31, "v2.1");
    assert.deepStrictEqual(bodyOf(calls[0]), { seq: 31, name: "v2.1", note: "" });
  });
});

test("deleteKnowhowMilestone: DELETE 到 .../milestones/{id}", () => {
  return withFetchStub(null, async (calls) => {
    await deleteKnowhowMilestone("nb-1", "t1", "khms-9");
    assert.match(calls[0].url, /\/notebooks\/nb-1\/knowhow\/t1\/milestones\/khms-9$/);
    assert.doesNotMatch(calls[0].url, /\/api\/api\//, "双 /api 会 404（PR#207）");
    assert.strictEqual(calls[0].init.method, "DELETE");
  });
});

// --- pruneKnowhowHistory --------------------------------------------------------
// 请求体字段名 before_days 锁定 KnowhowHistoryPruneRequest 契约；响应
// {removed:number} 已是合法 camelCase，无需映射。

test("pruneKnowhowHistory: POST 到 .../history/prune，请求体字段名锁定", () => {
  return withFetchStub({ removed: 17 }, async (calls) => {
    const result = await pruneKnowhowHistory("nb-1", "t1", 90);
    assert.match(calls[0].url, /\/notebooks\/nb-1\/knowhow\/t1\/history\/prune$/);
    assert.doesNotMatch(calls[0].url, /\/api\/api\//, "双 /api 会 404（PR#207）");
    assert.strictEqual(calls[0].init.method, "POST");
    assert.deepStrictEqual(bodyOf(calls[0]), { before_days: 90 });
    assert.deepStrictEqual(result, { removed: 17 });
  });
});

test("pruneKnowhowHistory: removed=0（没有比截止时间更老的流水）时如实返回", () => {
  return withFetchStub({ removed: 0 }, async () => {
    const result = await pruneKnowhowHistory("nb-1", "t1", 3650);
    assert.deepStrictEqual(result, { removed: 0 });
  });
});
