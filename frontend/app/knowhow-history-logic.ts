// Knowhow 表版本管理 — 前端纯逻辑（无 JSX，可被 knowhow-history-logic.test.mjs
// 直接 import）。knowhow-history-drawer.tsx / knowhow-cell-history.tsx（后续任务）
// 含 JSX，Node 原生 TS 类型剥离不支持 .tsx（仅 .ts/.mts/.cts 可被 node --test
// 直接 import），故把变更摘要文案 / 来源徽章映射 / 按天分组 / 区间 diff 聚合 /
// 陈旧判定这些可测纯逻辑单独抽出（镜像 knowhow-panel.tsx <-> knowhow-panel-
// logic.ts 的既有拆分）。
//
// KnowhowChange/KnowhowChangeKind 类型定义在 knowhow-model.ts（该文件是本仓库
// wire<->domain 映射的唯一权威落点，见其头注释），本文件只 import 类型、不重
// 复定义——与 knowhow-panel-logic.ts 从 knowhow-model.ts 引入 KnowhowColumn/
// KnowhowRow/ProjectionStatus 的既有约定一致。
//
// ⚠ payload 字段名以后端真实形状为准（design doc
// 2026-07-22-knowhow-table-version-control-design.md §4.4），不是任务简报最初
// 猜的驼峰形——knowhow-model.ts 的 mapChange() 刻意不对 payload 做逐 kind 递归
// camelCase 转换（14 种 kind 形状差异很大，转换成本与出错面都不小；payload 类
// 型本就声明为 Record<string, any> 的宽松透传），所以这里读到的是后端原始
// snake_case 字段（row_id/column_id/target_seq/rows 等）。summarizeChange /
// aggregateDiff 的输出（面向展示，不再回旋到网络）才按本文件约定统一用
// camelCase。

import type { KnowhowChange, KnowhowChangeKind } from "./knowhow-model.ts";

export type { KnowhowChange, KnowhowChangeKind };

// --- 来源徽章 ------------------------------------------------------------------

// 与后端 VALID_ORIGINS（app/models/knowhow.py）逐字匹配——7 个值；此处只是给
// 时间线加一个可读徽章。"user"（人工手动编辑，时间线上占绝大多数）故意给空
// 字符串：不加徽章，免得每一条都挂一个「手动」标签刷屏——只有非默认来源才
// 值得被特别标出。
const ORIGIN_LABELS: Record<string, string> = {
  user: "",
  llm_optimize: "表达优化",
  llm_reformat: "格式规整",
  import: "导入",
  agent: "Agent",
  revert: "回退",
  backfill: "批量回填",
};

// 未知来源（未来新增 origin 值、或线上后端版本领先前端）原样回显而不是崩掉
// 或吞成空——防御性与本文件其余「未知枚举值不炸」的既有取向一致（见
// knowhow-model.ts 对未知 kind 的处理）。
export function originLabel(origin: string): string {
  return origin in ORIGIN_LABELS ? ORIGIN_LABELS[origin] : origin;
}

// --- 变更摘要文案 ---------------------------------------------------------------

// 按 kind 给一句人话摘要，用于时间线每一行的一句话描述。14 种 kind 逐一覆盖
// （与 KnowhowChangeKind 的字面量集合一一对应），未知 kind 落到 default 分支，
// 不抛异常。
export function summarizeChange(change: KnowhowChange): string {
  const payload = change.payload ?? {};
  switch (change.kind) {
    case "cell_update":
      return `修改了 ${(payload.cells ?? []).length} 个格子`;
    case "row_add":
      return `新增了 ${(payload.rows ?? []).length} 行`;
    case "import_append":
      return `导入追加了 ${(payload.rows ?? []).length} 行`;
    case "row_delete":
      return `删除了 ${(payload.rows ?? []).length} 行`;
    case "column_add":
      return `新增了列「${payload.column?.name ?? ""}」`;
    case "column_delete":
      return `删除了列「${payload.column?.name ?? ""}」`;
    case "column_rename":
      return `列改名：${payload.before} → ${payload.after}`;
    case "column_kind":
      return "修改了列的内容类型";
    case "anchor_set":
      return "修改了行标题列";
    case "table_meta":
      return "修改了表信息";
    case "cell_code_put":
      return "更新了格子代码";
    case "cell_code_delete":
      return "删除了格子代码";
    case "table_create":
      return change.note || "建表";
    case "revert":
      // 后端 payload 字段名是 target_seq（snake_case，_revert_payload 与设计
      // 文档 §4.4 的 revert 形状一致），不是 targetSeq。
      return `回退到 #${payload.target_seq}`;
    default:
      return "修改";
  }
}

// --- 按天分组 ------------------------------------------------------------------

// 时间线按天分组展示。调用方保证传入的 changes 已按 seq/createdAt 倒序（后端
// GET .../history 天然按 seq DESC 返回）——本函数只做"相邻同天合并"的一次线性
// 扫描，不做排序，也不假设跨天重复出现（乱序输入会产出多个同日期分组而不是
// 报错，属于宽松而非严格校验，与本文件其余函数的防御性取向一致）。
export function groupChangesByDay(
  changes: KnowhowChange[],
): { day: string; changes: KnowhowChange[] }[] {
  const groups: { day: string; changes: KnowhowChange[] }[] = [];
  for (const change of changes) {
    const day = (change.createdAt ?? "").slice(0, 10);
    const last = groups[groups.length - 1];
    if (last && last.day === day) last.changes.push(change);
    else groups.push({ day, changes: [change] });
  }
  return groups;
}

// --- 区间净变化（客户端本地折叠，不经网络）--------------------------------------
//
// 这是一次**客户端本地**的折叠，操作对象是调用方已经持有的一段 KnowhowChange
// 数组（例如时间线里已加载的若干条）——不等价于 fetchKnowhowHistoryDiff() 那次
// 服务端权威计算（GET .../history/diff，对应后端 aggregate_diff()）。两者刻意
// 是两个不同的类型/函数：
//   - 本函数只折叠"格子净变化"与"行的净增删"，不处理列结构变更
//     （column_add/delete/rename/kind/anchor_set）与表元变更（table_meta）——
//     DiffResult 类型本身就只有 3 个键，没有地方放这些信息。
//   - 需要完整净变化（含列结构、表元）时用 fetchKnowhowHistoryDiff()——后端
//     aggregate_diff() 的 5 键输出由 KnowhowHistoryDiff 类型承载，见
//     knowhow-model.ts。
//
// cells 折叠规则与后端 aggregate_diff 对齐：按 (row_id, column_id) 取"区间内
// 第一条的 before"与"最后一条的 after"，两者相等则不出现在结果里。
//
// 行折叠只识别 row_add/import_append/row_delete，以及 revert 自身携带的
// rows_added/rows_removed（历史教训：Task 11 的服务端 aggregate_diff 起初漏了
// revert 分支，导致跨越一次回退的区间 diff 静默丢内容，见
// .superpowers/sdd/progress.md Task 11 记录——这里同样显式处理 revert，不重演
// 那个坑）。column_delete payload 里同样有一个顶层 cells 数组，但形状是
// {row_id, content_md}（没有 column_id/before/after）——特意不在这里读它：
// 只对 kind==='cell_update'|'revert' 才读 payload.cells，避免把形状不兼容的
// 数组当成格子净变化误读（哪怕误读也会因 before/after 都读到 undefined 而被
// 后面的"before===after 跳过"过滤掉，但那是巧合，不该依赖巧合）。
export interface DiffCell {
  rowId: string;
  columnId: string;
  before: string | null;
  after: string | null;
}

export interface DiffResult {
  cells: DiffCell[];
  rowsAdded: string[];
  rowsRemoved: string[];
}

export function aggregateDiff(changes: KnowhowChange[]): DiffResult {
  // (row_id -> (column_id -> [区间内第一条 before, 目前为止最后一条 after]))；
  // 用嵌套 Map 而非字符串拼接键，避免 id 里若恰好出现分隔符导致的键冲突
  // （row_id/column_id 目前都是 uuid 派生的短 hex id，本不会有这个问题，但
  // 嵌套 Map 零额外成本且更直接地表达"这是复合键"）。
  const cellAcc = new Map<string, Map<string, [string | null, string | null]>>();
  const rowState = new Map<string, "added" | "removed">();

  function noteCell(rowId: string, columnId: string, before: string | null, after: string | null): void {
    let byColumn = cellAcc.get(rowId);
    if (!byColumn) {
      byColumn = new Map();
      cellAcc.set(rowId, byColumn);
    }
    const existing = byColumn.get(columnId);
    if (!existing) byColumn.set(columnId, [before, after]);
    else existing[1] = after; // 保留首条 before，滚动更新到最后一条 after
  }

  // 无条件抵消（design doc §6.5 明文：不比较行内容）：同一行在区间内先增后删
  // 或先删后增，两边都不出现；重复同一状态（如两条 row_delete 误指同一行，
  // 现实中不应发生但防御性地不特殊处理）保持该状态不变。
  function noteRow(rowId: string, status: "added" | "removed"): void {
    const existing = rowState.get(rowId);
    if (existing !== undefined && existing !== status) rowState.delete(rowId);
    else rowState.set(rowId, status);
  }

  for (const change of changes) {
    const payload = change.payload ?? {};
    if (change.kind === "cell_update" || change.kind === "revert") {
      for (const cell of payload.cells ?? []) {
        noteCell(cell.row_id, cell.column_id, cell.before ?? null, cell.after ?? null);
      }
    }
    if (change.kind === "row_add" || change.kind === "import_append") {
      for (const row of payload.rows ?? []) noteRow(row.row_id, "added");
    }
    if (change.kind === "row_delete") {
      for (const row of payload.rows ?? []) noteRow(row.row_id, "removed");
    }
    if (change.kind === "revert") {
      for (const row of payload.rows_added ?? []) noteRow(row.row_id, "added");
      for (const row of payload.rows_removed ?? []) noteRow(row.row_id, "removed");
    }
  }

  const cells: DiffCell[] = [];
  for (const [rowId, byColumn] of cellAcc) {
    for (const [columnId, [before, after]] of byColumn) {
      if (before === after) continue; // 改了又改回去：不出现
      cells.push({ rowId, columnId, before, after });
    }
  }

  return {
    cells,
    rowsAdded: [...rowState].filter(([, status]) => status === "added").map(([rowId]) => rowId),
    rowsRemoved: [...rowState].filter(([, status]) => status === "removed").map(([rowId]) => rowId),
  };
}

// --- 陈旧判定 ------------------------------------------------------------------

// 前端持有的 head（上次拉时间线时看到的 headSeq）是否已落后于服务端当前真实
// head。用于在用户点「回退」前先本地判断一次，给出"已被别人改过，请刷新"的
// 提示——服务端 revert 端点自己也会用 expected_head_seq 做同款校验并 409，这
// 里只是让前端能在发请求前就给出提示，不是安全边界（真正的把关在服务端）。
export function isStaleHead(seenHeadSeq: number, actualHeadSeq: number): boolean {
  return seenHeadSeq !== actualHeadSeq;
}
