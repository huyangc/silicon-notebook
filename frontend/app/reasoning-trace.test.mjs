import test from "node:test";
import assert from "node:assert/strict";

import {
  formatDuration,
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
    totalLabel: "",
  });
});

test("expand_community/ppr 有短标签(不回退长英文串→徽章不溢出压摘要)", () => {
  assert.equal(
    getReasoningTraceSummary(
      [{ step_type: "expand_community", summary: "横向对比:纳入 8 个同社区实体,新增候选 48" }],
      true,
    ).latestLabel,
    "对比",   // 映射存在;若缺失会回退 "expand_community" 撑爆 48px 徽章列
  );
  assert.equal(
    getReasoningTraceSummary(
      [{ step_type: "ppr", summary: "概念漫游:跨文档检索,得到 20 段原文" }],
      true,
    ).latestLabel,
    "漫游",
  );
});

test("follow_chain uses a concise label and renders hops, inference count, and trust", () => {
  const step = {
    step_type: "follow_chain",
    summary: "沿关系链形成 3 条查询期推论",
    detail: { hops: 2, count: 3, chain_trust: 0.784 },
  };

  assert.equal(getReasoningTraceSummary([step], true).latestLabel, "推导");
  assert.equal(getTraceStepDetail(step), "2 跳 · 3 条 · 可信度 78%");
});

test("follow_chain detail omits unavailable metrics and clamps trust percentages", () => {
  assert.equal(
    getTraceStepDetail({
      step_type: "follow_chain",
      summary: "未形成推论",
      detail: { hops: 2, count: 0 },
    }),
    "2 跳 · 0 条",
  );
  assert.equal(
    getTraceStepDetail({
      step_type: "follow_chain",
      summary: "推论可信度",
      detail: { chain_trust: 1.2 },
    }),
    "可信度 100%",
  );
});

test("sums per-step durations into a total label for the collapsed row", () => {
  const steps = [
    { step_type: "plan", summary: "规划", detail: {}, duration_ms: 1200 },
    { step_type: "retrieve", summary: "检索", detail: { count: 8 }, duration_ms: 800 },
    { step_type: "answer", summary: "合成", detail: { kg: 9 }, duration_ms: 10340 },
  ];
  // 1200 + 800 + 10340 = 12340ms -> 12.3s
  assert.equal(getReasoningTraceSummary(steps, false).totalLabel, "12.3s");
});

test("omits the total label when no step carries a duration", () => {
  const steps = [{ step_type: "plan", summary: "规划", detail: {} }];
  assert.equal(getReasoningTraceSummary(steps, false).totalLabel, "");
});

test("formatDuration renders ms / seconds / minutes buckets", () => {
  assert.equal(formatDuration(0), "0ms");
  assert.equal(formatDuration(820), "820ms");
  assert.equal(formatDuration(999), "999ms");
  assert.equal(formatDuration(1000), "1.0s");
  assert.equal(formatDuration(1200), "1.2s");
  assert.equal(formatDuration(12340), "12.3s");
  assert.equal(formatDuration(60000), "1m0s");
  assert.equal(formatDuration(63000), "1m3s");
  assert.equal(formatDuration(119600), "2m0s");
});

test("formatDuration guards against non-finite and negative inputs", () => {
  assert.equal(formatDuration(-5), "0ms");
  assert.equal(formatDuration(NaN), "0ms");
  assert.equal(formatDuration(Infinity), "0ms");
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
    totalLabel: "",
  });
});
