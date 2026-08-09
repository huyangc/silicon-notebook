import { test } from "node:test";
import assert from "node:assert/strict";
import {
  groupRowsByAnchor,
  computeGridSpans,
  buildConceptMatrix,
  groupCellWriteTargets,
  isSharedColumn,
} from "../../app/knowhow-grouping-logic.ts";

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

test("buildConceptMatrix: 属性行×分支列，共享属性标记 sharedSpan", () => {
  const group = {
    anchorValue: "hold&setup",
    rows: [
      rowC("r1", { con: "hold&setup", sym: "共享现象", root: "根因1" }),
      rowC("r2", { con: "hold&setup", sym: "共享现象", root: "根因2" }),
    ],
  };
  const cols = [col("con"), { id: "sym", name: "现象", role: "attribute", position: 1 },
    { id: "root", name: "根因", role: "procedure", position: 2 }];
  const m = buildConceptMatrix(group, cols, "con");
  assert.equal(m.anchorValue, "hold&setup");
  assert.deepEqual(m.branchRowIds, ["r1", "r2"]);
  // anchor 列(con)不进属性行
  assert.deepEqual(m.attrRows.map((r) => r.columnId), ["sym", "root"]);
  const sym = m.attrRows.find((r) => r.columnId === "sym");
  assert.deepEqual(sym.values, ["共享现象", "共享现象"]);
  assert.equal(sym.sharedSpan, true);
  const root = m.attrRows.find((r) => r.columnId === "root");
  assert.deepEqual(root.values, ["根因1", "根因2"]);
  assert.equal(root.sharedSpan, false);
});

test("groupCellWriteTargets: 返回组内所有行 id", () => {
  const group = { anchorValue: "A", rows: [rowC("r1", {}), rowC("r2", {}), rowC("r3", {})] };
  assert.deepEqual(groupCellWriteTargets(group, "any"), ["r1", "r2", "r3"]);
});

test("isSharedColumn: 组内该列全分支同值（trim 后）→ true", () => {
  const group = {
    anchorValue: "hold&setup",
    rows: [
      rowC("r1", { sym: "共享现象" }),
      rowC("r2", { sym: "共享现象 " }), // 尾随空白 trim 后仍算同值
      rowC("r3", { sym: "共享现象" }),
    ],
  };
  assert.equal(isSharedColumn(group, "sym"), true);
});

test("isSharedColumn: 组内该列有异值 → false", () => {
  const group = {
    anchorValue: "hold&setup",
    rows: [rowC("r1", { root: "根因1" }), rowC("r2", { root: "根因2" })],
  };
  assert.equal(isSharedColumn(group, "root"), false);
});

test("isSharedColumn: 单行组 → false（无「其他分支」可言，不算共享格）", () => {
  const group = { anchorValue: "A", rows: [rowC("r1", { sym: "x" })] };
  assert.equal(isSharedColumn(group, "sym"), false);
});
