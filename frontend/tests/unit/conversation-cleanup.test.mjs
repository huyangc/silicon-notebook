import test from "node:test";
import assert from "node:assert/strict";

import {
  conversationCleanupToast,
  conversationsOlderThan,
  CLEANUP_PRESETS,
  reconcileConversationCleanup,
  runOwnedConversationCleanup,
} from "../../app/conversation-cleanup.ts";
import { workspaceRequestIsCurrent } from "../../app/workspace-transitions.ts";

// Fixed "now" so the test is deterministic regardless of wall clock / timezone.
// updated_at strings are naive-local ISO, parsed as local — same basis as NOW.
const NOW = Date.parse("2026-06-24T12:00:00");
const conv = (id, updated_at) => ({ id, updated_at });

test("picks only conversations whose last activity is older than N days", () => {
  const sessions = [
    conv("old", "2026-06-20T12:00:00"),    // 4 days ago  -> older than 3
    conv("fresh", "2026-06-23T12:00:00"),  // 1 day ago   -> within 3
  ];
  const ids = conversationsOlderThan(sessions, 3, NOW).map((s) => s.id);
  assert.deepEqual(ids, ["old"]);
});

test("exact boundary is kept (strict less-than)", () => {
  const exactly3 = conv("edge", "2026-06-21T12:00:00"); // exactly NOW - 3*24h
  assert.equal(conversationsOlderThan([exactly3], 3, NOW).length, 0);
});

test("presets are 3 / 7 / 30 days", () => {
  assert.deepEqual([...CLEANUP_PRESETS], [3, 7, 30]);
});

test("stale candidate is preserved when backend confirms zero deletions", () => {
  const sessions = [
    conv("current", "2000-01-01T00:00:00"),
    conv("other", "2000-01-01T00:00:00"),
  ];
  const result = reconcileConversationCleanup(sessions, "current", []);
  assert.deepEqual(result.sessions.map((session) => session.id), ["current", "other"]);
  assert.equal(result.currentDeleted, false);
  assert.equal(
    conversationCleanupToast(0),
    "没有删除会话；这些会话可能已有新活动或问答正在进行",
  );
});

test("confirmed current deletion removes only returned ids and clears current", () => {
  const sessions = [
    conv("current", "2000-01-01T00:00:00"),
    conv("other", "2000-01-01T00:00:00"),
  ];
  const result = reconcileConversationCleanup(
    sessions,
    "current",
    ["current"],
  );
  assert.deepEqual(result.sessions.map((session) => session.id), ["other"]);
  assert.equal(result.currentDeleted, true);
  assert.equal(conversationCleanupToast(1), "已删除 1 条会话");
});

function deferredResponse() {
  let resolve;
  let reject;
  const promise = new Promise((accept, decline) => {
    resolve = accept;
    reject = decline;
  });
  return { promise, resolve, reject };
}

test("delayed cleanup response cannot mutate a newly opened session resource", async () => {
  const response = deferredResponse();
  const owner = { workspaceEpoch: 7, notebookId: "notebook-a" };
  let currentWorkspaceEpoch = owner.workspaceEpoch;
  let currentNotebookId = owner.notebookId;
  const state = {
    sessions: [conv("conversation-a", "2000-01-01T00:00:00")],
    conversationId: "conversation-a",
    turns: ["turn-a"],
    pendingQuestion: "pending-a",
    reloads: 0,
    messages: [],
  };
  const isCurrent = () => workspaceRequestIsCurrent(
    false,
    owner.workspaceEpoch,
    currentWorkspaceEpoch,
    owner.notebookId,
    currentNotebookId,
  );
  const cleanup = runOwnedConversationCleanup(
    response.promise,
    isCurrent,
    (result) => {
      const reconciled = reconcileConversationCleanup(
        state.sessions,
        state.conversationId,
        result.deleted_ids,
      );
      state.sessions = reconciled.sessions;
      if (reconciled.currentDeleted) {
        state.conversationId = null;
        state.turns = [];
        state.pendingQuestion = "";
      }
    },
    async () => { state.reloads += 1; },
    (result) => { state.messages.push(conversationCleanupToast(result.deleted)); },
  );

  // Opening B in the same notebook advances the workspace/session epoch.
  currentWorkspaceEpoch += 1;
  state.sessions = [conv("conversation-b", "2026-07-23T00:00:00")];
  state.conversationId = "conversation-b";
  state.turns = ["turn-b"];
  state.pendingQuestion = "pending-b";
  response.resolve({ deleted: 1, deleted_ids: ["conversation-a"] });

  assert.equal(await cleanup, false);
  assert.deepEqual(state.sessions.map((session) => session.id), ["conversation-b"]);
  assert.equal(state.conversationId, "conversation-b");
  assert.deepEqual(state.turns, ["turn-b"]);
  assert.equal(state.pendingQuestion, "pending-b");
  assert.equal(state.reloads, 0);
  assert.deepEqual(state.messages, []);
});

test("same-resource delayed cleanup reconciles, reloads, and reports normally", async () => {
  const response = deferredResponse();
  const state = {
    sessions: [
      conv("conversation-a", "2000-01-01T00:00:00"),
      conv("conversation-b", "2026-07-23T00:00:00"),
    ],
    conversationId: "conversation-a",
    turns: ["turn-a"],
    pendingQuestion: "pending-a",
    reloads: 0,
    messages: [],
  };
  const cleanup = runOwnedConversationCleanup(
    response.promise,
    () => true,
    (result) => {
      const reconciled = reconcileConversationCleanup(
        state.sessions,
        state.conversationId,
        result.deleted_ids,
      );
      state.sessions = reconciled.sessions;
      if (reconciled.currentDeleted) {
        state.conversationId = null;
        state.turns = [];
        state.pendingQuestion = "";
      }
    },
    async () => { state.reloads += 1; },
    (result) => { state.messages.push(conversationCleanupToast(result.deleted)); },
  );
  response.resolve({ deleted: 1, deleted_ids: ["conversation-a"] });

  assert.equal(await cleanup, true);
  assert.deepEqual(state.sessions.map((session) => session.id), ["conversation-b"]);
  assert.equal(state.conversationId, null);
  assert.deepEqual(state.turns, []);
  assert.equal(state.pendingQuestion, "");
  assert.equal(state.reloads, 1);
  assert.deepEqual(state.messages, ["已删除 1 条会话"]);
});

test("reload rejection after a session switch is stale and cannot affect the new resource", async () => {
  const reload = deferredResponse();
  const reloadStarted = deferredResponse();
  const owner = { workspaceEpoch: 11, notebookId: "notebook-a" };
  let currentWorkspaceEpoch = owner.workspaceEpoch;
  let currentNotebookId = owner.notebookId;
  let switched = false;
  let staleWrites = 0;
  const state = {
    conversationId: "conversation-a",
    turns: ["turn-a"],
    pendingQuestion: "pending-a",
    messages: [],
  };
  const isCurrent = () => workspaceRequestIsCurrent(
    false,
    owner.workspaceEpoch,
    currentWorkspaceEpoch,
    owner.notebookId,
    currentNotebookId,
  );
  const cleanup = runOwnedConversationCleanup(
    Promise.resolve({ deleted: 1, deleted_ids: ["conversation-a"] }),
    isCurrent,
    () => {
      if (switched) staleWrites += 1;
      state.conversationId = null;
      state.turns = [];
      state.pendingQuestion = "";
    },
    async () => {
      reloadStarted.resolve();
      await reload.promise;
      if (switched) staleWrites += 1;
    },
    (result) => {
      if (switched) staleWrites += 1;
      state.messages.push(conversationCleanupToast(result.deleted));
    },
  );

  await reloadStarted.promise;
  switched = true;
  currentWorkspaceEpoch += 1; // same notebook, newly opened session B
  state.conversationId = "conversation-b";
  state.turns = ["turn-b"];
  state.pendingQuestion = "pending-b";
  reload.reject(new Error("old session reload failed"));

  assert.equal(await cleanup, false);
  assert.equal(state.conversationId, "conversation-b");
  assert.deepEqual(state.turns, ["turn-b"]);
  assert.equal(state.pendingQuestion, "pending-b");
  assert.deepEqual(state.messages, []);
  assert.equal(staleWrites, 0);
});

test("same-resource reload rejection remains reportable", async () => {
  const failure = new Error("current session reload failed");
  let notifications = 0;
  const cleanup = runOwnedConversationCleanup(
    Promise.resolve({ deleted: 1, deleted_ids: ["conversation-a"] }),
    () => true,
    () => undefined,
    async () => { throw failure; },
    () => { notifications += 1; },
  );

  await assert.rejects(cleanup, (error) => error === failure);
  assert.equal(notifications, 0);
});
