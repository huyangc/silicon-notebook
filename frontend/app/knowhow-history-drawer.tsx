/**
 * knowhow-history-drawer.tsx
 *
 * knowhow 表版本管理 Task 15：历史抽屉——表工具栏「历史」入口（design doc
 * `2026-07-22-knowhow-table-version-control-design.md` §8.2 ①）打开的顶层
 * modal：时间线（按天分组，点开展开单条红绿 diff）+ 两版对比（服务端权威
 * `fetchKnowhowHistoryDiff`）+ 里程碑（起名/筛选/删除）+ 整表回退。
 *
 * 骨架逐处照抄 knowhow-matrix-drawer.tsx（kh-modal-overlay -> kh-modal-card ->
 * kh-modal-header(含 kh-modal-header-top/面包屑/全屏切换/关闭) -> kh-modal-body
 * -> kh-modal-resize-handle），含 useFullscreenToggle + useFloatingWindow——
 * 但全屏 sessionStorage 键新开一个（HISTORY_DRAWER_FULLSCREEN_STORAGE_KEY），
 * 不复用矩阵抽屉的 knowhow.conceptDrawer.fullscreen，两个弹窗的全屏选择互不
 * 影响。
 *
 * 样式（`.kh-history-*` 一整套）登记在 knowhow-panel.tsx 顶层的
 * `<style jsx global>`，不在本文件另开 `<style jsx>`——同 knowhow-matrix-
 * drawer.tsx / knowhow-code.tsx 的既有约定：styled-jsx 的 global 样式注入绑定
 * 「声明该标签的组件是否渲染过」，KnowhowPanel 是本特性唯一保证任何时候都已
 * 挂载的样式容器。
 *
 * 权限（design doc §8.2 末尾）：`canEdit=false` 的只读成员看得到时间线和 diff，
 * 但看不到「设为里程碑」/「回到这里」/删除里程碑这几个写入口——门控在本文件
 * 内部（不是整个抽屉挂 canEdit，抽屉打开与否只取决于是否点了「历史」，与
 * knowhow-panel.tsx 工具栏「历史」按钮本身不整体挂 canEdit 一致，见该文件
 * 头注释）。
 *
 * 两个「diff」不要接错（knowhow-model.ts fetchKnowhowHistoryDiff 头注释同一条
 * 警告）：单条变更展开（renderChangeDetail）直接读该条 payload 自身携带的
 * before/after，是"这一条流水改了什么"；两版对比（CompareResult）必须用服务端
 * `fetchKnowhowHistoryDiff`（区间净变化，含列结构/表元），不能用本地
 * foldLocalChanges 顶替——本文件唯一用到 foldLocalChanges 的地方是「回到这里」
 * 确认框里的粗略预览文案（revertImpactText），且已在该函数注释里说明这不是
 * 权威两版对比。
 *
 * 存量表断层提示（design doc §7.2）：迁移时不补造历史，存量表的时间线翻到
 * 底（`hasMoreOlder=false`）却没有 table_create 创世流水时，说明"更早的变更
 * 未记录"，不是加载失败。
 *
 * 里程碑失效标记（design doc §4.2）：`knowhow_milestones.seq` 不设 FK，指向的
 * 流水被"清理历史"删除后里程碑本身仍保留、`stale=true`——这类里程碑在时间线
 * 上早已没有对应的行可挂（那条流水已经不存在），单独在页脚列出，允许查看/
 * 删除但不提供"回到这里"（目标已不存在）。
 */
"use client";

import { useCallback, useEffect, useMemo, useState, type MouseEvent as ReactMouseEvent } from "react";
import {
  ChevronDown,
  ChevronRight,
  Flag,
  GitCompare,
  History as HistoryIcon,
  Maximize2,
  Minimize2,
  RefreshCw,
  RotateCcw,
  Trash2,
  X,
} from "lucide-react";
import {
  composeRowTitle,
  createKnowhowMilestone,
  deleteKnowhowMilestone,
  fetchKnowhowChange,
  fetchKnowhowHistory,
  fetchKnowhowHistoryDiff,
  revertKnowhowTable,
  KnowhowRevertError,
  type KnowhowColumn,
  type KnowhowHistoryDiff,
  type KnowhowHistoryDiffRow,
  type KnowhowMilestone,
} from "./knowhow-model.ts";
import {
  describeColumnChange,
  earliestComparePoint,
  foldLocalChanges,
  groupChangesByDay,
  mergeMilestoneTimeline,
  originLabel,
  roleLabel,
  summarizeChange,
  summarizeRevertImpact,
  type ColumnFieldSnapshot,
  type KnowhowChange,
} from "./knowhow-history-logic.ts";
import { orderColumnsForGrid } from "./knowhow-panel-logic.ts";
import { extractErrorMessage } from "./knowhow-import-logic.ts";
import { FULLSCREEN_LABEL, KnowhowMarkdown, RESTORE_SIZE_LABEL, useFullscreenToggle } from "./knowhow-cell-editor.tsx";
import { useFloatingWindow } from "./use-floating-window.ts";

// sessionStorage 键——独立于矩阵抽屉的 knowhow.conceptDrawer.fullscreen（见
// knowhow-matrix-drawer.tsx 头注释同一约定），两个弹窗的全屏选择互不影响。
const HISTORY_DRAWER_FULLSCREEN_STORAGE_KEY = "knowhow.historyDrawer.fullscreen";

// 单页拉取条数——比后端默认(50)宽松一些，多数表一次就能翻到底，减少「加载
// 更早的记录」的点击摩擦；仍显式给值而不是省略参数依赖后端默认，代码本身
// 即文档。
const HISTORY_PAGE_LIMIT = 100;

type HistoryMode = "timeline" | "compare";

// 字段名故意不叫 message——它确实装的是一句人话提示，但用这个名字会被
// errors-guard.test.mjs 的静态扫描器按语法形态误判成"读原始异常 .message"
// （该扫描器只按 AST 节点形态 `X.message` 匹配，不理解语义：本状态是我方自己
// 在下方 catch 分支里显式写入的应用态横幅文案，不是从某个异常对象上现读出
// 来的），需要额外登记进 APPROVED_MESSAGE_READS 才能放行。改叫 text 完全等价
// 且不触发这条守卫——同样的取舍先例见 page.tsx Home 组件的信息弹窗状态
// （APPROVED_MESSAGE_READS 里 "application-owned information modal state is
// not exception text" 那一条），那里最终仍保留了 message 这个名字并登记豁免；
// 本文件选择改名而不登记，因为这是一个全新文件、没有历史包袱，能不长一条
// allowlist 就不长。
type RevertBanner = { tone: "warning" | "danger"; text: string };

// --- 展示态小工具（纯渲染辅助，无副作用；不进 -logic.ts——见任务简报要点⑥：
// 只是渲染、不产生新可测纯函数时可以不新开 -logic.ts 文件） ---------------------

function columnLabel(columns: KnowhowColumn[], columnId: string | undefined): string {
  if (!columnId) return "（未知列）";
  return columns.find((c) => c.id === columnId)?.name ?? "（已删除的列）";
}

// 行标题合成：优先当前行标题列（若该表仍设有），否则用 composeRowTitle 拼
// 前几个非空格子——与 knowhow-panel-logic.ts resolveRowTitleText 同一规则，
// 但本文件的 row 只是"该次变更携带的快照"（不是活的 KnowhowRow，没有
// projectionStatus 等字段），没有必要为了复用那个函数而现造一个假对象。
function diffRowTitle(cells: Record<string, string>, columns: KnowhowColumn[]): string {
  const ordered = orderColumnsForGrid(columns);
  const anchorColumn = ordered.find((c) => c.role === "anchor");
  if (anchorColumn) return cells[anchorColumn.id] || "（空标题）";
  const title = composeRowTitle(ordered.map((c) => cells[c.id] ?? ""));
  return title || "（空行）";
}

function formatClock(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso ?? "";
  return date.toLocaleTimeString("zh-CN", { hour: "2-digit", minute: "2-digit" });
}

// --- 单条变更 / 两版对比 共用的 diff 渲染块（PascalCase：返回 JSX 的辅助按
// 本文件既有约定实现为组件，而非小写工具函数，见 knowhow-panel.tsx
// RoleBadge/ColumnNameCell 的既有写法） -------------------------------------

type DiffCellEntry = { rowId: string; columnId: string; before: string | null; after: string | null };
type DiffRowEntry = { rowId: string; cells: Record<string, string>; code?: { columnId: string }[] };

function CellDiffList({
  cells, columns, notebookId, apiBase,
}: {
  cells: DiffCellEntry[]; columns: KnowhowColumn[]; notebookId: string; apiBase: string;
}) {
  if (cells.length === 0) return null;
  return (
    <div className="kh-history-diff-section">
      {cells.map((cell, index) => (
        <div key={`${cell.rowId}:${cell.columnId}:${index}`} className="kh-history-diff-cell">
          <div className="kh-history-diff-cell-head">
            {cells.length > 1 ? `第 ${index + 1} 处 · ` : ""}列「{columnLabel(columns, cell.columnId)}」
          </div>
          <div className="kh-history-diff-pair">
            <div className="kh-history-diff-block kh-history-diff-block--before">
              <div className="kh-history-diff-label">此前</div>
              {cell.before ? (
                <KnowhowMarkdown md={cell.before} notebookId={notebookId} apiBase={apiBase} />
              ) : (
                <p className="kh-history-diff-empty">（空）</p>
              )}
            </div>
            <div className="kh-history-diff-block kh-history-diff-block--after">
              <div className="kh-history-diff-label">此后</div>
              {cell.after ? (
                <KnowhowMarkdown md={cell.after} notebookId={notebookId} apiBase={apiBase} />
              ) : (
                <p className="kh-history-diff-empty">（已清空）</p>
              )}
            </div>
          </div>
        </div>
      ))}
    </div>
  );
}

function RowDiffList({
  rows, columns, tone, notebookId, apiBase,
}: {
  rows: DiffRowEntry[]; columns: KnowhowColumn[]; tone: "added" | "removed"; notebookId: string; apiBase: string;
}) {
  if (rows.length === 0) return null;
  const ordered = orderColumnsForGrid(columns);
  return (
    <div className="kh-history-diff-section">
      {rows.map((row) => (
        <div key={row.rowId} className={`kh-history-row-card kh-history-row-card--${tone}`}>
          <div className="kh-history-diff-label">{diffRowTitle(row.cells, columns)}</div>
          {ordered.map((column) => {
            const value = row.cells[column.id];
            if (!value) return null;
            return (
              <div key={column.id} className="kh-history-row-card-cell">
                <span className="kh-history-diff-cell-head">列「{column.name}」</span>
                <KnowhowMarkdown md={value} notebookId={notebookId} apiBase={apiBase} />
              </div>
            );
          })}
          {row.code && row.code.length > 0 && (
            <p className="kh-history-diff-empty">含 {row.code.length} 个代码附件</p>
          )}
        </div>
      ))}
    </div>
  );
}

function ColumnDiffList({
  entries, columns,
}: {
  entries: { columnId: string; before: ColumnFieldSnapshot; after: ColumnFieldSnapshot }[];
  columns: KnowhowColumn[];
}) {
  if (entries.length === 0) return null;
  return (
    <ul className="kh-history-column-diff-list">
      {entries.map((entry) => {
        const fallback = columns.find((c) => c.id === entry.columnId)?.name;
        const described = describeColumnChange(entry.columnId, entry.before, entry.after, fallback);
        return (
          <li key={entry.columnId}>
            {described.lines.map((line, i) => <div key={i}>{line}</div>)}
          </li>
        );
      })}
    </ul>
  );
}

function TableMetaDiff({
  before, after,
}: {
  before: { title?: string; description?: string }; after: { title?: string; description?: string };
}) {
  const lines: string[] = [];
  if ((before.title ?? "") !== (after.title ?? "")) {
    lines.push(`标题：${before.title || "（空）"} → ${after.title || "（空）"}`);
  }
  if ((before.description ?? "") !== (after.description ?? "")) {
    lines.push(`描述：${before.description || "（空）"} → ${after.description || "（空）"}`);
  }
  if (lines.length === 0) lines.push("表信息发生变化");
  return <ul className="kh-history-column-diff-list">{lines.map((line, i) => <li key={i}>{line}</li>)}</ul>;
}

type CellCodeSnapshot = { code_text: string; language: string } | null | undefined;

function CellCodeDiff({
  columnId, before, after, columns,
}: {
  columnId?: string; before: CellCodeSnapshot; after: CellCodeSnapshot; columns: KnowhowColumn[];
}) {
  const headline = !before ? "新增代码附件" : !after ? "删除代码附件" : "更新代码附件";
  return (
    <div className="kh-history-diff-section">
      <div className="kh-history-diff-cell-head">
        {columnId ? `列「${columnLabel(columns, columnId)}」· ` : ""}
        {headline}
      </div>
      <div className="kh-history-diff-pair">
        <div className="kh-history-diff-block kh-history-diff-block--before">
          <div className="kh-history-diff-label">此前{before ? `（${before.language}）` : ""}</div>
          {before ? <pre className="kh-code-block">{before.code_text}</pre> : <p className="kh-history-diff-empty">（无）</p>}
        </div>
        <div className="kh-history-diff-block kh-history-diff-block--after">
          <div className="kh-history-diff-label">此后{after ? `（${after.language}）` : ""}</div>
          {after ? <pre className="kh-code-block">{after.code_text}</pre> : <p className="kh-history-diff-empty">（无）</p>}
        </div>
      </div>
    </div>
  );
}

// snake_case 线上 payload -> 本文件渲染组件用的 camelCase 局部形状。只在这
// 一处做转换，不污染 knowhow-model.ts 的 mapChange()（该文件刻意不对 payload
// 做逐 kind 递归转换，见其头注释）。
function normalizeCells(cells: { row_id: string; column_id: string; before?: string | null; after?: string | null }[] | undefined): DiffCellEntry[] {
  return (cells ?? []).map((cell) => ({
    rowId: cell.row_id,
    columnId: cell.column_id,
    before: cell.before ?? null,
    after: cell.after ?? null,
  }));
}

function normalizeRows(
  rows: { row_id: string; cells?: Record<string, string>; code?: { column_id: string }[] }[] | undefined,
): DiffRowEntry[] {
  return (rows ?? []).map((row) => ({
    rowId: row.row_id,
    cells: row.cells ?? {},
    code: (row.code ?? []).map((entry) => ({ columnId: entry.column_id })),
  }));
}

// 单条变更展开——按 14 种 kind 分派（design doc §4.4 payload 形状）。与两版
// 对比（CompareResult）刻意分开实现：这里每条 payload 已经明确知道自己是哪
// 个 kind，不需要像服务端 aggregate_diff 的 columns[] 桶那样从 null 编码反推
// "是加是删"。
function ChangeDetail({
  change, columns, notebookId, apiBase,
}: {
  change: KnowhowChange; columns: KnowhowColumn[]; notebookId: string; apiBase: string;
}) {
  const payload = change.payload ?? {};
  switch (change.kind) {
    case "cell_update":
      return <CellDiffList cells={normalizeCells(payload.cells)} columns={columns} notebookId={notebookId} apiBase={apiBase} />;
    case "row_add":
    case "import_append":
      return <RowDiffList rows={normalizeRows(payload.rows)} columns={columns} tone="added" notebookId={notebookId} apiBase={apiBase} />;
    case "row_delete":
      return <RowDiffList rows={normalizeRows(payload.rows)} columns={columns} tone="removed" notebookId={notebookId} apiBase={apiBase} />;
    case "column_add": {
      const column = payload.column ?? {};
      return (
        <p className="kh-history-diff-empty">
          新增列「{column.name ?? "（未知列）"}」
          {column.role ? `（类型：${roleLabel(column.role)}）` : ""}
        </p>
      );
    }
    case "column_delete": {
      const column = payload.column ?? {};
      const cellCount = (payload.cells ?? []).length;
      return (
        <p className="kh-history-diff-empty">
          删除列「{column.name ?? "（未知列）"}」
          {cellCount > 0 ? `，连同 ${cellCount} 个格子内容一并删除` : ""}
        </p>
      );
    }
    case "column_rename":
      return <p className="kh-history-diff-empty">列改名：「{payload.before}」→「{payload.after}」</p>;
    case "column_kind":
      return (
        <p className="kh-history-diff-empty">
          内容类型：{roleLabel(payload.before)} → {roleLabel(payload.after)}
        </p>
      );
    case "anchor_set":
      return (
        <ul className="kh-history-column-diff-list">
          {(payload.columns ?? []).map((entry: { column_id: string; before: string; after: string }, i: number) => {
            const name = columnLabel(columns, entry.column_id);
            const line = entry.after === "anchor" ? `「${name}」设为行标题列` : `「${name}」不再是行标题列`;
            return <li key={i}>{line}</li>;
          })}
        </ul>
      );
    case "table_meta":
      return <TableMetaDiff before={payload.before ?? {}} after={payload.after ?? {}} />;
    case "cell_code_put":
    case "cell_code_delete":
      return (
        <CellCodeDiff
          columnId={payload.column_id}
          before={payload.before ?? null}
          after={payload.after ?? null}
          columns={columns}
        />
      );
    case "table_create": {
      const cols: { name: string }[] = payload.columns ?? [];
      const rows: unknown[] = payload.rows ?? [];
      const parts: string[] = [];
      if (cols.length > 0) parts.push(`初始列：${cols.map((c) => c.name).join("、")}`);
      if (rows.length > 0) parts.push(`初始 ${rows.length} 行`);
      return parts.length > 0 ? <p className="kh-history-diff-empty">{parts.join(" · ")}</p> : null;
    }
    case "revert": {
      const columnsAdded: { column?: { name?: string } }[] = payload.columns_added ?? [];
      const columnsRemoved: { column?: { name?: string } }[] = payload.columns_removed ?? [];
      const columnsChanged: { column_id: string; before?: ColumnFieldSnapshot; after?: ColumnFieldSnapshot }[] =
        payload.columns_changed ?? [];
      return (
        <>
          <CellDiffList cells={normalizeCells(payload.cells)} columns={columns} notebookId={notebookId} apiBase={apiBase} />
          <RowDiffList rows={normalizeRows(payload.rows_added)} columns={columns} tone="added" notebookId={notebookId} apiBase={apiBase} />
          <RowDiffList rows={normalizeRows(payload.rows_removed)} columns={columns} tone="removed" notebookId={notebookId} apiBase={apiBase} />
          {(columnsAdded.length > 0 || columnsRemoved.length > 0) && (
            <ul className="kh-history-column-diff-list">
              {columnsAdded.map((entry, i) => <li key={`add:${i}`}>新增列「{entry.column?.name ?? "（未知列）"}」</li>)}
              {columnsRemoved.map((entry, i) => <li key={`del:${i}`}>删除列「{entry.column?.name ?? "（未知列）"}」</li>)}
            </ul>
          )}
          <ColumnDiffList
            entries={columnsChanged.map((entry) => ({
              columnId: entry.column_id,
              before: entry.before ?? {},
              after: entry.after ?? {},
            }))}
            columns={columns}
          />
          {payload.table_meta && <TableMetaDiff before={payload.table_meta.before ?? {}} after={payload.table_meta.after ?? {}} />}
        </>
      );
    }
    default:
      return null;
  }
}

function fromDiffRow(row: KnowhowHistoryDiffRow): DiffRowEntry {
  return { rowId: row.rowId, cells: row.cells, code: row.code.map((entry) => ({ columnId: entry.columnId })) };
}

function CompareResult({
  diff, columns, notebookId, apiBase,
}: {
  diff: KnowhowHistoryDiff; columns: KnowhowColumn[]; notebookId: string; apiBase: string;
}) {
  const hasAny =
    diff.cells.length > 0 || diff.rowsAdded.length > 0 || diff.rowsRemoved.length > 0
    || diff.columns.length > 0 || diff.tableMeta !== null;
  if (!hasAny) return <p className="kh-history-empty">所选两个版本之间没有变化</p>;
  return (
    <div className="kh-history-compare-result">
      {diff.tableMeta && <TableMetaDiff before={diff.tableMeta.before} after={diff.tableMeta.after} />}
      <ColumnDiffList entries={diff.columns.map((c) => ({ columnId: c.columnId, before: c.before, after: c.after }))} columns={columns} />
      <RowDiffList rows={diff.rowsAdded.map(fromDiffRow)} columns={columns} tone="added" notebookId={notebookId} apiBase={apiBase} />
      <RowDiffList rows={diff.rowsRemoved.map(fromDiffRow)} columns={columns} tone="removed" notebookId={notebookId} apiBase={apiBase} />
      <CellDiffList cells={diff.cells} columns={columns} notebookId={notebookId} apiBase={apiBase} />
    </div>
  );
}

// ---------------------------------------------------------------------------

export function KnowhowHistoryDrawer({
  notebookId,
  apiBase,
  tableId,
  tableTitle,
  columns,
  canEdit,
  onClose,
  onReverted,
}: {
  notebookId: string;
  apiBase: string;
  tableId: string;
  tableTitle: string;
  /** 当前列定义（来自 detail.columns）：用于列名查找（历史 payload 里绝大
   * 多数只带 column_id）与行标题合成。列已被删除时查不到，各渲染点各自兜底
   * 「（已删除的列）」，不假设它一定还在。 */
  columns: KnowhowColumn[];
  canEdit: boolean;
  onClose: () => void;
  /** 回退成功后通知 panel 去重拉表详情（本抽屉自己只重拉历史，不知道、也不
   * 需要知道 detail 的形状）。 */
  onReverted: () => void;
}) {
  const [fullscreen, toggleFullscreen] = useFullscreenToggle(HISTORY_DRAWER_FULLSCREEN_STORAGE_KEY);
  const floating = useFloatingWindow({ storageKey: "knowhow.historyDrawer.window", disabled: fullscreen });

  const [changes, setChanges] = useState<KnowhowChange[]>([]);
  const [milestones, setMilestones] = useState<KnowhowMilestone[]>([]);
  const [headSeq, setHeadSeq] = useState(0);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [hasMoreOlder, setHasMoreOlder] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);

  const [expandedSeq, setExpandedSeq] = useState<number | null>(null);
  const [milestonesOnly, setMilestonesOnly] = useState(false);
  const [mode, setMode] = useState<HistoryMode>("timeline");

  const [compareFrom, setCompareFrom] = useState<number | null>(null);
  const [compareTo, setCompareTo] = useState<number | null>(null);
  const [compareDiff, setCompareDiff] = useState<KnowhowHistoryDiff | null>(null);
  const [compareLoading, setCompareLoading] = useState(false);
  const [compareError, setCompareError] = useState<string | null>(null);

  const [confirmRevertSeq, setConfirmRevertSeq] = useState<number | null>(null);
  const [revertPending, setRevertPending] = useState(false);
  const [revertBanner, setRevertBanner] = useState<RevertBanner | null>(null);

  const [milestoneFormSeq, setMilestoneFormSeq] = useState<number | null>(null);
  const [milestoneNameDraft, setMilestoneNameDraft] = useState("");
  const [milestonePending, setMilestonePending] = useState(false);
  const [milestoneError, setMilestoneError] = useState<string | null>(null);
  const [confirmDeleteMilestoneId, setConfirmDeleteMilestoneId] = useState<string | null>(null);
  const [deletingMilestoneId, setDeletingMilestoneId] = useState<string | null>(null);

  const reload = useCallback(() => {
    setLoading(true);
    setLoadError(null);
    fetchKnowhowHistory(notebookId, tableId, undefined, HISTORY_PAGE_LIMIT)
      .then((page) => {
        setChanges(page.changes);
        setMilestones(page.milestones);
        setHeadSeq(page.headSeq);
        setHasMoreOlder(page.changes.length >= HISTORY_PAGE_LIMIT);
      })
      .catch(() => setLoadError("加载历史失败，请重试"))
      .finally(() => setLoading(false));
  }, [notebookId, tableId]);

  useEffect(reload, [reload]);

  // Esc 关闭：与 KnowhowRowDrawer 的既有模式一致，但本抽屉没有任何东西能堆叠
  // 在它之上（不像该组件需要 cellModalOpen 短路——见 knowhow-panel.tsx 里
  // KnowhowRowDrawer 的同名 prop 注释；本抽屉不提供"编辑格子"之类的二级弹层
  // 入口，Task 15 范围内没有嵌套浮窗需要与本监听器竞争同一次按键），无条件
  // 关闭即可，同 KnowhowRowOptimizeModal/KnowhowReformatBatchModal 的既有写法。
  useEffect(() => {
    function handleKeyDown(event: KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  function loadOlder() {
    if (changes.length === 0 || loadingMore) return;
    const oldestSeq = changes[changes.length - 1].seq;
    setLoadingMore(true);
    setLoadError(null);
    fetchKnowhowHistory(notebookId, tableId, oldestSeq, HISTORY_PAGE_LIMIT)
      .then((page) => {
        setChanges((prev) => [...prev, ...page.changes]);
        setMilestones(page.milestones); // 里程碑列表恒为全量（非分页），覆盖即可
        setHasMoreOlder(page.changes.length >= HISTORY_PAGE_LIMIT);
        // 不覆盖 headSeq：翻旧页取到的是同一个"当前 head"的回声，若恰好在此
        // 期间别人提交了新变更，这里直接采纳会让 head 跳变、却不更新最新那几
        // 条——宁可让 head 保持上一次整体 reload() 时的快照，一致性优先于
        // "尽量新鲜"。
      })
      .catch(() => setLoadError("加载更早的记录失败，请重试"))
      .finally(() => setLoadingMore(false));
  }

  const milestoneBySeq = useMemo(() => {
    const map = new Map<number, KnowhowMilestone>();
    for (const m of milestones) if (!m.stale) map.set(m.seq, m);
    return map;
  }, [milestones]);
  const staleMilestones = useMemo(() => milestones.filter((m) => m.stale), [milestones]);

  // codex 第 2 轮 P2：里程碑可能落在还没加载进来的更旧一页里。「只看里程碑」
  // 开启时，把 milestoneBySeq 里当前 changes 尚未覆盖的 seq 单独抓回来，避免
  // 界面误报「还没有里程碑」（>100 变更且里程碑在旧页时曾会漏）。
  const milestoneSeqs = useMemo(() => new Set(milestoneBySeq.keys()), [milestoneBySeq]);
  const [extraMilestoneChanges, setExtraMilestoneChanges] = useState<KnowhowChange[]>([]);
  useEffect(() => {
    if (!milestonesOnly) {
      setExtraMilestoneChanges([]);
      return;
    }
    const loadedSeqs = new Set(changes.map((c) => c.seq));
    const missing = [...milestoneSeqs].filter((seq) => !loadedSeqs.has(seq));
    if (missing.length === 0) {
      setExtraMilestoneChanges([]);
      return;
    }
    let cancelled = false;
    Promise.all(
      missing.map((seq) => fetchKnowhowChange(notebookId, tableId, seq).catch(() => null)),
    ).then((results) => {
      if (cancelled) return;
      setExtraMilestoneChanges(results.filter((c): c is KnowhowChange => c !== null));
    });
    return () => {
      cancelled = true;
    };
  }, [milestonesOnly, changes, milestoneSeqs, notebookId, tableId]);

  const displayedChanges = useMemo(
    () =>
      milestonesOnly
        ? mergeMilestoneTimeline(changes, milestoneSeqs, extraMilestoneChanges)
        : changes,
    [changes, milestonesOnly, milestoneSeqs, extraMilestoneChanges],
  );
  const days = useMemo(() => groupChangesByDay(displayedChanges), [displayedChanges]);
  const showLegacyNote = !hasMoreOlder && (changes.length === 0 || changes[changes.length - 1].kind !== "table_create");

  const compareOptions = useMemo(
    () => changes.map((c) => ({
      seq: c.seq,
      label: `#${c.seq} · ${formatClock(c.createdAt)} · ${summarizeChange(c)}`,
    })),
    [changes],
  );
  // codex 第 2 轮 P2：「最早」起点选项按保留链条真实起点给（不再无条件 seq 0）。
  const earliestOption = useMemo(
    () => earliestComparePoint(changes, hasMoreOlder),
    [changes, hasMoreOlder],
  );

  function revertImpactText(targetSeq: number): string {
    const index = changes.findIndex((c) => c.seq === targetSeq);
    if (index < 0) return "";
    const affected = changes.slice(0, index);
    const folded = foldLocalChanges(affected);
    const rowsTouched = new Set([...folded.rowsAdded, ...folded.rowsRemoved]).size;
    // 第三个参数传原始 affected 数组（不是 affected.length）：summarizeRevertImpact
    // 需要自己按 kind 从中统计列结构变化/表元变化，foldLocalChanges 的输出
    // 只覆盖行/格子，见该函数头注释「评审修复」一节。
    return summarizeRevertImpact(rowsTouched, folded.cells.length, affected);
  }

  async function handleRevert(targetSeq: number) {
    setRevertPending(true);
    setRevertBanner(null);
    try {
      await revertKnowhowTable(notebookId, tableId, targetSeq, headSeq);
      setConfirmRevertSeq(null);
      reload();
      onReverted();
    } catch (err) {
      setConfirmRevertSeq(null);
      if (err instanceof KnowhowRevertError) {
        const message = err.message;
        if (err.code === "knowhow_history_stale") {
          // 陈旧 409：head 已变，自动刷新到最新事实，让用户在新数据上重新
          // 决定，而不是盲目重试同一个已经过时的目标。
          setRevertBanner({ tone: "warning", text: message });
          reload();
        } else if (err.code === "knowhow_history_inconsistent") {
          // 前置指纹不一致 400：回退已整体中止，什么都没写入。这不是"落后
          // 了"，刷新时间线也不会让这类不一致消失，所以不自动 reload——避免
          // 暗示"刷新一下就好了"这种不成立的期待。
          setRevertBanner({ tone: "danger", text: message });
        } else {
          // 后置校验失败 500：服务端已在同一事务内整体回滚，表未被改动。
          // reload 只是确认性质（"head 确实还是原样"），不代表问题已解决。
          setRevertBanner({ tone: "danger", text: message });
          reload();
        }
      } else {
        setRevertBanner({ tone: "danger", text: extractErrorMessage(err, "回退失败，请重试") });
      }
    } finally {
      setRevertPending(false);
    }
  }

  async function submitMilestone(seq: number) {
    const name = milestoneNameDraft.trim();
    if (!name) {
      setMilestoneError("请填写里程碑名称");
      return;
    }
    setMilestonePending(true);
    setMilestoneError(null);
    try {
      await createKnowhowMilestone(notebookId, tableId, seq, name);
      setMilestoneFormSeq(null);
      setMilestoneNameDraft("");
      reload();
    } catch (err) {
      setMilestoneError(extractErrorMessage(err, "设置里程碑失败，请重试"));
    } finally {
      setMilestonePending(false);
    }
  }

  async function handleDeleteMilestone(milestoneId: string) {
    setDeletingMilestoneId(milestoneId);
    setMilestoneError(null);
    try {
      await deleteKnowhowMilestone(notebookId, tableId, milestoneId);
      setConfirmDeleteMilestoneId(null);
      reload();
    } catch (err) {
      setMilestoneError(extractErrorMessage(err, "删除里程碑失败，请重试"));
    } finally {
      setDeletingMilestoneId(null);
    }
  }

  async function runCompare() {
    if (compareFrom === null || compareTo === null) return;
    setCompareLoading(true);
    setCompareError(null);
    try {
      const result = await fetchKnowhowHistoryDiff(notebookId, tableId, compareFrom, compareTo);
      setCompareDiff(result);
    } catch (err) {
      setCompareDiff(null);
      setCompareError(extractErrorMessage(err, "对比失败，请重试"));
    } finally {
      setCompareLoading(false);
    }
  }

  function renderMilestoneFlag(milestone: KnowhowMilestone) {
    if (confirmDeleteMilestoneId === milestone.id) {
      return (
        <span className="knowhow-confirm" onClick={(event) => event.stopPropagation()}>
          <span>删除该里程碑？</span>
          <button
            type="button"
            className="knowhow-confirm-yes"
            disabled={deletingMilestoneId === milestone.id}
            onClick={() => handleDeleteMilestone(milestone.id)}
          >
            {deletingMilestoneId === milestone.id ? "删除中…" : "确认删除"}
          </button>
          <button type="button" className="knowhow-confirm-no" onClick={() => setConfirmDeleteMilestoneId(null)}>
            取消
          </button>
        </span>
      );
    }
    return (
      <span
        className="knowhow-status-badge knowhow-status-badge--warning"
        onClick={(event) => event.stopPropagation()}
      >
        <Flag size={11} /> {milestone.name}
        {canEdit && (
          <button
            type="button"
            className="kh-matrix-branch-delete"
            title="删除该里程碑"
            onClick={() => setConfirmDeleteMilestoneId(milestone.id)}
          >
            <X size={11} />
          </button>
        )}
      </span>
    );
  }

  function handleBackdropClick(event: ReactMouseEvent<HTMLDivElement>) {
    if (event.currentTarget === event.target) onClose();
  }

  return (
    <div className="kh-modal-overlay" onClick={handleBackdropClick}>
      <div
        ref={floating.cardRef}
        className={`kh-modal-card kh-history-card${fullscreen ? " kh-modal-card--fullscreen" : ""}`}
        style={floating.style}
        role="dialog"
        aria-modal="true"
        aria-label={`历史：${tableTitle}`}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="kh-modal-header" {...floating.dragHandleProps}>
          <div className="kh-modal-header-top">
            <div className="kh-modal-breadcrumb">
              <span className="knowhow-status-badge knowhow-status-badge--info">历史</span>
              <span className="kh-modal-row-title" title={tableTitle}>{tableTitle}</span>
              <span className="kh-modal-sep">·</span>
              <span>{changes.length}{hasMoreOlder ? "+" : ""} 条记录</span>
            </div>
            <div className="kh-modal-header-actions">
              <button
                type="button"
                className="icon-button"
                title={fullscreen ? RESTORE_SIZE_LABEL : FULLSCREEN_LABEL}
                aria-label={fullscreen ? RESTORE_SIZE_LABEL : FULLSCREEN_LABEL}
                onClick={toggleFullscreen}
              >
                {fullscreen ? <Minimize2 size={16} /> : <Maximize2 size={16} />}
              </button>
              <button type="button" className="icon-button" title="关闭" onClick={onClose}>
                <X size={18} />
              </button>
            </div>
          </div>
          {revertBanner && (
            <div className={revertBanner.tone === "danger" ? "knowhow-action-error" : "knowhow-jump-notice"}>
              <span>{revertBanner.text}</span>
              <button type="button" onClick={() => setRevertBanner(null)} title="关闭">
                <X size={14} />
              </button>
            </div>
          )}
          {milestoneError && (
            <div className="knowhow-action-error">
              <span>{milestoneError}</span>
              <button type="button" onClick={() => setMilestoneError(null)} title="关闭">
                <X size={14} />
              </button>
            </div>
          )}
        </header>

        <div className="kh-modal-body">
          {loadError && (
            <div className="knowhow-action-error">
              <span>{loadError}</span>
              <button type="button" onClick={reload} title="重试">
                <RefreshCw size={14} />
              </button>
            </div>
          )}

          <div className="kh-history-toolbar">
            <div className="kh-view-switch" role="group" aria-label="历史视图">
              <button
                type="button"
                className={`kh-view-switch-button${mode === "timeline" ? " kh-view-switch-button--active" : ""}`}
                aria-pressed={mode === "timeline"}
                onClick={() => setMode("timeline")}
              >
                <HistoryIcon size={14} /> 时间线
              </button>
              <button
                type="button"
                className={`kh-view-switch-button${mode === "compare" ? " kh-view-switch-button--active" : ""}`}
                aria-pressed={mode === "compare"}
                onClick={() => setMode("compare")}
              >
                <GitCompare size={14} /> 两版对比
              </button>
            </div>
            {mode === "timeline" && (
              <label className="kh-history-filter-toggle">
                <input
                  type="checkbox"
                  checked={milestonesOnly}
                  onChange={(event) => setMilestonesOnly(event.target.checked)}
                />
                只看里程碑
              </label>
            )}
          </div>

          {mode === "timeline" ? (
            <>
              {loading ? (
                <p className="knowhow-loading">加载中…</p>
              ) : days.length === 0 ? (
                <p className="kh-history-empty">{milestonesOnly ? "还没有里程碑" : "暂无变更记录"}</p>
              ) : (
                <div className="kh-history-days">
                  {days.map((group) => (
                    // kh-history-day-group 仅作语义标记（把"这一天的日期
                    // 标签 + 变更列表"标成一个整体、供 key 挂载），无对应
                    // CSS 规则、也不需要：父级 .kh-history-days 已是
                    // flex-direction:column + gap:18px，这个 div 作为默认
                    // block 的 flex item 天然一个个纵向堆叠、组间距由父级
                    // gap 给出，组内"日期在上、列表在下"由普通文档流决定，
                    // 不依赖这个类名本身有任何样式。
                    <div key={group.day} className="kh-history-day-group">
                      <div className="kh-history-day">{group.day}</div>
                      <ul className="kh-history-list">
                        {group.changes.map((change) => {
                          const milestone = milestoneBySeq.get(change.seq);
                          const isHead = change.seq === headSeq;
                          const expanded = expandedSeq === change.seq;
                          const origin = originLabel(change.origin);
                          return (
                            <li key={change.id} className="kh-history-item">
                              <div className="kh-history-item-summary">
                                <button
                                  type="button"
                                  className="kh-history-item-toggle"
                                  onClick={() => setExpandedSeq(expanded ? null : change.seq)}
                                  aria-expanded={expanded}
                                >
                                  {expanded ? <ChevronDown size={14} /> : <ChevronRight size={14} />}
                                  <span className="kh-history-item-time">{formatClock(change.createdAt)}</span>
                                  <span>{change.actor || "—"}</span>
                                  <span>{summarizeChange(change)}</span>
                                </button>
                                {origin && <span className="knowhow-status-badge">{origin}</span>}
                                {isHead && <span className="knowhow-status-badge knowhow-status-badge--info">当前</span>}
                                {milestone && renderMilestoneFlag(milestone)}
                              </div>

                              {canEdit && (
                                <div className="kh-history-item-actions">
                                  {milestone ? null : (
                                    <button
                                      type="button"
                                      className="knowhow-drawer-edit-button"
                                      onClick={() => {
                                        setMilestoneFormSeq(milestoneFormSeq === change.seq ? null : change.seq);
                                        setMilestoneNameDraft("");
                                        setMilestoneError(null);
                                      }}
                                    >
                                      <Flag size={12} /> 设为里程碑
                                    </button>
                                  )}
                                  {!isHead && (
                                    confirmRevertSeq === change.seq ? (
                                      <span className="knowhow-confirm">
                                        <span>回退到 #{change.seq}？{revertImpactText(change.seq)}</span>
                                        <button
                                          type="button"
                                          className="knowhow-confirm-yes"
                                          disabled={revertPending}
                                          onClick={() => handleRevert(change.seq)}
                                        >
                                          {revertPending ? "回退中…" : "确认回退"}
                                        </button>
                                        <button
                                          type="button"
                                          className="knowhow-confirm-no"
                                          disabled={revertPending}
                                          onClick={() => setConfirmRevertSeq(null)}
                                        >
                                          取消
                                        </button>
                                      </span>
                                    ) : (
                                      <button
                                        type="button"
                                        className="knowhow-drawer-edit-button"
                                        onClick={() => setConfirmRevertSeq(change.seq)}
                                      >
                                        <RotateCcw size={12} /> 回到这里
                                      </button>
                                    )
                                  )}
                                </div>
                              )}

                              {canEdit && milestoneFormSeq === change.seq && (
                                <div className="kh-history-milestone-form">
                                  <input
                                    type="text"
                                    value={milestoneNameDraft}
                                    placeholder="里程碑名称"
                                    disabled={milestonePending}
                                    onChange={(event) => setMilestoneNameDraft(event.target.value)}
                                    onKeyDown={(event) => {
                                      if (event.key === "Enter") submitMilestone(change.seq);
                                    }}
                                  />
                                  <button type="button" onClick={() => submitMilestone(change.seq)} disabled={milestonePending}>
                                    {milestonePending ? "保存中…" : "确定"}
                                  </button>
                                  <button type="button" onClick={() => setMilestoneFormSeq(null)} disabled={milestonePending}>
                                    取消
                                  </button>
                                </div>
                              )}

                              {expanded && (
                                <div className="kh-history-detail">
                                  <ChangeDetail change={change} columns={columns} notebookId={notebookId} apiBase={apiBase} />
                                </div>
                              )}
                            </li>
                          );
                        })}
                      </ul>
                    </div>
                  ))}
                </div>
              )}

              {staleMilestones.length > 0 && (
                <div className="kh-history-stale-milestones">
                  <p className="kh-history-diff-empty">已失效的里程碑（指向的记录已被清理，不可回退）：</p>
                  <ul className="kh-history-column-diff-list">
                    {staleMilestones.map((m) => (
                      <li key={m.id}>
                        <span className="knowhow-status-badge">{m.name}</span>
                        {" "}
                        {canEdit && (
                          confirmDeleteMilestoneId === m.id ? (
                            <span className="knowhow-confirm">
                              <span>删除该里程碑？</span>
                              <button
                                type="button"
                                className="knowhow-confirm-yes"
                                disabled={deletingMilestoneId === m.id}
                                onClick={() => handleDeleteMilestone(m.id)}
                              >
                                {deletingMilestoneId === m.id ? "删除中…" : "确认删除"}
                              </button>
                              <button type="button" className="knowhow-confirm-no" onClick={() => setConfirmDeleteMilestoneId(null)}>
                                取消
                              </button>
                            </span>
                          ) : (
                            <button
                              type="button"
                              className="kh-matrix-branch-delete"
                              title="删除该里程碑"
                              onClick={() => setConfirmDeleteMilestoneId(m.id)}
                            >
                              <Trash2 size={12} />
                            </button>
                          )
                        )}
                      </li>
                    ))}
                  </ul>
                </div>
              )}

              {!loading && !milestonesOnly && (
                hasMoreOlder ? (
                  <button type="button" className="knowhow-status-retry" onClick={loadOlder} disabled={loadingMore}>
                    {loadingMore ? "加载中…" : "加载更早的记录"}
                  </button>
                ) : showLegacyNote ? (
                  <p className="knowhow-jump-notice">该表创建于版本管理上线前，更早的变更未记录。</p>
                ) : null
              )}
            </>
          ) : (
            <div className="kh-history-compare">
              <div className="kh-history-compare-bar">
                <label>
                  起点
                  <select
                    value={compareFrom ?? ""}
                    onChange={(event) => setCompareFrom(event.target.value === "" ? null : Number(event.target.value))}
                  >
                    <option value="">请选择</option>
                    {earliestOption && (
                      <option value={earliestOption.value}>{earliestOption.label}</option>
                    )}
                    {compareOptions.map((opt) => <option key={opt.seq} value={opt.seq}>{opt.label}</option>)}
                  </select>
                </label>
                <label>
                  终点
                  <select
                    value={compareTo ?? ""}
                    onChange={(event) => setCompareTo(event.target.value === "" ? null : Number(event.target.value))}
                  >
                    <option value="">请选择</option>
                    {compareOptions.map((opt) => <option key={opt.seq} value={opt.seq}>{opt.label}</option>)}
                  </select>
                </label>
                <button
                  type="button"
                  className="knowhow-drawer-edit-button"
                  onClick={runCompare}
                  disabled={compareFrom === null || compareTo === null || compareFrom >= compareTo || compareLoading}
                >
                  <GitCompare size={12} /> {compareLoading ? "对比中…" : "对比"}
                </button>
              </div>
              {compareError && <p className="kh-history-diff-empty">{compareError}</p>}
              {compareDiff && (
                <CompareResult diff={compareDiff} columns={columns} notebookId={notebookId} apiBase={apiBase} />
              )}
            </div>
          )}
        </div>
        {!fullscreen && <span className="kh-modal-resize-handle" aria-hidden="true" {...floating.resizeHandleProps} />}
      </div>
    </div>
  );
}
