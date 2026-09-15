import test from "node:test";
import assert from "node:assert/strict";

import { refreshKgDeleteDependents } from "../../app/kg-delete-dependents.ts";

function deferred() {
  let resolve;
  const promise = new Promise((resolvePromise) => { resolve = resolvePromise; });
  return { promise, resolve };
}

function fakePort(overrides = {}) {
  const calls = [];
  const state = { active: "nb-a", knowledgeOpen: false, indexOpen: false };
  const port = {
    activeNotebookId: () => state.active,
    reloadSources: async (notebookId, guard) => { calls.push(["reloadSources", notebookId, guard()]); },
    invalidateKnowledge: () => { calls.push(["invalidateKnowledge"]); },
    knowledgeBrowserOpen: () => state.knowledgeOpen,
    reenterKnowledge: async () => { calls.push(["reenterKnowledge"]); },
    indexPanelOpen: () => state.indexOpen,
    fetchIndexStatus: async (notebookId) => { calls.push(["fetchIndexStatus", notebookId]); return { nb: notebookId }; },
    applyIndexStatus: (status) => { calls.push(["applyIndexStatus", status]); },
    ...overrides,
  };
  return { port, calls, state };
}

test("owner 已不可见 → 什么都不碰（不替别的笔记本作废 Knowledge 缓存）", async () => {
  const { port, calls, state } = fakePort();
  state.knowledgeOpen = true;
  state.indexOpen = true;
  await refreshKgDeleteDependents("nb-a", () => false, port);
  assert.deepEqual(calls, []);
});

test("此刻打开的已是另一本 → 什么都不碰", async () => {
  const { port, calls, state } = fakePort();
  state.active = "nb-b";
  state.knowledgeOpen = true;
  await refreshKgDeleteDependents("nb-a", () => true, port);
  assert.deepEqual(calls, []);
});

test("仍是当前笔记本：作废 Knowledge 并重拉来源；浏览器与看板没开就不多发请求", async () => {
  const { port, calls } = fakePort();
  await refreshKgDeleteDependents("nb-a", () => true, port);
  assert.deepEqual(calls, [["invalidateKnowledge"], ["reloadSources", "nb-a", true]]);
});

test("Knowledge 浏览器开着 → 作废之后重新进入；看板开着 → 重拉并落「索引与构建」状态", async () => {
  const { port, calls, state } = fakePort();
  state.knowledgeOpen = true;
  state.indexOpen = true;
  await refreshKgDeleteDependents("nb-a", () => true, port);
  assert.deepEqual(calls.map(([name]) => name), [
    "invalidateKnowledge",
    "reloadSources",
    "reenterKnowledge",
    "fetchIndexStatus",
    "applyIndexStatus",
  ]);
  const invalidateAt = calls.findIndex(([name]) => name === "invalidateKnowledge");
  const reenterAt = calls.findIndex(([name]) => name === "reenterKnowledge");
  assert.ok(invalidateAt < reenterAt, "必须先作废再重新进入，否则重新进入会沿用旧行");
});

test("看板状态在途时切走 → 迟到的状态不落到新笔记本的看板上；来源重拉拿到的守卫同样随之失效", async () => {
  const pendingStatus = deferred();
  let sourcesGuard = null;
  const { port, calls, state } = fakePort({
    fetchIndexStatus: () => pendingStatus.promise,
    reloadSources: async (_notebookId, guard) => { sourcesGuard = guard; },
  });
  state.indexOpen = true;
  const running = refreshKgDeleteDependents("nb-a", () => true, port);
  state.active = "nb-b";
  pendingStatus.resolve({ nb: "nb-a" });
  await running;
  assert.equal(calls.some(([name]) => name === "applyIndexStatus"), false);
  assert.equal(sourcesGuard(), false);
});

test("任一块失败都不拖住其余几块，也不向上抛", async () => {
  const { port, calls, state } = fakePort({
    reloadSources: async () => { throw new Error("sources down"); },
    reenterKnowledge: async () => { throw new Error("knowledge down"); },
  });
  state.knowledgeOpen = true;
  state.indexOpen = true;
  await refreshKgDeleteDependents("nb-a", () => true, port);
  assert.ok(calls.some(([name]) => name === "applyIndexStatus"));
});
