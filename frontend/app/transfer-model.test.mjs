import test from "node:test";
import assert from "node:assert/strict";

import {
  confirmedOnly,
  destinationNotebooks,
  knowhowTransferBody,
  memoryTransferBody,
  parseCleanupFailure,
  singleSourceNotebookId,
  summarizeTransferResults,
} from "./transfer-model.ts";

test("destinationNotebooks: 排除源 + 排除只读", () => {
  const all = [
    { id: "n1", name: "A" },
    { id: "n2", name: "B", access: "reader" },
    { id: "n3", name: "C", access: "owner" },
  ];
  const out = destinationNotebooks(all, "n1");
  assert.deepEqual(out.map((n) => n.id), ["n3"]);
});

test("destinationNotebooks: 源不在候选里时不排除任何只读之外的库", () => {
  const all = [
    { id: "n2", name: "B", access: "reader" },
    { id: "n3", name: "C", access: "owner" },
    { id: "n4", name: "D" },
  ];
  const out = destinationNotebooks(all, "n1");
  assert.deepEqual(out.map((n) => n.id), ["n3", "n4"]);
});

test("knowhowTransferBody: 锁字段名 target_notebook_id/mode", () => {
  assert.deepEqual(knowhowTransferBody("nb-2", "move"), {
    target_notebook_id: "nb-2",
    mode: "move",
  });
});

test("knowhowTransferBody: copy 模式同样锁字段名", () => {
  assert.deepEqual(knowhowTransferBody("nb-3", "copy"), {
    target_notebook_id: "nb-3",
    mode: "copy",
  });
});

test("memoryTransferBody: 锁字段名 memory_ids/target_notebook_id/mode/extract_kg", () => {
  assert.deepEqual(memoryTransferBody(["m1", "m2"], "nb-2", "copy", false), {
    memory_ids: ["m1", "m2"],
    target_notebook_id: "nb-2",
    mode: "copy",
    extract_kg: false,
  });
});

test("memoryTransferBody: extract_kg=true 也原样透传（不是恒 truthy 兜底）", () => {
  assert.deepEqual(memoryTransferBody(["m1"], "nb-9", "move", true), {
    memory_ids: ["m1"],
    target_notebook_id: "nb-9",
    mode: "move",
    extract_kg: true,
  });
});

test("memoryTransferBody: 不别名调用方数组（防调用后原地变更污染已发请求体）", () => {
  const ids = ["m1", "m2"];
  const body = memoryTransferBody(ids, "nb-2", "copy", true);
  ids.push("m3");
  assert.deepEqual(body.memory_ids, ["m1", "m2"]);
});

// --- singleSourceNotebookId（AMENDMENT 1）-----------------------------------

test("singleSourceNotebookId: 空数组 → null", () => {
  assert.equal(singleSourceNotebookId([]), null);
});

test("singleSourceNotebookId: 单条 → 就是它的 notebook_id", () => {
  assert.equal(singleSourceNotebookId([{ notebook_id: "nb-1" }]), "nb-1");
});

test("singleSourceNotebookId: 多条同源 → 该 notebook_id", () => {
  const items = [{ notebook_id: "nb-1" }, { notebook_id: "nb-1" }, { notebook_id: "nb-1" }];
  assert.equal(singleSourceNotebookId(items), "nb-1");
});

test("singleSourceNotebookId: 跨源（哪怕只有一条不同）→ null", () => {
  const items = [{ notebook_id: "nb-1" }, { notebook_id: "nb-2" }, { notebook_id: "nb-1" }];
  assert.equal(singleSourceNotebookId(items), null);
});

test("singleSourceNotebookId: 不同源出现在中间也能识别（不是只比对首尾两条）", () => {
  const items = [
    { notebook_id: "nb-1" },
    { notebook_id: "nb-9" },
    { notebook_id: "nb-1" },
    { notebook_id: "nb-1" },
  ];
  assert.equal(singleSourceNotebookId(items), null);
});

// --- parseCleanupFailure（AMENDMENT 2）--------------------------------------

test("parseCleanupFailure: 命中 409 source_cleanup_failed → {newTableId, message}", () => {
  const body = {
    detail: {
      code: "source_cleanup_failed",
      new_table_id: "kt-99",
      message: "已复制到目标，但源表未删除；请手动删除源表，或先删掉多余副本再重试",
    },
  };
  assert.deepEqual(parseCleanupFailure(409, body), {
    newTableId: "kt-99",
    message: "已复制到目标，但源表未删除；请手动删除源表，或先删掉多余副本再重试",
  });
});

test("parseCleanupFailure: 非 409 状态码 → null（即使 body 形状对）", () => {
  const body = {
    detail: { code: "source_cleanup_failed", new_table_id: "kt-99", message: "x" },
  };
  assert.equal(parseCleanupFailure(500, body), null);
  assert.equal(parseCleanupFailure(404, body), null);
  assert.equal(parseCleanupFailure(200, body), null);
});

test("parseCleanupFailure: 409 但 code 不是 source_cleanup_failed → null", () => {
  assert.equal(
    parseCleanupFailure(409, { detail: { code: "other_error", new_table_id: "kt-99" } }),
    null
  );
});

test("parseCleanupFailure: 409 但 detail 是字符串（普通 HTTPException(detail=str)）→ null", () => {
  assert.equal(parseCleanupFailure(409, { detail: "Table not found" }), null);
});

test("parseCleanupFailure: body 缺失/非对象/null → null", () => {
  assert.equal(parseCleanupFailure(409, undefined), null);
  assert.equal(parseCleanupFailure(409, null), null);
  assert.equal(parseCleanupFailure(409, "plain text"), null);
  assert.equal(parseCleanupFailure(409, 42), null);
});

test("parseCleanupFailure: detail 缺失 → null", () => {
  assert.equal(parseCleanupFailure(409, {}), null);
});

test("parseCleanupFailure: new_table_id 缺失或非字符串 → null", () => {
  assert.equal(parseCleanupFailure(409, { detail: { code: "source_cleanup_failed" } }), null);
  assert.equal(
    parseCleanupFailure(409, {
      detail: { code: "source_cleanup_failed", new_table_id: 123 },
    }),
    null
  );
  assert.equal(
    parseCleanupFailure(409, { detail: { code: "source_cleanup_failed", new_table_id: "" } }),
    null
  );
});

test("parseCleanupFailure: message 缺失时给非空兜底文案", () => {
  const out = parseCleanupFailure(409, {
    detail: { code: "source_cleanup_failed", new_table_id: "kt-1" },
  });
  assert.equal(out.newTableId, "kt-1");
  assert.equal(typeof out.message, "string");
  assert.ok(out.message.length > 0);
});

// --- summarizeTransferResults（AMENDMENT 3）---------------------------------

test("summarizeTransferResults: 全部成功（copied + moved 混合）", () => {
  const results = [
    { source_id: "m1", new_id: "m1x", ok: true, error: null, status: "copied" },
    { source_id: "m2", new_id: "m2x", ok: true, error: null, status: "moved" },
  ];
  const summary = summarizeTransferResults(results);
  assert.equal(summary.total, 2);
  assert.equal(summary.succeeded, 2);
  assert.equal(summary.failed, 0);
  assert.deepEqual(summary.copiedSourceNotRemoved, []);
});

test("summarizeTransferResults: 混合结果（成功/普通失败/复制未删源）", () => {
  const results = [
    { source_id: "m1", new_id: "m1x", ok: true, error: null, status: "copied" },
    { source_id: "m2", new_id: null, ok: false, error: "not found", status: "failed" },
    {
      source_id: "m3",
      new_id: "m3x",
      ok: false,
      error: "复制已成功，但源未删除：disk full",
      status: "copied_source_not_removed",
    },
  ];
  const summary = summarizeTransferResults(results);
  assert.equal(summary.total, 3);
  assert.equal(summary.succeeded, 1);
  assert.equal(summary.failed, 2);
  assert.equal(summary.copiedSourceNotRemoved.length, 1);
  assert.equal(summary.copiedSourceNotRemoved[0].source_id, "m3");
  assert.equal(summary.copiedSourceNotRemoved[0].new_id, "m3x");
});

test("summarizeTransferResults: 全部是 copied_source_not_removed", () => {
  const results = [
    { source_id: "m1", new_id: "m1x", ok: false, error: "e1", status: "copied_source_not_removed" },
    { source_id: "m2", new_id: "m2x", ok: false, error: "e2", status: "copied_source_not_removed" },
  ];
  const summary = summarizeTransferResults(results);
  assert.equal(summary.total, 2);
  assert.equal(summary.succeeded, 0);
  assert.equal(summary.failed, 2);
  assert.equal(summary.copiedSourceNotRemoved.length, 2);
});

test("summarizeTransferResults: 空数组", () => {
  const summary = summarizeTransferResults([]);
  assert.deepEqual(summary, { total: 0, succeeded: 0, failed: 0, copiedSourceNotRemoved: [] });
});

// --- confirmedOnly（P2-B，round 6 评审）---------------------------------
// 批量传输选择走 checkbox，不像单条卡片操作区那样天生只在 status==="confirmed"
// 的卡片上才渲染传输入口——用户能选中 candidate/rejected/deprecated 条目。
// 后端 memory_service.transfer() 会把这些逐条报成 per-item failed（status
// 字段的既有契约），与其让用户点了才看到一堆可预见的失败，不如在打开
// DestinationPicker 之前先筛掉。

test("confirmedOnly: 全部 confirmed → 原样透传", () => {
  const items = [{ id: "m1", status: "confirmed" }, { id: "m2", status: "confirmed" }];
  assert.deepEqual(confirmedOnly(items), items);
});

test("confirmedOnly: 混合状态 → 只留 confirmed，保持原有相对顺序", () => {
  const items = [
    { id: "m1", status: "confirmed" },
    { id: "m2", status: "candidate" },
    { id: "m3", status: "confirmed" },
    { id: "m4", status: "rejected" },
    { id: "m5", status: "deprecated" },
  ];
  assert.deepEqual(confirmedOnly(items).map((item) => item.id), ["m1", "m3"]);
});

test("confirmedOnly: 全部非 confirmed → 空数组", () => {
  const items = [{ id: "m1", status: "candidate" }, { id: "m2", status: "deprecated" }];
  assert.deepEqual(confirmedOnly(items), []);
});

test("confirmedOnly: 空数组 → 空数组", () => {
  assert.deepEqual(confirmedOnly([]), []);
});
