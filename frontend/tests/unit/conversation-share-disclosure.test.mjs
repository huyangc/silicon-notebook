import assert from "node:assert/strict";
import test from "node:test";

import {
  SHARE_DISCLOSURE_COUNTS_ERROR,
  SHARE_UPDATE_COUNTS_ERROR,
  summarizeShareDisclosure,
  summarizeShareUpdate,
  withinWatermark,
} from "../../app/conversation-share-disclosure.ts";

// Minimal turn shaped like ConversationDetail["turns"][number]: only the fields
// summarizeShareDisclosure reads (answer_id + created_at + response.anchors/
// citations). Classification is by answer_id position now, NOT timestamp.
function turnId(answerId, createdAt, { anchors = [], citations = [] } = {}) {
  return {
    answer_id: answerId,
    question: "q",
    asked_at: createdAt,
    created_at: createdAt,
    response: { anchors, citations },
  };
}

function turn(createdAt, opts = {}) {
  return turnId(`ans-${createdAt}`, createdAt, opts);
}

function citation(overrides = {}) {
  return { images: [], ...overrides };
}

// --- Memory disclosure red line (设计 §五 consent; codex T5 review P2-1) -------

test("K counts DISTINCT memory ids, deduped across turns and repeat citations", () => {
  const turns = [
    turn("2026-01-01T00:00:00Z", {
      citations: [citation({ memory_id: "mem-a" }), citation({ memory_id: "mem-a" })],
    }),
    turn("2026-01-01T00:01:00Z", {
      citations: [citation({ memory_id: "mem-b" }), citation({ memory_id: "" })],
    }),
  ];
  // mem-a appears twice (one turn, two citations) + mem-b once -> 2 distinct.
  assert.equal(summarizeShareDisclosure(turns, "", "").memoryCount, 2);
});

test("a memory-backed citation is counted (the disclosure can never be zero when one exists)", () => {
  const turns = [turn("2026-01-01T00:00:00Z", { citations: [citation({ memory_id: "mem-x" })] })];
  assert.ok(summarizeShareDisclosure(turns, "", "").memoryCount > 0);
});

// --- Watermark filter excludes turns AFTER the watermark id (freeze) ----------

test("turns after the watermark id are counted as new, not included in shared counts", () => {
  const turns = [
    turn("2026-01-01T00:00:00Z", { citations: [citation({ memory_id: "mem-in" })] }),
    turn("2026-01-01T00:05:00Z", { citations: [citation({ memory_id: "mem-late" })] }),
  ];
  // Watermark id at the first turn: the second (later) turn is "new", its memory
  // must NOT enter the disclosure — you only publish up to the watermark.
  const d = summarizeShareDisclosure(turns, turns[0].answer_id, turns[0].created_at);
  assert.equal(d.sharedCount, 1);
  assert.equal(d.newCount, 1);
  assert.equal(d.memoryCount, 1); // only mem-in, not mem-late
});

test("empty id (unshared preview / afterUpdate) counts every turn", () => {
  const turns = [
    turn("2026-01-01T00:00:00Z", { citations: [citation({ memory_id: "mem-1" })] }),
    turn("2026-01-01T00:05:00Z", { citations: [citation({ memory_id: "mem-2" })] }),
  ];
  const d = summarizeShareDisclosure(turns, "", "");
  assert.equal(d.sharedCount, 2);
  assert.equal(d.newCount, 0);
  assert.equal(d.memoryCount, 2);
});

// --- Same-instant tie-break: classify by ORDER, not timestamp (codex #522 R3) -
// Two answers at the SAME created_at. The `turns` array order IS the backend
// keyset order (rowid/ordinal tie-break). The watermark points to the EARLIER
// one; the later one must be classified NEW even though its timestamp is equal —
// the exact bug where a pure-timestamp classifier hides it from "更新到最新".

test("a same-instant answer AFTER the watermark id is 'new', not 'shared'", () => {
  const at = "2026-01-01T00:00:00Z";
  const turns = [
    turnId("ans-a", at, { citations: [citation({ memory_id: "mem-a" })] }),
    turnId("ans-b", at, { citations: [citation({ memory_id: "mem-b" })] }),
  ];
  // Watermark pinned to ans-a (the earlier tie member); ans-b sorts AFTER it.
  const d = summarizeShareDisclosure(turns, "ans-a", at);
  assert.equal(d.sharedCount, 1);
  assert.equal(d.newCount, 1); // ans-b is NEW despite the equal timestamp
  assert.equal(d.memoryCount, 1); // only mem-a — mem-b is not yet published
  // Forward-looking update must flag the newly-exposed memory BEFORE the click.
  const preview = summarizeShareUpdate(turns, "ans-a", at);
  assert.equal(preview.afterUpdate.memoryCount, 2);
  assert.equal(preview.newMemoryCount, 1);
});

// --- Deleted/drifted watermark answer -> timestamp fallback (never crash) ------

test("a shared_through_id absent from turns falls back to the timestamp interval", () => {
  const turns = [
    turn("2026-01-01T00:00:00Z", { citations: [citation({ memory_id: "mem-in" })] }),
    turn("2026-01-01T00:05:00Z", { citations: [citation({ memory_id: "mem-late" })] }),
  ];
  // Watermark answer was deleted: its id is not in `turns`. Fall back to the
  // created_at interval (mirrors the backend's deleted-watermark fallback).
  const d = summarizeShareDisclosure(turns, "ans-deleted", "2026-01-01T00:00:00Z");
  assert.equal(d.sharedCount, 1);
  assert.equal(d.newCount, 1);
  assert.equal(d.memoryCount, 1); // only mem-in (created_at <= watermark)
});

// --- Forward-looking "update to latest" disclosure (consent; codex #522 R1) ---
// "更新到最新" pushes the watermark to ALL turns, so its consent judgement is
// "what will this button publish". A memory referenced ONLY by a post-watermark
// turn must be counted in the forward-looking disclosure BEFORE the click — the
// bug was that its count only rose AFTER the update, i.e. after it was published.

test("summarizeShareUpdate counts post-watermark memory in the forward-looking disclosure", () => {
  const turns = [
    turn("2026-01-01T00:00:00Z", { citations: [citation({ memory_id: "mem-in" })] }),
    turn("2026-01-01T00:05:00Z", { citations: [citation({ memory_id: "mem-late" })] }),
  ];
  const id = turns[0].answer_id;
  // Current disclosure freezes at the watermark: only mem-in is public today.
  assert.equal(summarizeShareDisclosure(turns, id, turns[0].created_at).memoryCount, 1);
  // Forward-looking: "更新到最新" would publish BOTH mem-in and mem-late.
  const preview = summarizeShareUpdate(turns, id, turns[0].created_at);
  assert.equal(preview.afterUpdate.memoryCount, 2); // mem-late IS counted
  assert.equal(preview.newMemoryCount, 1);          // and flagged as newly exposed
});

test("summarizeShareUpdate flags newly exposed images from post-watermark turns", () => {
  const turns = [
    turn("2026-01-01T00:00:00Z", { citations: [citation({ images: [{ asset_id: "a1" }] })] }),
    turn("2026-01-01T00:05:00Z", { citations: [citation({ images: [{ asset_id: "a2" }] })] }),
  ];
  const preview = summarizeShareUpdate(turns, turns[0].answer_id, turns[0].created_at);
  assert.equal(preview.afterUpdate.imageCount, 2);
  assert.equal(preview.newImageCount, 1);
});

test("summarizeShareUpdate reports no new memory when a post-watermark turn reuses an already-public id", () => {
  const turns = [
    turn("2026-01-01T00:00:00Z", { citations: [citation({ memory_id: "mem-a" })] }),
    turn("2026-01-01T00:05:00Z", { citations: [citation({ memory_id: "mem-a" })] }),
  ];
  const preview = summarizeShareUpdate(turns, turns[0].answer_id, turns[0].created_at);
  assert.equal(preview.afterUpdate.memoryCount, 1); // mem-a, deduped
  assert.equal(preview.newMemoryCount, 0);          // already disclosed -> not "new"
});

// --- Images: dedup by asset_id per turn, summed; anchors ∪ citations ----------

test("M dedups images by asset_id within a turn and spans anchors and citations", () => {
  const turns = [
    turn("2026-01-01T00:00:00Z", {
      anchors: [{ images: [{ asset_id: "a1" }, { asset_id: "a1" }] }], // dup a1
      citations: [citation({ images: [{ asset_id: "a2" }] })],
    }),
    turn("2026-01-01T00:01:00Z", {
      citations: [citation({ images: [{ asset_id: "a3" }] })],
    }),
  ];
  // turn 1: {a1, a2} = 2; turn 2: {a3} = 1 -> 3 summed.
  assert.equal(summarizeShareDisclosure(turns, "", "").imageCount, 3);
});

// --- countsError fallback discloses BOTH images and memory (codex #522 R2 P2) -
// When the conversation detail fails to load the modal can't compute exact
// counts, but the consent surface has TWO halves — attached images AND private
// memory — and dropping either from the fallback is a silent omission. Both
// fallback strings must name both. Mutation guard: deleting 附图 from either
// constant reds this.

test("countsError fallbacks name BOTH images and memory, never just one", () => {
  for (const message of [SHARE_DISCLOSURE_COUNTS_ERROR, SHARE_UPDATE_COUNTS_ERROR]) {
    assert.ok(message.includes("附图"), `fallback must mention images: ${message}`);
    assert.ok(message.includes("个人记忆"), `fallback must mention memory: ${message}`);
  }
});

// --- withinWatermark degrades safe (unparseable -> include, "宁可多披露") ------

test("withinWatermark includes a turn when either timestamp is unparseable", () => {
  assert.equal(withinWatermark("not-a-date", "2026-01-01T00:00:00Z"), true);
  assert.equal(withinWatermark("2026-01-01T00:00:00Z", "garbage"), true);
  assert.equal(withinWatermark("anything", ""), true); // empty watermark = include all
});
