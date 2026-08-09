import test from "node:test";
import assert from "node:assert/strict";

import {
  followLatestNotebookRequest,
  historyModeForTransition,
  notebookIsActive,
  openMemoryDeepLink,
  ownsWorkspaceRun,
  restoreLatestConversation,
  sessionListRequestIsCurrent,
} from "../../app/workspace-transitions.ts";


function deferred() {
  let resolve;
  let reject;
  const promise = new Promise((resolvePromise, rejectPromise) => {
    resolve = resolvePromise;
    reject = rejectPromise;
  });
  return { promise, resolve, reject };
}


test("workspace run ownership requires both epochs and notebook identity", () => {
  assert.equal(ownsWorkspaceRun(2, 2, 4, 4, "nb-1", "nb-1"), true);
  assert.equal(ownsWorkspaceRun(1, 2, 4, 4, "nb-1", "nb-1"), false);
  assert.equal(ownsWorkspaceRun(2, 2, 3, 4, "nb-1", "nb-1"), false);
  assert.equal(ownsWorkspaceRun(2, 2, 4, 4, "nb-1", "nb-2"), false);
});


test("only the latest session-list request may publish into its workspace", () => {
  assert.equal(sessionListRequestIsCurrent(3, 3, "nb-1", "nb-1"), true);
  assert.equal(sessionListRequestIsCurrent(2, 3, "nb-1", "nb-1"), false);
  assert.equal(sessionListRequestIsCurrent(3, 3, "nb-1", "nb-2"), false);
});


test("session-list ownership follows the notebook, not the selected conversation epoch", () => {
  assert.equal(sessionListRequestIsCurrent(3, 3, "nb-1", "nb-1"), true);
  assert.equal(sessionListRequestIsCurrent(3, 3, "nb-1", "nb-2"), false);
});


test("Ask history may follow the active notebook after the visible run loses ownership", () => {
  assert.equal(notebookIsActive("nb-1", "nb-1"), true);
  assert.equal(notebookIsActive("nb-1", "nb-2"), false);
  assert.equal(notebookIsActive("nb-1", null), false);
});


test("an older session-list caller follows the newest same-notebook request", async () => {
  const first = deferred();
  const second = deferred();
  let latest = { notebookId: "nb-1", requestId: 1, promise: first.promise };
  const resultPromise = followLatestNotebookRequest(
    latest,
    () => latest,
    () => true,
  );
  latest = { notebookId: "nb-1", requestId: 2, promise: second.promise };
  first.resolve(["old"]);
  await Promise.resolve();
  second.resolve(["new"]);

  assert.deepEqual(await resultPromise, {
    requestId: 2,
    generationId: 2,
    value: ["new"],
  });
});


test("a failed superseding session-list request preserves a valid caller fallback", async () => {
  const first = deferred();
  const second = deferred();
  let latest = { notebookId: "nb-1", requestId: 1, promise: first.promise };
  const resultPromise = followLatestNotebookRequest(
    latest,
    () => latest,
    () => true,
  );
  latest = { notebookId: "nb-1", requestId: 2, promise: second.promise };
  first.resolve(["fallback"]);
  await Promise.resolve();
  second.reject(new Error("refresh failed"));

  assert.deepEqual(await resultPromise, {
    requestId: 1,
    generationId: 2,
    value: ["fallback"],
  });
});


test("a failed intermediate session-list request follows an already newer request", async () => {
  const first = deferred();
  const second = deferred();
  const third = deferred();
  let latest = { notebookId: "nb-1", requestId: 1, promise: first.promise };
  const resultPromise = followLatestNotebookRequest(
    latest,
    () => latest,
    () => true,
  );
  latest = { notebookId: "nb-1", requestId: 2, promise: second.promise };
  first.resolve(["old"]);
  await Promise.resolve();
  latest = { notebookId: "nb-1", requestId: 3, promise: third.promise };
  second.reject(new Error("intermediate refresh failed"));
  await Promise.resolve();
  third.resolve(["newest"]);

  assert.deepEqual(await resultPromise, {
    requestId: 3,
    generationId: 3,
    value: ["newest"],
  });
});


test("history transitions replace the current notebook and push a new one", () => {
  assert.equal(historyModeForTransition("nb-1", "nb-1"), "replace");
  assert.equal(historyModeForTransition("nb-1", "nb-2"), "push");
  assert.equal(historyModeForTransition(null, "nb-1"), "push");
});


test("latest conversation restore is empty-safe and failure-safe", async () => {
  assert.equal(await restoreLatestConversation([], async () => "unused"), null);
  assert.equal(
    await restoreLatestConversation(
      [{ id: "conv-2" }, { id: "conv-1" }],
      async (id) => `restored:${id}`,
    ),
    "restored:conv-2",
  );
  assert.equal(
    await restoreLatestConversation(
      [{ id: "conv-2" }],
      async () => { throw new Error("stale"); },
    ),
    null,
  );
});


test("Memory deep-link restore returns failure after applying fallback", async () => {
  let fallbackCalls = 0;
  assert.equal(
    await openMemoryDeepLink(
      "nb-1",
      async () => { throw new Error("gone"); },
      () => { fallbackCalls += 1; },
    ),
    false,
  );
  assert.equal(fallbackCalls, 1);
});
