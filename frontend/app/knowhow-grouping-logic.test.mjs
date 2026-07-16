import { test } from "node:test";
import assert from "node:assert/strict";
import { groupRowsByAnchor, computeGridSpans } from "./knowhow-grouping-logic.ts";

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

const col = (id) => ({ id, name: id, role: "attribute", position: 0 });
const rowC = (id, cells) => ({ id, position: 0, projectionStatus: "synced", cells });

test("computeGridSpans: 组内同值列合并成 rowSpan，被覆盖格 rowSpan=0", () => {
  const groups = [{
    anchorValue: "A",
    rows: [
      rowC("r1", { a: "A", b: "shared", c: "x1" }),
      rowC("r2", { a: "A", b: "shared", c: "x2" }),
    ],
  }];
  const cols = [col("a"), col("b"), col("c")];
  const grid = computeGridSpans(groups, cols);
  // 第一行：a 合并(2)、b 合并(2)、c 独立(1)
  assert.deepEqual(grid[0].cells.map((c) => [c.text, c.rowSpan]),
    [["A", 2], ["shared", 2], ["x1", 1]]);
  // 第二行：a/b 被覆盖(0)、c 独立(1)
  assert.deepEqual(grid[1].cells.map((c) => [c.text, c.rowSpan]),
    [["A", 0], ["shared", 0], ["x2", 1]]);
});

test("computeGridSpans: 跨组不合并（即使同值）", () => {
  const groups = [
    { anchorValue: "A", rows: [rowC("r1", { a: "A", b: "pt" })] },
    { anchorValue: "B", rows: [rowC("r2", { a: "B", b: "pt" })] },
  ];
  const grid = computeGridSpans(groups, [col("a"), col("b")]);
  assert.deepEqual(grid[0].cells.map((c) => c.rowSpan), [1, 1]);
  assert.deepEqual(grid[1].cells.map((c) => c.rowSpan), [1, 1]);
});

test("computeGridSpans: 组内某列部分同值只合并相邻段", () => {
  const groups = [{
    anchorValue: "A",
    rows: [
      rowC("r1", { a: "A", tool: "pt" }),
      rowC("r2", { a: "A", tool: "innovus" }),
      rowC("r3", { a: "A", tool: "innovus" }),
    ],
  }];
  const grid = computeGridSpans(groups, [col("a"), col("tool")]);
  assert.deepEqual(grid.map((r) => r.cells[1].rowSpan), [1, 2, 0]);
});
