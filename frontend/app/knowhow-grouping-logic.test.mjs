import { test } from "node:test";
import assert from "node:assert/strict";
import { groupRowsByAnchor } from "./knowhow-grouping-logic.ts";

const mk = (id, anchorVal, colId = "c0") => ({
  id, position: 0, projectionStatus: "synced", cells: { [colId]: anchorVal },
});

test("groupRowsByAnchor: 同值聚成一组，保持首现顺序", () => {
  const rows = [mk("r1", "A"), mk("r2", "A"), mk("r3", "A")];
  const groups = groupRowsByAnchor(rows, "c0");
  assert.equal(groups.length, 1);
  assert.equal(groups[0].anchorValue, "A");
  assert.deepEqual(groups[0].rows.map((r) => r.id), ["r1", "r2", "r3"]);
});

test("groupRowsByAnchor: 多概念按首现顺序分组", () => {
  const rows = [mk("r1", "A"), mk("r2", "A"), mk("r3", "B")];
  const groups = groupRowsByAnchor(rows, "c0");
  assert.deepEqual(groups.map((g) => g.anchorValue), ["A", "B"]);
  assert.deepEqual(groups[1].rows.map((r) => r.id), ["r3"]);
});

test("groupRowsByAnchor: 不相邻同值也聚到一组（稳定聚合）", () => {
  const rows = [mk("r1", "A"), mk("r2", "B"), mk("r3", "A")];
  const groups = groupRowsByAnchor(rows, "c0");
  assert.deepEqual(groups.map((g) => g.anchorValue), ["A", "B"]);
  assert.deepEqual(groups[0].rows.map((r) => r.id), ["r1", "r3"]);
});

test("groupRowsByAnchor: 空 anchor 行各自单行成组", () => {
  const rows = [mk("r1", "A"), mk("r2", ""), mk("r3", "")];
  const groups = groupRowsByAnchor(rows, "c0");
  assert.equal(groups.length, 3);
  assert.deepEqual(groups.map((g) => g.anchorValue), ["A", "", ""]);
});
