import test from "node:test";
import assert from "node:assert/strict";

import {
  describeScaleIndex,
  SCALE_OP_MODE,
  latestScaleIndexDoneKey,
  latestScaleIndexDoneForNotebook,
  queuedScaleIndexImmediateOp,
  queuedScheduleHint,
  scaleIndexOpConfirm,
  shouldRefreshScaleIndexFromDone,
  shouldShowIndexRequiredBanner,
} from "../../app/scale-index.ts";
import { groupLabel } from "../../app/ask-modes.ts";

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

test("stale 且只有派生内容变更 → visible count stays zero but primary=update", () => {
  assert.deepEqual(describeScaleIndex({
    ...base,
    state: "stale",
    exists: true,
    stale: true,
    unindexed_sources: 0,
    has_unindexed_content: true,
  }), {
    state: "stale", stateLabel: "已过期", tone: "warn", primaryOp: "update", canRebuild: true,
  });
  assert.match(
    scaleIndexOpConfirm("update", {
      ...base,
      unindexed_sources: 0,
      has_unindexed_content: true,
    }),
    /新增\/变更内容/,
  );
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

test("historical index_required banner follows the live published-index state", () => {
  assert.equal(shouldShowIndexRequiredBanner(true, null), true);
  assert.equal(shouldShowIndexRequiredBanner(true, { exists: false }), true);
  assert.equal(shouldShowIndexRequiredBanner(true, { exists: true }), false);
  assert.equal(shouldShowIndexRequiredBanner(false, { exists: false }), false);
  assert.equal(shouldShowIndexRequiredBanner(undefined, { exists: false }), false);
});

test("index_done refreshes only the active notebook after foreground polling ends", () => {
  assert.equal(shouldRefreshScaleIndexFromDone(
    { notebook_id: "nb-active", kind: "index_done" },
    "nb-active",
  ), true);
  assert.equal(shouldRefreshScaleIndexFromDone(
    { notebook_id: "nb-other", kind: "index_done" },
    "nb-active",
  ), false);
  assert.equal(shouldRefreshScaleIndexFromDone(
    { notebook_id: "nb-active", kind: "paper_meta_done" },
    "nb-active",
  ), false);
  assert.equal(shouldRefreshScaleIndexFromDone(null, "nb-active"), false);
});

test("batched completion events retain an active index_done that is not the final toast", () => {
  const activeDone = { notebook_id: "nb-active", kind: "index_done", ts: 1 };
  const events = [
    { notebook_id: "nb-other", kind: "paper_meta_done", ts: 3 },
    { notebook_id: "nb-other", kind: "index_done", ts: 2 },
    activeDone,
  ];
  assert.equal(latestScaleIndexDoneForNotebook(events, "nb-active"), activeDone);
  assert.equal(latestScaleIndexDoneForNotebook(events, "nb-missing"), null);
});

test("unrelated completion additions or dismissals keep the active index event key stable", () => {
  const activeDone = { notebook_id: "nb-active", kind: "index_done", ts: 7 };
  const initial = [activeDone];
  const withUnrelated = [
    { notebook_id: "nb-other", kind: "paper_meta_done", ts: 8 },
    activeDone,
  ];
  assert.equal(latestScaleIndexDoneKey(initial, "nb-active"), "nb-active:7");
  assert.equal(
    latestScaleIndexDoneKey(withUnrelated, "nb-active"),
    "nb-active:7",
  );
  assert.equal(
    latestScaleIndexDoneKey(withUnrelated.slice(1), "nb-active"),
    "nb-active:7",
  );
  // Dismissing the active completion removes the display key.  The page's
  // request generation deliberately ignores this empty transition so the
  // already-started authoritative status read may still publish.
  assert.equal(latestScaleIndexDoneKey([], "nb-active"), "");
});

test("queued operations can be upgraded to the matching immediate operation", () => {
  assert.equal(queuedScaleIndexImmediateOp({ ...base, state: "queued" }), "build");
  assert.equal(queuedScaleIndexImmediateOp({
    ...base,
    state: "queued",
    exists: true,
    has_unindexed_content: true,
  }), "update");
  assert.equal(queuedScaleIndexImmediateOp({
    ...base,
    state: "queued",
    exists: true,
    has_unindexed_content: false,
  }), "rebuild");
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
  // 全量而非增量:散文去黑话后由动词「建立」承担(对比 update 的「更新…增量收进」、
  // rebuild 的「全量重建」),原先的「从零」措辞已随黑话一并重写掉。锁语义不锁字面。
  assert.match(build, /建立/);
  assert.ok(!build.includes("增量"), "build 确认文案不该出现增量语义");
  assert.match(build, /后台/);   // 异步不阻塞
});

// ---- queuedScheduleHint: 排队态透明化(位次/空闲窗口/上次耗时) ----

test("queuedScheduleHint: 全字段齐 → 队列长度前缀 + 今天窗口 + 耗时", () => {
  const now = new Date(2026, 7, 11, 9, 0);
  const start = new Date(2026, 7, 11, 12, 0); // 同一本地日
  const hint = queuedScheduleHint({
    ...base,
    exists: true,
    queue_position: 2,
    queue_length: 5,
    offpeak_in_window: false,
    offpeak_next_start_at: start.toISOString(),
    last_build_ms: 125000, // 125s ≈ 2 分钟
  }, now);
  assert.match(hint, /已排队（共 5 项）/);
  assert.match(hint, /预计今天 12:00 后开始/);
  assert.match(hint, /上次构建约 2 分钟/);
  // 两段(窗口+耗时)用「；」连接
  assert.equal(hint.split("；").length, 2);
});

test("queuedScheduleHint: 旧后端缺全部新字段 → 回退默认文案", () => {
  const hint = queuedScheduleHint({ ...base, exists: true }, new Date());
  assert.equal(hint, "已排队，将在服务器空闲时构建；完成后自动更新");
});

test("queuedScheduleHint: offpeak_in_window=true → 「即将开始」分支，忽略 next_start_at", () => {
  const hint = queuedScheduleHint({
    ...base,
    exists: true,
    offpeak_in_window: true,
    offpeak_next_start_at: "2026-08-11T10:00:00Z",
  }, new Date());
  assert.match(hint, /已进入空闲时段，即将开始/);
  assert.ok(!hint.includes("预计"), "offpeak_in_window 为真时不应再显示下一次窗口");
});

test("queuedScheduleHint: queue_length >= 2 → 前缀带队列项数，不含位次措辞", () => {
  const hint = queuedScheduleHint({ ...base, exists: true, queue_position: 1, queue_length: 3 }, new Date());
  assert.match(hint, /已排队（共 3 项）/);
  assert.ok(!hint.includes("排在第"), "排队文案不该再声称位次(空闲窗口开启后队列项并发启动,位次是假串行语义)");
});

test("queuedScheduleHint: queue_length 为 1 或缺失 → 不带项数后缀", () => {
  const single = queuedScheduleHint({ ...base, exists: true, queue_position: 1, queue_length: 1 }, new Date());
  assert.ok(!single.includes("共"), "队列仅一项时不该出现「共 N 项」");
  assert.ok(single.startsWith("已排队，"), single);

  const missing = queuedScheduleHint({ ...base, exists: true, queue_position: 1 }, new Date());
  assert.ok(!missing.includes("共 "), "queue_length 缺失时不该输出「共 N 项」");
});

test("queuedScheduleHint: 下一空闲窗口在本地明天 → 「明天」", () => {
  const now = new Date(2026, 7, 11, 23, 0);
  const start = new Date(2026, 7, 12, 1, 15); // 跨本地日
  const hint = queuedScheduleHint({ ...base, exists: true, offpeak_next_start_at: start.toISOString() }, now);
  assert.match(hint, /预计明天 01:15 后开始/);
});

test("queuedScheduleHint: offpeak_next_start_at 为非法 ISO → 该分段省略，不抛异常", () => {
  const now = new Date(2026, 7, 11, 9, 0);
  const hint = queuedScheduleHint({
    ...base,
    exists: true,
    queue_length: 3,
    offpeak_in_window: false,
    offpeak_next_start_at: "not-a-real-timestamp",
    last_build_ms: 45000,
  }, now);
  assert.ok(!hint.includes("预计"), "非法 ISO 时不该输出窗口分段");
  assert.match(hint, /已排队（共 3 项）/);
  assert.match(hint, /上次构建约 1 分钟内/);
});

test("queuedScheduleHint: queue_length 缺失 → 不输出「共 N 项」（脏数据/旧后端兼容）", () => {
  const now = new Date(2026, 7, 11, 9, 0);
  const start = new Date(2026, 7, 11, 12, 0);
  const hint = queuedScheduleHint({
    ...base,
    exists: true,
    queue_position: 1,
    offpeak_next_start_at: start.toISOString(),
  }, now);
  assert.ok(!hint.includes("共 "), "queue_length 缺失时不该出现「共 N 项」");
  assert.match(hint, /预计今天 12:00 后开始/);
});

test("queuedScheduleHint: 上次构建耗时三档格式化", () => {
  const now = new Date();
  assert.match(
    queuedScheduleHint({ ...base, exists: true, last_build_ms: 45000 }, now),
    /上次构建约 1 分钟内/,
  );
  assert.match(
    queuedScheduleHint({ ...base, exists: true, last_build_ms: 1500000 }, now), // 25 分钟
    /上次构建约 25 分钟/,
  );
  assert.match(
    queuedScheduleHint({ ...base, exists: true, last_build_ms: 5400000 }, now), // 90 分钟 = 1 小时 30 分
    /上次构建约 1 小时 30 分/,
  );
});

test("queuedScheduleHint: 数值档位/内部术语不上屏", () => {
  const now = new Date(2026, 7, 11, 9, 0);
  const start = new Date(2026, 7, 11, 12, 0);
  const hint = queuedScheduleHint({
    ...base,
    exists: true,
    queue_position: 1,
    queue_length: 4,
    offpeak_next_start_at: start.toISOString(),
    last_build_ms: 5400000,
  }, now);
  for (const banned of ["manifest", "chunk", "KG", "canonical", "tier", "projection"]) {
    assert.ok(!hint.toLowerCase().includes(banned.toLowerCase()), `内部术语「${banned}」不该上屏: ${hint}`);
  }
});
