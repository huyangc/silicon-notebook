import test from "node:test";
import assert from "node:assert/strict";

import {
  TRACE_STEP_LABELS,
  formatDuration,
  getReasoningTraceSummary,
  getTraceStepDetail,
} from "../../app/reasoning-trace.ts";

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

test("plugin trace uses the generic label and discloses bounded truncation", () => {
  const step = {
    step_type: "plugin",
    summary: "部署检索",
    detail: { detail: "完成企业检索", truncated: true },
  };
  assert.equal(TRACE_STEP_LABELS.plugin, "扩展");
  assert.equal(getReasoningTraceSummary([step], false).latestLabel, "扩展");
  assert.equal(getTraceStepDetail(step), "完成企业检索 · 后续步骤已截断");
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

test("历史回答中的来源子图内部步骤不向用户显示", () => {
  const internal = {
    step_type: "source_subgraph",
    summary: "来源子图：shadow，新增 4 条证据",
    detail: { state: "shadow", selected_source_count: 1, enrichment_count: 4 },
  };
  const retrieve = { step_type: "retrieve", summary: "检索完成", detail: { hits: 3 } };
  const onlyInternal = getReasoningTraceSummary([internal], true);
  assert.equal(onlyInternal.latestLabel, "");
  assert.equal(onlyInternal.stepCountLabel, "0 步");
  const mixed = getReasoningTraceSummary([retrieve, internal], false);
  assert.equal(mixed.latestLabel, "检索");
  assert.equal(mixed.latestSummary, "检索完成");
  assert.equal(mixed.stepCountLabel, "1 步");
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

test("NEXT_ACTION 覆盖后端全部真实取值(非机制名)", () => {
  // 真源是 reasoning_retrieval.py 里 reflect 循环的 next_action if/elif 分发链
  // (不按行号钉——本仓库行号指针已知会腐烂;按分支内容定位,见 reasoning-trace.ts
  // 顶部 NEXT_ACTION 上方注释)。精确查找通道加了 exact_lookup,PR-2 加了
  // enumerate_elements/enumerate_kg_objects,原为 7 个。PR-2.5 的来源清单是
  // enumerate 动作的一个参数值而不是第 11 个动作,所以那次没有改这张表。O1 的
  // update_outline(大纲便签)是货真价实的第 11 个动作 id,那次补上了这条注释。
  // Agentic Memory P4 的 consult_memory(仅 deep 及以上档且经验注入闸开启时出现)
  // 是第 12 个,这条随之补上。
  // search_evidence 曾短暂作为一个动作存在(模型判断来源那一版),随该特性整体
  // 移除;它是这张表**减少**过的唯一一次,所以这里也留个记号:动作被删时同样要改这张表。
  const cases = {
    answer: "开始作答", expand_graph: "顺着相关内容继续找", add_subquery: "换个角度再查一遍",
    search_elements: "回原文里找细节", enumerate_elements: "列元素清单",
    enumerate_kg_objects: "列知识对象清单",
    ppr_retrieve: "顺着关联扩大范围",
    expand_community: "找相似内容对比", follow_chain: "顺着推导链继续",
    exact_lookup: "按名称精确查找", update_outline: "整理大纲",
    consult_memory: "回想以往的查法",
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

// O1(大纲便签,设计文档 §3.1):update_outline 落的 outline 步。缺标签会回退成
// "outline" 直出英文机制名;detail 按「N 节(M 节待补)」给出比 summary 更短的复述。
test("outline 步(大纲便签)有短标签,detail 按节数/待补节数渲染", () => {
  const step = {
    step_type: "outline",
    summary: "更新大纲: 3 节(1 节待补证据)",
    detail: {
      sections: [
        { id: "s1", title: "背景", parent: null, evidence: ["k1"] },
        { id: "s2", title: "方法", parent: null, evidence: ["k2", "k3"] },
        { id: "s3", title: "尚待检索", parent: null, evidence: [] },
      ],
      empty_sections: ["尚待检索"],
      replaced: 0,
      dropped_evidence: 0,
      changed: true,
    },
  };
  assert.equal(getReasoningTraceSummary([step], true).latestLabel, "大纲");
  assert.equal(getTraceStepDetail(step), "3 节(1 节待补)");

  // 零值形态要稳:全部节都已绑证据时不写成 "3 节(undefined 节待补)"。
  assert.equal(
    getTraceStepDetail({
      step_type: "outline",
      summary: "更新大纲: 2 节(0 节待补证据)",
      detail: { sections: [{}, {}], empty_sections: [] },
    }),
    "2 节(0 节待补)",
  );
  // 畸形 detail(缺字段)不抛异常,退化为 "0 节(0 节待补)"。
  assert.equal(
    getTraceStepDetail({ step_type: "outline", summary: "", detail: {} }),
    "0 节(0 节待补)",
  );
});

// O2(按节合成,设计文档 §3.1):每节写完发一条进度步,复用 synthesis 这个
// step_type,但 detail 没有 anchors —— 若落到既有的 anchors 分支会读成空字符串,
// 白白丢掉「写到第几节了」这条本该有的进度文案。
test("按节合成的进度步(synthesis 类型,不带 anchors)显示第几节", () => {
  const step = {
    step_type: "synthesis",
    summary: "已写完第 2/共 3 节",
    detail: { section_index: 2, section_total: 3, section_title: "方法" },
  };
  assert.equal(getReasoningTraceSummary([step], true).latestLabel, "作答");
  assert.equal(getTraceStepDetail(step), "第 2/共 3 节");

  // 既有的收尾 synthesis 步(带 anchors,不带 section_index)分支必须逐字不变。
  assert.equal(
    getTraceStepDetail({
      step_type: "synthesis",
      detail: { citations: 12, anchors: 5, evidence_level: "grounded" },
    }),
    "5 处引用",
  );
  // 两个形状都缺时兜底为空,不抛异常。
  assert.equal(getTraceStepDetail({ step_type: "synthesis", detail: {} }), "");
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

// PR-2.5:来源清单是第三个 collection。它的 kind 恒为空串,所以渲染分支必须只
// 依赖 detail.collection 是否非空 + returned_total 是否为数字——若哪天改成按 kind
// 判分支,来源清单的 detail 就会掉进「无可显示」的兜底,轨迹上只剩一句摘要。
test("集合枚举 enumerate 步:来源清单(kind 为空串)照常渲染条数", () => {
  assert.equal(
    getTraceStepDetail({
      step_type: "enumerate",
      summary: "枚举来源清单: 已全部列出 7 条",
      detail: {
        collection: "sources", kind: "", returned_total: 7, total: 7,
        complete: true, truncated_reason: "",
      },
    }),
    "已全部列出",
  );
  assert.equal(
    getTraceStepDetail({
      step_type: "enumerate",
      summary: "枚举来源清单: 部分结果,已达本轮上限,累计 4 条/共 12",
      detail: {
        collection: "sources", kind: "", returned_total: 4, total: 12,
        complete: false, truncated_reason: "budget",
      },
    }),
    "4/12 条",
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

// codex r5:outline_skipped(大纲里有节但证据不足未进答案)与 ungrounded_sections
// (成节但没过依据门)是按节合成的诚实披露,必须上屏——不显示的话用户拿到的是
// 一份「看起来完整」的多节答案,却不知道少了哪节/哪节依据不足。
test("收尾合成步渲染略过节与依据不足节的披露(有界:前 3 个标题+计数)", () => {
  const base = { step_type: "synthesis", summary: "已生成答案" };
  assert.equal(
    getTraceStepDetail({
      ...base,
      detail: { anchors: 5, outline_skipped: ["背景"], ungrounded_sections: [] },
    }),
    "5 处引用 · 证据不足略过 1 节: 背景",
  );
  assert.equal(
    getTraceStepDetail({
      ...base,
      detail: { anchors: 3, outline_skipped: [], ungrounded_sections: ["展望", "结论"] },
    }),
    "3 处引用 · 2 节依据不足: 展望、结论",
  );
  // 超过 3 个标题只点名前 3 个,余下用计数收口,折叠行不被 12×60 字符撑爆。
  assert.equal(
    getTraceStepDetail({
      ...base,
      detail: {
        anchors: 8,
        outline_skipped: ["甲", "乙", "丙", "丁", "戊"],
        ungrounded_sections: [],
      },
    }),
    "8 处引用 · 证据不足略过 5 节: 甲、乙、丙 等 5 节",
  );
  // 畸形 detail(非字符串成员)被过滤,不上屏也不抛异常;两份列表都空时逐字
  // 回到既有「N 处引用」形态。
  assert.equal(
    getTraceStepDetail({
      ...base,
      detail: { anchors: 5, outline_skipped: [7, ""], ungrounded_sections: null },
    }),
    "5 处引用",
  );
});

// Agentic Memory P1:Agent 对这个库的已有理解进了这一轮的规划/反思。缺标签会
// 回退成 "profile" 直出英文机制名,撑爆 48px 徽章列。
test("profile 有短标签,detail 说清带了几条已有理解", () => {
  const step = {
    step_type: "profile",
    summary: "带上对这个库的已有理解",
    detail: { blocks: 3, chars: 218 },
  };
  assert.equal(getReasoningTraceSummary([step], true).latestLabel, "经验");
  assert.equal(getTraceStepDetail(step), "3 条已有理解");

  // blocks 缺失/非数字时给空串,绝不上屏 "undefined 条已有理解"。
  assert.equal(getTraceStepDetail({ step_type: "profile", summary: "", detail: {} }), "");
  assert.equal(
    getTraceStepDetail({ step_type: "profile", summary: "", detail: { blocks: "3" } }),
    "",
  );
  // chars 单独存在也不足以渲染:它是整块字符数,不是「几条」。
  assert.equal(
    getTraceStepDetail({ step_type: "profile", summary: "", detail: { chars: 218 } }),
    "",
  );
});

// 分支顺序守卫:profile 必须排在通用 `detail.count` 分支之前(同 memory/synthesis
// 的理由)。哪天 profile 的 detail 多出一个 count 键,排在后面就会被静默渲染成
// 「N 个候选」——一个读起来对、其实完全错位的数。
test("profile 分支先于通用 count 分支命中", () => {
  assert.equal(
    getTraceStepDetail({
      step_type: "profile",
      summary: "",
      detail: { blocks: 2, count: 99 },
    }),
    "2 条已有理解",
  );
});

// Agentic Memory P2:以往检索攒下的「打法」进了这一轮的规划/反思。标签必须与
// profile 的「经验」区分开——两步同名会让轨迹里出现两条读起来一样、说的却是两
// 回事的步。
test("experience 有自己的短标签,且与 profile 不同名", () => {
  const step = {
    step_type: "experience",
    summary: "带上以往检索攒下的打法",
    detail: { entries: 2, chars: 318 },
  };
  assert.equal(getReasoningTraceSummary([step], true).latestLabel, "打法");
  assert.equal(getTraceStepDetail(step), "2 条打法");
  assert.notEqual(TRACE_STEP_LABELS.experience, TRACE_STEP_LABELS.profile);

  // entries 缺失/非数字时给空串,绝不上屏 "undefined 条打法"。
  assert.equal(
    getTraceStepDetail({ step_type: "experience", summary: "", detail: {} }),
    "",
  );
  assert.equal(
    getTraceStepDetail({ step_type: "experience", summary: "", detail: { entries: "2" } }),
    "",
  );
  // chars 单独存在也不足以渲染:它是整块字符数,不是「几条」。
  assert.equal(
    getTraceStepDetail({ step_type: "experience", summary: "", detail: { chars: 318 } }),
    "",
  );
});

// 分支顺序守卫(同 profile 的理由):experience 必须排在通用 `detail.count`
// 分支之前,否则哪天它的 detail 多出一个 count 键就会被渲染成「N 个候选」。
test("experience 分支先于通用 count 分支命中", () => {
  assert.equal(
    getTraceStepDetail({
      step_type: "experience",
      summary: "",
      detail: { entries: 1, count: 99 },
    }),
    "1 条打法",
  );
});

// Agentic Memory P4(T5):reflect 循环里模型主动拉取的一次经验查询。标签必须
// 与 memory("记忆")/profile("经验")/experience("打法")互不相同——四步各自说的
// 是不同的事(记忆召回 / 库理解 / 打法背景 / 主动回想),同名会让轨迹读不出区别。
test("consult_memory 有自己的短标签,且与另外三步不同名", () => {
  const step = {
    step_type: "consult_memory",
    summary: "回想以往检索打法,新增 2 条",
    detail: { entries: 2, chars: 118 },
  };
  assert.equal(getReasoningTraceSummary([step], true).latestLabel, "回想");
  assert.equal(getTraceStepDetail(step), "2 条打法");
  assert.notEqual(TRACE_STEP_LABELS.consult_memory, TRACE_STEP_LABELS.memory);
  assert.notEqual(TRACE_STEP_LABELS.consult_memory, TRACE_STEP_LABELS.profile);
  assert.notEqual(TRACE_STEP_LABELS.consult_memory, TRACE_STEP_LABELS.experience);
  assert.notEqual(TRACE_STEP_LABELS.consult_memory, TRACE_STEP_LABELS.answer);

  // entries 缺失/非数字时给空串,绝不上屏 "undefined 条打法"。
  assert.equal(
    getTraceStepDetail({ step_type: "consult_memory", summary: "", detail: {} }),
    "",
  );
  assert.equal(
    getTraceStepDetail({ step_type: "consult_memory", summary: "", detail: { entries: "2" } }),
    "",
  );
  // chars 单独存在也不足以渲染:它是整块字符数,不是「几条」。后端 detail 里
  // 没有独立的「心得条数」字段(本人覆盖层那句话只是块内附加的一行文字),所以
  // 这里不该渲染出任何"M 条心得"字样。
  assert.equal(
    getTraceStepDetail({ step_type: "consult_memory", summary: "", detail: { chars: 118 } }),
    "",
  );
  // entries=0(只带出覆盖层那句话、经验库没有新条目)仍是合法数字,照常渲染。
  assert.equal(
    getTraceStepDetail({ step_type: "consult_memory", summary: "", detail: { entries: 0 } }),
    "0 条打法",
  );
});

// 分支顺序守卫(同 profile/experience 的理由):consult_memory 必须排在通用
// `detail.count` 分支之前,否则哪天它的 detail 多出一个 count 键就会被渲染成
// 「N 个候选」——一个读起来对、其实完全错位的数。
test("consult_memory 分支先于通用 count 分支命中", () => {
  assert.equal(
    getTraceStepDetail({
      step_type: "consult_memory",
      summary: "",
      detail: { entries: 1, count: 99 },
    }),
    "1 条打法",
  );
});

// 站外来源建议(ask.gap_consult,X9 PR-A T3):这一步问的是「这个库以外还有什么」,
// 与 memory("记忆"/召回本笔记本内容)是完全不同的东西——同名会让轨迹读起来像是
// 又召回了一批笔记本内证据,而这些建议从未参与检索。
test("gap_consult 有自己的短标签,且与 memory 不同名,detail 说清带回了几条建议", () => {
  const step = {
    step_type: "gap_consult",
    summary: "外扩检索:笔记本之外有 3 条相关建议",
    detail: { count: 3 },
  };
  assert.equal(getReasoningTraceSummary([step], true).latestLabel, "外扩");
  assert.equal(getTraceStepDetail(step), "3 条建议");
  assert.notEqual(TRACE_STEP_LABELS.gap_consult, TRACE_STEP_LABELS.memory);

  // count 缺失/非数字时给空串,绝不上屏 "undefined 条建议"。
  assert.equal(getTraceStepDetail({ step_type: "gap_consult", summary: "", detail: {} }), "");
  assert.equal(
    getTraceStepDetail({ step_type: "gap_consult", summary: "", detail: { count: "3" } }),
    "",
  );
  // count=0(问过部署插件,一条建议都没拿回)仍是合法数字,照常渲染。
  assert.equal(
    getTraceStepDetail({ step_type: "gap_consult", summary: "", detail: { count: 0 } }),
    "0 条建议",
  );
});

// 分支顺序守卫(同 profile/experience/consult_memory 的理由):gap_consult 必须
// 排在通用 `detail.count` 分支之前——它们读的恰好是同一个键名 `count`,但含义
// 完全不同(站外建议数 vs 笔记本内候选数),顺序颠倒会让两者的措辞互相顶替而
// 不是「静默多出一个键才会被顶替」,比 profile/experience 那几支更容易踩空。
test("gap_consult 分支先于通用 count 分支命中(且两者共用同一个键名)", () => {
  assert.equal(
    getTraceStepDetail({ step_type: "gap_consult", summary: "", detail: { count: 3 } }),
    "3 条建议",
  );
});

// Agentic Memory P4(T1/T2):result_ids/anchor_evidence_ids 是 step→anchor 归因
// 的原始材料,经 ports.py 的 project_run 无条件写进多类步骤的 detail(包括零命中
// 的空列表)。它们只是不透明句柄,不该改变任何一步既有的折叠态展示——钉住"A 不
// 上屏":同一份 detail 加上/不加这两个键,getTraceStepDetail 的输出必须逐字相同。
test("result_ids/anchor_evidence_ids 出现在 detail 里不改变既有渲染(不上屏)", () => {
  const ids = ["ko-aaaaaaaaaaaaaaaa", "ko-bbbbbbbbbbbbbbbb"];

  const retrieveBase = { step_type: "retrieve", summary: "", detail: { count: 4 } };
  const retrieveWithIds = {
    step_type: "retrieve",
    summary: "",
    detail: { count: 4, result_ids: ids },
  };
  assert.equal(getTraceStepDetail(retrieveWithIds), getTraceStepDetail(retrieveBase));

  const pprBase = { step_type: "ppr", summary: "", detail: { found: 3, phase: "seed" } };
  const pprWithIds = {
    step_type: "ppr",
    summary: "",
    detail: { found: 3, phase: "seed", result_ids: ids },
  };
  assert.equal(getTraceStepDetail(pprWithIds), getTraceStepDetail(pprBase));

  const expandBase = {
    step_type: "expand",
    summary: "",
    detail: { object_id: "ko-x", name: "示例节点", found: 2 },
  };
  const expandWithIds = {
    step_type: "expand",
    summary: "",
    detail: { object_id: "ko-x", name: "示例节点", found: 2, result_ids: ids },
  };
  assert.equal(getTraceStepDetail(expandWithIds), getTraceStepDetail(expandBase));

  const synthesisBase = { step_type: "synthesis", summary: "", detail: { anchors: 5 } };
  const synthesisWithIds = {
    step_type: "synthesis",
    summary: "",
    detail: { anchors: 5, anchor_evidence_ids: ids, anchor_evidence_ids_truncated: true },
  };
  assert.equal(getTraceStepDetail(synthesisWithIds), getTraceStepDetail(synthesisBase));

  // 零命中(空列表)同样不该泄漏成任何数字。
  assert.equal(
    getTraceStepDetail({
      step_type: "follow_chain",
      summary: "",
      detail: { hops: 2, count: 0, result_ids: [] },
    }),
    getTraceStepDetail({ step_type: "follow_chain", summary: "", detail: { hops: 2, count: 0 } }),
  );
});

// profile 步不进 source_subgraph 那类隐藏名单:它是用户该看到的一步。
test("profile 步计入可见步数与总耗时", () => {
  const summary = getReasoningTraceSummary(
    [
      { step_type: "profile", summary: "带上对这个库的已有理解", detail: { blocks: 1 }, duration_ms: 2 },
      { step_type: "plan", summary: "规划", detail: {}, duration_ms: 1000 },
    ],
    false,
  );
  assert.equal(summary.stepCountLabel, "2 步");
  assert.equal(summary.totalLabel, "1.0s");
});
