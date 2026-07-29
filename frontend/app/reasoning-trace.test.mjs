import test from "node:test";
import assert from "node:assert/strict";

import {
  TRACE_STEP_LABELS,
  formatDuration,
  getReasoningTraceSummary,
  getTraceStepDetail,
} from "./reasoning-trace.ts";

test("summarizes the latest reasoning step for a collapsed trace row", () => {
  const steps = [
    { step_type: "plan", summary: "规划了 1 个子查询", detail: { sub_queries: [{ query: "Engram" }] } },
    { step_type: "retrieve", summary: "初检索得到 8 个候选节点", detail: { count: 8 } },
    { step_type: "answer", summary: "合成候选", detail: { kg: 9, elements: 0 } },
  ];

  assert.deepEqual(getReasoningTraceSummary(steps, true), {
    title: "Agent 推理中",
    latestLabel: "合成",
    latestSummary: "合成候选",
    latestDetail: "9 个知识对象 / 0 段原文",
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

// memory/synthesis 是后来补进轨迹的两段:一段是私有记忆参与了作答,另一段是
// 答案真的写出来了(整轮里通常最长的一段,此前完全不在轨迹里)。
test("记忆与作答两步有自己的短标签,detail 不落到通用「候选」口径", () => {
  const memory = { step_type: "memory", summary: "找到 3 条相关记忆", detail: { count: 3 } };
  assert.equal(getReasoningTraceSummary([memory], true).latestLabel, "记忆");
  // 通用分支会把 count 读成「N 个候选」—— 记忆数的不是候选。
  assert.equal(getTraceStepDetail(memory), "3 条记忆");

  const synthesis = {
    step_type: "synthesis",
    summary: "已生成答案，引用 12 处证据",
    detail: { citations: 12, anchors: 5, evidence_level: "grounded" },
  };
  assert.equal(getReasoningTraceSummary([synthesis], false).latestLabel, "作答");
  // 取 anchors(模型真正绑上的 [k]),不取 citations —— 后者是「每条检索到的证据
  // 一张卡」,零绑定的回答上会读成「12 处引用」。
  assert.equal(getTraceStepDetail(synthesis), "5 处引用");
  assert.equal(
    getTraceStepDetail({ ...synthesis, detail: { citations: 12 } }), "",
  );
});

test("答案合成的耗时进入轨迹总耗时(此前整段不计,总耗时系统性偏低)", () => {
  const steps = [
    { step_type: "intent", summary: "已理解问题", detail: {}, duration_ms: 1500 },
    { step_type: "answer", summary: "合成", detail: { kg: 9 }, duration_ms: 500 },
    { step_type: "synthesis", summary: "已生成答案", detail: { citations: 2 }, duration_ms: 8000 },
  ];
  assert.equal(getReasoningTraceSummary(steps, false).totalLabel, "10.0s");
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
  assert.equal(getTraceStepDetail({
    step_type: "intent",
    summary: "理解",
    detail: { resolved_question: "分析电荷泵 PLL 的锁定问题" },
  }), "分析电荷泵 PLL 的锁定问题");
  assert.equal(getTraceStepDetail({ step_type: "plan", summary: "", detail: { sub_queries: [{}, {}] } }), "2 个子查询");
  assert.equal(getTraceStepDetail({ step_type: "retrieve", summary: "", detail: { count: 8 } }), "8 个候选");
  assert.equal(getTraceStepDetail({ step_type: "expand", summary: "", detail: { found: 1 } }), "新增 1");
  assert.equal(getTraceStepDetail({ step_type: "reflect", summary: "", detail: { next_action: "answer" } }), "开始作答");
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

test("next_action 不把状态机动作名直出给用户,而是显示中文人话", () => {
  assert.equal(
    getTraceStepDetail({ step_type: "reflect", summary: "", detail: { next_action: "expand_graph" } }),
    "顺着相关内容继续找",
  );
  assert.equal(
    getTraceStepDetail({ step_type: "reflect", summary: "", detail: { next_action: "add_subquery" } }),
    "换个角度再查一遍",
  );
});

test("未知 next_action 不显示,而不是显示原值", () => {
  assert.equal(
    getTraceStepDetail({ step_type: "reflect", summary: "", detail: { next_action: "brand_new_action" } }),
    "",
  );
});

test("latestLabel 遇到未知 step_type 退到中性词,不直出英文", () => {
  assert.equal(
    getReasoningTraceSummary(
      [{ step_type: "brand_new_step_type", summary: "", detail: {} }],
      true,
    ).latestLabel,
    "处理中",
  );
});

test("NEXT_ACTION 覆盖后端全部 10 个真实取值(非机制名)", () => {
  // 真源是 reasoning_retrieval.py 里 reflect 循环的 next_action if/elif 分发链
  // (不按行号钉——本仓库行号指针已知会腐烂;按分支内容定位,见 reasoning-trace.ts
  // 顶部 NEXT_ACTION 上方注释)。精确查找通道加了 exact_lookup,PR-2 加了
  // enumerate_elements/enumerate_kg_objects,原为 7 个。后端加第 11 个值时这条会
  // 提醒补。
  const cases = {
    answer: "开始作答", expand_graph: "顺着相关内容继续找", add_subquery: "换个角度再查一遍",
    search_elements: "回原文里找细节", enumerate_elements: "列元素清单",
    enumerate_kg_objects: "列知识对象清单", ppr_retrieve: "顺着关联扩大范围",
    expand_community: "找相似内容对比", follow_chain: "顺着推导链继续",
    exact_lookup: "按名称精确查找",
  };
  for (const [action, zh] of Object.entries(cases)) {
    const out = getTraceStepDetail({ step_type: "reflect", detail: { next_action: action } });
    assert.equal(out, zh, `${action} 未译或译错`);
    assert.notEqual(out, action, `${action} 泄漏了英文机制名`);
  }
});

// 精确查找步是后端新增的一段轨迹(seed pass 与 agent 动作共用 step_type)。缺标签
// 会回退成 "exact_lookup" 直出英文机制名,撑爆 48px 徽章列。
test("exact_lookup 有短标签,detail 显示查了哪个名称、捞回多少段", () => {
  const seed = {
    step_type: "exact_lookup",
    summary: "按名称精确查找:命中 7 段原文",
    detail: { terms: ["set_db"], found: 7, phase: "seed" },
  };
  assert.equal(getReasoningTraceSummary([seed], true).latestLabel, "精查");
  assert.equal(getTraceStepDetail(seed), "set_db · 新增 7 段");

  // 多名称、以及只有其中一半字段可用时都不能露出空壳分隔符。
  assert.equal(
    getTraceStepDetail({
      step_type: "exact_lookup",
      summary: "",
      detail: { terms: ["set_db", "place_opt_design"], found: 0, phase: "reflect" },
    }),
    "set_db、place_opt_design · 新增 0 段",
  );
  assert.equal(
    getTraceStepDetail({ step_type: "exact_lookup", summary: "", detail: { found: 3 } }),
    "新增 3 段",
  );
  assert.equal(
    getTraceStepDetail({
      step_type: "exact_lookup",
      summary: "",
      detail: { terms: ["set_db", 42, ""] },
    }),
    "set_db",
  );
  assert.equal(
    getTraceStepDetail({ step_type: "exact_lookup", summary: "", detail: {} }),
    "",
  );
});

test("新增 exact_lookup 后,未知 step_type / next_action 的兜底仍成立(精确匹配,不前缀命中)", () => {
  assert.equal(
    getReasoningTraceSummary(
      [{ step_type: "exact_lookup_v2", summary: "", detail: { terms: ["x"], found: 1 } }],
      true,
    ).latestLabel,
    "处理中",
  );
  // 兜底走通用分支,不能被新的 exact_lookup 专用分支前缀吃掉。
  assert.equal(
    getTraceStepDetail({ step_type: "exact_lookup_v2", summary: "", detail: { found: 1 } }),
    "新增 1",
  );
  assert.equal(
    getTraceStepDetail({ step_type: "reflect", summary: "", detail: { next_action: "exact_lookup_v2" } }),
    "",
  );
});

// PR-2 T6:集合枚举工具的 enumerate 步用 detail.collection 存在与否与 Knowhow 的
// scanned_rows/known_total_rows 分支区分,数字读法(条目 vs 行、分母可能未知)不同,
// 混用会把「12 条」渲染成「12/0 行」。
test("集合枚举 enumerate 步:分母已知时渲染「N/M 条」", () => {
  assert.equal(
    getTraceStepDetail({
      step_type: "enumerate",
      summary: "枚举公式清单: 部分结果,已达本轮上限,累计 12 条/共 40",
      detail: {
        collection: "elements", kind: "formula", returned_total: 12, total: 40,
        complete: false, truncated_reason: "budget",
      },
    }),
    "12/40 条",
  );
});

test("集合枚举 enumerate 步:分母未知(total=null)不得渲染成 /0", () => {
  const out = getTraceStepDetail({
    step_type: "enumerate",
    summary: "枚举概念知识对象清单: 部分结果,已达本轮上限,累计 30 条",
    detail: {
      collection: "kg_objects", kind: "concept", returned_total: 30, total: null,
      complete: false, truncated_reason: "budget",
    },
  });
  assert.equal(out, "30 条（总数未知）");
  assert.ok(!out.includes("/0"), "分母未知不得写成 /0");
});

test("集合枚举 enumerate 步:complete 时渲染「已全部列出」而不重复条数", () => {
  assert.equal(
    getTraceStepDetail({
      step_type: "enumerate",
      summary: "枚举图片清单: 已全部列出 5 条",
      detail: {
        collection: "elements", kind: "image", returned_total: 5, total: 5,
        complete: true, truncated_reason: "",
      },
    }),
    "已全部列出",
  );
});

test("Knowhow 的 scanned_rows 分支与集合枚举分支不串台(仍走各自数字读法)", () => {
  assert.equal(
    getTraceStepDetail({
      step_type: "enumerate",
      summary: "枚举方法清单表: 12/100 行",
      detail: { scanned_rows: 12, known_total_rows: 100 },
    }),
    "12/100 行",
  );
});

test("跳过步:未执行的已确认检索方向给出数量,不被通用分支读成候选数", () => {
  const skipped = {
    step_type: "skip",
    summary: "检索预算不足,以下已确认方向未能执行:方向四、方向五",
    detail: {
      reason: "intent_coverage_incomplete",
      pending: 2,
      directions: ["方向四", "方向五"],
    },
  };
  assert.equal(getTraceStepDetail(skipped), "2 个方向未执行");
  assert.equal(TRACE_STEP_LABELS.skip, "跳过");
  // 其他跳过步(熔断、重复子查询…)不带 pending,继续走既有分支/空兜底。
  assert.equal(
    getTraceStepDetail({ step_type: "skip", summary: "", detail: { reason: "stale_circuit_breaker" } }),
    "",
  );
  assert.equal(
    getTraceStepDetail({ step_type: "skip", summary: "", detail: { reason: "x", count: 3 } }),
    "3 个候选",
  );
});
