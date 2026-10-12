/**
 * knowhow-row-optimize-modal.tsx
 *
 * 行详情抽屉「优化整行」弹窗 KnowhowRowOptimizeModal 及其 props 类型，从
 * knowhow-panel.tsx 原样搬来（S4-c）：组件体、类型与段落注释一字未改，只把
 * 声明改为 export 供面板 import；import 只带走本组件实际用到的名字。
 *
 * 只由 KnowhowPanel 渲染，挂载点仍在面板 JSX 的原位置，所以弹窗的 DOM 叠放
 * 顺序、effect 顺序和传入回调的身份都不变。样式仍由 knowhow-panel-styles.tsx
 * 的 `<style jsx global>` 提供，本文件不含 styled-jsx。
 */
"use client";

import {
  useEffect,
  useRef,
  useState,
  type MouseEvent as ReactMouseEvent,
} from "react";
import {
  Check,
  X,
} from "lucide-react";
import { useFloatingWindow } from "./use-floating-window.ts";
import {
  optimizeKnowhowCell,
  type KnowhowRow,
  type KnowhowColumn,
} from "./knowhow-model.ts";
import { extractErrorMessage } from "./knowhow-import-logic.ts";
import { KnowhowMarkdown } from "./knowhow-cell-editor.tsx";
import {
  ACCEPT_SUGGESTION_LABEL,
  OPTIMIZE_ORIGINAL_LABEL,
  OPTIMIZE_SUGGESTION_LABEL,
  ROW_OPTIMIZE_ABORT_LABEL,
  ROW_OPTIMIZE_BUTTON_LABEL,
  ROW_OPTIMIZE_CLOSE_LABEL,
  ROW_OPTIMIZE_DONE_TEXT,
  ROW_OPTIMIZE_EMPTY_TEXT,
  ROW_OPTIMIZE_RETRY_LABEL,
  ROW_OPTIMIZE_SKIP_LABEL,
  ROW_OPTIMIZE_STATUS_LABELS,
  abortQueue,
  applyError,
  applySuggestion,
  beginAcceptCurrent,
  completeAcceptCurrent,
  currentRowOptimizeItem,
  failAcceptCurrent,
  initRowOptimizeQueue,
  isRowOptimizeQueueFinished,
  markCurrentInProgress,
  retryCurrent,
  rowOptimizeProgress,
  skipCurrent,
  type RowOptimizeItem,
  type RowOptimizeQueueState,
} from "./knowhow-optimize-logic.ts";

// ---------------------------------------------------------------------------
// KnowhowRowOptimizeModal — 行详情抽屉「优化整行」批量弹窗（Task 9，规格③ +
// 任务简报：非空格子逐格顺序调用、每格接受/跳过、进度与错误逐格显示）。
//
// 堆叠在 KnowhowRowDrawer 之上（复用同一套 kh-modal-* 视觉语言——与格子浮窗
// 同款外壳），抽屉本身留在背后不关闭，方便用户随时看行全貌；本弹窗只在这
// 一行的格子间推进，不涉及切行，交互形态与面向单格的 KnowhowCellEditor 不
// 同，故不复用那个组件，而是自己按 knowhow-optimize-logic.ts 的队列状态机
// 驱动渲染。
//
// 队列的推进方式：每次「当前格」从 waiting 变为其它状态后，若还有下一格且
// 未中止，本地算出下一格状态后立即触发它的 optimizeKnowhowCell 调用——不
// 依赖 useEffect 监听状态变化去自动推进（避免 React 18 StrictMode 开发期
// 双调用导致同一格重复发起请求那一整套额外复杂度），纯靠事件处理函数内部
// 显式串联「计算下一状态 -> setQueue -> 若有下一格则手动触发」。唯一的
// useEffect 只负责"挂载时启动队列首格"这一次性动作，用 startedRef 闩防
// StrictMode 双调用重复触发首格请求。
// ---------------------------------------------------------------------------

interface KnowhowRowOptimizeModalProps {
  notebookId: string;
  apiBase: string;
  tableId: string;
  row: KnowhowRow;
  columns: KnowhowColumn[];
  rowTitle: string;
  /** 接受：真正落库(patchKnowhowCell 或合并格批量走 batchPatchKnowhowCells)
   * +把结果合并回 detail 状态——复用面板自己的 handleCellSave，与格子浮窗
   * 保存走同一条路径，不重复实现。 */
  onAcceptCell: (rowId: string, columnId: string, contentMd: string) => Promise<void>;
  onClose: () => void;
}

export function KnowhowRowOptimizeModal({
  notebookId,
  apiBase,
  tableId,
  row,
  columns,
  rowTitle,
  onAcceptCell,
  onClose,
}: KnowhowRowOptimizeModalProps) {
  const [queue, setQueue] = useState<RowOptimizeQueueState>(() => initRowOptimizeQueue(row, columns));
  const [acceptBusy, setAcceptBusy] = useState(false);
  const startedRef = useRef(false);
  const mountedRef = useRef(true);
  const optimizeControllerRef = useRef<AbortController | null>(null);
  const pendingOptimizeAbortRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  // 本弹窗没有全屏概念（任务表未列出）——不传 disabled，拖动/resize 恒生效。
  const floating = useFloatingWindow({ storageKey: "knowhow.rowOptimize.window" });

  useEffect(() => {
    if (pendingOptimizeAbortRef.current !== null) {
      clearTimeout(pendingOptimizeAbortRef.current);
      pendingOptimizeAbortRef.current = null;
    }
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      const controller = optimizeControllerRef.current;
      pendingOptimizeAbortRef.current = setTimeout(() => {
        if (optimizeControllerRef.current === controller) {
          controller?.abort();
          optimizeControllerRef.current = null;
        }
        pendingOptimizeAbortRef.current = null;
      }, 0);
    };
  }, []);

  const current = currentRowOptimizeItem(queue);

  async function fireOptimizeFor(item: RowOptimizeItem) {
    optimizeControllerRef.current?.abort();
    const controller = new AbortController();
    optimizeControllerRef.current = controller;
    setQueue((state) => markCurrentInProgress(state));
    try {
      const result = await optimizeKnowhowCell(
        notebookId,
        tableId,
        row.id,
        item.columnId,
        controller.signal,
      );
      if (controller.signal.aborted || !mountedRef.current) return;
      setQueue((state) => applySuggestion(state, result.suggestionMd));
    } catch (err) {
      if (controller.signal.aborted || !mountedRef.current) return;
      setQueue((state) => applyError(state, extractErrorMessage(err, "优化失败，请重试")));
    } finally {
      if (optimizeControllerRef.current === controller) {
        optimizeControllerRef.current = null;
      }
    }
  }

  // 挂载时启动队列首格（若队列非空）。StrictMode 探测的 setup 复用同一请求，
  // 不形成第二次物理模型调用；真实卸载的延后 cleanup 才会取消它。
  useEffect(() => {
    if (startedRef.current) return;
    startedRef.current = true;
    if (current) void fireOptimizeFor(current);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function requestClose() {
    if (acceptBusy) return;
    optimizeControllerRef.current?.abort();
    onClose();
  }

  useEffect(() => {
    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key === "Escape") requestClose();
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [acceptBusy]);

  function handleBackdropClick(event: ReactMouseEvent<HTMLDivElement>) {
    if (event.currentTarget === event.target) requestClose();
  }

  // 「接受」：begin(标记进行中，复用同一状态展示 PATCH 落库这个短暂过程)
  // -> await onAcceptCell -> 成功则 complete(落地+推进游标)并立即触发下一格，
  // 失败则 fail(出错，游标不动，允许跳过或重试)。begin/complete 都基于同一个
  // 本地 `queue` 快照顺序计算(游标在这期间不会被其它操作改动——acceptBusy
  // 已经把接受按钮和跳过按钮都禁用)，不依赖异步 setQueue 是否已经落地。
  async function handleAccept() {
    if (!current || current.status !== "ready" || acceptBusy) return;
    setAcceptBusy(true);
    const busyState = beginAcceptCurrent(queue);
    setQueue(busyState);
    try {
      await onAcceptCell(row.id, current.columnId, current.suggestionMd ?? "");
      if (!mountedRef.current) return;
      const advanced = completeAcceptCurrent(busyState);
      setQueue(advanced);
      const next = currentRowOptimizeItem(advanced);
      if (next && !advanced.aborted) void fireOptimizeFor(next);
    } catch (err) {
      if (!mountedRef.current) return;
      setQueue((state) => failAcceptCurrent(state, extractErrorMessage(err, "保存失败，请重试")));
    } finally {
      if (mountedRef.current) setAcceptBusy(false);
    }
  }

  function handleSkip() {
    if (!current || acceptBusy) return;
    const advanced = skipCurrent(queue);
    if (advanced === queue) return; // 无操作（当前格不在 ready/error）
    setQueue(advanced);
    const next = currentRowOptimizeItem(advanced);
    if (next && !advanced.aborted) void fireOptimizeFor(next);
  }

  function handleRetry() {
    if (!current || acceptBusy) return;
    const retried = retryCurrent(queue);
    if (retried === queue) return; // 无操作（当前格不在 error）
    setQueue(retried);
    const retryItem = currentRowOptimizeItem(retried);
    if (retryItem) void fireOptimizeFor(retryItem);
  }

  function handleAbort() {
    optimizeControllerRef.current?.abort();
    setQueue((state) => abortQueue(state));
  }

  const finished = isRowOptimizeQueueFinished(queue);
  const progress = rowOptimizeProgress(queue);

  return (
    <div className="kh-modal-overlay" onClick={handleBackdropClick}>
      <div
        ref={floating.cardRef}
        className="kh-modal-card"
        style={floating.style}
        role="dialog"
        aria-modal="true"
        aria-label={`${ROW_OPTIMIZE_BUTTON_LABEL} · ${rowTitle}`}
        onClick={(event) => event.stopPropagation()}
      >
        <header className="kh-modal-header" {...floating.dragHandleProps}>
          <div className="kh-modal-header-top">
            <div className="kh-modal-breadcrumb">
              <span className="kh-modal-row-title" title={rowTitle}>
                {rowTitle}
              </span>
              <span className="kh-modal-sep">›</span>
              <span className="kh-modal-col-name">{ROW_OPTIMIZE_BUTTON_LABEL}</span>
              {queue.items.length > 0 && (
                <span className="knowhow-status-badge">{`${progress.done}/${progress.total}`}</span>
              )}
            </div>
            <div className="kh-modal-header-actions">
              {!finished && !queue.aborted && (
                <button type="button" className="kh-preview-edit-button" onClick={handleAbort}>
                  {ROW_OPTIMIZE_ABORT_LABEL}
                </button>
              )}
              <button
                type="button"
                className="icon-button"
                title="关闭"
                onClick={requestClose}
                disabled={acceptBusy}
              >
                <X size={18} />
              </button>
            </div>
          </div>
        </header>

        <div className="kh-modal-body">
          {queue.items.length === 0 ? (
            <p className="kh-row-context-empty">{ROW_OPTIMIZE_EMPTY_TEXT}</p>
          ) : (
            <>
              <ul className="kh-optimize-queue-list">
                {queue.items.map((item, index) => (
                  <li
                    key={item.columnId}
                    className={`kh-optimize-queue-item${index === queue.cursor ? " kh-optimize-queue-item--current" : ""}`}
                  >
                    <span className="kh-optimize-queue-col">{item.columnName}</span>
                    <span className={`kh-optimize-queue-status kh-optimize-queue-status--${item.status}`}>
                      {ROW_OPTIMIZE_STATUS_LABELS[item.status]}
                    </span>
                  </li>
                ))}
              </ul>

              {current && current.status === "ready" && (
                <div className="kh-optimize-compare">
                  <div className="kh-optimize-pane">
                    <h5>{OPTIMIZE_ORIGINAL_LABEL}</h5>
                    <div className="kh-optimize-pane-body">
                      <KnowhowMarkdown md={current.originalMd} notebookId={notebookId} apiBase={apiBase} />
                    </div>
                  </div>
                  <div className="kh-optimize-pane kh-optimize-pane--suggestion">
                    <h5>{OPTIMIZE_SUGGESTION_LABEL}</h5>
                    <div className="kh-optimize-pane-body">
                      <KnowhowMarkdown md={current.suggestionMd ?? ""} notebookId={notebookId} apiBase={apiBase} />
                    </div>
                  </div>
                </div>
              )}

              {current && current.status === "error" && <p className="kh-inline-error">{current.errorMessage}</p>}

              {finished && <p className="kh-row-context-empty">{ROW_OPTIMIZE_DONE_TEXT}</p>}
            </>
          )}
        </div>

        <footer className="kh-modal-footer">
          {current && current.status === "ready" ? (
            <div className="kh-footer-actions">
              <button type="button" onClick={handleSkip} disabled={acceptBusy}>
                {ROW_OPTIMIZE_SKIP_LABEL}
              </button>
              <button type="button" className="kh-primary-button" onClick={handleAccept} disabled={acceptBusy}>
                <Check size={14} /> {acceptBusy ? "保存中…" : ACCEPT_SUGGESTION_LABEL}
              </button>
            </div>
          ) : current && current.status === "error" ? (
            <div className="kh-footer-actions">
              <button type="button" onClick={handleSkip}>
                {ROW_OPTIMIZE_SKIP_LABEL}
              </button>
              <button type="button" onClick={handleRetry}>
                {ROW_OPTIMIZE_RETRY_LABEL}
              </button>
            </div>
          ) : (
            <div className="kh-footer-actions">
              <button type="button" onClick={requestClose} disabled={acceptBusy}>
                {ROW_OPTIMIZE_CLOSE_LABEL}
              </button>
            </div>
          )}
        </footer>
        <span className="kh-modal-resize-handle" aria-hidden="true" {...floating.resizeHandleProps} />
      </div>
    </div>
  );
}
