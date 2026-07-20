import test from "node:test";
import assert from "node:assert/strict";

import {
  ASK_MODES, DEFAULT_ASK_MODE, ASK_MODE_GROUPS,
  askModeIds, askModeLabels, groupOf, groupLabel, modeLabel,
  defaultModeForGroup, requiresKg, canUseMode, modeFromTurn,
} from "./ask-modes.ts";
import {
  appSourceModules,
  jsxTextValues,
  stringLiterals,
} from "./test/semantic-source.mjs";

async function appSourceCopy({ exclude = [] } = {}) {
  return (await appSourceModules())
    .filter(({ path }) => !exclude.includes(path))
    .map(({ path, module }) => ({
      path,
      values: [...stringLiterals(module), ...jsxTextValues(module)],
    }));
}

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

test("user-facing labels/descs are the current names (locks against silent drift)", () => {
  const byId = Object.fromEntries(ASK_MODES.map((m) => [m.id, m]));
  assert.equal(byId.chunk.label, "通用问答");
  assert.equal(byId.reasoning.label, "逐步推理");
  assert.equal(byId.graph.label, "关联追溯");
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
  const retired = ["严格推理", "深挖推理", "图谱多跳"];
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
