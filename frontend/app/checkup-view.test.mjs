import test from "node:test";
import assert from "node:assert/strict";

import {
  sourceHealthGroups,
  checkupCount,
  checkupAlertSignature,
} from "./checkup-view.ts";

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
