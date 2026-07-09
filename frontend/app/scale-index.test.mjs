import test from "node:test";
import assert from "node:assert/strict";

import { describeScaleIndex, SCALE_OP_MODE, scaleIndexOpConfirm } from "./scale-index.ts";

const base = {
  exists: false, stale: false, building: false, eligible: true,
  n_nodes: 0, n_chunks: 0, n_ann: 0, n_chunk_ann: 0, has_chunk_ann: false,
  unindexed_sources: 0,
};

test("unindexed & eligible → 未构建, primary=build", () => {
  assert.deepEqual(describeScaleIndex({ ...base, state: "unindexed", eligible: true }), {
    state: "unindexed", stateLabel: "未构建", tone: "warn", primaryOp: "build", canRebuild: false,
  });
});

test("unindexed & not eligible (小库) → 不需要, no ops", () => {
  assert.deepEqual(describeScaleIndex({ ...base, state: "unindexed", eligible: false, exists: false }), {
    state: "unindexed", stateLabel: "不需要", tone: "muted", primaryOp: null, canRebuild: false,
  });
});

test("suggested → 建议构建, primary=build", () => {
  const v = describeScaleIndex({ ...base, state: "suggested" });
  assert.equal(v.stateLabel, "建议构建");
  assert.equal(v.primaryOp, "build");
  assert.equal(v.canRebuild, false);
});

test("stale (exists) → 已过期, primary=update + canRebuild", () => {
  assert.deepEqual(describeScaleIndex({ ...base, state: "stale", exists: true, stale: true }), {
    state: "stale", stateLabel: "已过期", tone: "warn", primaryOp: "update", canRebuild: true,
  });
});

test("indexed → 最新, no primary but canRebuild", () => {
  assert.deepEqual(describeScaleIndex({ ...base, state: "indexed", exists: true }), {
    state: "indexed", stateLabel: "最新", tone: "ok", primaryOp: null, canRebuild: true,
  });
});

test("building → busy, no ops", () => {
  const v = describeScaleIndex({ ...base, state: "building", building: true, exists: true });
  assert.equal(v.stateLabel, "构建中…");
  assert.equal(v.primaryOp, null);
  assert.equal(v.canRebuild, false);
});

test("queued → busy, no ops (even if index exists)", () => {
  const v = describeScaleIndex({ ...base, state: "queued", exists: true });
  assert.equal(v.primaryOp, null);
  assert.equal(v.canRebuild, false);
});

test("state falls back from flags when absent", () => {
  assert.equal(describeScaleIndex({ ...base, state: undefined, exists: true, stale: true }).state, "stale");
  assert.equal(describeScaleIndex({ ...base, state: undefined, exists: true, stale: false }).state, "indexed");
  assert.equal(describeScaleIndex({ ...base, state: undefined, exists: false }).state, "unindexed");
});

test("SCALE_OP_MODE maps each op to its backend mode", () => {
  assert.deepEqual(SCALE_OP_MODE, { build: "full", update: "fold", rebuild: "full" });
});

test("confirm text is precise + honest per op", () => {
  assert.match(scaleIndexOpConfirm("build", base), /从零/);
  const upd = scaleIndexOpConfirm("update", { ...base, unindexed_sources: 3 });
  assert.match(upd, /3 个新增来源/);
  assert.match(upd, /增量/);
  assert.match(upd, /整体重建/); // 诚实注明会自动转全量的条件
  assert.match(scaleIndexOpConfirm("rebuild", base), /删除现有索引/);
});
