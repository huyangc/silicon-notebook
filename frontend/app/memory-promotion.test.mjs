import assert from "node:assert/strict";
import test from "node:test";

import {
  canPromoteMemory,
  memoryPromotionLabel,
  memoryPromotionPath,
} from "./memory-model.ts";
import {
  importsFrom,
  jsxTextValues,
  parseModule,
  stringLiterals,
} from "./test/semantic-source.mjs";

test("only confirmed unproposed Memory exposes the promotion action", () => {
  assert.equal(canPromoteMemory({ status: "confirmed", promotion_state: "none" }), true);
  assert.equal(canPromoteMemory({ status: "candidate", promotion_state: "none" }), false);
  assert.equal(canPromoteMemory({ status: "rejected", promotion_state: "none" }), false);
  assert.equal(canPromoteMemory({ status: "deprecated", promotion_state: "none" }), false);
  assert.equal(canPromoteMemory({ status: "confirmed", promotion_state: "proposed" }), false);
  assert.equal(canPromoteMemory({ status: "confirmed", promotion_state: "approved" }), false);
  assert.equal(canPromoteMemory({ status: "confirmed", promotion_state: "rejected" }), false);
});

test("promotion helpers target the owner-authenticated Memory endpoint and explain state", () => {
  assert.equal(memoryPromotionPath("memory/a"), "/memories/memory%2Fa/promote");
  assert.equal(memoryPromotionLabel("none"), "贡献到公共知识库");
  assert.equal(memoryPromotionLabel("proposed"), "审核中");
  assert.equal(memoryPromotionLabel("approved"), "已收录");
  assert.equal(memoryPromotionLabel("rejected"), "未采纳");
});

test("Memory panel exposes promotion while the admin queue identifies Memory proposals", async () => {
  const panel = await parseModule("memory-panel.tsx");
  const page = await parseModule("page.tsx");
  const panelModelImports = new Set(
    importsFrom(panel, "./memory-model").map((item) => item.imported),
  );

  assert.equal(panelModelImports.has("memoryPromotionPath"), true);
  assert.equal(panelModelImports.has("canPromoteMemory"), true);
  assert.ok(jsxTextValues(panel).some((value) => value.includes("贡献到公共知识库")));
  assert.ok(stringLiterals(page).includes("memory"));
  assert.ok(jsxTextValues(page).some((value) => value.includes("记忆提取候选")));
});
