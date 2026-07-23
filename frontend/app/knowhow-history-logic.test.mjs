import { test } from "node:test";
import assert from "node:assert/strict";
import {
  summarizeChange, originLabel, groupChangesByDay, foldLocalChanges, isStaleHead,
  describeColumnChange, summarizeRevertImpact, isCellHistoryEntryRestorable,
  mergeMilestoneTimeline, earliestComparePoint,
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
  assert.equal(originLabel("llm_complete"), "智能补全");
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

// --- describeColumnChange（两版对比 columns[] 桶——新增/删除/改名/改类型/
// 行标题列切换/顺序变化，逐场景覆盖 aggregate_diff 的 null 编码约定） --------

test("describeColumnChange: 纯新增列——name 为 null 判据，附带类型标签", () => {
  const result = describeColumnChange(
    "c1",
    { name: null, role: null, position: null },
    { name: "现象", role: "attribute", position: 2 },
  );
  assert.equal(result.label, "现象");
  assert.deepEqual(result.lines, ["新增列「现象」（类型：普通）"]);
});

test("describeColumnChange: 纯删除列——after.name 为 null 判据", () => {
  const result = describeColumnChange(
    "c1",
    { name: "现象", role: "attribute", position: 2 },
    { name: null, role: null, position: null },
  );
  assert.equal(result.label, "现象");
  assert.deepEqual(result.lines, ["删除列「现象」"]);
});

test("describeColumnChange: 只改名", () => {
  const result = describeColumnChange("c1", { name: "旧名" }, { name: "新名" });
  assert.equal(result.label, "新名");
  assert.deepEqual(result.lines, ["列改名：「旧名」→「新名」"]);
});

test("describeColumnChange: 只改内容类型（非行标题相关）", () => {
  const result = describeColumnChange("c1", { role: "attribute" }, { role: "procedure" }, "现象");
  assert.deepEqual(result.lines, ["内容类型：普通 → 方法步骤"]);
});

test("describeColumnChange: 设为行标题列——role 变化涉及 anchor 时走专属文案，不落到内容类型那句", () => {
  const result = describeColumnChange("c1", { role: "attribute" }, { role: "anchor" }, "现象");
  assert.equal(result.label, "现象"); // name 键本身不在场，落到调用方传入的兜底名字
  assert.deepEqual(result.lines, ["「现象」设为行标题列"]);
});

test("describeColumnChange: 取消行标题列", () => {
  const result = describeColumnChange("c1", { role: "anchor" }, { role: "attribute" }, "现象");
  assert.deepEqual(result.lines, ["「现象」不再是行标题列"]);
});

test("describeColumnChange: 同一区间内又改名又改类型——两条描述都出现", () => {
  const result = describeColumnChange(
    "c1",
    { name: "旧名", role: "attribute" },
    { name: "新名", role: "procedure" },
  );
  assert.deepEqual(result.lines, ["列改名：「旧名」→「新名」", "内容类型：普通 → 方法步骤"]);
});

test("describeColumnChange: 只有顺序变化", () => {
  const result = describeColumnChange("c1", { position: 1 }, { position: 3 }, "现象");
  assert.deepEqual(result.lines, ["列顺序发生变化"]);
});

test("describeColumnChange: name/fallbackLabel 都拿不到时落到通用占位", () => {
  const result = describeColumnChange("c1", { role: "attribute" }, { role: "procedure" });
  assert.equal(result.label, "（未知列）");
  assert.deepEqual(result.lines, ["内容类型：普通 → 方法步骤"]);
});

test("describeColumnChange: 未知角色值原样回显而不是崩掉", () => {
  const result = describeColumnChange("c1", { role: "future_kind" }, { role: "attribute" }, "X");
  assert.deepEqual(result.lines, ["内容类型：future_kind → 普通"]);
});

test("describeColumnChange: 没有任何字段差异时兜底一句通用描述（防御性，正常不应发生）", () => {
  const result = describeColumnChange("c1", {}, {}, "现象");
  assert.deepEqual(result.lines, ["列「现象」发生变化"]);
});

// --- summarizeRevertImpact（回退确认框「将影响 N 行、M 个格子」） -----------------
//
// 第三个参数是原始 changes 数组（评审修复后不再是单纯计数，见函数头注释）：
// 下面凡是只想孤立测试"行/格子计数"文案、不关心列结构/表元的用例，用
// neutralChanges(n) 造 n 条默认 kind（cell_update，不落在 COLUMN_STRUCTURE_
// KINDS 或 table_meta 里）的占位变更，只贡献 changes.length，不影响
// structureCount/metaCount。
const neutralChanges = (n) => Array.from({ length: n }, (_, i) => chg({ seq: i + 1 }));

test("summarizeRevertImpact: 区间内没有任何变更", () => {
  assert.equal(summarizeRevertImpact(0, 0, []), "不会撤销任何改动");
});

test("summarizeRevertImpact: 行与格子都有涉及", () => {
  assert.equal(summarizeRevertImpact(2, 3, neutralChanges(4)), "将影响 2 行、3 个格子");
});

test("summarizeRevertImpact: 只涉及行", () => {
  assert.equal(summarizeRevertImpact(2, 0, neutralChanges(2)), "将影响 2 行");
});

test("summarizeRevertImpact: 只涉及格子", () => {
  assert.equal(summarizeRevertImpact(0, 3, neutralChanges(3)), "将影响 3 个格子");
});

test("summarizeRevertImpact: 区间内有列结构变化与表元变化（不涉及行或格子）——两类都要出现，不再吞并成笼统提示", () => {
  const changes = [
    chg({ seq: 1, kind: "column_add", payload: { column: { name: "根因分析" } } }),
    chg({ seq: 2, kind: "table_meta", payload: { before: {}, after: {} } }),
  ];
  assert.equal(summarizeRevertImpact(0, 0, changes), "将影响 1 处列结构变化、1 处表信息变更");
});

test("summarizeRevertImpact: 区间内全是既非行列格子、也非列结构/表元的变更（如仅代码附件）——兜底提示不为空", () => {
  const changes = [chg({ seq: 1, kind: "cell_code_put", payload: {} })];
  assert.equal(summarizeRevertImpact(0, 0, changes), "将撤销一些改动（不涉及行、格子、列结构或表信息）");
});

// 评审发现（Moderate，必修）：混合区间（区间内同时有列结构变化与格子变化）
// 早期实现只从 foldLocalChanges 的行/格子净变化算文案，列结构变化会被完全
// 吞掉——用户在不知情的情况下确认回退，回退后才发现列名也被撤销。下面两条
// 分别覆盖"列结构 + 格子"与"表元 + 格子"两种吞掉方式，确保两类各自都不会
// 被另一类目吞掉。
test("summarizeRevertImpact: 混合区间（列改名 + 格子改动）——列结构变化不能被格子变化吞掉（评审发现）", () => {
  const changes = [
    chg({ seq: 1, kind: "column_rename", payload: { column_id: "c1", before: "旧名", after: "新名" } }),
    chg({ seq: 2, kind: "cell_update", payload: { cells: [{ row_id: "r1", column_id: "c1", before: "A", after: "B" }] } }),
  ];
  const text = summarizeRevertImpact(0, 1, changes);
  assert.ok(text.includes("个格子"), `期望提到格子变化，实际："${text}"`);
  assert.ok(text.includes("列结构变化"), `期望提到列结构变化，实际："${text}"`);
});

test("summarizeRevertImpact: 混合区间（表信息改动 + 格子改动）——表元变化同样不能被格子变化吞掉", () => {
  const changes = [
    chg({ seq: 1, kind: "cell_update", payload: { cells: [{ row_id: "r1", column_id: "c1", before: "A", after: "B" }] } }),
    chg({ seq: 2, kind: "table_meta", payload: { before: { title: "旧标题" }, after: { title: "新标题" } } }),
  ];
  const text = summarizeRevertImpact(0, 1, changes);
  assert.ok(text.includes("个格子"), `期望提到格子变化，实际："${text}"`);
  assert.ok(text.includes("表信息变更"), `期望提到表元变化，实际："${text}"`);
});

test("summarizeRevertImpact: 行/格子/列结构/表元四类同时出现——文案里一个都不能少", () => {
  const changes = [
    chg({ seq: 1, kind: "row_add", payload: { rows: [{ row_id: "r1" }] } }),
    chg({ seq: 2, kind: "cell_update", payload: { cells: [{ row_id: "r2", column_id: "c1", before: "A", after: "B" }] } }),
    chg({ seq: 3, kind: "column_kind", payload: { column_id: "c1", before: "attribute", after: "procedure" } }),
    chg({ seq: 4, kind: "table_meta", payload: { before: {}, after: {} } }),
  ];
  const text = summarizeRevertImpact(1, 1, changes);
  assert.ok(text.includes("1 行"), `期望提到行变化，实际："${text}"`);
  assert.ok(text.includes("1 个格子"), `期望提到格子变化，实际："${text}"`);
  assert.ok(text.includes("列结构变化"), `期望提到列结构变化，实际："${text}"`);
  assert.ok(text.includes("表信息变更"), `期望提到表元变化，实际："${text}"`);
});

// --- isCellHistoryEntryRestorable（单格历史「恢复此版本」可用性，Task 16）---

test("isCellHistoryEntryRestorable: after 为 null（所在行/列当时已被删除）恒不可恢复", () => {
  assert.equal(isCellHistoryEntryRestorable(null, "任意当前内容"), false);
  assert.equal(isCellHistoryEntryRestorable(null, ""), false);
});

test("isCellHistoryEntryRestorable: after 与当前实时内容相同——恢复没有意义，不可恢复", () => {
  assert.equal(isCellHistoryEntryRestorable("同一段内容", "同一段内容"), false);
  assert.equal(isCellHistoryEntryRestorable("", ""), false);
});

test("isCellHistoryEntryRestorable: after 是与当前不同的非空字符串——可恢复", () => {
  assert.equal(isCellHistoryEntryRestorable("历史上的旧内容", "当前的新内容"), true);
});

test("isCellHistoryEntryRestorable: after 是空字符串且当前非空——可恢复（清空是合法的可恢复状态，不同于 null）", () => {
  assert.equal(isCellHistoryEntryRestorable("", "当前非空内容"), true);
});


// --- codex 第 2 轮 P2：里程碑在旧页 / 两版对比最早起点 -----------------------
// 复用文件顶部既有的 chg({ ...override }) helper（别重定义，const 重声明会崩）。

test("mergeMilestoneTimeline: 里程碑对应流水在旧页（不在已加载 changes）也能显示", () => {
  const loaded = [chg({ seq: 50 }), chg({ seq: 49 })];        // 当前页
  const milestoneSeqs = new Set([50, 12]);                    // 12 在更旧的一页
  const extra = [chg({ seq: 12, kind: "table_meta" })];       // 被单独抓回来的那条
  const merged = mergeMilestoneTimeline(loaded, milestoneSeqs, extra);
  assert.deepEqual(merged.map((c) => c.seq), [50, 12], "seq 倒序，含补抓回来的 12");
});

test("mergeMilestoneTimeline: 已加载的优先于补抓的（同 seq 不重复）", () => {
  const loaded = [chg({ seq: 50, kind: "cell_update" })];
  const merged = mergeMilestoneTimeline(loaded, new Set([50]), [chg({ seq: 50, kind: "revert" })]);
  assert.equal(merged.length, 1);
  assert.equal(merged[0].kind, "cell_update", "同 seq 保留已加载的权威副本");
});

test("mergeMilestoneTimeline: 非里程碑 seq 不进结果", () => {
  const merged = mergeMilestoneTimeline([chg({ seq: 50 }), chg({ seq: 49 })], new Set([50]), []);
  assert.deepEqual(merged.map((c) => c.seq), [50]);
});

test("earliestComparePoint: 链条到达建表 → seq 0「表创建之初」", () => {
  const changes = [chg({ seq: 3 }), chg({ seq: 2 }), chg({ seq: 1, kind: "table_create" })];
  assert.deepEqual(earliestComparePoint(changes, false), {
    value: 0, label: "最早（表创建之初）",
  });
});

test("earliestComparePoint: 存量表/prune 后（最老不是建表）→ 最早保留 seq−1、不谎称建表", () => {
  const changes = [chg({ seq: 9 }), chg({ seq: 8 }), chg({ seq: 7 })];  // 最老 seq=7，非建表
  assert.deepEqual(earliestComparePoint(changes, false), {
    value: 6, label: "最早（保留的记录起点）",
  });
});

test("earliestComparePoint: 还有更早未加载（hasMoreOlder）→ 不给「最早」选项", () => {
  assert.equal(earliestComparePoint([chg({ seq: 9 }), chg({ seq: 8 })], true), null);
});

test("earliestComparePoint: 空 changes → null", () => {
  assert.equal(earliestComparePoint([], false), null);
});
