import assert from "node:assert/strict";
import test from "node:test";

import {
  summarizeShareDisclosure,
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
