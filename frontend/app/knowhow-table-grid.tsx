/**
 * knowhow-table-grid.tsx
 *
 * Knowhow 面板第二层「表格网格」的呈现：工具栏（返回、添加行、模板、追加导入、
 * 一键规整整表、管理、复制/移动、历史、重新同步、删除表）、操作错误横幅、行
 * 过滤、colgroup 列宽（≤720px 窄屏走紧凑档），以及记录型表的平铺渲染与 anchor
 * 表的 G2 合并矩阵；表头列名 inline 改名的 ColumnNameCell 也在这里。从
 * knowhow-panel.tsx 原样搬来（S4-d）：组件体与段落注释一字未改，只把
 * KnowhowTableGrid 的声明改为 export 供面板 import。
 *
 * 网格的写命令都依赖面板持有的 detail，所以仍留在 knowhow-panel.tsx，以 props
 * 传入；props 签名与搬家前一字不差。样式仍由 knowhow-panel-styles.tsx 的
 * `<style jsx global>` 提供，本文件不含 styled-jsx。
 */
"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import {
  ChevronLeft,
  Copy,
  Download,
  History,
  ListChecks,
  ListPlus,
  Loader2,
  Plus,
  RefreshCw,
  Search,
  Settings2,
  Trash2,
  Upload,
  X,
} from "lucide-react";
import {
  cellSummary,
  type KnowhowTableDetail,
  type KnowhowColumn,
} from "./knowhow-model.ts";
import { filterRows, orderColumnsForGrid } from "./knowhow-panel-logic.ts";
import { groupRowsByAnchor, computeGridSpans } from "./knowhow-grouping-logic.ts";
import {
  computeKnowhowColumnWidths,
  sampleVisibleKnowhowRows,
} from "./knowhow-column-widths.ts";
import { isFloatingDisabledWidth } from "./floating-window-logic.ts";
import { BATCH_REFORMAT_TABLE_BUTTON_LABEL } from "./knowhow-optimize-logic.ts";
import { RoleBadge, ProjectionStatusBadge } from "./knowhow-badges.tsx";

// ---------------------------------------------------------------------------
// Layer 2 — 表格网格
// ---------------------------------------------------------------------------

export function KnowhowTableGrid({
  detail,
  loading,
  error,
  actionError,
  canEdit,
  onDismissActionError,
  onRetryLoad,
  query,
  onQueryChange,
  onOpenRow,
  onOpenCell,
  onOpenConcept,
  onBack,
  confirmDelete,
  onRequestDelete,
  onCancelDelete,
  onConfirmDelete,
  deleting,
  onRetryReproject,
  retryingReproject,
  onOpenManage,
  onTransfer,
  onAddRow,
  addingRow,
  onAddConcept,
  onDownloadTemplate,
  templateDownloading,
  onAppendClick,
  onReformatTableClick,
  onRenameColumn,
  onOpenHistory,
}: {
  detail: KnowhowTableDetail | null;
  loading: boolean;
  error: string | null;
  actionError: string | null;
  canEdit: boolean;
  onDismissActionError: () => void;
  onRetryLoad: () => void;
  query: string;
  onQueryChange: (value: string) => void;
  onOpenRow: (rowId: string) => void;
  /** 非行标题列格子点击（规格②路A「点格子弹浮窗」）：panel 按该格是否已有
   * 内容自动决定预览还是编辑态。 */
  onOpenCell: (rowId: string, columnId: string) => void;
  /** anchor 列格子点击（Task 8，规格 §4.3「点概念打开矩阵抽屉」）：传回该格
   * 的概念值，panel 据此打开「属性×分支」矩阵抽屉。只有已填值的 anchor 格
   * 才走这里（见 tbody 内 opensConceptDrawer 判据）——forward-fill 后仍空的
   * 「无概念」行（规格 §4.2.2）保持走 onOpenCell 原地补概念，不经这里。 */
  onOpenConcept: (anchorValue: string) => void;
  onBack: () => void;
  confirmDelete: boolean;
  onRequestDelete: () => void;
  onCancelDelete: () => void;
  onConfirmDelete: () => void;
  deleting: boolean;
  onRetryReproject: () => void;
  retryingReproject: boolean;
  onOpenManage: () => void;
  /** C3「复制/移动到…」：打开目标笔记本选择器（DestinationPicker）。与
   * onOpenManage 同一模式——面板自持 modal 显隐态，这里只负责打开。不像
   * 其余写入口那样整体挂在 canEdit 门内：copy 只需对源笔记本有读权限，只读
   * 成员也该看到这个按钮（能复制、不能移动），故渲染处单独处理，见下方
   * JSX。 */
  onTransfer: () => void;
  onAddRow: () => void;
  addingRow: boolean;
  /** Task 10（加概念）：主网格底部「+ 概念」——只在有 anchor 列（分组视图）
   * 且 canEdit 时渲染；记录型表没有这个入口，继续只有顶部「添加行」一个
   * 写入口不变。 */
  onAddConcept: () => void;
  /** 「下载模板」（Task 9，规格②路B）：按当前表头下载 xlsx 模板。 */
  onDownloadTemplate: () => void;
  templateDownloading: boolean;
  /** 「追加导入」（Task 9，规格②路B）：打开追加导入向导。 */
  onAppendClick: () => void;
  /** 「一键规整整表」（knowhow-md-normalize Task 9）：打开批量规整弹窗，对
   * 整表非空格子逐格调用 reformat_cell，汇总后人工整体确认才落库——是导入
   * 后把整表 LLM 精整的入口（设计文档 §5.3）。 */
  onReformatTableClick: () => void;
  /** 表头列名 inline 改名（canEdit 时启用）：双击列名进入编辑框，回车/失焦
   * 时把新名字丢回来落库。用户不必再翻「管理」抽屉找列改名——就地改。 */
  onRenameColumn: (columnId: string, name: string) => void;
  /** knowhow 表版本管理 Task 15：打开历史抽屉——同 onTransfer 一样不整体挂在
   * canEdit 门内（只读成员也能看时间线和 diff，见按钮渲染处注释），面板自持
   * historyOpen 显隐态，这里只负责打开。 */
  onOpenHistory: () => void;
}) {
  const orderedColumns = useMemo(() => (detail ? orderColumnsForGrid(detail.columns) : []), [detail]);
  const filteredRows = useMemo(() => (detail ? filterRows(detail.rows, query) : []), [detail, query]);
  const [compactColumnWidths, setCompactColumnWidths] = useState(false);

  // Reuse the established 720px narrow-screen boundary. Width computation stays
  // data-only; viewport changes merely select the approved compact clamp table.
  useEffect(() => {
    const sync = () => setCompactColumnWidths(isFloatingDisabledWidth(window.innerWidth));
    sync();
    window.addEventListener("resize", sync);
    return () => window.removeEventListener("resize", sync);
  }, []);

  const sampledRows = useMemo(() => sampleVisibleKnowhowRows(filteredRows), [filteredRows]);

  // 有 anchor 列 → 分组合并矩阵渲染（spec §4.2 G2）；无 anchor（记录型表）
  // → gridDisplayRows 为 null，<tbody> 落回原平铺渲染，零改动（spec §4.2.3）。
  const anchorColumnId = detail?.anchorColumnId ?? null;
  const columnWidths = useMemo(() => computeKnowhowColumnWidths({
    columns: orderedColumns,
    visibleRowsSample: sampledRows,
    anchorColumnId,
    compact: compactColumnWidths,
  }), [anchorColumnId, compactColumnWidths, orderedColumns, sampledRows]);
  const gridDisplayRows = useMemo(() => {
    if (!anchorColumnId) return null;
    const groups = groupRowsByAnchor(filteredRows, anchorColumnId);
    return computeGridSpans(groups, orderedColumns);
  }, [anchorColumnId, filteredRows, orderedColumns]);

  return (
    <div className="knowhow-grid-view">
      <div className="knowhow-grid-toolbar">
        <button type="button" className="sort-button" onClick={onBack}>
          <ChevronLeft size={16} /> 返回
        </button>
        <div className="knowhow-grid-title">
          <h3 title={detail?.title}>{detail?.title ?? ""}</h3>
          {detail?.description && <p>{detail.description}</p>}
        </div>
        {/* 添加行/下载模板/追加导入/管理/重建投影/删除表——这些写入口只对
            canEdit 出现；只读成员的工具栏在返回与标题之外，只剩下面
            「复制/移动到…」一个入口（规格⑦的例外，C3）：它不写入
            本笔记本——copy 只需对源笔记本有读权限，只读成员也该能把表复制到
            自己有写权限的另一个笔记本；真正的写权限边界（能否「移动」=会从
            源删除）收在 allowMove={canEdit}，由 DestinationPicker 内部据此
            隐藏移动单选项，与后端 source_check 的 copy/move 分支对齐。 */}
        <div className="knowhow-grid-toolbar-actions">
          {canEdit && (
            <>
              <button
                type="button"
                className="sort-button knowhow-reproject-button"
                onClick={onAddRow}
                disabled={addingRow || deleting || !detail}
                title="在表末尾添加一行"
              >
                <ListPlus size={14} />
                {addingRow ? "添加中…" : "添加行"}
              </button>
              {/* 「下载模板」+「追加导入」（Task 9，规格②路B）：前者按当前表头
                  下载 xlsx 模板供线下批量填写，后者把填好的文件追加导入回这
                  张表——两个入口紧邻，模板下载天然是追加导入的前置步骤。 */}
              <button
                type="button"
                className="sort-button knowhow-reproject-button"
                onClick={onDownloadTemplate}
                disabled={!detail || templateDownloading}
                title="按当前表头下载 Excel 模板，线下批量填写"
              >
                {templateDownloading ? <Loader2 size={14} className="knowhow-spin" /> : <Download size={14} />}
                {templateDownloading ? "下载中…" : "下载模板"}
              </button>
              <button
                type="button"
                className="sort-button knowhow-reproject-button"
                onClick={onAppendClick}
                disabled={!detail || deleting}
                title="从填好的 Excel/CSV/Markdown 追加导入行"
              >
                <Upload size={14} />
                追加导入
              </button>
              {/* 「一键规整整表」（knowhow-md-normalize Task 9）：紧邻「追加导入」
                  ——最典型的使用时机就是导入后把整表统一交给 AI 精整排版一遍
                  （设计文档 §5.3）。批量弹窗自己会先展示将处理的格子数、跑完后
                  要求人工整体确认才落库，这里的按钮本身不需要二次确认。 */}
              <button
                type="button"
                className="sort-button knowhow-reproject-button"
                onClick={onReformatTableClick}
                disabled={!detail || deleting}
                title="对整张表的非空格子批量规整格式（跑完后需人工确认才保存）"
              >
                <ListChecks size={14} />
                {BATCH_REFORMAT_TABLE_BUTTON_LABEL}
              </button>
              <button
                type="button"
                className="sort-button knowhow-reproject-button"
                onClick={onOpenManage}
                disabled={!detail || deleting}
                title="表信息、行标题列、列与行管理"
              >
                <Settings2 size={14} />
                管理
              </button>
            </>
          )}
          {/* C3：「复制/移动到…」——不整体挂在 canEdit 门内的表级动作之一
              （另一个是紧随其后的「历史」，见该按钮注释），紧邻「管理」，
              理由见上方容器注释。 */}
          <button
            type="button"
            className="sort-button knowhow-reproject-button"
            onClick={onTransfer}
            disabled={!detail || deleting}
            title="把这张表复制或移动到另一个笔记本"
          >
            <Copy size={14} />
            复制/移动到…
          </button>
          {/* knowhow 表版本管理 Task 15：「历史」——同「复制/移动到…」一样不
              整体挂在 canEdit 门内（只读成员也能看时间线和单次改动/两版
              diff，design doc §8.2 末尾"权限"一节明文；只有回退/里程碑这两个
              写入口收在抽屉内部按 canEdit 二级门控，见 knowhow-history-
              drawer.tsx）。 */}
          <button
            type="button"
            className="sort-button knowhow-reproject-button"
            onClick={onOpenHistory}
            disabled={!detail || deleting}
            title="查看这张表的变更历史"
          >
            <History size={14} />
            历史
          </button>
          {canEdit && (
            <>
              {/* 表级「重建投影」逃生口(spec 要求)：整表重投影，后台执行。区别于
                  失败行徽标上的行内「重试」——那只在某行 failed 时出现，这个入口
                  任何时候都在，供用户在整表走样时一键重建。进行中禁用防重复触发。 */}
              <button
                type="button"
                className="sort-button knowhow-reproject-button"
                onClick={onRetryReproject}
                disabled={retryingReproject || deleting || !detail}
                title="重新同步整张表（后台执行）"
              >
                <RefreshCw size={14} className={retryingReproject ? "knowhow-spin" : undefined} />
                {retryingReproject ? "重建中…" : "重新同步"}
              </button>
              {confirmDelete ? (
                <span className="knowhow-confirm">
                  <span>删除这张表？行、格与同步生成的内容将一并删除</span>
                  <button type="button" className="knowhow-confirm-yes" disabled={deleting} onClick={onConfirmDelete}>
                    {deleting ? "删除中…" : "确认删除"}
                  </button>
                  <button type="button" className="knowhow-confirm-no" onClick={onCancelDelete}>
                    取消
                  </button>
                </span>
              ) : (
                <button type="button" className="icon-button" title="删除表" onClick={onRequestDelete}>
                  <Trash2 size={18} />
                </button>
              )}
            </>
          )}
        </div>
      </div>

      {actionError && (
        <div className="knowhow-action-error">
          <span>{actionError}</span>
          <button type="button" onClick={onDismissActionError} title="关闭">
            <X size={14} />
          </button>
        </div>
      )}

      {loading ? (
        <p className="knowhow-loading">加载中…</p>
      ) : error ? (
        <p className="knowhow-inline-error">
          {error}
          <button type="button" className="sort-button" onClick={onRetryLoad}>
            重试
          </button>
        </p>
      ) : detail ? (
        <>
          <div className="knowhow-filter-row">
            <Search size={15} className="knowhow-filter-icon" />
            <input
              className="knowhow-filter-input"
              type="search"
              placeholder="按行标题/全文过滤行…"
              value={query}
              onChange={(event) => onQueryChange(event.target.value)}
            />
          </div>
          <div className="knowhow-grid-scroll">
            <table
              className="knowhow-grid-table"
              style={{ width: columnWidths.tableWidthPx, minWidth: "100%" }}
            >
              <colgroup>
                {columnWidths.columns.map((column) => (
                  <col key={column.columnId} style={{ width: column.widthPx }} />
                ))}
                <col key="projection-status" style={{ width: columnWidths.statusWidthPx }} />
              </colgroup>
              <thead>
                <tr>
                  {orderedColumns.map((column) => (
                    <th key={column.id}>
                      <div className="knowhow-col-header">
                        <ColumnNameCell
                          column={column}
                          canEdit={canEdit}
                          onRename={(name) => onRenameColumn(column.id, name)}
                        />
                        <RoleBadge role={column.role} />
                      </div>
                    </th>
                  ))}
                  <th>同步状态</th>
                </tr>
              </thead>
              <tbody>
                {gridDisplayRows === null ? (
                  // 记录型表（无 anchor 列）：原平铺渲染，逐物理行展开，
                  // 一行代码都不动——逻辑与改动前完全一致（规格 §4.2.3）。
                  filteredRows.map((row) => (
                    <tr key={row.id}>
                      {orderedColumns.map((column, index) => {
                        const text = cellSummary(row.cells[column.id] ?? "");
                        const filled = Boolean(text);
                        // 行标题列（网格首列，规格②路A）填了内容后仍走既有的
                        // 整行抽屉入口（规格⑤「点行首/概念列打开」）；空的话
                        // 直接进格子编辑态更有引导性（规格②路A「空格子直接进
                        // 编辑态」），而不是打开一个几乎全空的抽屉。其余列
                        // （或空的行标题列）一律走格子浮窗，由 panel 按内容
                        // 有无决定预览/编辑。空格 + 只读成员没有可点的入口
                        // （规格⑦「只读成员可看」——没什么可填，也没什么可读）。
                        const opensRowDrawer = index === 0 && filled;
                        const clickable = opensRowDrawer || filled || canEdit;
                        return (
                          <td key={column.id}>
                            {clickable ? (
                              <button
                                type="button"
                                className={`knowhow-cell-open${filled ? "" : " knowhow-cell-open--empty"}`}
                                onClick={() => (opensRowDrawer ? onOpenRow(row.id) : onOpenCell(row.id, column.id))}
                                title={filled ? text : "点击填写这一格"}
                              >
                                {filled ? text : "+"}
                              </button>
                            ) : (
                              <span className="knowhow-cell-text">—</span>
                            )}
                          </td>
                        );
                      })}
                      <td>
                        <ProjectionStatusBadge
                          status={row.projectionStatus}
                          canRetry={canEdit}
                          onRetry={onRetryReproject}
                          retrying={retryingReproject}
                        />
                      </td>
                    </tr>
                  ))
                ) : (
                  // 有 anchor 列：G2 合并矩阵——同概念内相邻同值列合并成一个
                  // rowspan 起始格（规格 §4.2.1），rowSpan===0 的格被上方合并
                  // 格覆盖、渲染跳过。非 anchor 格点击仍落到该格所属的具体物理
                  // 行、复用现有 onOpenCell 单格写入路径（合并格批量写回整组是
                  // 后续 task 的编辑增强，此处先保证显示正确、行为不倒退）；
                  // anchor 列格子改走 onOpenConcept 打开该概念的矩阵抽屉
                  // （Task 8，规格 §4.3），见下方 opensConceptDrawer 判据。
                  gridDisplayRows.map(({ row, cells }) => (
                    <tr key={row.id}>
                      {cells.map((cell) => {
                        if (cell.rowSpan === 0) return null;
                        const text = cellSummary(cell.text);
                        const filled = Boolean(text);
                        const clickable = filled || canEdit;
                        const merged = cell.rowSpan > 1;
                        // 只有已填值的 anchor 格才打开概念矩阵抽屉——forward-fill
                        // 后仍空的「无概念」行（规格 §4.2.2，leading-blank）不
                        // 聚合、每行各自单独成一个 anchorValue==="" 的组
                        // （groupRowsByAnchor），若也路由到这里，多个空概念行会
                        // 撞同一个 "" 值、打开彼此不相干的那一个；而矩阵抽屉本身
                        // 又不含 anchor 列（buildConceptMatrix 排除），压根没地方
                        // 补概念名，会把用户卡死。保持走 onOpenCell 原地补概念
                        // （空格子直接进编辑态，规格②路A），与下方「非 anchor
                        // 格」同一套行为，只是列身份不同。
                        const opensConceptDrawer = cell.columnId === anchorColumnId && filled;
                        return (
                          <td
                            key={cell.columnId}
                            rowSpan={merged ? cell.rowSpan : undefined}
                            className={merged ? "knowhow-cell-merged" : undefined}
                          >
                            {clickable ? (
                              <button
                                type="button"
                                className={`knowhow-cell-open${filled ? "" : " knowhow-cell-open--empty"}`}
                                onClick={() =>
                                  opensConceptDrawer
                                    ? onOpenConcept(cell.text.trim())
                                    : onOpenCell(row.id, cell.columnId)
                                }
                                title={filled ? text : "点击填写这一格"}
                              >
                                {filled ? text : "+"}
                              </button>
                            ) : (
                              <span className="knowhow-cell-text">—</span>
                            )}
                          </td>
                        );
                      })}
                      <td>
                        <ProjectionStatusBadge
                          status={row.projectionStatus}
                          canRetry={canEdit}
                          onRetry={onRetryReproject}
                          retrying={retryingReproject}
                        />
                      </td>
                    </tr>
                  ))
                )}
              </tbody>
            </table>
            {filteredRows.length === 0 && (
              <p className="knowhow-no-match">
                {detail.rows.length === 0
                  ? canEdit
                    ? anchorColumnId
                      ? "这张表还没有概念，点下方「概念」开始填写。"
                      : "这张表还没有行，点上方「添加行」开始填写。"
                    : "这张表还没有行。"
                  : `没有匹配「${query}」的行。`}
              </p>
            )}
          </div>
          {/* Task 10（加概念）：只在有 anchor 列的分组视图出现——记录型表
              （anchorColumnId 为 null）保持现状，仍只有顶部工具栏「添加行」
              一个入口，不在这里重复一个几乎等价的按钮。故意放在
              .knowhow-grid-scroll 之外（而不是紧贴表格最后一行内部）：矩阵
              可能很长，把入口钉在滚动区域外层能让它不必滚到底才可见，也不与
              G2 合并单元格的 rowSpan 结构绞在一起。 */}
          {canEdit && anchorColumnId && (
            <div>
              <button
                type="button"
                className="sort-button knowhow-reproject-button"
                onClick={onAddConcept}
                disabled={addingRow || deleting}
                title="新增一个概念"
              >
                <Plus size={14} />
                {addingRow ? "添加中…" : "概念"}
              </button>
            </div>
          )}
        </>
      ) : null}
    </div>
  );
}

// 表头列名 inline 改名（canEdit=true 时启用）：双击列名切编辑框，Enter/失焦
// 保存（改动才落库，同名不落），Esc 取消。落库经父组件 onRename 走既有
// patchKnowhowColumn；本组件只负责 UX 状态机与键盘/焦点管理。
function ColumnNameCell({
  column,
  canEdit,
  onRename,
}: {
  column: KnowhowColumn;
  canEdit: boolean;
  onRename: (name: string) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(column.name);
  const inputRef = useRef<HTMLInputElement>(null);

  useEffect(() => {
    if (editing) {
      inputRef.current?.focus();
      inputRef.current?.select();
    }
  }, [editing]);

  // 外部（其他会话/管理面板/reproject 后）改动了列名：只要不在本地编辑中就
  // 跟着刷新草稿——避免旧草稿覆盖新值。
  useEffect(() => {
    if (!editing) setDraft(column.name);
  }, [column.name, editing]);

  function commit() {
    const trimmed = draft.trim();
    setEditing(false);
    if (!trimmed || trimmed === column.name) {
      setDraft(column.name);
      return;
    }
    onRename(trimmed);
  }

  if (!canEdit) {
    return (
      <span className="knowhow-col-name" title={column.name}>
        {column.name}
      </span>
    );
  }

  if (editing) {
    return (
      <input
        ref={inputRef}
        className="knowhow-col-name-input"
        value={draft}
        onChange={(e) => setDraft(e.target.value)}
        onBlur={commit}
        onKeyDown={(e) => {
          if (e.key === "Enter") {
            e.preventDefault();
            commit();
          } else if (e.key === "Escape") {
            e.preventDefault();
            setDraft(column.name);
            setEditing(false);
          }
        }}
      />
    );
  }

  return (
    <span
      className="knowhow-col-name knowhow-col-name--editable"
      title={`${column.name}（双击改名）`}
      onDoubleClick={() => setEditing(true)}
    >
      {column.name}
    </span>
  );
}
