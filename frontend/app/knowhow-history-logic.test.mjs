import { test } from "node:test";
import assert from "node:assert/strict";
import {
  summarizeChange, originLabel, groupChangesByDay, foldLocalChanges, isStaleHead,
} from "./knowhow-history-logic.ts";

// payload 字段名按后端真实形状（design doc
// 2026-07-22-knowhow-table-version-control-design.md §4.4，与
// backend/app/services/knowhow/history.py / knowhow_history_store.py 逐字核对）：
// row_id/column_id/target_seq 都是 snake_case——knowhow-model.ts 的 mapChange()
// 不对 payload 做逐 kind 递归 camelCase 转换（该字段类型即为 Record<string,
// any>，透传后端原样结构），这里的夹具必须反映真实网络形状，而不是任务简报
// 定稿前的驼峰猜测。change 顶层字段（seq/kind/actor/origin/createdAt/note）
// 才是 mapChange() 转换过的 camelCase。
const chg = (over = {}) => ({
  seq: 1, kind: "cell_update", actor: "user-1", origin: "user",
  createdAt: "2026-07-22T14:30:00", payload: { cells: [] }, note: "", ...over,
});

test("summarizeChange: 单格改动说清改了几个格子", () => {
  const change = chg({ payload: { cells: [{ row_id: "r1", column_id: "c1" }] } });
  assert.equal(summarizeChange(change), "修改了 1 个格子");
});

test("summarizeChange: 合并格批量写按格子数汇总", () => {
  const change = chg({ payload: { cells: [{ row_id: "r1" }, { row_id: "r2" }, { row_id: "r3" }] } });
  assert.equal(summarizeChange(change), "修改了 3 个格子");
});

test("summarizeChange: 回退说明回到哪里（payload.target_seq，snake_case）", () => {
  const change = chg({ kind: "revert", payload: { target_seq: 12 } });
  assert.equal(summarizeChange(change), "回退到 #12");
});

test("summarizeChange: 删列点名列名", () => {
  const change = chg({ kind: "column_delete", payload: { column: { name: "修复方法" } } });
  assert.equal(summarizeChange(change), "删除了列「修复方法」");
});

test("summarizeChange: 加列点名列名", () => {
  const change = chg({ kind: "column_add", payload: { column: { name: "根因分析" } } });
  assert.equal(summarizeChange(change), "新增了列「根因分析」");
});

test("summarizeChange: 列改名前后对照", () => {
  const change = chg({ kind: "column_rename", payload: { column_id: "c1", before: "旧名", after: "新名" } });
  assert.equal(summarizeChange(change), "列改名：旧名 → 新名");
});

test("summarizeChange: 行新增/删除/导入追加按行数汇总", () => {
  assert.equal(summarizeChange(chg({ kind: "row_add", payload: { rows: [{ row_id: "r1" }, { row_id: "r2" }] } })), "新增了 2 行");
  assert.equal(summarizeChange(chg({ kind: "row_delete", payload: { rows: [{ row_id: "r1" }] } })), "删除了 1 行");
  assert.equal(summarizeChange(chg({ kind: "import_append", payload: { rows: [{ row_id: "r1" }, { row_id: "r2" }, { row_id: "r3" }] } })), "导入追加了 3 行");
});

test("summarizeChange: 列内容类型/行标题列/表信息/代码附件/建表——固定文案", () => {
  assert.equal(summarizeChange(chg({ kind: "column_kind", payload: { column_id: "c1", before: "attribute", after: "procedure" } })), "修改了列的内容类型");
  assert.equal(summarizeChange(chg({ kind: "anchor_set", payload: { columns: [] } })), "修改了行标题列");
  assert.equal(summarizeChange(chg({ kind: "table_meta", payload: { before: {}, after: {} } })), "修改了表信息");
  assert.equal(summarizeChange(chg({ kind: "cell_code_put", payload: {} })), "更新了格子代码");
  assert.equal(summarizeChange(chg({ kind: "cell_code_delete", payload: {} })), "删除了格子代码");
});

test("summarizeChange: 建表流水优先用 note，没有 note 时兜底「建表」", () => {
  assert.equal(summarizeChange(chg({ kind: "table_create", note: "由 旧表 复制而来", payload: {} })), "由 旧表 复制而来");
  assert.equal(summarizeChange(chg({ kind: "table_create", note: "", payload: {} })), "建表");
});

test("summarizeChange: 未知 kind 落到默认文案，不抛异常", () => {
  assert.equal(summarizeChange(chg({ kind: "something_new_from_the_future", payload: {} })), "修改");
});

test("originLabel: 已知来源给中文标签", () => {
  assert.equal(originLabel("llm_reformat"), "格式规整");
  assert.equal(originLabel("llm_optimize"), "表达优化");
  assert.equal(originLabel("import"), "导入");
  assert.equal(originLabel("revert"), "回退");
  assert.equal(originLabel("agent"), "Agent");
  assert.equal(originLabel("backfill"), "批量回填");
});

test("originLabel: 普通用户编辑不加标签（避免每条都挂徽章）", () => {
  assert.equal(originLabel("user"), "");
});

test("originLabel: 未知来源原样回显而不是崩掉", () => {
  assert.equal(originLabel("something_new"), "something_new");
});

test("groupChangesByDay: 同一天聚成一组，按天倒序", () => {
  const groups = groupChangesByDay([
    chg({ seq: 3, createdAt: "2026-07-22T09:00:00" }),
    chg({ seq: 2, createdAt: "2026-07-21T18:00:00" }),
    chg({ seq: 1, createdAt: "2026-07-21T08:00:00" }),
  ]);
  assert.deepEqual(groups.map((g) => g.day), ["2026-07-22", "2026-07-21"]);
  assert.deepEqual(groups[1].changes.map((c) => c.seq), [2, 1]);
});

test("groupChangesByDay: 空数组返回空数组", () => {
  assert.deepEqual(groupChangesByDay([]), []);
});

test("groupChangesByDay: 全部同一天时只产出一组", () => {
  const groups = groupChangesByDay([
    chg({ seq: 2, createdAt: "2026-07-22T09:00:00" }),
    chg({ seq: 1, createdAt: "2026-07-22T08:00:00" }),
  ]);
  assert.equal(groups.length, 1);
  assert.equal(groups[0].day, "2026-07-22");
});

test("foldLocalChanges: 反复编辑同一格折叠成一条净变化", () => {
  const result = foldLocalChanges([
    chg({ seq: 1, payload: { cells: [{ row_id: "r1", column_id: "c1", before: "A", after: "B" }] } }),
    chg({ seq: 2, payload: { cells: [{ row_id: "r1", column_id: "c1", before: "B", after: "C" }] } }),
  ]);
  assert.deepEqual(result.cells, [{ rowId: "r1", columnId: "c1", before: "A", after: "C" }]);
});

test("foldLocalChanges: 改回原样的格子不出现在结果里", () => {
  const result = foldLocalChanges([
    chg({ seq: 1, payload: { cells: [{ row_id: "r1", column_id: "c1", before: "A", after: "B" }] } }),
    chg({ seq: 2, payload: { cells: [{ row_id: "r1", column_id: "c1", before: "B", after: "A" }] } }),
  ]);
  assert.deepEqual(result.cells, []);
});

test("foldLocalChanges: 多个格子各自独立折叠", () => {
  const result = foldLocalChanges([
    chg({
      seq: 1,
      payload: {
        cells: [
          { row_id: "r1", column_id: "c1", before: "A", after: "B" },
          { row_id: "r1", column_id: "c2", before: "X", after: "Y" },
        ],
      },
    }),
  ]);
  assert.deepEqual(result.cells.sort((a, b) => a.columnId.localeCompare(b.columnId)), [
    { rowId: "r1", columnId: "c1", before: "A", after: "B" },
    { rowId: "r1", columnId: "c2", before: "X", after: "Y" },
  ]);
});

test("foldLocalChanges: row_add 新增的行出现在 rowsAdded", () => {
  const result = foldLocalChanges([
    chg({ kind: "row_add", payload: { rows: [{ row_id: "r1" }] } }),
  ]);
  assert.deepEqual(result.rowsAdded, ["r1"]);
  assert.deepEqual(result.rowsRemoved, []);
});

test("foldLocalChanges: import_append 与 row_add 同记一次「新增」", () => {
  const result = foldLocalChanges([
    chg({ kind: "import_append", payload: { rows: [{ row_id: "r9" }] } }),
  ]);
  assert.deepEqual(result.rowsAdded, ["r9"]);
});

test("foldLocalChanges: row_delete 删除的行出现在 rowsRemoved", () => {
  const result = foldLocalChanges([
    chg({ kind: "row_delete", payload: { rows: [{ row_id: "r1" }] } }),
  ]);
  assert.deepEqual(result.rowsRemoved, ["r1"]);
  assert.deepEqual(result.rowsAdded, []);
});

test("foldLocalChanges: 同一行先增后删互相抵消，不出现在任何一边", () => {
  const result = foldLocalChanges([
    chg({ seq: 1, kind: "row_add", payload: { rows: [{ row_id: "r1" }] } }),
    chg({ seq: 2, kind: "row_delete", payload: { rows: [{ row_id: "r1" }] } }),
  ]);
  assert.deepEqual(result.rowsAdded, []);
  assert.deepEqual(result.rowsRemoved, []);
});

test("foldLocalChanges: 同一行先删后增互相抵消，不出现在任何一边", () => {
  const result = foldLocalChanges([
    chg({ seq: 1, kind: "row_delete", payload: { rows: [{ row_id: "r1" }] } }),
    chg({ seq: 2, kind: "row_add", payload: { rows: [{ row_id: "r1" }] } }),
  ]);
  assert.deepEqual(result.rowsAdded, []);
  assert.deepEqual(result.rowsRemoved, []);
});

// revert 自身的 payload 用 rows_added/rows_removed（不是 row_add/row_delete 的
// 顶层 rows 字段名）——见 knowhow_history_store.py _revert_payload。跨越一次
// 回退的区间若不识别这两个字段，行级净变化会静默丢失（Task 11 服务端
// aggregate_diff 曾踩过同一个坑，见 progress.md）。
test("foldLocalChanges: revert 自身携带的 rows_added/rows_removed 同样被折叠", () => {
  const result = foldLocalChanges([
    chg({
      kind: "revert",
      payload: {
        cells: [],
        rows_added: [{ row_id: "back1" }],
        rows_removed: [{ row_id: "back2" }],
      },
    }),
  ]);
  assert.deepEqual(result.rowsAdded, ["back1"]);
  assert.deepEqual(result.rowsRemoved, ["back2"]);
});

test("foldLocalChanges: revert 自身携带的顶层 cells 与 cell_update 同规则折叠", () => {
  const result = foldLocalChanges([
    chg({
      kind: "revert",
      payload: { cells: [{ row_id: "r1", column_id: "c1", before: "旧值", after: "回退后的值" }] },
    }),
  ]);
  assert.deepEqual(result.cells, [{ rowId: "r1", columnId: "c1", before: "旧值", after: "回退后的值" }]);
});

// column_delete 的顶层 cells 数组形状是 {row_id, content_md}（没有 column_id/
// before/after）——不属于 cell_update/revert 那套 4 字段形状。foldLocalChanges
// 只在 kind 恰为 cell_update/revert 时才读 payload.cells，column_delete 这条
// 流水应被完全忽略（不产出任何格子净变化条目），而不是把 content_md 误当
// after 读出一条 {columnId: undefined} 的坏数据。
test("foldLocalChanges: column_delete 的顶层 cells（{row_id,content_md} 形状）不被误读为格子净变化", () => {
  const result = foldLocalChanges([
    chg({
      kind: "column_delete",
      payload: {
        column: { id: "c1", name: "备注" },
        cells: [{ row_id: "r1", content_md: "曾经的内容" }],
      },
    }),
  ]);
  assert.deepEqual(result.cells, []);
});

test("foldLocalChanges: 空数组返回三个空字段", () => {
  assert.deepEqual(foldLocalChanges([]), { cells: [], rowsAdded: [], rowsRemoved: [] });
});

test("isStaleHead: 看到的 head 落后于实际即为陈旧", () => {
  assert.equal(isStaleHead(50, 53), true);
  assert.equal(isStaleHead(53, 53), false);
});

test("isStaleHead: 看到的 head 大于实际（理论不应发生，但不应崩）也判定为陈旧", () => {
  assert.equal(isStaleHead(60, 53), true);
});
