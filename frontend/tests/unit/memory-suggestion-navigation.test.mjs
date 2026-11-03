import test from "node:test";
import assert from "node:assert/strict";
import { navigateMemorySuggestionAnswer } from "../../app/memory-suggestion-navigation.ts";
import { callSitesIn, findFunctionIn, parseModule } from "../../test-support/semantic-source.mjs";

const target = { notebookId: "target-nb", conversationId: "conversation", answerId: "answer" };
function deferred() { let resolve; const promise = new Promise((done) => { resolve = done; }); return { promise, resolve }; }
function harness(notebookId = null) {
  const state = { actorId: "actor", notebookId, epoch: 1 };
  const controller = new AbortController();
  const calls = [];
  const navigation = {
    actorId: "actor", signal: controller.signal, currentActorId: () => state.actorId, currentNotebookId: () => state.notebookId, currentEpoch: () => state.epoch,
    openNotebook: async (id, actor) => { calls.push(["notebook", id, actor]); state.epoch++; state.notebookId = id; return true; },
    openSession: async (id) => { calls.push(["session", id]); state.epoch++; return true; },
  };
  return { state, controller, calls, navigation };
}
test("global suggestion opens its actual notebook before existing session owner", async () => {
  const h = harness();
  assert.deepEqual(await navigateMemorySuggestionAnswer(target, h.navigation), { ...target, actorId: "actor", epoch: 3 });
  assert.deepEqual(h.calls, [["notebook", "target-nb", "actor"], ["session", "conversation"]]);
});
test("same-notebook suggestion uses existing session open without redundant notebook I/O", async () => {
  const h = harness("target-nb");
  assert.deepEqual(await navigateMemorySuggestionAnswer(target, h.navigation), { ...target, actorId: "actor", epoch: 2 });
  assert.deepEqual(h.calls, [["session", "conversation"]]);
});
for (const change of ["actor", "epoch", "abort", "aba"]) test(`late notebook hop is fenced after ${change}`, async () => {
  const h = harness(); const pending = deferred();
  h.navigation.openNotebook = () => { h.state.epoch++; return pending.promise; };
  const result = navigateMemorySuggestionAnswer(target, h.navigation);
  h.state.notebookId = "target-nb";
  if (change === "actor") h.state.actorId = "other";
  if (change === "epoch" || change === "aba") h.state.epoch += 2;
  if (change === "abort") h.controller.abort();
  pending.resolve(true);
  assert.equal(await result, null);
  assert.deepEqual(h.calls, []);
});
test("late session reply cannot publish a destination into a new workspace", async () => {
  const h = harness("target-nb"); const pending = deferred();
  h.navigation.openSession = () => { h.state.epoch++; return pending.promise; };
  const result = navigateMemorySuggestionAnswer(target, h.navigation);
  h.state.epoch++; pending.resolve(true);
  assert.equal(await result, null);
});
test("page binds the production navigation helper to existing openNotebook/openAskSession", async () => {
  const page = await parseModule("page.tsx");
  const calls = callSitesIn(findFunctionIn(page, "Home", "openMemorySourceAnswer"));
  assert.ok(calls.some(({ target }) => target === "navigateMemorySuggestionAnswer"));
  assert.ok(calls.some(({ target }) => target === "openNotebook"));
});
