import test from "node:test";
import assert from "node:assert/strict";
import { memorySuggestionAnswer, memorySuggestionConflictsWithSources, memorySuggestionReason, memorySuggestionRefines } from "../../app/memory-suggestion-model.ts";

const memory = { id: "m", notebook_id: "nb", suggested_signal: "repeated", provenance: { conversation_id: "c", answer_id: "a", suggestion: { cluster_size: 3, refines: "old" } } };
test("suggestion reasons use validated class/count without leaking malformed counts", () => {
  assert.equal(memorySuggestionReason(memory), "你问过 3 次相近的问题");
  assert.equal(memorySuggestionReason({ ...memory, suggested_signal: "clarified" }), "你在问题理解时确认了这一点");
  assert.equal(memorySuggestionReason({ ...memory, suggested_signal: "corrected" }), "你在追问中纠正了回答");
  for (const count of [null, -1, 0, 1.5, "private", Infinity]) assert.equal(memorySuggestionReason({ ...memory, provenance: { suggestion: { cluster_size: count } } }), "你问过相近的问题");
});
test("only corrected suggestions with an explicit boolean conflict receive the warning", () => {
  for (const suggested_signal of ["clarified", "repeated", "corrected", null]) {
    for (const conflicts_with_sources of [true, false, null, undefined, 0, 1, "true", {}, []]) {
      assert.equal(memorySuggestionConflictsWithSources({ ...memory, suggested_signal, provenance: { suggestion: { conflicts_with_sources } } }), suggested_signal === "corrected" && conflicts_with_sources === true);
    }
  }
  for (const suggestion of [null, "true", true, [], {}]) assert.equal(memorySuggestionConflictsWithSources({ ...memory, suggested_signal: "corrected", provenance: { suggestion } }), false);
});
test("source targets require real provenance identities and stay archival after sync/transfer", () => {
  assert.deepEqual(memorySuggestionAnswer(memory), { notebookId: "nb", conversationId: "c", answerId: "a" });
  for (const extra of [{ answer_id: null }, { conversation_id: "" }, { sync_origin: "elsewhere" }, { imported_from: {} }]) assert.equal(memorySuggestionAnswer({ ...memory, provenance: { ...memory.provenance, ...extra } }), null);
});
test("refines cannot target itself or interpret a non-string as identity", () => {
  assert.equal(memorySuggestionRefines(memory), "old");
  for (const refines of ["m", null, 10, {}, ""]) assert.equal(memorySuggestionRefines({ ...memory, provenance: { suggestion: { refines } } }), null);
});
