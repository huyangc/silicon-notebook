import test from "node:test";
import assert from "node:assert/strict";

import {
  ASK_MODES, DEFAULT_ASK_MODE, ASK_MODE_GROUPS,
  askModeIds, askModeLabels, groupOf, groupLabel, modeLabel,
  defaultModeForGroup, normalizeAskModeProjection, requiresKg, canUseMode,
  modeFromTurn, streamsTrace,
} from "../../app/ask-modes.ts";
import {
  appSourceModules,
  jsxTextValues,
  stringLiterals,
} from "../../test-support/semantic-source.mjs";

async function appSourceCopy({ exclude = [] } = {}) {
  return (await appSourceModules())
    .filter(({ path }) => !exclude.includes(path))
    .map(({ path, module }) => ({
      path,
      values: [...stringLiterals(module), ...jsxTextValues(module)],
    }));
}

test("user-facing ids and default", () => {
  assert.deepEqual(askModeIds(), ["chunk", "reasoning"]);
  assert.equal(DEFAULT_ASK_MODE, "chunk");
  assert.deepEqual(ASK_MODE_GROUPS.map((g) => g.id), ["general", "strict", "extension"]);
});

test("deployment mode projection is data-driven, strict, and restorable", () => {
  const modes = normalizeAskModeProjection([
    { id: "chunk", group: "general", requires_kg: false, streams_trace: false },
    {
      id: "corp.search", group: "extension", label: "企业检索",
      desc: "使用部署内检索策略回答", requires_kg: true,
      streaming: true, streams_trace: true,
    },
    // Duplicate and malformed entries are ignored rather than replacing a live mode.
    {
      id: "corp.search", group: "extension", label: "覆盖",
      desc: "覆盖", requires_kg: false, streaming: true, streams_trace: true,
    },
    {
      id: "corp.blocking", group: "extension", label: "旧阻塞投影",
      desc: "扩展引擎必须声明实时轨迹", requires_kg: false,
      streaming: false, streams_trace: false,
    },
    {
      id: "corp.inconsistent", group: "extension", label: "不一致",
      desc: "两个流式字段必须同时为真", requires_kg: false,
      streaming: true, streams_trace: false,
    },
    {
      id: "corp.非法", group: "extension", label: "非法标识",
      desc: "浏览器也执行稳定标识守卫", requires_kg: false,
      streaming: true, streams_trace: true,
    },
    {
      id: "hardcoded", group: "extension", label: "无前缀",
      desc: "无点号", requires_kg: false, streaming: true, streams_trace: true,
    },
  ]);
  assert.deepEqual(askModeIds(modes), ["chunk", "reasoning", "corp.search"]);
  assert.equal(groupOf("corp.search", modes), "extension");
  assert.equal(requiresKg("corp.search", modes), true);
  assert.equal(canUseMode("corp.search", false, modes), false);
  assert.equal(streamsTrace("corp.search", modes), true);
  assert.equal(modeFromTurn({ response: { mode: "corp.search" } }, modes), "corp.search");
  assert.equal(modeFromTurn({ response: { mode: "corp.disabled" } }, modes), "chunk");
});

test("grouping + default engine per group", () => {
  assert.equal(groupOf("chunk"), "general");
  assert.equal(groupOf("reasoning"), "strict");
  assert.equal(defaultModeForGroup("general"), "chunk");
  assert.equal(defaultModeForGroup("strict"), "reasoning");   // groupDefault
});

test("kg gating", () => {
  assert.equal(requiresKg("chunk"), false);
  assert.equal(requiresKg("reasoning"), true);
  assert.equal(canUseMode("chunk", false), true);     // 通用问答无需 KG
  assert.equal(canUseMode("reasoning", false), false);
  assert.equal(canUseMode("reasoning", true), true);
});

// 后端 ask_modes.py 的 streaming 决定跑的过程中有没有轨迹步骤流下来,按引擎判断
// 而不是按分组——graph 模式(与 reasoning 同组、不流轨迹)已退役,当前分组内只剩
// reasoning 一个成员,但判断口径本身仍是 streamsTrace(mode),不是 groupOf(mode)。
// 跨栈的一致性由 scripts/check_ask_modes_contract.py 锁死,这里锁的是前端语义。
test("实时轨迹面板按引擎是否流轨迹判断", () => {
  assert.equal(streamsTrace("reasoning"), true);
  assert.equal(streamsTrace("chunk"), false);
});

test("restore mode from a prior turn (exact engine, safe fallback)", () => {
  assert.equal(modeFromTurn({ response: { mode: "reasoning" } }), "reasoning");
  assert.equal(modeFromTurn({ response: { mode: "fast" } }), "chunk");   // 非 user-facing → 兜底
  assert.equal(modeFromTurn({ response: { mode: "graph" } }), "chunk");  // 退役模式 → 兜底
  assert.equal(modeFromTurn({ response: {} }), "chunk");
  assert.equal(modeFromTurn(undefined), "chunk");
});

test("user-facing labels/descs are the current names (locks against silent drift)", () => {
  const byId = Object.fromEntries(ASK_MODES.map((m) => [m.id, m]));
  assert.equal(byId.chunk.label, "通用问答");
  assert.equal(byId.reasoning.label, "逐步推理");
  assert.equal(ASK_MODE_GROUPS.find((g) => g.id === "strict").label, "深入分析");
  // desc 不逐字锁(允许润色),但不得含机制黑话
  for (const m of ASK_MODES) {
    for (const jargon of ["agent", "多跳", "子图", "遍历"]) {
      assert.ok(!m.desc.includes(jargon), `mode ${m.id} desc 含黑话「${jargon}」`);
    }
  }
});

test("显示名查询函数由注册表派生(单一真源的读取口)", () => {
  assert.equal(groupLabel("strict"), ASK_MODE_GROUPS.find((g) => g.id === "strict").label);
  assert.equal(groupLabel("general"), ASK_MODE_GROUPS.find((g) => g.id === "general").label);
  assert.equal(modeLabel("chunk"), ASK_MODES.find((m) => m.id === "chunk").label);
  assert.throws(() => groupLabel("nope"), /unknown ask mode group/);
  assert.throws(() => modeLabel("nope"), /unknown ask mode/);
  // 比对集 = 全部模式名 ∪ 全部分组名,散落守卫据此扫描。
  assert.deepEqual(
    [...askModeLabels()].sort(),
    [...ASK_MODES.map((m) => m.label), ...ASK_MODE_GROUPS.map((g) => g.label)].sort(),
  );
});

test("退休模式名不得在前端源码里复活(含子目录)", async () => {
  const retired = ["严格推理", "深挖推理", "图谱多跳", "关联追溯"];
  const offenders = [];
  for (const { path, values } of await appSourceCopy()) {
    for (const term of retired) {
      if (values.some((value) => value.includes(term))) {
        offenders.push(`${path}: ${term}`);
      }
    }
  }
  assert.deepEqual(offenders, [], `退休模式名复活: ${offenders.join(", ")}`);
});

// 退休名单只禁「已知的历史名」,管不住下一次改名 —— 真正等价于单一真源的是这条:
// 当前显示名(由 ASK_MODES/ASK_MODE_GROUPS 现场派生)不得在 ask-modes.ts 之外出现。
// 只要有人把中文名抄进散文,这里就红;唯一合法写法是 groupLabel()/modeLabel() 插值。
test("当前模式名不得散落在 ask-modes.ts 之外(单一真源守卫)", async () => {
  const labels = askModeLabels();
  assert.ok(labels.length >= 4, "比对集为空则守卫形同虚设");
  const offenders = [];
  for (const { path, values } of await appSourceCopy({ exclude: ["ask-modes.ts"] })) {
    for (const label of labels) {
      if (values.some((value) => value.includes(label))) {
        offenders.push(`${path}: 「${label}」`);
      }
    }
  }
  assert.deepEqual(
    offenders, [],
    `模式显示名被硬编码(应改用 groupLabel()/modeLabel() 插值): ${offenders.join(", ")}`,
  );
});

// 守卫自身的自检:比对集真的来自注册表,而不是写死的历史清单。
// (改注册表 label → 守卫立刻改扫新名字;这是「变异测试」的可执行版本。)
test("散落守卫的比对集随注册表改名而变(变异自检)", async () => {
  const slot = ASK_MODE_GROUPS.find((g) => g.id === "strict");
  const original = slot.label;
  const MUTANT = "«变异分组名»";     // 合成哨兵,永不与真实 label 撞名
  try {
    slot.label = MUTANT;
    assert.ok(askModeLabels().includes(MUTANT), "改名后比对集未跟随 → 守卫扫的是死名单");
    assert.ok(!askModeLabels().includes(original), "旧名仍在比对集 → 比对集不是现场派生");
    assert.equal(groupLabel("strict"), MUTANT);
  } finally {
    slot.label = original;
  }
});
