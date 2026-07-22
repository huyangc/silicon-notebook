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
// foldLocalChanges 的输出（面向展示，不再回旋到网络）才按本文件约定统一用
// camelCase。

import type { KnowhowChange, KnowhowChangeKind } from "./knowhow-model.ts";
import { ROLE_LABELS, type CellKind } from "./knowhow-model.ts";

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
// ⚠ 本地折叠 ≠ 服务端全量 diff：两版对比视图请用后者
// （fetchKnowhowHistoryDiff，knowhow-model.ts），不要用这个函数——接错不会有
// 类型错误，只会静默漏掉列/表元变化，详见下文范围差异。
//
// 这是一次**客户端本地**的折叠，操作对象是调用方已经持有的一段 KnowhowChange
// 数组（例如时间线里已加载的若干条）——不等价于 fetchKnowhowHistoryDiff() 那次
// 服务端权威计算（GET .../history/diff，对应后端 aggregate_diff()）。两者刻意
// 是两个不同的类型/函数（函数名 foldLocalChanges、结果类型名 LocalDiffResult
// 都特意带上"Local"，把这条范围差异钉进名字里）：
//   - 本函数只折叠"格子净变化"与"行的净增删"，不处理列结构变更
//     （column_add/delete/rename/kind/anchor_set）与表元变更（table_meta）——
//     LocalDiffResult 类型本身就只有 3 个键，没有地方放这些信息。
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
export interface LocalDiffCell {
  rowId: string;
  columnId: string;
  before: string | null;
  after: string | null;
}

export interface LocalDiffResult {
  cells: LocalDiffCell[];
  rowsAdded: string[];
  rowsRemoved: string[];
}

export function foldLocalChanges(changes: KnowhowChange[]): LocalDiffResult {
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

  const cells: LocalDiffCell[] = [];
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

// --- 列结构字段级变更描述（两版对比 columns[] 桶专用，Task 15）-----------------
//
// 服务端 aggregate_diff() 的 columns[] 桶把 column_add/column_delete/
// column_rename/column_kind/anchor_set 四种事件按字段（name/role/position）
// 统一折叠进同一个 {before, after} Partial 形状（app/services/knowhow/
// history.py note_field/apply_column_added/apply_column_removed 逐字核对）：
// 新增列表现为 before={name:null,role:null,position:null}/after={真实值}
// （apply_column_added 对 3 个字段一律先记 before=None）；删除列相反
// （apply_column_removed 一律后记 after=None）；单纯改名/改类型/切换行标题
// 列时只有相应的一个字段（name 或 role）会出现在 before/after 里——position
// 目前没有任何事件单独触发，只会跟随新增/删除一起出现。
//
// 这个"key 存在但值为 null 代表原本不存在"的编码，只有聚合 diff 这一个桶
// 需要——单条变更展开视图不必读这个函数：单条 payload 已经明确知道自己是
// column_add 还是 column_delete，不需要从字段模式反推"是加是删"（那些 kind
// 有自己的 payload 形状，直接读 payload.column 即可，见 knowhow-history-
// drawer.tsx 的按 kind 分派渲染）。
export type ColumnFieldSnapshot = Partial<{
  name: string | null;
  role: string | null;
  position: number | null;
}>;

export interface ColumnChangeDescription {
  columnId: string;
  /** 这一条目该挂什么名字展示——优先用变化后的名字，该列已被删除时用变化前
   * 的名字；本次只是字段级改了 role/position、没有触碰 name（比如单独切换
   * 行标题列）两者都拿不到时，落到调用方传入的兜底名字（通常是当前表里这
   * 一列的实时名字）；连兜底都没有才用通用占位。 */
  label: string;
  /** 人话描述行，可能不止一条（如同一区间内又改名又改类型）。 */
  lines: string[];
}

// 导出（非本文件私有）：knowhow-history-drawer.tsx 单条变更展开视图渲染
// column_kind/anchor_set 时，payload 里的 before/after 就是裸的 role 字符串
// （不经过本文件的 describeColumnChange），需要同一份"role 值 -> 中文标签"
// 映射，不另外复制一份可能与这里漂移的翻译表。
export function roleLabel(value: string | null | undefined): string {
  if (value == null) return "";
  return value in ROLE_LABELS ? ROLE_LABELS[value as CellKind] : value;
}

export function describeColumnChange(
  columnId: string,
  before: ColumnFieldSnapshot,
  after: ColumnFieldSnapshot,
  fallbackLabel?: string,
): ColumnChangeDescription {
  const label = after.name ?? before.name ?? fallbackLabel ?? "（未知列）";
  // 新增/删除：name 字段一定跟 role/position 一起出现（apply_column_added/
  // apply_column_removed 对 3 个字段一视同仁地记，见上方头注释），用 name
  // 是否为 null 当判据就足够，不需要同时核对另外两个字段。
  const added = "name" in before && before.name == null && after.name != null;
  const removed = "name" in after && after.name == null && before.name != null;
  if (added) {
    const roleSuffix = after.role ? `（类型：${roleLabel(after.role)}）` : "";
    return { columnId, label, lines: [`新增列「${label}」${roleSuffix}`] };
  }
  if (removed) {
    return { columnId, label, lines: [`删除列「${label}」`] };
  }

  const lines: string[] = [];
  if ("name" in before && "name" in after && before.name !== after.name) {
    lines.push(`列改名：「${before.name ?? ""}」→「${after.name ?? ""}」`);
  }
  if ("role" in before && "role" in after && before.role !== after.role) {
    if (before.role === "anchor") lines.push(`「${label}」不再是行标题列`);
    else if (after.role === "anchor") lines.push(`「${label}」设为行标题列`);
    else lines.push(`内容类型：${roleLabel(before.role)} → ${roleLabel(after.role)}`);
  }
  if ("position" in before && "position" in after && before.position !== after.position) {
    lines.push("列顺序发生变化");
  }
  if (lines.length === 0) lines.push(`列「${label}」发生变化`);
  return { columnId, label, lines };
}

// --- 回退影响预览（确认框「将影响 N 行、M 个格子」，Task 15）-------------------
//
// 抽屉在用户点「回到这里」时，用本地已加载的时间线对目标 seq 之后、直到当前
// head 的这段区间跑一次 foldLocalChanges，拿到粗略的行/格子净变化数目，连同
// 同一段 changes 原始数组一起喂给本函数拼成一句确认文案——这不是权威两版对比
// （同 knowhow-model.ts fetchKnowhowHistoryDiff 头注释的同一条警告），只是确认
// 框里给用户一个大致量级的提示，帮助其判断这次回退动作有多大。
//
// ⚠ 评审修复（混合区间漏报列结构变化）：foldLocalChanges 只折叠 cells 与行
// 增删（其定义如此，不动它），不覆盖列结构变更（column_add/column_delete/
// column_rename/column_kind/anchor_set）与表元变更（table_meta）。早期实现
// 只用 foldLocalChanges 的输出算「行数/格子数」喂给本函数，混合区间（比如
// 区间内同时有一次 column_rename 和一次 cell_update）会整个落到「将影响 N
// 个格子」分支——列结构变化被完全吞掉，用户在不知情的情况下点确认，回退后
// 才发现列名也被撤销了。这是破坏性操作的确认框，必须说全。现在第三个参数
// 直接是原始 changes 数组（不再是单纯的计数），本函数自己按 kind 统计列结构
// 变化与表元变化这两类，与行/格子一起体现在文案里——任何一类有变化都必须
// 出现在文案里，不能被其它类目吞掉。
const COLUMN_STRUCTURE_KINDS: ReadonlySet<string> = new Set([
  "column_add", "column_delete", "column_rename", "column_kind", "anchor_set",
]);

export function summarizeRevertImpact(
  rowsTouched: number,
  cellsTouched: number,
  changes: KnowhowChange[],
): string {
  if (changes.length === 0) return "不会撤销任何改动";
  const structureCount = changes.filter((c) => COLUMN_STRUCTURE_KINDS.has(c.kind)).length;
  const metaCount = changes.filter((c) => c.kind === "table_meta").length;
  const parts: string[] = [];
  if (rowsTouched > 0) parts.push(`${rowsTouched} 行`);
  if (cellsTouched > 0) parts.push(`${cellsTouched} 个格子`);
  if (structureCount > 0) parts.push(`${structureCount} 处列结构变化`);
  if (metaCount > 0) parts.push(`${metaCount} 处表信息变更`);
  // 残余兜底：区间内确实有变更，但既不涉及行/格子，也不是列结构/表元
  // （如仅代码附件 cell_code_put/cell_code_delete）——理论上少见，但防御性
  // 地不产出空文案。
  if (parts.length === 0) {
    return "将撤销一些改动（不涉及行、格子、列结构或表信息）";
  }
  return `将影响 ${parts.join("、")}`;
}

// --- 单格历史「恢复此版本」可用性（knowhow 表版本管理 Task 16）---------------
//
// 格子浮窗历史页签（knowhow-cell-history.tsx）为 fetchKnowhowCellHistory 返回
// 的每条历史条目决定是否展示「恢复此版本」按钮。canEdit 门控（规格⑦「只读
// 成员看得到历史、看不到恢复按钮」）由调用方在渲染处另行 `canEdit && ...`
// 判断，不并入本函数——本函数只回答"内容层面是否有必要展示"，与权限判断分离
// 便于独立单测（同本文件其余函数一贯只管"这一件事"的取向）。
//
// after 为 null 的两种可能来源（knowhow_history_store.py _cell_entries_in_change
// 的 row_delete/column_delete/revert.rows_removed/columns_removed 四个分支）：
// 这一条历史对应的是"这一格当时所在的行/列被删除"，不是"格子内容被清空"
// （清空是 after===""，一个合法的可恢复空字符串，与 null 语义不同）。
// patchKnowhowCell 只能写入字符串内容，没有对应"恢复行/列存在性"的语义，
// 这类条目恒不可恢复——即便当前这一格确实存在（行/列被删后又被回退带回，
// 同一个 row_id/column_id 可能在更晚的条目里重新出现 after 有值，那些条目
// 各自独立判定，不受这一条 after=null 影响）。
export function isCellHistoryEntryRestorable(after: string | null, currentContentMd: string): boolean {
  if (after === null) return false;
  // 与当前实时内容完全相同：恢复等于原样重写一遍，没有意义，不展示（镜像
  // knowhow-history-drawer.tsx 对当前 head 隐藏「回到这里」按钮的既有取向，
  // 见该文件 isHead 用法）。
  return after !== currentContentMd;
}
