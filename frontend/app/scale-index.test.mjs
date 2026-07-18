import test from "node:test";
import assert from "node:assert/strict";

import { describeScaleIndex, SCALE_OP_MODE, scaleIndexOpConfirm } from "./scale-index.ts";
import { groupLabel } from "./ask-modes.ts";

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

test("stale 且有新增来源 → primary=update(fold 增量收进) + canRebuild", () => {
  assert.deepEqual(describeScaleIndex({ ...base, state: "stale", exists: true, stale: true, unindexed_sources: 3 }), {
    state: "stale", stateLabel: "已过期", tone: "warn", primaryOp: "update", canRebuild: true,
  });
});

test("stale 但无新增来源(过期由图谱/概念变更引起)→ primary=rebuild(fold 会空转,须全量重建才清过期)", () => {
  assert.deepEqual(describeScaleIndex({ ...base, state: "stale", exists: true, stale: true, unindexed_sources: 0 }), {
    state: "stale", stateLabel: "已过期", tone: "warn", primaryOp: "rebuild", canRebuild: true,
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
  assert.match(scaleIndexOpConfirm("build", base), /快速查找结构/);
  const upd = scaleIndexOpConfirm("update", { ...base, unindexed_sources: 3 });
  assert.match(upd, /3 个新增来源/);
  assert.match(upd, /增量/);
  assert.match(upd, /整体重建/); // 诚实注明会自动转全量的条件
  assert.match(scaleIndexOpConfirm("rebuild", base), /删除现有索引/);
});

// 语义级断言(刻意不锁整段):这段措辞正在 #296 被重写,锁字面量只会制造假冲突。
// 只钉住必须成立的含义 + 模式名必须来自 ask-modes.ts 真源。
test("build 确认文案:引用当前模式名、无退休名、保留关键含义", () => {
  const build = scaleIndexOpConfirm("build", base);
  // 模式名随注册表走 —— 改名后本断言自动跟随,不需要改测试。
  assert.ok(
    build.includes(groupLabel("strict")),
    `确认文案未引用当前分组名「${groupLabel("strict")}」(是否写死了字面量?)`,
  );
  for (const retired of ["严格推理", "深挖推理", "图谱多跳"]) {
    assert.ok(!build.includes(retired), `确认文案含退休模式名「${retired}」`);
  }
  assert.match(build, /从零/);   // 全量而非增量
  assert.match(build, /后台/);   // 异步不阻塞
});
