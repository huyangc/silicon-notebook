import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import test from "node:test";

import {
  canPromoteMemory,
  memoryPromotionLabel,
  memoryPromotionPath,
} from "./memory-model.ts";

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
  const panel = await readFile(new URL("./memory-panel.tsx", import.meta.url), "utf8");
  const page = await readFile(new URL("./page.tsx", import.meta.url), "utf8");
  assert.match(panel, /memoryPromotionPath/);
  assert.match(panel, /canPromoteMemory/);
  assert.match(panel, /贡献到公共知识库/);
  assert.match(page, /source_kind === "memory"/);
  assert.match(page, /记忆提取候选/);
});
