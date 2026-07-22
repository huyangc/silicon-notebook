/**
 * knowhow-cell-history.tsx
 *
 * knowhow 表版本管理 Task 16：格子浮窗第三态——历史。把两态（预览/编辑）扩成
 * 三态的第三块拼图：这一格的历次值时间线（最新在前，来自
 * fetchKnowhowCellHistory）+「恢复此版本」（canEdit 门控，规格⑦「只读成员
 * 看得到历史但看不到恢复按钮」）。
 *
 * 外壳逐处照抄 KnowhowCellPreview（knowhow-cell-editor.tsx）：kh-modal-overlay
 * -> kh-modal-card -> kh-modal-header(含 breadcrumb/模式徽标/header-actions) ->
 * kh-modal-body -> kh-modal-resize-handle，同一个 useFloatingWindow storageKey
 * （"knowhow.cellModal.window"）与同一个 FULLSCREEN_STORAGE_KEY——三态各自
 * mount 一份 hook 实例但读写同一个 sessionStorage 键，切页签时浮窗位置/尺寸/
 * 全屏选择不跳动，效果等同「共享」（同预览态/编辑态两个既有消费方之间的既有
 * 约定，见 KnowhowCellPreview 头部注释）。
 *
 * 样式（含新增的 .kh-mode-tag--history / .kh-modal-card--history 两条）登记在
 * knowhow-panel.tsx 顶层的 `<style jsx global>`，不在本文件另开 `<style
 * jsx>`——同 KnowhowMatrixDrawer/KnowhowHistoryDrawer 的既有约定：styled-jsx
 * 的 global 样式注入绑定「声明该标签的组件是否渲染过」，KnowhowPanel 是本特性
 * 唯一保证任何时候都已挂载的样式容器。本组件绝大多数视觉都直接复用既有类名
 * （.kh-history-list/.kh-history-item/.knowhow-status-badge/.knowhow-confirm/
 * .knowhow-drawer-edit-button/.knowhow-action-error 等——同 knowhow-history-
 * drawer.tsx 用的那一整套），新增的只有「模式徽标第三色」与「卡片左边条第三
 * 色」两条一次性规则。
 *
 * 单格恢复 ≠ 整表回退（简报disambiguation①）：这里直接调 patchKnowhowCell
 * (..., origin: "revert")——普通的单格保存，不调 revertKnowhowTable，不受
 * 整表回退那套 expected_head_seq 指纹守卫约束。真正的网络调用留在本组件内部
 * 完成（镜像 KnowhowHistoryDrawer 自己直接调 revertKnowhowTable、只经
 * onReverted 回调通知 panel 事后合并/刷新的既有分工），成功后把服务端返回的
 * 最新值经 onRestored 报给 panel，由 panel 合并进它自己的 detail 状态（规则
 * 同 handleCellSave 单格分支——见 knowhow-panel.tsx 该函数尾部）。
 *
 * 范围之外（有意，非遗漏）：恢复只对"这一格、这一行"生效，不会像
 * handleCellSave 那样探测 anchor 分组并批量写整个「合并共享格」——若这一格
 * 当前是被概念组内多个分支共享的合并格，恢复后只有这一行会变成历史值，其余
 * 分支仍是当前值，直到有人再手动编辑一次才会重新趋同。这是简报①明确要求的
 * 简化（"就是一次普通的格子保存"），不是本文件的疏漏。
 */
"use client";

import { useCallback, useEffect, useState, type MouseEvent as ReactMouseEvent } from "react";
import { ChevronLeft, History as HistoryIcon, Maximize2, Minimize2, RefreshCw, RotateCcw, X } from "lucide-react";
import { useFloatingWindow } from "./use-floating-window.ts";
import {
  ROLE_LABELS,
  fetchKnowhowCellHistory,
  patchKnowhowCell,
  type KnowhowCellHistoryEntry,
  type KnowhowCellPatchResult,
  type KnowhowColumn,
  type KnowhowTableDetail,
} from "./knowhow-model.ts";
import { originLabel, isCellHistoryEntryRestorable } from "./knowhow-history-logic.ts";
import { extractErrorMessage } from "./knowhow-import-logic.ts";
import {
  FULLSCREEN_LABEL,
  FULLSCREEN_STORAGE_KEY,
  HISTORY_MODE_TAG,
  KnowhowMarkdown,
  RESTORE_SIZE_LABEL,
  useFullscreenToggle,
} from "./knowhow-cell-editor.tsx";

// 单次拉取条数上限——后端 GET .../cells/{column_id}/history 只支持 limit，没有
// before_seq 游标（不像表级 fetchKnowhowHistory 那样可翻页，见 knowhow-model.ts
// fetchKnowhowCellHistory 头注释与后端 get_knowhow_cell_history 路由），本组件
// 因此不做「加载更早」的分页 UI——给一个比后端默认(50)宽松的上限，减少长期
// 频繁编辑的格子被截断的概率。
const CELL_HISTORY_LIMIT = 100;

export interface KnowhowCellHistoryProps {
  /** 行标题面包屑文本——与预览态/编辑态同一份（由 panel 用 resolveRowTitleText
   * 统一算好传入），保证三态标题逐字一致。 */
  rowTitle: string;
  column: KnowhowColumn;
  /** 当前实时格子内容——用来判定某条历史条目是否已经与当前值一致（一致则
   * 该条「恢复此版本」按钮不出现，恢复到与当前完全相同的值没有意义，镜像
   * knowhow-history-drawer.tsx 对当前 head 隐藏「回到这里」按钮的既有取向）。
   * 判定逻辑在 knowhow-history-logic.ts 的 isCellHistoryEntryRestorable。 */
  contentMd: string;
  notebookId: string;
  apiBase: string;
  /** 整表详情——本组件只读 table.id 发请求，与 KnowhowCellEditor
   * optimizeKnowhowCell/reformatKnowhowCell 调用处同一套用法（该组件也是拿
   * `table.id` 而不是另收一个 tableId prop）。 */
  table: KnowhowTableDetail;
  rowId: string;
  canEdit: boolean;
  /** 恢复成功后把服务端返回的最新值报给 panel，由 panel 合并进它自己的
   * detail 状态——本组件自己不持有整表状态、也不负责重拉表详情。 */
  onRestored: (result: KnowhowCellPatchResult) => void;
  /** 切回预览态（浮窗保持打开，同一个 cellModal.rowId/columnId）。「关闭」
   * 走 onClose 整体关掉浮窗——两者是不同的两件事，故意分开两个回调，不是
   * onBack 的同义重复。 */
  onBack: () => void;
  onClose: () => void;
}

export function KnowhowCellHistory({
  rowTitle,
  column,
  contentMd,
  notebookId,
  apiBase,
  table,
  rowId,
  canEdit,
  onRestored,
  onBack,
  onClose,
}: KnowhowCellHistoryProps) {
  const [fullscreen, toggleFullscreen] = useFullscreenToggle(FULLSCREEN_STORAGE_KEY);
  // 三态共用同一个浮窗身份——同 KnowhowCellPreview/KnowhowCellEditor 头注释：
  // 各 mount 一份 hook 实例但读写同一个 sessionStorage 键，效果等同共享，切
  // 页签时位置/尺寸不跳动。
  const floating = useFloatingWindow({ storageKey: "knowhow.cellModal.window", disabled: fullscreen });

  const [entries, setEntries] = useState<KnowhowCellHistoryEntry[]>([]);
  const [loading, setLoading] = useState(false);
  const [loadError, setLoadError] = useState<string | null>(null);

  const [confirmSeq, setConfirmSeq] = useState<number | null>(null);
  const [restoringSeq, setRestoringSeq] = useState<number | null>(null);
  const [restoreError, setRestoreError] = useState<string | null>(null);

  const reload = useCallback(() => {
    setLoading(true);
    setLoadError(null);
    fetchKnowhowCellHistory(notebookId, table.id, rowId, column.id, CELL_HISTORY_LIMIT)
      .then((list) => setEntries(list))
      .catch((err) => setLoadError(extractErrorMessage(err, "加载历史失败，请重试")))
      .finally(() => setLoading(false));
  }, [notebookId, table.id, rowId, column.id]);

  useEffect(reload, [reload]);

  // Esc 关闭：与 KnowhowCellPreview 同一习语——历史页签是纯只读浏览（恢复的
  // 二次确认另有独立状态），没有未保存内容的概念，直接关闭即可，不需要经过
  // 任何守卫层。
  useEffect(() => {
    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") onClose();
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
  }, [onClose]);

  function handleBackdropClick(event: ReactMouseEvent<HTMLDivElement>) {
    if (event.currentTarget === event.target) onClose();
  }

  async function handleRestore(entry: KnowhowCellHistoryEntry) {
    // after===null 的条目本就不会渲染出「恢复此版本」按钮（见下方
    // isCellHistoryEntryRestorable 门控），这里再挡一道防御性早退——纯粹是
    // 类型收窄 + 防止未来渲染逻辑改动后误触发一次没有内容可写的恢复。
    if (entry.after === null || restoringSeq !== null) return;
    setRestoringSeq(entry.seq);
    setRestoreError(null);
    try {
      // 单格恢复不是「回退」：普通的一次格子保存，只是 origin 传 "revert"
      // （第 7 个位置参数）——不带 expectedBefore（不受并发防护 P1-b 的基线
      // 比对约束，同手动格子编辑的既有 last-write-wins 语义），也不调
      // revertKnowhowTable（不受整表回退的 expected_head_seq 指纹守卫约束）。
      const result = await patchKnowhowCell(
        notebookId,
        table.id,
        rowId,
        column.id,
        entry.after,
        undefined,
        "revert",
      );
      setConfirmSeq(null);
      onRestored(result);
      reload();
    } catch (err) {
      setRestoreError(extractErrorMessage(err, "恢复失败，请重试"));
    } finally {
      setRestoringSeq(null);
    }
  }

  return (
    <div className="kh-modal-overlay" onClick={handleBackdropClick}>
      <div
        ref={floating.cardRef}
        className={`kh-modal-card kh-modal-card--history${fullscreen ? " kh-modal-card--fullscreen" : ""}`}
        style={floating.style}
        role="dialog"
        aria-modal="true"
        aria-label={`${rowTitle} › ${column.name} › 历史`}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="kh-modal-header" {...floating.dragHandleProps}>
          <div className="kh-modal-header-top">
            <div className="kh-modal-breadcrumb">
              <span className="kh-modal-row-title" title={rowTitle}>
                {rowTitle}
              </span>
              <span className="kh-modal-sep">›</span>
              <span className="kh-modal-col-name">{column.name}</span>
              {column.role !== "attribute" && (
                <span className={`knowhow-role-badge knowhow-role-badge--${column.role}`}>
                  {ROLE_LABELS[column.role]}
                </span>
              )}
              <span className="kh-mode-tag kh-mode-tag--history">
                <HistoryIcon size={11} /> {HISTORY_MODE_TAG}
              </span>
            </div>
            <div className="kh-modal-header-actions">
              <button type="button" className="kh-preview-edit-button" onClick={onBack}>
                <ChevronLeft size={14} /> 返回预览
              </button>
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
        </header>
        <div className="kh-modal-body">
          {restoreError && (
            <div className="knowhow-action-error">
              <span>{restoreError}</span>
              <button type="button" onClick={() => setRestoreError(null)} title="关闭">
                <X size={14} />
              </button>
            </div>
          )}
          {loading ? (
            <p className="knowhow-loading">加载中…</p>
          ) : loadError ? (
            <div className="knowhow-action-error">
              <span>{loadError}</span>
              <button type="button" onClick={reload} title="重试">
                <RefreshCw size={14} />
              </button>
            </div>
          ) : entries.length === 0 ? (
            <p className="kh-history-empty">暂无历史记录</p>
          ) : (
            <>
              <ul className="kh-history-list">
                {entries.map((entry) => {
                  const origin = originLabel(entry.origin);
                  const isCurrent = entry.after === contentMd;
                  const restorable = canEdit && isCellHistoryEntryRestorable(entry.after, contentMd);
                  return (
                    <li key={entry.seq} className="kh-history-item">
                      <div className="kh-history-item-summary">
                        <span className="kh-history-item-time">{formatCellHistoryTime(entry.createdAt)}</span>
                        <span>{entry.actor || "—"}</span>
                        {origin && <span className="knowhow-status-badge">{origin}</span>}
                        {isCurrent && <span className="knowhow-status-badge knowhow-status-badge--info">当前</span>}
                      </div>
                      {restorable && (
                        <div className="kh-history-item-actions">
                          {confirmSeq === entry.seq ? (
                            <span className="knowhow-confirm">
                              <span>恢复到这个版本？</span>
                              <button
                                type="button"
                                className="knowhow-confirm-yes"
                                disabled={restoringSeq === entry.seq}
                                onClick={() => handleRestore(entry)}
                              >
                                {restoringSeq === entry.seq ? "恢复中…" : "确认恢复"}
                              </button>
                              <button
                                type="button"
                                className="knowhow-confirm-no"
                                disabled={restoringSeq === entry.seq}
                                onClick={() => setConfirmSeq(null)}
                              >
                                取消
                              </button>
                            </span>
                          ) : (
                            <button
                              type="button"
                              className="knowhow-drawer-edit-button"
                              onClick={() => setConfirmSeq(entry.seq)}
                            >
                              <RotateCcw size={12} /> 恢复此版本
                            </button>
                          )}
                        </div>
                      )}
                      <div className="kh-history-detail">
                        {entry.after !== null ? (
                          <KnowhowMarkdown md={entry.after} notebookId={notebookId} apiBase={apiBase} />
                        ) : (
                          <p className="kh-history-diff-empty">（这一格当时因所在行/列被删除而不存在）</p>
                        )}
                      </div>
                    </li>
                  );
                })}
              </ul>
              {entries.length >= CELL_HISTORY_LIMIT && (
                <p className="kh-history-empty">仅显示最近 {CELL_HISTORY_LIMIT} 条记录</p>
              )}
            </>
          )}
        </div>
        {!fullscreen && <span className="kh-modal-resize-handle" aria-hidden="true" {...floating.resizeHandleProps} />}
      </div>
    </div>
  );
}

// 时间格式化——纯展示 helper，不导出、不单测，镜像 knowhow-history-drawer.tsx
// 私有 formatClock 的既有取向（同类单一用途的展示格式化在该文件里也没有被
// 抽成可测纯函数/挪进 -logic.ts）。与该文件的 formatClock 不同之处：这里没有
// 「按天分组」的日期表头替我们分担日期信息，所以日期+时间都要给，不能只给
// 时分。
function formatCellHistoryTime(iso: string): string {
  const date = new Date(iso);
  if (Number.isNaN(date.getTime())) return iso ?? "";
  return date.toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}
