import test from "node:test";
import assert from "node:assert/strict";

import {
  getReasoningTraceSummary,
  getTraceStepDetail,
} from "./reasoning-trace.ts";

test("summarizes the latest reasoning step for a collapsed trace row", () => {
  const steps = [
    { step_type: "plan", summary: "规划了 1 个子查询", detail: { sub_queries: [{ query: "Engram" }] } },
    { step_type: "retrieve", summary: "初检索得到 8 个候选节点", detail: { count: 8 } },
    { step_type: "answer", summary: "合成: 采用 9 个KG候选 + 0 段原文", detail: { kg: 9, elements: 0 } },
  ];

  assert.deepEqual(getReasoningTraceSummary(steps, true), {
    title: "Agent 推理中",
    latestLabel: "合成",
    latestSummary: "合成: 采用 9 个KG候选 + 0 段原文",
    latestDetail: "9 个 KG / 0 段原文",
    stepCountLabel: "3 步",
  });
});

test("uses concise detail labels for trace step payloads", () => {
  assert.equal(getTraceStepDetail({ step_type: "plan", summary: "", detail: { sub_queries: [{}, {}] } }), "2 个子查询");
  assert.equal(getTraceStepDetail({ step_type: "retrieve", summary: "", detail: { count: 8 } }), "8 个候选");
  assert.equal(getTraceStepDetail({ step_type: "expand", summary: "", detail: { found: 1 } }), "新增 1");
  assert.equal(getTraceStepDetail({ step_type: "reflect", summary: "", detail: { next_action: "answer" } }), "answer");
});

test("summarizes an empty live trace as waiting for backend events", () => {
  assert.deepEqual(getReasoningTraceSummary([], true), {
    title: "Agent 推理中",
    latestLabel: "",
    latestSummary: "等待后端事件…",
    latestDetail: "",
    stepCountLabel: "0 步",
  });
});
