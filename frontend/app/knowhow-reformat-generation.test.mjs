import test from "node:test";
import assert from "node:assert/strict";

import {
  beginReformatBatchRetry,
  canonicalReformatCellTarget,
  initReformatBatch,
  openSavedReformatCellAfterReload,
  REFORMAT_OPEN_CELL_ERROR,
  resolveReformatGenerationConcurrency,
  runReformatGenerationPool,
  reformatBatchSummary,
} from "./knowhow-optimize-logic.ts";

function item(rowId, columnId, originalMd) {
  return { rowId, columnId, columnName: columnId, originalMd, status: "pending" };
}

function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((yes, no) => {
    resolve = yes;
    reject = no;
  });
  return { promise, resolve, reject };
}

const targetColumns = [
  { id: "anchor", name: "概念", role: "anchor", position: 0 },
  { id: "value", name: "内容", role: "attribute", position: 1 },
];

test("并发 resolver: 唯一 workload clamp 到 1..3，异常/重复绑定 fallback=2", () => {
  const snapshot = (maximum) => ({ services: [{ maximum, workloads: [{ id: "knowhow_reformat" }] }] });
  assert.equal(resolveReformatGenerationConcurrency(snapshot(1)), 1);
  assert.equal(resolveReformatGenerationConcurrency(snapshot(2)), 2);
  assert.equal(resolveReformatGenerationConcurrency(snapshot(9)), 3);
  assert.equal(resolveReformatGenerationConcurrency(null), 2);
  assert.equal(resolveReformatGenerationConcurrency(snapshot(0)), 2);
  assert.equal(resolveReformatGenerationConcurrency({ services: [] }), 2);
  assert.equal(resolveReformatGenerationConcurrency({ services: [...snapshot(2).services, ...snapshot(3).services] }), 2);
});

test("generation pool: 不同 key 真实并发且最大在途不超过 limit", async () => {
  const items = Array.from({ length: 8 }, (_, index) => item(`r${index}`, `c${index}`, `v${index}`));
  let active = 0;
  let maximum = 0;
  const gates = items.map(() => deferred());
  let started = 0;
  const promise = runReformatGenerationPool({
    items,
    concurrency: 3,
    signal: new AbortController().signal,
    cache: new Map(),
    onRunning: () => undefined,
    onFresh: () => undefined,
    onStale: () => undefined,
    onError: () => undefined,
    run: async (_item, _signal) => {
      const index = started;
      started += 1;
      active += 1;
      maximum = Math.max(maximum, active);
      await gates[index].promise;
      active -= 1;
      return { candidateMd: `new-${index}`, source: "rule", changed: true, sourceMd: `v${index}` };
    },
  });
  while (started < 3) await Promise.resolve();
  assert.equal(maximum, 3);
  for (let index = 0; index < gates.length; index += 1) {
    gates[index].resolve();
    await Promise.resolve();
  }
  await promise;
  assert.equal(maximum, 3);
});

test("generation pool: 相同 key single-flight，fresh leader 扇出并按物理格计数", async () => {
  const items = [item("r1", "c1", "same"), item("r2", "c1", "same "), item("r3", "c1", "same")];
  let requests = 0;
  const fresh = [];
  const result = await runReformatGenerationPool({
    items,
    concurrency: 3,
    signal: new AbortController().signal,
    cache: new Map(),
    onRunning: () => undefined,
    onFresh: (entry, _result, cached) => fresh.push([entry.rowId, cached]),
    onStale: () => undefined,
    onError: () => undefined,
    run: async () => {
      requests += 1;
      return { candidateMd: "candidate", source: "llm", changed: true, sourceMd: "same" };
    },
  });
  assert.equal(requests, 1);
  assert.deepEqual(fresh, [["r1", false], ["r2", true], ["r3", true]]);
  assert.equal(result.cacheFanOutCount, 2);
});

test("generation pool: error leader 不污染 follower，下一物理格成为 leader", async () => {
  const items = [item("r1", "c1", "same"), item("r2", "c1", "same")];
  const errors = [];
  const successes = [];
  let requests = 0;
  const cache = new Map();
  const result = await runReformatGenerationPool({
    items,
    concurrency: 2,
    signal: new AbortController().signal,
    cache,
    onRunning: () => undefined,
    onFresh: (entry) => successes.push(entry.rowId),
    onStale: () => undefined,
    onError: (entry) => errors.push(entry.rowId),
    run: async (entry) => {
      requests += 1;
      if (entry.rowId === "r1") throw new Error("boom");
      return { candidateMd: "ok", source: "rule", changed: true, sourceMd: "same" };
    },
  });
  assert.equal(requests, 2);
  assert.deepEqual(errors, ["r1"]);
  assert.deepEqual(successes, ["r2"]);
  assert.equal(cache.size, 1);
  assert.equal(result.leaderRetryCount, 1);
});

test("generation pool: stale leader 不缓存/不扇出，follower 可重试成功", async () => {
  const items = [item("r1", "c1", "same"), item("r2", "c1", "same")];
  const stale = [];
  const success = [];
  const cache = new Map();
  const result = await runReformatGenerationPool({
    items,
    concurrency: 3,
    signal: new AbortController().signal,
    cache,
    onRunning: () => undefined,
    onFresh: (entry) => success.push(entry.rowId),
    onStale: (entry) => stale.push(entry.rowId),
    onError: () => undefined,
    run: async (entry) => ({
      candidateMd: "ok",
      source: "rule",
      changed: true,
      sourceMd: entry.rowId === "r1" ? "new live value" : entry.originalMd,
    }),
  });
  assert.deepEqual(stale, ["r1"]);
  assert.deepEqual(success, ["r2"]);
  assert.equal(cache.size, 1);
  assert.equal(result.staleCount, 1);
});

test("generation pool: AbortSignal 停止出队，迟到结果不回调", async () => {
  const controller = new AbortController();
  const gate = deferred();
  const fresh = [];
  let requests = 0;
  const promise = runReformatGenerationPool({
    items: [item("r1", "c1", "a"), item("r2", "c2", "b"), item("r3", "c3", "c")],
    concurrency: 1,
    signal: controller.signal,
    cache: new Map(),
    onRunning: () => undefined,
    onFresh: (entry) => fresh.push(entry.rowId),
    onStale: () => undefined,
    onError: () => undefined,
    run: async (entry) => {
      requests += 1;
      await gate.promise;
      return { candidateMd: "ok", source: "rule", changed: true, sourceMd: entry.originalMd };
    },
  });
  while (requests === 0) await Promise.resolve();
  controller.abort();
  gate.resolve();
  await promise;
  assert.equal(requests, 1);
  assert.deepEqual(fresh, []);
});

test("generation pool: 已观察 stale 后再 abort，调用方的 pending reload 记账不会丢", async () => {
  const controller = new AbortController();
  const slowGate = deferred();
  let pendingReload = false;
  let slowStarted = false;
  const promise = runReformatGenerationPool({
    items: [item("r-stale", "c1", "old"), item("r-slow", "c2", "slow")],
    concurrency: 1,
    signal: controller.signal,
    cache: new Map(),
    onRunning: (entry) => {
      if (entry.rowId === "r-slow") slowStarted = true;
    },
    onFresh: () => undefined,
    onStale: () => { pendingReload = true; },
    onError: () => undefined,
    run: async (entry) => {
      if (entry.rowId === "r-stale") {
        return { candidateMd: "candidate", source: "rule", changed: true, sourceMd: "new live value" };
      }
      await slowGate.promise;
      return { candidateMd: "candidate", source: "rule", changed: true, sourceMd: entry.originalMd };
    },
  });
  while (!slowStarted) await Promise.resolve();
  assert.equal(pendingReload, true);
  controller.abort();
  slowGate.resolve();
  await promise;
  assert.equal(pendingReload, true);
});

test("retry: 只重置 error/aborted/pending，保留 fresh 与 stale", () => {
  const rows = [{ id: "r1", position: 0, cells: { a: "1", b: "2", c: "3", d: "4" } }];
  let state = initReformatBatch("row", rows, [
    { id: "a", name: "a", position: 0 }, { id: "b", name: "b", position: 1 },
    { id: "c", name: "c", position: 2 }, { id: "d", name: "d", position: 3 },
  ]);
  state = {
    ...state,
    phase: "reviewing",
    aborted: true,
    items: state.items.map((entry, index) => ({
      ...entry,
      status: ["changed", "reformat_error", "aborted", "stale_skipped"][index],
      errorMessage: index ? "x" : undefined,
    })),
  };
  const retried = beginReformatBatchRetry(state);
  assert.equal(retried.phase, "running");
  assert.deepEqual(retried.items.map((entry) => entry.status), ["changed", "pending", "pending", "stale_skipped"]);
  assert.equal(reformatBatchSummary(retried).total, 4);
});

test("打开共享格: 按 position、再 rowId 选择 canonical representative", () => {
  const rows = [
    { id: "r-z", position: 2, projectionStatus: "synced", cells: { anchor: "A", value: "same", branch: "z" } },
    { id: "r-b", position: 1, projectionStatus: "synced", cells: { anchor: "A", value: "same", branch: "b" } },
    { id: "r-a", position: 1, projectionStatus: "synced", cells: { anchor: "A", value: "same", branch: "a" } },
  ];
  assert.deepEqual(canonicalReformatCellTarget("r-z", "value", rows, "anchor"), {
    rowId: "r-a", columnId: "value",
  });
  assert.deepEqual(canonicalReformatCellTarget("r-z", "branch", rows, "anchor"), {
    rowId: "r-z", columnId: "branch",
  });
});

test("saved+stale 打开格子: 先关批次并等待 reload，再按新分组打开 stable representative", async () => {
  const events = [];
  const gate = deferred();
  const promise = openSavedReformatCellAfterReload({
    rowId: "r-old",
    columnId: "value",
    currentRows: [
      { id: "r-old", position: 0, projectionStatus: "synced", cells: { anchor: "A", value: "saved" } },
    ],
    currentColumns: targetColumns,
    currentAnchorColumnId: "anchor",
    closeBatch: () => events.push("close"),
    reload: async () => {
      events.push("reload:start");
      const detail = await gate.promise;
      events.push("reload:end");
      return detail;
    },
    openCell: (rowId, columnId) => events.push(`open:${rowId}:${columnId}`),
  });

  await Promise.resolve();
  assert.deepEqual(events, ["close", "reload:start"]);
  gate.resolve({
    anchorColumnId: "anchor",
    columns: targetColumns,
    rows: [
      { id: "r-old", position: 5, projectionStatus: "synced", cells: { anchor: "A", value: "saved" } },
      { id: "r-stable", position: 1, projectionStatus: "synced", cells: { anchor: "A", value: "saved" } },
    ],
  });
  assert.deepEqual(await promise, { ok: true });
  assert.deepEqual(events, ["close", "reload:start", "reload:end", "open:r-stable:value"]);
});

test("saved+stale 打开格子: reload 失败或请求 epoch 失效时不闪开旧目标", async () => {
  const opened = [];
  const result = await openSavedReformatCellAfterReload({
    rowId: "r-old",
    columnId: "value",
    currentRows: [
      { id: "r-old", position: 0, projectionStatus: "synced", cells: { value: "saved" } },
    ],
    currentColumns: targetColumns,
    currentAnchorColumnId: null,
    closeBatch: () => undefined,
    reload: async () => null,
    openCell: (rowId) => opened.push(rowId),
  });
  assert.deepEqual(result, { ok: false, errorText: REFORMAT_OPEN_CELL_ERROR });
  assert.deepEqual(opened, []);
});

test("saved+stale 打开格子: 刷新后物理行已删除时不打开悬空目标", async () => {
  const opened = [];
  const result = await openSavedReformatCellAfterReload({
    rowId: "r-deleted",
    columnId: "value",
    currentRows: [
      { id: "r-deleted", position: 0, projectionStatus: "synced", cells: { value: "saved" } },
    ],
    currentColumns: targetColumns,
    currentAnchorColumnId: null,
    closeBatch: () => undefined,
    reload: async () => ({ rows: [], columns: targetColumns, anchorColumnId: null }),
    openCell: (rowId) => opened.push(rowId),
  });
  assert.deepEqual(result, { ok: false, errorText: REFORMAT_OPEN_CELL_ERROR });
  assert.deepEqual(opened, []);
});

test("saved+stale 打开格子: 刷新后列已删除时给出可恢复失败且不打开旧目标", async () => {
  const opened = [];
  const result = await openSavedReformatCellAfterReload({
    rowId: "r-old",
    columnId: "value",
    currentRows: [
      { id: "r-old", position: 0, projectionStatus: "synced", cells: { value: "saved" } },
    ],
    currentColumns: targetColumns,
    currentAnchorColumnId: null,
    closeBatch: () => undefined,
    reload: async () => ({
      rows: [{ id: "r-old", position: 0, projectionStatus: "synced", cells: {} }],
      columns: targetColumns.filter((column) => column.id !== "value"),
      anchorColumnId: null,
    }),
    openCell: (rowId) => opened.push(rowId),
  });
  assert.deepEqual(result, { ok: false, errorText: REFORMAT_OPEN_CELL_ERROR });
  assert.deepEqual(opened, []);
});
