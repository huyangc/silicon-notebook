import test from "node:test";
import assert from "node:assert/strict";

import {
  sourceHealthGroups,
  checkupCount,
  checkupAlertSignature,
  repairRelease,
  isRepairing,
} from "../../app/checkup-view.ts";

const item = (code, count, sample = [], fix = "reparse") => ({ code, count, sample, fix });

const checkup = (checks, extra = {}) => ({
  notebook_id: "nb-1",
  checked_at: "2026-07-23T00:00:00Z",
  healthy: checks.every((c) => c.count === 0),
  checks,
  ...extra,
});

test("null checkup → 空分组 / 计数 0 / 无签名", () => {
  assert.deepEqual(sourceHealthGroups(null), []);
  assert.equal(checkupCount(null, "H2"), 0);
  assert.equal(checkupAlertSignature(null), null);
});

test("健康(全 0)→ 无源级分组、无铃铛签名", () => {
  const c = checkup([
    item("H2", 0), item("H3", 0), item("H4", 0, [], "backfill_vectors"),
    item("H5", 0, [], "backfill_vectors"), item("H6", 0, [], "extract_kg"),
    item("H7", 0, [], "fold_index"), item("H8", 0, [], "rebuild_index"),
  ]);
  assert.deepEqual(sourceHealthGroups(c), []);
  assert.equal(checkupAlertSignature(c), null);
});

test("H2/H3 各自成行,带界面词标签与样本", () => {
  const c = checkup([
    item("H2", 2, ["s1", "s2"], "reparse"),
    item("H3", 1, ["s3"], "reparse"),
  ]);
  const groups = sourceHealthGroups(c);
  assert.equal(groups.length, 2);
  assert.deepEqual(groups[0], {
    key: "H2", label: "未解析出内容", count: 2, unit: "篇", fix: "reparse", sample: ["s1", "s2"],
  });
  assert.deepEqual(groups[1], {
    key: "H3", label: "检索片段缺失", count: 1, unit: "篇", fix: "reparse", sample: ["s3"],
  });
});

test("H4+H5 同 label(检索向量缺失)合并成一行,计数相加、单位为项", () => {
  const c = checkup([
    item("H4", 5, [], "backfill_vectors"),
    item("H5", 3, [], "backfill_vectors"),
  ]);
  const groups = sourceHealthGroups(c);
  assert.equal(groups.length, 1);
  assert.deepEqual(groups[0], {
    key: "H4", label: "检索向量缺失", count: 8, unit: "项", fix: "backfill_vectors", sample: [],
  });
});

test("H6 → 待分析来源 / extract_kg / 篇", () => {
  const c = checkup([item("H6", 4, [], "extract_kg")]);
  const groups = sourceHealthGroups(c);
  assert.deepEqual(groups[0], {
    key: "H6", label: "待分析来源", count: 4, unit: "篇", fix: "extract_kg", sample: [],
  });
});

test("H7/H8 是索引级,不进源级分组", () => {
  const c = checkup([
    item("H7", 1, [], "fold_index"),
    item("H8", 1, [], "rebuild_index"),
  ]);
  assert.deepEqual(sourceHealthGroups(c), []);
  assert.equal(checkupCount(c, "H7"), 1);
  assert.equal(checkupCount(c, "H8"), 1);
});

test("顺序稳定:按 H2..H6 首次出现排列", () => {
  const c = checkup([
    item("H6", 1, [], "extract_kg"),
    item("H4", 1, [], "backfill_vectors"),
    item("H2", 1, ["s1"], "reparse"),
  ]);
  const keys = sourceHealthGroups(c).map((g) => g.key);
  assert.deepEqual(keys, ["H2", "H4", "H6"]);
});

test("铃铛签名 = notebook + 排序后的命中代号集合", () => {
  const c = checkup([
    item("H5", 2, [], "backfill_vectors"),
    item("H2", 1, ["s1"], "reparse"),
    item("H8", 1, [], "rebuild_index"),
    item("H3", 0),
  ]);
  assert.equal(checkupAlertSignature(c), "nb-1:H2,H5,H8");
});

test("签名随命中集合变化(新问题/修好)", () => {
  const before = checkup([item("H2", 3, ["a", "b", "c"], "reparse"), item("H4", 1, [], "backfill_vectors")]);
  const after = checkup([item("H2", 0), item("H4", 1, [], "backfill_vectors")]);
  assert.notEqual(checkupAlertSignature(before), checkupAlertSignature(after));
  assert.equal(checkupAlertSignature(after), "nb-1:H4");
});

// ---- 修复忙碌位的解除条件(repairRelease / isRepairing)----------------------
// 两类修复的形状不同,解除条件不能一刀切。这几条把差异钉住——尤其是最后两条,
// 它们钉的是**刻意保留**的取舍,不是待修的 bug(理由见 checkup-view.ts 的注释)。

test("reparse 逐轮修一批:count 一变就恢复可点(让用户接着修下一批)", () => {
  const entry = repairRelease("reparse", 37);
  assert.deepEqual(entry, { release: "count-changed", count: 37 });
  assert.equal(isRepairing(entry, 37), true, "count 没变 → 这一轮还在跑");
  assert.equal(isRepairing(entry, 17), false, "count 降了 → 这一轮见效,放行下一轮");
});

test("backfill_vectors 一次修全库:count 递减**不**解锁(否则会排出并发全库补齐)", () => {
  // 这是 codex 第 2 轮 P2 的回归钉:看板 H4/H5 排除活跃租约,job 逐源补齐时 count
  // 一路递减。若按 count 解除,job 还剩大半按钮就放行,用户能再排一次全库补齐——
  // 正是后端 checkup.py 里 H4/H5 注释点名要防的「并发 backfill 重复模型调用」。
  const entry = repairRelease("backfill_vectors", 25);
  assert.deepEqual(entry, { release: "group-gone" });
  assert.equal(isRepairing(entry, 25), true);
  assert.equal(isRepairing(entry, 12), true, "补到一半 count 降了,但 job 还在跑,不能放行");
  assert.equal(isRepairing(entry, 1), true, "只剩一项也还在跑");
});

test("没有在跑的修复时不显示忙碌态", () => {
  assert.equal(isRepairing(undefined, 25), false);
});

test("group-gone 的解除只能靠该组消失/窗口结束——这是刻意取舍,不是待修 bug", () => {
  // ⚠ 改这条之前请先读 checkup-view.ts::repairRelease 的注释。
  // backfill job 若因嵌入服务不可用而每源都失败,H4/H5 不归零(后端逐源吞异常,且
  // backfill **没有**持久 job 状态),按钮会锁到有界轮询窗口结束(≤10 分钟)或
  // 切库/重开看板为止,即便此刻已没有 job 在跑。反方向(提前放行)会重新打开
  // 「并发全库补齐 = 重复付费模型调用」那道口子,而补齐失败几乎都是嵌入服务不可用
  // ——立刻重试也还是失败。真正的修法是给 backfill 加持久 job 状态(新表 + 迁移 +
  // PG 对等),那是独立特性。这条用例存在的意义就是让「顺手放宽」改不动而不报红。
  const entry = repairRelease("backfill_vectors", 25);
  assert.equal(isRepairing(entry, 25), true, "全失败、count 原封不动 → 仍显示修复中");
  assert.notEqual(entry.release, "count-changed", "被改成按 count 解除即视为退化");
});

test("extract_kg 不走 repairingFix(忙碌位由 buildingKg 单独表达)", () => {
  // page.tsx 对 extract_kg 直接早退、从不写这张表,故这里查不到条目即恒不忙碌。
  // 真正的忙碌位是 buildingKg —— 它在建图 POST 失败时会被 startKgBuild 自己清掉,
  // 而 repairingFix 里的条目没人清得掉(codex 第 1 轮 P2)。
  assert.equal(isRepairing(undefined, 8), false);
});
