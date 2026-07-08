import test from "node:test";
import assert from "node:assert/strict";
import { jobPollDone, newTraceSteps } from "./ask-reconnect.ts";

test("jobPollDone: 仅 running 未完成", () => {
  assert.equal(jobPollDone("running"), false);
  assert.equal(jobPollDone("done"), true);
  assert.equal(jobPollDone("cancelled"), true);
  assert.equal(jobPollDone("failed"), true);
  assert.equal(jobPollDone("interrupted"), true);
});

test("newTraceSteps: 只取已见之后的新步", () => {
  const persisted = [{ step_type: "a", summary: "", detail: {} }, { step_type: "b", summary: "", detail: {} }, { step_type: "c", summary: "", detail: {} }];
  assert.deepEqual(newTraceSteps(persisted, 1).map((s) => s.step_type), ["b", "c"]);
  assert.deepEqual(newTraceSteps(persisted, 3), []);
  assert.deepEqual(newTraceSteps(persisted, 5), []);   // 防越界
  assert.deepEqual(newTraceSteps([], 0), []);
});
