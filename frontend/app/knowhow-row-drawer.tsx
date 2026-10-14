/**
 * knowhow-row-drawer.tsx
 *
 * Knowhow 面板第三层「行详情抽屉」的呈现：按列分节渲染整行内容，可写时每节带
 * 「编辑」，抽屉头部是「优化整行」「智能补全空列」「一键规整整行」三个批量入口，
 * 各节挂本格的代码 chip。从 knowhow-panel.tsx 原样搬来（S4-d）：组件体与段落
 * 注释一字未改，只把声明改为 export 供面板 import。
 *
 * 抽屉自带 window Esc 监听，依赖 `[onClose, cellModalOpen]`。面板传入身份稳定的
 * onClose（S6 起；此前是内联箭头，面板每渲染一次都重挂一次），所以监听只在挂载和
 * cellModalOpen 翻转时重挂；有弹窗叠在上面时监听短路，Esc 只关最上层。
 * 代码状态由 use-knowhow-row-code.ts 持有、经 props 传入。样式仍由
 * knowhow-panel-styles.tsx 的 `<style jsx global>` 提供，本文件不含 styled-jsx。
 */
"use client";

import { useEffect, useMemo } from "react";
import { Edit3, ListChecks, Sparkles, X } from "lucide-react";
import {
  cellSummary,
  type KnowhowRow,
  type KnowhowColumn,
  type KnowhowCellCode,
} from "./knowhow-model.ts";
import { completableKnowhowColumns } from "./knowhow-complete-logic.ts";
import { sortColumnsByPosition, resolveRowTitleText } from "./knowhow-panel-logic.ts";
import { KnowhowMarkdown } from "./knowhow-cell-editor.tsx";
import { KnowhowCodeChip } from "./knowhow-code.tsx";
import { resolveCellCodeView } from "./knowhow-code-logic.ts";
import { RoleBadge } from "./knowhow-badges.tsx";
import {
  ROW_OPTIMIZE_BUTTON_LABEL,
  BATCH_REFORMAT_ROW_BUTTON_LABEL,
} from "./knowhow-optimize-logic.ts";

// ---------------------------------------------------------------------------
// Layer 3 — 行详情抽屉
// ---------------------------------------------------------------------------

export function KnowhowRowDrawer({
  row,
  columns,
  notebookId,
  apiBase,
  canEdit,
  onEditCell,
  onClose,
  onOptimizeRow,
  anchorColumnId,
  onCompleteRow,
  onReformatRow,
  cellModalOpen,
  codeByColumn,
  codeLoaded,
  codeError,
  onRetryCode,
  onOpenCode,
}: {
  row: KnowhowRow;
  columns: KnowhowColumn[];
  notebookId: string;
  apiBase: string;
  /** 只读成员不出现分节「编辑」按钮（规格⑦「只读成员可看」）。 */
  canEdit: boolean;
  /** 分节「编辑」按钮（规格⑤「每节「编辑」按钮打开同一个格子浮窗」）：打开
   * 该列对应格子的编辑态，不经预览态中转——用户已经明确点了「编辑」。 */
  onEditCell: (rowId: string, columnId: string) => void;
  onClose: () => void;
  /** 「优化整行」入口（Task 9，规格③）：打开批量优化弹窗，堆叠在本抽屉之上。 */
  onOptimizeRow: () => void;
  /** 智能补全只面向空的非行标题列；只读成员无入口。 */
  anchorColumnId: string | null;
  onCompleteRow: (trigger: HTMLButtonElement) => void;
  /** 「一键规整整行」入口（knowhow-md-normalize Task 9）：打开批量规整弹窗，
   * 同样堆叠在本抽屉之上——与「优化整行」并列但语义不同（规整=只改格式，
   * 优化=改措辞，见 knowhow-cell-editor-logic.ts TOOLBAR_REFORMAT_LABEL
   * 头注释同一区分）。 */
  onReformatRow: () => void;
  /** 任何堆叠在本抽屉之上的顶层弹窗（格子浮窗编辑态/预览态，Task 9 的
   * KnowhowRowOptimizeModal，或 Task 11 的 KnowhowCodeModal）当前是否打开
   * （T7 复审 Important 修复）：为 true 时本抽屉的 Esc 监听器短路、不关闭
   * 抽屉——各自独立的 window keydown 监听器按注册顺序（抽屉先挂载，先注册）
   * 依次响应同一次按键，若不在这里短路，一次 Esc 会先无条件关闭本抽屉（它
   * 自己没有"未保存内容"的概念），顶层弹窗随后可能因为有未保存内容/进行中
   * 的操作改为弹出确认层而不真正关闭——结果抽屉已经消失、弹窗却还留着，
   * 用户之后关掉弹窗时会发现连"返回这一行"的抽屉上下文都丢了。真正想关的
   * 是最顶层的弹窗，Esc 应该只作用于它，抽屉留到弹窗自己关闭之后再响应下
   * 一次 Esc。 */
  cellModalOpen: boolean;
  /** Task 11：本行按列索引的代码状态 map（父组件在抽屉打开时一次性拉取，见
   * use-knowhow-row-code.ts 的 loadRowCode）。 */
  codeByColumn: Record<string, KnowhowCellCode>;
  /** 是否已成功加载过 codeByColumn——false 时（含加载失败）任何分节都不渲染
   * chip，宁可暂时不显示也不能显示可能误导的"添加代码"。 */
  codeLoaded: boolean;
  /** 非 null 时在抽屉正文顶部渲染一条轻量内联提示 + 「重试」。 */
  codeError: string | null;
  onRetryCode: () => void;
  /** 打开某一格的代码查看/编辑浮层（chip 本身，及 canEdit 时的"添加代码"
   * 安静入口共用同一个回调）。 */
  onOpenCode: (rowId: string, columnId: string) => void;
}) {
  useEffect(() => {
    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape" && !cellModalOpen) onClose();
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose, cellModalOpen]);

  const orderedColumns = useMemo(() => sortColumnsByPosition(columns), [columns]);
  const titleText = cellSummary(resolveRowTitleText(row, columns), 200) || "行详情";
  const hasCompletableColumns = completableKnowhowColumns(row, columns, anchorColumnId).length > 0;

  return (
    <>
      <button type="button" className="knowhow-drawer-backdrop" aria-label="关闭行详情" onClick={onClose} />
      <section className="knowhow-drawer" role="dialog" aria-modal="true" aria-label={titleText}>
        <div className="knowhow-drawer-header">
          <h2>{titleText}</h2>
          <div className="knowhow-drawer-header-actions">
            {/* 「优化整行」批量入口（Task 9，规格③）——只读成员看不到（规格⑦）。 */}
            {canEdit && (
              <button type="button" className="knowhow-drawer-edit-button" onClick={onOptimizeRow}>
                <Sparkles size={13} /> {ROW_OPTIMIZE_BUTTON_LABEL}
              </button>
            )}
            {canEdit && hasCompletableColumns && (
              <button
                type="button"
                className="knowhow-drawer-edit-button"
                onClick={(event) => onCompleteRow(event.currentTarget)}
              >
                <Sparkles size={13} /> 智能补全空列
              </button>
            )}
            {/* 「一键规整整行」批量入口（knowhow-md-normalize Task 9）——挨着
                「优化整行」，同一只读门控。 */}
            {canEdit && (
              <button type="button" className="knowhow-drawer-edit-button" onClick={onReformatRow}>
                <ListChecks size={13} /> {BATCH_REFORMAT_ROW_BUTTON_LABEL}
              </button>
            )}
            <button className="icon-button" onClick={onClose} title="关闭">
              <X size={20} />
            </button>
          </div>
        </div>
        <div className="knowhow-drawer-body">
          {/* Task 11：本行代码状态加载失败的轻量提示——不阻塞其余分节的正常
              渲染，只影响代码 chip 这一小块辅助信息。 */}
          {codeError && (
            <p className="kh-code-status-error">
              <span>{codeError}</span>
              <button type="button" onClick={onRetryCode}>
                重试
              </button>
            </p>
          )}
          {orderedColumns.map((column) => (
            <section key={column.id}>
              <div className="knowhow-drawer-section-head">
                <h4>{column.name}</h4>
                <RoleBadge role={column.role} />
                {codeLoaded && (
                  <KnowhowCodeChip
                    code={resolveCellCodeView(codeByColumn, column.id)}
                    canEdit={canEdit}
                    onOpen={() => onOpenCode(row.id, column.id)}
                  />
                )}
                {canEdit && (
                  <button
                    type="button"
                    className="knowhow-drawer-edit-button"
                    onClick={() => onEditCell(row.id, column.id)}
                  >
                    <Edit3 size={13} /> 编辑
                  </button>
                )}
              </div>
              <KnowhowMarkdown md={row.cells[column.id] ?? ""} notebookId={notebookId} apiBase={apiBase} />
            </section>
          ))}
        </div>
      </section>
    </>
  );
}

// KnowhowMarkdown（复用 answer-markdown.tsx 的 GFM+KaTeX 渲染管线，供本文件
// 的行详情抽屉 与 knowhow-cell-editor.tsx 的格子浮窗预览/编辑分栏共用）随格子
// 浮窗一起挪到了 knowhow-cell-editor.tsx——见该文件头注释；本文件已在顶部
// import 使用，不再在此重复定义。
