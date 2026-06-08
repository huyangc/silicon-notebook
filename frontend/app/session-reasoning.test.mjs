import test from "node:test";
import assert from "node:assert/strict";

import { lastTurnUsedReasoning } from "./session-reasoning.ts";

const fast = { response: {} };
const fastEmptyTrace = { response: { reasoning_trace: [] } };
const reasoning = { response: { reasoning_trace: [{ step_type: "answer", summary: "s" }] } };

test("空会话 → false", () => {
  assert.equal(lastTurnUsedReasoning([]), false);
});

test("最后一轮快速(无 trace) → false", () => {
  assert.equal(lastTurnUsedReasoning([reasoning, fast]), false);
});

test("最后一轮推理(非空 trace) → true", () => {
  assert.equal(lastTurnUsedReasoning([fast, reasoning]), true);
});

test("最后一轮 trace 为空数组 → false", () => {
  assert.equal(lastTurnUsedReasoning([fastEmptyTrace]), false);
});

test("单条推理 → true", () => {
  assert.equal(lastTurnUsedReasoning([reasoning]), true);
});
