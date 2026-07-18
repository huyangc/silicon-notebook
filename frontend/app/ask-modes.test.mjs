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

test("退休模式名不得在前端源码里复活(drift guard)", async () => {
  const fs = await import("node:fs/promises");
  const path = await import("node:path");
  const url = await import("node:url");
  const appDir = path.dirname(url.fileURLToPath(import.meta.url));
  const retired = ["严格推理", "深挖推理", "图谱多跳"];
  const entries = await fs.readdir(appDir, { withFileTypes: true });
  const offenders = [];
  for (const e of entries) {
    if (!e.isFile()) continue;
    if (!/\.(ts|tsx)$/.test(e.name)) continue;      // 只扫源码
    if (e.name.endsWith(".test.mjs")) continue;
    const text = await fs.readFile(path.join(appDir, e.name), "utf8");
    for (const term of retired) {
      if (text.includes(term)) offenders.push(`${e.name}: ${term}`);
    }
  }
  assert.deepEqual(offenders, [], `退休模式名复活: ${offenders.join(", ")}`);
});
