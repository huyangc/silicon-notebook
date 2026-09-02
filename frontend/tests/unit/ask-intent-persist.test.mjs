import assert from "node:assert/strict";
import test from "node:test";

import {
  PENDING_INTENT_STORAGE_KEY,
  clearPersistedIntentRuns,
  findPersistedIntentRun,
  isPersistedIntentRun,
  readPersistedIntentRuns,
  removePersistedIntentRun,
  savePersistedIntentRun,
} from "../../app/ask-intent-persist.ts";


function fakeStorage(initial = {}) {
  const data = new Map(Object.entries(initial));
  return {
    getItem: (key) => (data.has(key) ? data.get(key) : null),
    setItem: (key, value) => { data.set(key, String(value)); },
    removeItem: (key) => { data.delete(key); },
    data,
  };
}

function run(overrides = {}) {
  return {
    version: 1,
    actorId: "user-a",
    notebookId: "notebook-a",
    conversationIdAtStart: null,
    question: "question",
    askedAt: "2026-09-02T00:00:00Z",
    retrievalEffort: "standard",
    sourceScope: { mode: "exclude", source_ids: [] },
    baseScope: { mode: "exclude", notebook_ids: [] },
    phase: "preview",
    contract: null,
    understandingMs: 0,
    ...overrides,
  };
}


test("save/find/remove round-trip keeps one entry per actor+notebook", () => {
  const store = fakeStorage();
  savePersistedIntentRun(run({ question: "first" }), store);
  savePersistedIntentRun(run({ question: "second" }), store);
  savePersistedIntentRun(run({ notebookId: "notebook-b", question: "other" }), store);
  assert.equal(readPersistedIntentRuns(store).length, 2);
  assert.equal(findPersistedIntentRun("user-a", "notebook-a", store)?.question, "second");
  assert.equal(findPersistedIntentRun("user-a", "notebook-b", store)?.question, "other");
  removePersistedIntentRun("user-a", "notebook-a", store);
  assert.equal(findPersistedIntentRun("user-a", "notebook-a", store), null);
  assert.equal(readPersistedIntentRuns(store).length, 1);
  removePersistedIntentRun("user-a", "notebook-b", store);
  // The last entry gone → the key itself is gone, not an empty list left behind.
  assert.equal(store.getItem(PENDING_INTENT_STORAGE_KEY), null);
});


test("review entries keep their contract; malformed entries are dropped whole", () => {
  const store = fakeStorage();
  const contract = { objective: "q", resolved_question: "q", needs_clarification: true };
  savePersistedIntentRun(run({ phase: "review", contract, understandingMs: 1200 }), store);
  const found = findPersistedIntentRun("user-a", "notebook-a", store);
  assert.equal(found.phase, "review");
  assert.deepEqual(found.contract, contract);
  assert.equal(found.understandingMs, 1200);

  // A review without a contract cannot re-open a dialog: invalid.
  assert.equal(isPersistedIntentRun(run({ phase: "review", contract: null })), false);
  assert.equal(isPersistedIntentRun(run({ question: "   " })), false);
  assert.equal(isPersistedIntentRun(run({ version: 2 })), false);
  assert.equal(isPersistedIntentRun(run({ sourceScope: { mode: "all" } })), false);
  assert.equal(isPersistedIntentRun(run({ conversationIdAtStart: 42 })), false);
  assert.equal(isPersistedIntentRun(run()), true);
});


test("corrupt or foreign storage content reads as empty instead of throwing", () => {
  assert.deepEqual(readPersistedIntentRuns(fakeStorage({ [PENDING_INTENT_STORAGE_KEY]: "{not json" })), []);
  assert.deepEqual(readPersistedIntentRuns(fakeStorage({ [PENDING_INTENT_STORAGE_KEY]: "{\"a\":1}" })), []);
  const mixed = JSON.stringify([run(), { version: 1, actorId: "x" }]);
  assert.equal(readPersistedIntentRuns(fakeStorage({ [PENDING_INTENT_STORAGE_KEY]: mixed })).length, 1);
  // No storage at all (private mode / disabled): every operation is a no-op.
  assert.deepEqual(readPersistedIntentRuns(null), []);
  savePersistedIntentRun(run(), null);
  removePersistedIntentRun("user-a", "notebook-a", null);
  assert.equal(findPersistedIntentRun("user-a", "notebook-a", null), null);
});


test("a throwing storage degrades to no persistence", () => {
  const store = {
    getItem: () => { throw new Error("blocked"); },
    setItem: () => { throw new Error("quota"); },
    removeItem: () => { throw new Error("blocked"); },
  };
  assert.deepEqual(readPersistedIntentRuns(store), []);
  savePersistedIntentRun(run(), store);
  removePersistedIntentRun("user-a", "notebook-a", store);
  clearPersistedIntentRuns("user-a", store);
});


test("clearPersistedIntentRuns forgets only the given actor", () => {
  const store = fakeStorage();
  savePersistedIntentRun(run(), store);
  savePersistedIntentRun(run({ actorId: "user-b", notebookId: "notebook-a" }), store);
  clearPersistedIntentRuns("user-a", store);
  const left = readPersistedIntentRuns(store);
  assert.equal(left.length, 1);
  assert.equal(left[0].actorId, "user-b");
});
