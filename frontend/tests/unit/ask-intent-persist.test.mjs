import assert from "node:assert/strict";
import test from "node:test";

import {
  PENDING_INTENT_STORAGE_KEY,
  claimIntentRun,
  clearPersistedIntentRuns,
  findPersistedIntentRuns,
  isPersistedIntentRun,
  isQueryIntentContractShape,
  readPersistedIntentRuns,
  releaseIntentRun,
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

function contract(overrides = {}) {
  return {
    objective: "q",
    resolved_question: "q",
    intent_type: "other",
    result_scope: "ranked",
    completeness_required: false,
    entities: [],
    mandatory_topics: [],
    comparison_axes: [],
    constraints: [],
    excluded_topics: [],
    expected_output: "answer",
    assumptions: [],
    ambiguities: [{ id: "which", question: "Which one?", required: true }],
    confidence: 0.5,
    needs_clarification: true,
    confirmed: false,
    ...overrides,
  };
}

let counter = 0;
function run(overrides = {}) {
  counter += 1;
  return {
    version: 1,
    id: `run-${counter}`,
    savedAt: counter,
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
    confirmation: null,
    ...overrides,
  };
}


test("records are keyed by submission id: several per notebook coexist, newest first", () => {
  const store = fakeStorage();
  const a = run({ question: "first", conversationIdAtStart: "c1" });
  const b = run({ question: "second", conversationIdAtStart: "c2" });
  const other = run({ notebookId: "notebook-b", question: "other" });
  savePersistedIntentRun(a, store);
  savePersistedIntentRun(b, store);
  savePersistedIntentRun(other, store);
  assert.equal(readPersistedIntentRuns(store).length, 3);
  assert.deepEqual(
    findPersistedIntentRuns("user-a", "notebook-a", store).map((item) => item.question),
    ["second", "first"],
  );
  // Re-saving the same submission (preview → review) updates in place.
  savePersistedIntentRun({ ...a, phase: "review", contract: contract() }, store);
  assert.equal(findPersistedIntentRuns("user-a", "notebook-a", store).length, 2);
  assert.equal(findPersistedIntentRuns("user-a", "notebook-a", store).find((item) => item.id === a.id).phase, "review");

  removePersistedIntentRun(a.id, store);
  assert.deepEqual(findPersistedIntentRuns("user-a", "notebook-a", store).map((item) => item.id), [b.id]);
  removePersistedIntentRun(b.id, store);
  removePersistedIntentRun(other.id, store);
  // The last entry gone → the key itself is gone, not an empty list left behind.
  assert.equal(store.getItem(PENDING_INTENT_STORAGE_KEY), null);
});


test("review entries need a well-formed contract; malformed entries are dropped whole", () => {
  assert.equal(isQueryIntentContractShape(contract()), true);
  assert.equal(isQueryIntentContractShape({ objective: "q" }), false);
  assert.equal(isQueryIntentContractShape(contract({ ambiguities: "nope" })), false);
  assert.equal(isQueryIntentContractShape(contract({ ambiguities: [{ id: 1 }] })), false);
  assert.equal(isQueryIntentContractShape(contract({ entities: [1] })), false);
  assert.equal(isQueryIntentContractShape(contract({ mandatory_topics: [{ id: "t" }] })), false);
  assert.equal(isQueryIntentContractShape(contract({ mandatory_topics: [{ id: "t", title: "T", question: "q?" }] })), false);
  assert.equal(isQueryIntentContractShape(contract({
    mandatory_topics: [{ id: "t", title: "T", question: "q?", retrieval_queries: ["a"] }],
  })), true);
  // Every required scalar/enum field is checked — a stale same-version entry
  // missing one of them must not reach the review card or the backend.
  for (const key of ["intent_type", "result_scope", "completeness_required", "expected_output", "confirmed"]) {
    const incomplete = contract();
    delete incomplete[key];
    assert.equal(isQueryIntentContractShape(incomplete), false, `missing ${key} must be rejected`);
  }
  assert.equal(isQueryIntentContractShape(contract({ result_scope: "everything" })), false);
  assert.equal(isQueryIntentContractShape(contract({ confidence: Number.NaN })), false);
  assert.equal(isQueryIntentContractShape(contract({ ambiguities: [{ id: "a", question: "q", options: [1] }] })), false);
  assert.equal(isQueryIntentContractShape(contract({ clarification_answers: [{ id: "a" }] })), false);
  assert.equal(isQueryIntentContractShape(contract({
    clarification_answers: [{ id: "a", question: "q", answer: "x" }],
  })), true);

  assert.equal(isPersistedIntentRun(run({ phase: "review", contract: contract() })), true);
  // hand-off entries carry the confirmed intent; anything less cannot be re-submitted.
  const confirmation = { contract: contract(), resolved_question: "q", answers: [{ id: "which", answer: "x" }], understanding_ms: 12 };
  assert.equal(isPersistedIntentRun(run({ phase: "handoff", contract: contract(), confirmation })), true);
  assert.equal(isPersistedIntentRun(run({ phase: "handoff", contract: contract(), confirmation: null })), false);
  assert.equal(isPersistedIntentRun(run({ phase: "handoff", contract: contract(), confirmation: { ...confirmation, answers: [{ id: 1 }] } })), false);
  assert.equal(isPersistedIntentRun(run({ phase: "handoff", contract: contract(), confirmation: { ...confirmation, contract: { objective: "q" } } })), false);
  // A preview/review entry must not smuggle a confirmation in.
  assert.equal(isPersistedIntentRun(run({ phase: "preview", confirmation })), false);
  assert.equal(isPersistedIntentRun(run({ phase: "review", contract: null })), false);
  assert.equal(isPersistedIntentRun(run({ phase: "review", contract: { objective: "q" } })), false);
  assert.equal(isPersistedIntentRun(run({ question: "   " })), false);
  assert.equal(isPersistedIntentRun(run({ version: 2 })), false);
  assert.equal(isPersistedIntentRun(run({ id: "" })), false);
  assert.equal(isPersistedIntentRun(run({ savedAt: "now" })), false);
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
  removePersistedIntentRun("run-x", null);
  assert.deepEqual(findPersistedIntentRuns("user-a", "notebook-a", null), []);
});


test("a throwing storage degrades to no persistence", () => {
  const store = {
    getItem: () => { throw new Error("blocked"); },
    setItem: () => { throw new Error("quota"); },
    removeItem: () => { throw new Error("blocked"); },
  };
  assert.deepEqual(readPersistedIntentRuns(store), []);
  savePersistedIntentRun(run(), store);
  removePersistedIntentRun("run-x", store);
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


test("claimIntentRun holds a Web Lock per record and refuses a record another tab holds", async () => {
  const held = new Map();
  const locks = {
    request: async (name, options, callback) => {
      assert.equal(options.ifAvailable, true);
      if (held.has(name)) return callback(null);
      const lock = { name };
      held.set(name, lock);
      try {
        return await callback(lock);
      } finally {
        held.delete(name);
      }
    },
  };
  assert.equal(await claimIntentRun("rec-1", locks), true);
  assert.equal(held.size, 1, "the lock stays held while this tab owns the record");
  // Same tab asking again is idempotent.
  assert.equal(await claimIntentRun("rec-1", locks), true);
  // "Another tab" (the lock is already held) is refused.
  let refused = false;
  await locks.request("silicon_notebook_pending_intent:rec-1", { ifAvailable: true }, async (lock) => {
    refused = lock === null;
  });
  assert.equal(refused, true);
  releaseIntentRun("rec-1");
  await new Promise((resolve) => setTimeout(resolve, 0));
  assert.equal(held.size, 0, "release lets the lock go");
  // Without Web Locks the claim is granted (sessionStorage isolation alone).
  assert.equal(await claimIntentRun("rec-2", null), true);
  // A lock manager that throws is treated as unavailable, not as a refusal.
  assert.equal(await claimIntentRun("rec-3", { request: () => Promise.reject(new Error("no")) }), true);
});
