import test from "node:test";
import assert from "node:assert/strict";

import {
  rewriteAssetUrls,
  cellSummary,
  composeRowTitle,
  ROLE_LABELS,
  KIND_LABELS,
  fetchKnowhowTable,
  patchKnowhowTable,
  addKnowhowColumn,
  patchKnowhowColumn,
  deleteKnowhowColumn,
  addKnowhowRow,
  deleteKnowhowRow,
  patchKnowhowCell,
  createKnowhowTable,
  knowhowTemplateUrl,
  appendKnowhowPreview,
  appendKnowhowCommit,
  optimizeKnowhowCell,
  getCellCode,
  putCellCode,
  deleteCellCode,
  mapCitationKnowhowRef,
} from "./knowhow-model.ts";

// --- fetch stub helper（镜像 edge-review-queue.test.mjs 的 withFetchStub，
// 加了可参数化的响应体，供本文件校验 mapper 的 snake->camel 转换）------------

function withFetchStub(responseBody, run) {
  const calls = [];
  const original = globalThis.fetch;
  globalThis.fetch = async (url, init) => {
    calls.push({ url, init });
    return {
      ok: true,
      status: 200,
      json: async () => responseBody,
      text: async () => JSON.stringify(responseBody),
    };
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

test("composeRowTitle: 每段截断到 <=16 字并加省略号（省略号计入 16）", () => {
  const longCell = "一二三四五六七八九十一二三四五六七八九十"; // 20 字
  const out = composeRowTitle([longCell]);
  assert.strictEqual(out.length, 16);
  assert.ok(out.endsWith("…"));
  assert.strictEqual(out, "一二三四五六七八九十一二三四五…");
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

// --- createKnowhowTable（建表向导；wire lands with T3）------------------------------

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

test("optimizeKnowhowCell: POST 到 .../optimize，响应 suggestion_md 映射为 suggestionMd", () => {
  return withFetchStub({ suggestion_md: "优化后的正文" }, async (calls) => {
    const result = await optimizeKnowhowCell("nb-1", "t1", "r1", "c1");
    assert.match(calls[0].url, /\/notebooks\/nb-1\/knowhow\/t1\/rows\/r1\/cells\/c1\/optimize$/);
    assert.strictEqual(calls[0].init.method, "POST");
    assert.deepStrictEqual(result, { suggestionMd: "优化后的正文" });
  });
});

// --- getCellCode / putCellCode / deleteCellCode（三态覆盖）------------------------

test("getCellCode: GET .../rows/{row}/cells/{col}/code，status=implemented 时正常映射", () => {
  const wireCode = { code_text: "print(1)", language: "python", status: "implemented", updated_at: "2026-07-15T00:00:00Z" };
  return withFetchStub(wireCode, async (calls) => {
    const code = await getCellCode("r1", "c1");
    assert.match(calls[0].url, /\/agent\/knowhow\/rows\/r1\/cells\/c1\/code$/);
    assert.deepStrictEqual(code, {
      codeText: "print(1)",
      language: "python",
      status: "implemented",
      updatedAt: "2026-07-15T00:00:00Z",
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
    assert.deepStrictEqual(code, { codeText: "", language: "", status: "none", updatedAt: null });
  });
});

test("putCellCode: PUT 请求体为 {code_text,language}，响应映射同 getCellCode", () => {
  const wireCode = { code_text: "print(2)", language: "python", status: "implemented", updated_at: "2026-07-15T01:00:00Z" };
  return withFetchStub(wireCode, async (calls) => {
    const code = await putCellCode("r1", "c1", "print(2)", "python");
    assert.match(calls[0].url, /\/agent\/knowhow\/rows\/r1\/cells\/c1\/code$/);
    assert.strictEqual(calls[0].init.method, "PUT");
    assert.deepStrictEqual(bodyOf(calls[0]), { code_text: "print(2)", language: "python" });
    assert.strictEqual(code.status, "implemented");
  });
});

test("deleteCellCode: DELETE .../rows/{row}/cells/{col}/code", () => {
  return withFetchStub(null, async (calls) => {
    await deleteCellCode("r1", "c1");
    assert.match(calls[0].url, /\/agent\/knowhow\/rows\/r1\/cells\/c1\/code$/);
    assert.strictEqual(calls[0].init.method, "DELETE");
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
