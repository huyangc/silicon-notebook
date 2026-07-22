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
 * 单格恢复 ≠ 整表回退（简报disambiguation①）：恢复不调 revertKnowhowTable，
 * 不受整表回退那套 expected_head_seq 指纹守卫约束——它是一次普通的格子
 * 保存，origin 传 "revert"。
 *
 * 评审修复（Important，2026-07-23）：初版实现按简报①"就是一次普通的格子
 * 保存"的简化描述，在本组件内部直调 patchKnowhowCell，绕开了 knowhow-
 * panel.tsx handleCellSave 的 anchor 分组 + isSharedColumn 批量写判定——若
 * 这一格恰好是概念组内多分支共享的合并格，只写当前这一行会让组内其余分支
 * 停留旧值：下次渲染 G2 因该列不再全同值而误判"不再共享"、自动把合并格散
 * 开，这是静默的数据不一致，不是可接受的范围简化。设计文档 §6.4 原话其实
 * 早已写明单格恢复走既有 update_knowhow_cell「含既有的 require_assets 校
 * 验、合并格扇写判定」——是简报①自己把这层要求简化掉了，不是本文件曾经的
 * 疏漏。
 *
 * 现在的分工：真正的网络调用不在本组件内部——落在 onRestore 回调里，由
 * panel 用 handleCellSave(rowId, columnId, entry.after, undefined, undefined,
 * undefined, "revert") 实现，与手动编辑器 onSave 走同一个函数、同一套
 * groupRowsByAnchor + isSharedColumn + batchPatchKnowhowCells 判定——单格
 * 写还是整组批量写完全由 handleCellSave 决定，本组件不重复算一遍分组，也
 * 不关心结果是单请求还是批量请求。affectedBranchCount 由 panel 传入（与
 * KnowhowCellEditor header 提示同源同一份 cellModalAffectedBranchCount），
 * 供确认框在合并共享格时给出"恢复将同步到全部 N 个分支"提示，避免用户以为
 * 恢复只影响眼前这一行。成功后 panel 的 setDetail 已经把新值合并回 detail
 * （同 handleCellSave 尾部），本组件只需 reload() 重新拉一次这一格的历史
 * 时间线。
 */
"use client";

import { useCallback, useEffect, useState, type MouseEvent as ReactMouseEvent } from "react";
import { ChevronLeft, History as HistoryIcon, Maximize2, Minimize2, RefreshCw, RotateCcw, X } from "lucide-react";
import { useFloatingWindow } from "./use-floating-window.ts";
import {
  ROLE_LABELS,
  fetchKnowhowCellHistory,
  type KnowhowCellHistoryEntry,
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
  /** 该格所属概念组的分支数——语义与 KnowhowCellEditorProps.affectedBranchCount
   * 完全一致（同一份 cellModalAffectedBranchCount，见 knowhow-panel.tsx），只是
   * 这里驱动确认框里的「恢复将同步到全部 N 个分支」提示，而不是 header 提示。
   * undefined 或 <=1（记录型表 / 非共享格 / 单行组）时不显示。 */
  affectedBranchCount?: number;
  /** 真正落库委托给 panel——panel 接的就是 handleCellSave 本身（多传一个
   * origin="revert"），与手动编辑走同一套 anchor 分组 + 批量写判定，本组件不
   * 重复判定、也不关心结果是单请求还是批量请求（见本文件头注释）。reject 时
   * 本组件原地展示错误。成功后 panel 已经把新值合并进它自己的 detail 状态
   * （同 handleCellSave 尾部），本组件只需 reload() 重新拉一次这一格的历史
   * 时间线。 */
  onRestore: (rowId: string, columnId: string, contentMd: string) => Promise<void>;
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
  affectedBranchCount,
  onRestore,
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

  // 「当前」徽标：按 seq 判定（同 knowhow-history-drawer.tsx 的 isHead ===
  // change.seq === headSeq 用法），不按值比较——历史里出现重复值（如
  // A→B→A）时值比较会把多条都误判成「当前」，seq 判定保证只有真正最新的
  // 那一条被标记，不受某条更早的值恰好与当前值相同影响。entries 已按 seq
  // 降序返回（最新在前，见 fetchKnowhowCellHistory 注释），故 entries[0]
  // 即对应当前值的那一条。
  const headSeq = entries.length > 0 ? entries[0].seq : null;

  async function handleRestore(entry: KnowhowCellHistoryEntry) {
    // after===null 的条目本就不会渲染出「恢复此版本」按钮（见下方
    // isCellHistoryEntryRestorable 门控），这里再挡一道防御性早退——纯粹是
    // 类型收窄 + 防止未来渲染逻辑改动后误触发一次没有内容可写的恢复。
    if (entry.after === null || restoringSeq !== null) return;
    setRestoringSeq(entry.seq);
    setRestoreError(null);
    try {
      // 单格恢复不是「回退」：普通的一次格子保存，只是 origin 传 "revert"——
      // 不带 expectedBefore（不受并发防护 P1-b 的基线比对约束，同手动格子
      // 编辑的既有 last-write-wins 语义），也不调 revertKnowhowTable（不受
      // 整表回退的 expected_head_seq 指纹守卫约束）。真正的写入委托给
      // onRestore（panel 接的是 handleCellSave 本身，见本文件头注释）——单格
      // 写还是合并格整组批量写，完全由那一层的 anchor 分组判定决定，本组件
      // 不重复判定。
      await onRestore(rowId, column.id, entry.after);
      setConfirmSeq(null);
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
                  const isCurrent = entry.seq === headSeq;
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
                              {/* 合并共享格提示（同 KnowhowCellEditor header 的 kh-affect-hint，
                                  anchor 分组 spec §4.4）：恢复也会批量写整组，必须在真正提交
                                  前就让用户知道范围不止这一行，同一份 affectedBranchCount 判定
                                  保证提示范围与 handleCellSave 实际写入范围一致。 */}
                              {affectedBranchCount && affectedBranchCount > 1 && (
                                <span className="kh-affect-hint">
                                  恢复将同步到该概念下全部 {affectedBranchCount} 个分支
                                </span>
                              )}
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
