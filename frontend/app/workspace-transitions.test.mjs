import test from "node:test";
import assert from "node:assert/strict";

import {
  historyModeForTransition,
  openMemoryDeepLink,
  ownsWorkspaceRun,
  restoreLatestConversation,
} from "./workspace-transitions.ts";


test("workspace run ownership requires both epochs and notebook identity", () => {
  assert.equal(ownsWorkspaceRun(2, 2, 4, 4, "nb-1", "nb-1"), true);
  assert.equal(ownsWorkspaceRun(1, 2, 4, 4, "nb-1", "nb-1"), false);
  assert.equal(ownsWorkspaceRun(2, 2, 3, 4, "nb-1", "nb-1"), false);
  assert.equal(ownsWorkspaceRun(2, 2, 4, 4, "nb-1", "nb-2"), false);
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
