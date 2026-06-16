import test from "node:test";
import assert from "node:assert/strict";

import {
  ASK_MODES, DEFAULT_ASK_MODE, ASK_MODE_GROUPS,
  askModeIds, groupOf, defaultModeForGroup, requiresKg, canUseMode, modeFromTurn,
} from "./ask-modes.ts";

test("user-facing ids and default", () => {
  assert.deepEqual(askModeIds(), ["chunk", "reasoning", "graph"]);
  assert.equal(DEFAULT_ASK_MODE, "chunk");
  assert.deepEqual(ASK_MODE_GROUPS.map((g) => g.id), ["general", "strict"]);
});

test("grouping + default engine per group", () => {
  assert.equal(groupOf("chunk"), "general");
  assert.equal(groupOf("graph"), "strict");
  assert.equal(defaultModeForGroup("general"), "chunk");
  assert.equal(defaultModeForGroup("strict"), "reasoning");   // groupDefault
});

test("kg gating", () => {
  assert.equal(requiresKg("chunk"), false);
  assert.equal(requiresKg("reasoning"), true);
  assert.equal(canUseMode("chunk", false), true);     // 通用问答无需 KG
  assert.equal(canUseMode("reasoning", false), false);
  assert.equal(canUseMode("graph", true), true);
});

test("restore mode from a prior turn (exact engine, safe fallback)", () => {
  assert.equal(modeFromTurn({ response: { mode: "graph" } }), "graph");
  assert.equal(modeFromTurn({ response: { mode: "reasoning" } }), "reasoning");
  assert.equal(modeFromTurn({ response: { mode: "fast" } }), "chunk");   // 非 user-facing → 兜底
  assert.equal(modeFromTurn({ response: {} }), "chunk");
  assert.equal(modeFromTurn(undefined), "chunk");
});
