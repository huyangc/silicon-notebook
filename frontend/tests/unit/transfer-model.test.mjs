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
} from "../../app/transfer-model.ts";

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

// round 10 P1-A：第二个 409 code——SourceCleanupFailed.reason 现在区分两种
// 成因（backend/app/services/knowhow/transfer.py），路由层（routes.py）据此
// 产出两个不同的 code。parseCleanupFailure 必须同时接受这个新 code、原样把
// message 带出去，不能只认旧的 source_cleanup_failed。
test("parseCleanupFailure: 命中 409 source_changed_kept → {newTableId, message}", () => {
  const body = {
    detail: {
      code: "source_changed_kept",
      new_table_id: "kt-100",
      message:
        "源表在复制完成后有更新的修改，为避免丢失该修改，源表已被保留——" +
        "目标笔记本里的副本是这次修改之前的旧版本。请核对后删除目标里的" +
        "旧副本，或重新发起一次搬迁；请勿删除源表。",
    },
  };
  assert.deepEqual(parseCleanupFailure(409, body), {
    newTableId: "kt-100",
    message:
      "源表在复制完成后有更新的修改，为避免丢失该修改，源表已被保留——" +
      "目标笔记本里的副本是这次修改之前的旧版本。请核对后删除目标里的" +
      "旧副本，或重新发起一次搬迁；请勿删除源表。",
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

test("parseCleanupFailure: 409 但 code 不是这两个已知值之一 → null", () => {
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
    { source_id: "m1", new_id: "m1x", ok: true, error: null, error_code: null, status: "copied" },
    { source_id: "m2", new_id: "m2x", ok: true, error: null, error_code: null, status: "moved" },
  ];
  const summary = summarizeTransferResults(results);
  assert.equal(summary.total, 2);
  assert.equal(summary.succeeded, 2);
  assert.equal(summary.failed, 0);
  assert.deepEqual(summary.copiedSourceNotRemoved, []);
  assert.deepEqual(summary.sourceChanged, []);
  assert.deepEqual(summary.promotionBlocked, []);
});

test("summarizeTransferResults: 混合结果（成功/普通失败/复制未删源-cleanup_error）", () => {
  const results = [
    { source_id: "m1", new_id: "m1x", ok: true, error: null, status: "copied" },
    { source_id: "m2", new_id: null, ok: false, error: "not found", status: "failed" },
    {
      source_id: "m3",
      new_id: "m3x",
      ok: false,
      error: "复制已成功，但源未删除：disk full",
      error_code: "cleanup_error",
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
  // round 10 P1-B：cleanup_error 不落进 sourceChanged 子集——它是"清理本身
  // 出错，源确实原封未动"，与"源被有意保留"是截然不同的两回事。
  assert.deepEqual(summary.sourceChanged, []);
});

test("summarizeTransferResults: 全部是 copied_source_not_removed（source_changed + cleanup_error 混合，round 10 P1-B）", () => {
  const results = [
    {
      source_id: "m1", new_id: "m1x", ok: false, error: "e1",
      error_code: "source_changed", status: "copied_source_not_removed",
    },
    {
      source_id: "m2", new_id: "m2x", ok: false, error: "e2",
      error_code: "cleanup_error", status: "copied_source_not_removed",
    },
  ];
  const summary = summarizeTransferResults(results);
  assert.equal(summary.total, 2);
  assert.equal(summary.succeeded, 0);
  assert.equal(summary.failed, 2);
  assert.equal(summary.copiedSourceNotRemoved.length, 2);
  // sourceChanged 必须只挑出 m1（error_code === "source_changed"），漏掉 m2
  // 会导致前端错误地告诉用户"删除源是安全的"，多挑上 m2 则相反——两个方向
  // 都要盯住。
  assert.equal(summary.sourceChanged.length, 1);
  assert.equal(summary.sourceChanged[0].source_id, "m1");
  assert.deepEqual(summary.promotionBlocked, []);
});

test("summarizeTransferResults: 空数组", () => {
  const summary = summarizeTransferResults([]);
  assert.deepEqual(summary, {
    total: 0,
    succeeded: 0,
    failed: 0,
    copiedSourceNotRemoved: [],
    sourceChanged: [],
    promotionBlocked: [],
  });
});

// --- promotionBlocked（round 8 P2-A）----------------------------------------
// 后端在 memory_service.py 的 _TransferRejected 上挂 error_code=
// "promotion_proposed"：该 Memory 正在公共知识库审批队列中，move 被拒绝。
// 这不是普通失败——重试在提案被处理之前永远还是这个结果，前端得把它从
// "N 条失败，请稍后重试"里摘出来单独提示，不能引导用户白费力气重试。

test("summarizeTransferResults: 识别 promotion_proposed 阻塞，与普通失败/copiedSourceNotRemoved 分开", () => {
  const results = [
    { source_id: "m1", new_id: "m1x", ok: true, error: null, error_code: null, status: "copied" },
    {
      source_id: "m2",
      new_id: null,
      ok: false,
      error: "该 Memory 正在提升到公共知识库的审批队列中，无法移动；请先在审批中心处理该提案后再移动",
      error_code: "promotion_proposed",
      status: "failed",
    },
    { source_id: "m3", new_id: null, ok: false, error: "not found", error_code: null, status: "failed" },
    {
      source_id: "m4",
      new_id: "m4x",
      ok: false,
      error: "复制已成功，但源未删除：disk full",
      error_code: null,
      status: "copied_source_not_removed",
    },
  ];
  const summary = summarizeTransferResults(results);
  assert.equal(summary.total, 4);
  assert.equal(summary.succeeded, 1);
  assert.equal(summary.failed, 3);
  assert.equal(summary.promotionBlocked.length, 1);
  assert.equal(summary.promotionBlocked[0].source_id, "m2");
  assert.equal(summary.copiedSourceNotRemoved.length, 1);
  assert.equal(summary.copiedSourceNotRemoved[0].source_id, "m4");
  // m4 的 error_code 是 null（不是 "source_changed"）——不该被 sourceChanged
  // 误收：null 只表示"没有机器可判的成因"，不能当成"源被有意保留"处理。
  assert.deepEqual(summary.sourceChanged, []);
});

test("summarizeTransferResults: error_code 缺失（旧结果/其它失败原因）不算 promotionBlocked", () => {
  const results = [
    { source_id: "m1", new_id: null, ok: false, error: "boom", status: "failed" },
  ];
  const summary = summarizeTransferResults(results);
  assert.deepEqual(summary.promotionBlocked, []);
  assert.equal(summary.failed, 1);
});

test("summarizeTransferResults: promotion_proposed 全部命中", () => {
  const results = [
    {
      source_id: "m1", new_id: null, ok: false,
      error: "e1", error_code: "promotion_proposed", status: "failed",
    },
    {
      source_id: "m2", new_id: null, ok: false,
      error: "e2", error_code: "promotion_proposed", status: "failed",
    },
  ];
  const summary = summarizeTransferResults(results);
  assert.equal(summary.total, 2);
  assert.equal(summary.failed, 2);
  assert.equal(summary.promotionBlocked.length, 2);
  assert.deepEqual(summary.copiedSourceNotRemoved, []);
  assert.deepEqual(summary.sourceChanged, []);
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
