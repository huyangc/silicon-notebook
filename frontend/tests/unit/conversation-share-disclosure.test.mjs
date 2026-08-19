import assert from "node:assert/strict";
import test from "node:test";

import {
  summarizeShareDisclosure,
  summarizeShareUpdate,
  withinWatermark,
} from "../../app/conversation-share-disclosure.ts";

// Minimal turn shaped like ConversationDetail["turns"][number]: only the fields
// summarizeShareDisclosure reads (created_at + response.anchors/citations).
function turn(createdAt, { anchors = [], citations = [] } = {}) {
  return {
    answer_id: `ans-${createdAt}`,
    question: "q",
    asked_at: createdAt,
    created_at: createdAt,
    response: { anchors, citations },
  };
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
  assert.equal(summarizeShareDisclosure(turns, "").memoryCount, 2);
});

test("a memory-backed citation is counted (the disclosure can never be zero when one exists)", () => {
  const turns = [turn("2026-01-01T00:00:00Z", { citations: [citation({ memory_id: "mem-x" })] })];
  assert.ok(summarizeShareDisclosure(turns, "").memoryCount > 0);
});

// --- Watermark filter excludes turns written after the share (freeze) ---------

test("turns after the watermark are counted as new, not included in shared counts", () => {
  const turns = [
    turn("2026-01-01T00:00:00Z", { citations: [citation({ memory_id: "mem-in" })] }),
    turn("2026-01-01T00:05:00Z", { citations: [citation({ memory_id: "mem-late" })] }),
  ];
  // Watermark at the first turn: the second (later) turn is "new", its memory
  // must NOT enter the disclosure — you only publish up to the watermark.
  const d = summarizeShareDisclosure(turns, "2026-01-01T00:00:00Z");
  assert.equal(d.sharedCount, 1);
  assert.equal(d.newCount, 1);
  assert.equal(d.memoryCount, 1); // only mem-in, not mem-late
});

test("empty watermark (unshared preview) counts every turn", () => {
  const turns = [
    turn("2026-01-01T00:00:00Z", { citations: [citation({ memory_id: "mem-1" })] }),
    turn("2026-01-01T00:05:00Z", { citations: [citation({ memory_id: "mem-2" })] }),
  ];
  const d = summarizeShareDisclosure(turns, "");
  assert.equal(d.sharedCount, 2);
  assert.equal(d.newCount, 0);
  assert.equal(d.memoryCount, 2);
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
  const watermark = "2026-01-01T00:00:00Z";
  // Current disclosure freezes at the watermark: only mem-in is public today.
  assert.equal(summarizeShareDisclosure(turns, watermark).memoryCount, 1);
  // Forward-looking: "更新到最新" would publish BOTH mem-in and mem-late.
  const preview = summarizeShareUpdate(turns, watermark);
  assert.equal(preview.afterUpdate.memoryCount, 2); // mem-late IS counted
  assert.equal(preview.newMemoryCount, 1);          // and flagged as newly exposed
});

test("summarizeShareUpdate flags newly exposed images from post-watermark turns", () => {
  const turns = [
    turn("2026-01-01T00:00:00Z", { citations: [citation({ images: [{ asset_id: "a1" }] })] }),
    turn("2026-01-01T00:05:00Z", { citations: [citation({ images: [{ asset_id: "a2" }] })] }),
  ];
  const preview = summarizeShareUpdate(turns, "2026-01-01T00:00:00Z");
  assert.equal(preview.afterUpdate.imageCount, 2);
  assert.equal(preview.newImageCount, 1);
});

test("summarizeShareUpdate reports no new memory when a post-watermark turn reuses an already-public id", () => {
  const turns = [
    turn("2026-01-01T00:00:00Z", { citations: [citation({ memory_id: "mem-a" })] }),
    turn("2026-01-01T00:05:00Z", { citations: [citation({ memory_id: "mem-a" })] }),
  ];
  const preview = summarizeShareUpdate(turns, "2026-01-01T00:00:00Z");
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
  assert.equal(summarizeShareDisclosure(turns, "").imageCount, 3);
});

// --- withinWatermark degrades safe (unparseable -> include, "宁可多披露") ------

test("withinWatermark includes a turn when either timestamp is unparseable", () => {
  assert.equal(withinWatermark("not-a-date", "2026-01-01T00:00:00Z"), true);
  assert.equal(withinWatermark("2026-01-01T00:00:00Z", "garbage"), true);
  assert.equal(withinWatermark("anything", ""), true); // empty watermark = include all
});
