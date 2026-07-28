import test from "node:test";
import assert from "node:assert/strict";

import {
  elapsedMs,
  intentClarifyStep,
  intentConfirmedStep,
  intentUnderstandingStep,
  intentUnderstoodStep,
  replaceLastIntentStep,
} from "./ask-intent-trace.ts";
import { TRACE_STEP_LABELS, getTraceStepDetail } from "./reasoning-trace.ts";


function contract(overrides = {}) {
  return {
    objective: "",
    resolved_question: "锁相环带宽如何选取",
    intent_type: "explain",
    result_scope: "ranked",
    completeness_required: false,
    entities: [],
    mandatory_topics: [],
    comparison_axes: [],
    constraints: [],
    excluded_topics: [],
    expected_output: "",
    assumptions: [],
    ambiguities: [],
    confidence: 0.9,
    needs_clarification: false,
    confirmed: false,
    ...overrides,
  };
}


test("理解阶段的步骤走轨迹面板的既有渲染路径(不是另造一种展示)", () => {
  const steps = [
    intentUnderstandingStep(),
    intentUnderstoodStep(contract(), 1200),
    intentClarifyStep(contract({ ambiguities: [{ id: "a", question: "指哪一代?" }] }), 900),
    intentConfirmedStep("锁相环带宽如何选取", 1),
  ];
  for (const step of steps) {
    // 标签映射缺失会回退成英文 step_type 撑爆折叠行的徽章列。
    assert.equal(TRACE_STEP_LABELS[step.step_type], "理解");
  }
  assert.equal(getTraceStepDetail(steps[1]), "锁相环带宽如何选取");
});


test("在途那一步明说尚未读取资料 —— 它取代了原来输入框上方的灰条提示", () => {
  const step = intentUnderstandingStep();
  assert.match(step.summary, /尚未读取资料或开始检索/);
  // 在途步骤不带耗时:还没跑完,写 0 会让总耗时读起来像已经结束。
  assert.equal(step.duration_ms, undefined);
});


test("理解完成带上真实耗时,必答要点数进摘要", () => {
  const plain = intentUnderstoodStep(contract(), 1234);
  assert.equal(plain.summary, "已理解问题");
  assert.equal(plain.duration_ms, 1234);

  const withTopics = intentUnderstoodStep(
    contract({ mandatory_topics: [{ id: "t1", title: "", question: "q", retrieval_queries: [] },
                                  { id: "t2", title: "", question: "q", retrieval_queries: [] }] }),
    50,
  );
  assert.match(withTopics.summary, /2 个必答要点/);
});


test("待澄清那一步报歧义条数,让用户知道下面的卡片要填几处", () => {
  const step = intentClarifyStep(
    contract({
      needs_clarification: true,
      ambiguities: [{ id: "a", question: "哪一代?" }, { id: "b", question: "哪个工艺?" }],
    }),
    700,
  );
  assert.match(step.summary, /2 处/);
  assert.equal(step.duration_ms, 700);
});


test("用户定稿那一步刻意不带耗时(填表是人的时间,不是系统耗时)", () => {
  const step = intentConfirmedStep("最终问题", 2);
  assert.equal(step.duration_ms, undefined);
  assert.match(step.summary, /补充了 2 条说明/);
  assert.equal(intentConfirmedStep("最终问题", 0).summary, "已确认最终问题");
});


test("替换只动最后一步 —— 轨迹不留「正在理解」的残影", () => {
  const resolved = intentUnderstoodStep(contract(), 10);
  assert.deepEqual(
    replaceLastIntentStep([{ step_type: "start", summary: "启动检索", detail: {} },
                           intentUnderstandingStep()], resolved),
    [{ step_type: "start", summary: "启动检索", detail: {} }, resolved],
  );
  // 空轨迹也不能抛/不能丢掉替换进来的那一步。
  assert.deepEqual(replaceLastIntentStep([], resolved), [resolved]);
});


test("耗时非有限/时钟回拨一律归零,不把负数泄漏进总耗时", () => {
  assert.equal(elapsedMs(100, 350), 250);
  assert.equal(elapsedMs(500, 100), 0);
  assert.equal(elapsedMs(Number.NaN, 100), 0);
  assert.equal(elapsedMs(0, Number.POSITIVE_INFINITY), 0);
});
