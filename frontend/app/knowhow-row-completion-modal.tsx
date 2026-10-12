/**
 * knowhow-row-completion-modal.tsx
 *
 * 行详情抽屉「智能补全空列」弹窗 KnowhowRowCompletionModal 及其保存回调类型
 * CompletionSave，从 knowhow-panel.tsx 原样搬来（S4-c）：组件体、类型与段落
 * 注释一字未改，import 只带走本组件实际用到的名字。
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
  type KeyboardEvent as ReactKeyboardEvent,
  type MouseEvent as ReactMouseEvent,
} from "react";
import {
  Check,
  Loader2,
  X,
} from "lucide-react";
import { modeLabel } from "./ask-modes.ts";
import { ReasoningTracePanel } from "./answer-panel.tsx";
import { useFloatingWindow } from "./use-floating-window.ts";
import {
  completeKnowhowRow,
  type KnowhowRow,
  type KnowhowColumn,
  type KnowhowRowCompletion,
  type KnowhowRowCompletionSuggestion,
} from "./knowhow-model.ts";
import {
  COMPLETION_CONFIDENCE_LABELS,
  canAcceptCompletionSuggestion,
  completableKnowhowColumns,
  completionEvidenceForSuggestion,
  completionEvidenceTierLabel,
  completionRetrievalStatusLabel,
  completionSavePlan,
  completionReferenceLabel,
} from "./knowhow-complete-logic.ts";
import { extractErrorMessage } from "./knowhow-import-logic.ts";
import { KnowhowMarkdown } from "./knowhow-cell-editor.tsx";

// ---------------------------------------------------------------------------
// KnowhowRowCompletionModal — 对当前行所有空的非行标题列生成建议，逐项审阅。
// 生成本身不写库；每次接受都复用 handleCellSave，并以空串为并发基线。
// ---------------------------------------------------------------------------

type CompletionSave = (
  rowId: string,
  columnId: string,
  contentMd: string,
  expectedByRowId?: Map<string, string>,
  targetRowIds?: string[],
  anchorGuard?: { anchorColumnId: string; expectedAnchorByRowId: Map<string, string> },
  origin?: string,
) => Promise<void>;

export function KnowhowRowCompletionModal({
  notebookId,
  apiBase,
  tableId,
  row,
  rows,
  columns,
  anchorColumnId,
  rowTitle,
  returnFocusTo,
  fallbackFocusTo,
  onAcceptCell,
  onClose,
}: {
  notebookId: string;
  apiBase: string;
  tableId: string;
  row: KnowhowRow;
  rows: KnowhowRow[];
  columns: KnowhowColumn[];
  anchorColumnId: string | null;
  rowTitle: string;
  returnFocusTo: HTMLButtonElement | null;
  fallbackFocusTo: HTMLElement | null;
  onAcceptCell: CompletionSave;
  onClose: () => void;
}) {
  // 弹窗打开时冻结目标列。接受一项后父 detail 会更新，但不能因此重新请求、
  // 也不能让尚待审阅的其它建议突然从列表消失。
  const [targetColumns] = useState(() => completableKnowhowColumns(row, columns, anchorColumnId));
  const [savePlans] = useState(() => new Map(
    completableKnowhowColumns(row, columns, anchorColumnId).map((column) => [
      column.id,
      completionSavePlan(row.id, column.id, rows, anchorColumnId),
    ]),
  ));
  const [completion, setCompletion] = useState<KnowhowRowCompletion | null>(null);
  const suggestions = completion?.suggestions ?? null;
  const [loadError, setLoadError] = useState<string | null>(null);
  const [savingColumnId, setSavingColumnId] = useState<string | null>(null);
  const [acceptedColumnIds, setAcceptedColumnIds] = useState<Set<string>>(() => new Set());
  const [saveErrors, setSaveErrors] = useState<Record<string, string>>({});
  const mountedRef = useRef(false);
  const startedRef = useRef(false);
  const requestRef = useRef(0);
  const requestControllerRef = useRef<AbortController | null>(null);
  const pendingRequestAbortRef = useRef<ReturnType<typeof setTimeout> | null>(null);
  const closeButtonRef = useRef<HTMLButtonElement>(null);
  const floating = useFloatingWindow({ storageKey: "knowhow.rowCompletion.window" });

  function requestSuggestions() {
    requestControllerRef.current?.abort();
    const controller = new AbortController();
    requestControllerRef.current = controller;
    const requestId = ++requestRef.current;
    setCompletion(null);
    setLoadError(null);
    completeKnowhowRow(
      notebookId,
      tableId,
      row.id,
      targetColumns.map((column) => column.id),
      controller.signal,
    )
      .then((result) => {
        if (controller.signal.aborted || !mountedRef.current || requestRef.current !== requestId) return;
        const byColumnId = new Map(result.suggestions.map((suggestion) => [suggestion.columnId, suggestion]));
        setCompletion({
          ...result,
          suggestions: targetColumns.flatMap((column) => {
            const suggestion = byColumnId.get(column.id);
            return suggestion ? [suggestion] : [];
          }),
        });
      })
      .catch((error) => {
        if (controller.signal.aborted || !mountedRef.current || requestRef.current !== requestId) return;
        setLoadError(extractErrorMessage(error, "生成建议失败，请稍后重试"));
      })
      .finally(() => {
        if (requestControllerRef.current === controller) {
          requestControllerRef.current = null;
        }
      });
  }

  useEffect(() => {
    if (pendingRequestAbortRef.current !== null) {
      clearTimeout(pendingRequestAbortRef.current);
      pendingRequestAbortRef.current = null;
    }
    mountedRef.current = true;
    // React StrictMode 会做一次 effect setup→cleanup→setup 探测。cleanup 把取消
    // 延后一拍；紧随其后的 setup 撤销它并继续持有同一请求，真实卸载才会取消。
    if (!startedRef.current) {
      startedRef.current = true;
      requestSuggestions();
    }
    closeButtonRef.current?.focus();
    return () => {
      mountedRef.current = false;
      const controller = requestControllerRef.current;
      pendingRequestAbortRef.current = setTimeout(() => {
        if (requestControllerRef.current === controller) {
          controller?.abort();
          requestControllerRef.current = null;
        }
        pendingRequestAbortRef.current = null;
      }, 0);
      const focusTarget = returnFocusTo?.isConnected ? returnFocusTo : fallbackFocusTo?.isConnected ? fallbackFocusTo : null;
      focusTarget?.focus();
    };
    // 每次挂载只请求一次；retry 由按钮显式触发，目标列已冻结。
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function requestClose() {
    // 保存中不允许卸载；请求建议时可以关闭并协作取消后端工作。
    if (savingColumnId) return;
    requestControllerRef.current?.abort();
    onClose();
  }

  useEffect(() => {
    function handleKeyDown(event: globalThis.KeyboardEvent) {
      if (event.key !== "Escape") return;
      event.preventDefault();
      requestClose();
    }
    window.addEventListener("keydown", handleKeyDown);
    return () => window.removeEventListener("keydown", handleKeyDown);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [savingColumnId]);

  async function acceptSuggestion(suggestion: KnowhowRowCompletionSuggestion) {
    if (savingColumnId || acceptedColumnIds.has(suggestion.columnId) || !canAcceptCompletionSuggestion(suggestion)) {
      return;
    }
    setSavingColumnId(suggestion.columnId);
    setSaveErrors((current) => {
      const next = { ...current };
      delete next[suggestion.columnId];
      return next;
    });
    try {
      const savePlan = savePlans.get(suggestion.columnId);
      if (!savePlan) throw new Error("completion save plan missing");
      // 弹窗打开时已冻结完整写目标：共享格包含每个成员的空串基线与 anchor
      // 原值；非共享列只有当前行。服务端据此以 409 阻止覆盖或成员漂移。
      await onAcceptCell(
        row.id,
        suggestion.columnId,
        suggestion.suggestionMd ?? "",
        savePlan.expectedByRowId,
        savePlan.targetRowIds,
        savePlan.anchorGuard,
        "llm_complete",
      );
      if (!mountedRef.current) return;
      setAcceptedColumnIds((current) => new Set(current).add(suggestion.columnId));
    } catch (error) {
      if (!mountedRef.current) return;
      setSaveErrors((current) => ({
        ...current,
        [suggestion.columnId]: extractErrorMessage(error, "保存失败，请稍后重试"),
      }));
    } finally {
      if (mountedRef.current) setSavingColumnId(null);
    }
  }

  function handleBackdropClick(event: ReactMouseEvent<HTMLDivElement>) {
    if (event.currentTarget === event.target) requestClose();
  }

  function trapFocus(event: ReactKeyboardEvent<HTMLDivElement>) {
    if (event.key !== "Tab") return;
    const focusable = Array.from(
      event.currentTarget.querySelectorAll<HTMLElement>(
        'button:not([disabled]), a[href], input:not([disabled]), select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])',
      ),
    );
    if (focusable.length === 0) {
      event.preventDefault();
      return;
    }
    const first = focusable[0];
    const last = focusable[focusable.length - 1];
    if (event.shiftKey && (document.activeElement === first || !event.currentTarget.contains(document.activeElement))) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  return (
    <div className="kh-modal-overlay" onClick={handleBackdropClick}>
      <div
        ref={floating.cardRef}
        className="kh-modal-card kh-completion-card"
        style={floating.style}
        role="dialog"
        aria-modal="true"
        aria-label={`智能补全空列 · ${rowTitle}`}
        onClick={(event) => event.stopPropagation()}
        onKeyDown={trapFocus}
      >
        <header className="kh-modal-header" {...floating.dragHandleProps}>
          <div className="kh-modal-header-top">
            <div className="kh-modal-breadcrumb">
              <span className="kh-modal-row-title" title={rowTitle}>{rowTitle}</span>
              <span className="kh-modal-sep">›</span>
              <span className="kh-modal-col-name">智能补全空列</span>
            </div>
            <button ref={closeButtonRef} type="button" className="icon-button" title="关闭" onClick={requestClose} disabled={Boolean(savingColumnId)}>
              <X size={18} />
            </button>
          </div>
        </header>

        <div className="kh-modal-body kh-completion-body">
          {suggestions === null && !loadError && (
            <p className="kh-row-context-empty" role="status" aria-live="polite"><Loader2 size={15} className="spin" /> 正在执行表内参考 + 全库推理检索并生成建议…</p>
          )}
          {loadError && (
            <div className="kh-completion-load-error">
              <p className="kh-inline-error" role="alert">{loadError}</p>
              <button type="button" onClick={requestSuggestions}>重试</button>
            </div>
          )}
          {completion && (
            <section className="kh-completion-retrieval" aria-label="补全检索信息">
              <div className="kh-completion-retrieval-meta">
                <span>检索模式：{modeLabel("reasoning")}</span>
                <span>范围：当前笔记本与已挂载参考库</span>
                <span>{completionRetrievalStatusLabel(completion.retrievalStatus)}</span>
              </div>
              {completion.reasoningTrace.length > 0 && (
                <ReasoningTracePanel steps={completion.reasoningTrace} />
              )}
            </section>
          )}
          {suggestions && suggestions.length === 0 && (
            <p className="kh-row-context-empty">暂时没有可用建议，你可以保留空白并稍后手动填写。</p>
          )}
          {suggestions?.map((suggestion) => {
            const column = targetColumns.find((candidate) => candidate.id === suggestion.columnId);
            if (!column) return null;
            const accepted = acceptedColumnIds.has(suggestion.columnId);
            const acceptable = canAcceptCompletionSuggestion(suggestion);
            const saving = savingColumnId === suggestion.columnId;
            const linkedEvidence = completion
              ? completionEvidenceForSuggestion(suggestion, completion.evidence)
              : [];
            return (
              <section key={suggestion.columnId} className="kh-completion-item">
                <div className="kh-completion-item-head">
                  <h4>{column.name}</h4>
                  <span className={`kh-completion-confidence kh-completion-confidence--${suggestion.confidence}`}>
                    {COMPLETION_CONFIDENCE_LABELS[suggestion.confidence]}
                  </span>
                </div>
                {acceptable ? (
                  <div className="kh-completion-suggestion">
                    <KnowhowMarkdown md={suggestion.suggestionMd ?? ""} notebookId={notebookId} apiBase={apiBase} />
                  </div>
                ) : (
                  <p className="kh-completion-abstain">暂不建议填写：{suggestion.abstainReason.trim() || "现有记录不足以可靠判断"}</p>
                )}
                {suggestion.basis.trim() && <p className="kh-completion-basis">依据：{suggestion.basis}</p>}
                <div className="kh-completion-references" aria-label={`${column.name} 的表内参考`}>
                  <span>表内参考</span>
                  {suggestion.basedOnRowIds.length === 0 ? (
                    <span className="kh-completion-no-evidence">未引用表内参考记录。</span>
                  ) : suggestion.basedOnRowIds.map((referenceRowId, index) => (
                      <span key={`${referenceRowId}:${index}`} className="kh-completion-reference">
                        {completionReferenceLabel(referenceRowId, rows, columns)}
                      </span>
                    ))}
                </div>
                <div className="kh-completion-evidence" aria-label={`${column.name} 的知识库证据`}>
                  <h5>知识库证据</h5>
                  {linkedEvidence.length === 0 ? (
                    <p className="kh-completion-no-evidence">此项未引用知识库证据。</p>
                  ) : linkedEvidence.map((item) => (
                    <article key={item.key} className="kh-completion-evidence-card">
                      <div className="kh-completion-evidence-head">
                        <strong>{item.label.trim() || "知识库证据"}</strong>
                        <span>{completionEvidenceTierLabel(item.tier)}</span>
                      </div>
                      {(item.objectType.trim() || item.sourceTitle.trim() || item.locationLabel.trim()) && (
                        <div className="kh-completion-evidence-meta">
                          {item.objectType.trim() && <span>{item.objectType}</span>}
                          {item.sourceTitle.trim() && <span>{item.sourceTitle}</span>}
                          {item.locationLabel.trim() && <span>{item.locationLabel}</span>}
                        </div>
                      )}
                      {item.excerptMd.trim() ? (
                        <div className="kh-completion-evidence-excerpt">
                          <KnowhowMarkdown md={item.excerptMd} notebookId={notebookId} apiBase={apiBase} inert />
                        </div>
                      ) : (
                        <p className="kh-completion-no-evidence">未提供可展示的证据摘录。</p>
                      )}
                    </article>
                  ))}
                </div>
                {saveErrors[suggestion.columnId] && <p className="kh-inline-error" role="alert">{saveErrors[suggestion.columnId]}</p>}
                <div className="kh-completion-item-actions">
                  <button
                    type="button"
                    className="kh-primary-button"
                    onClick={() => void acceptSuggestion(suggestion)}
                    disabled={!acceptable || accepted || Boolean(savingColumnId)}
                    aria-label={`接受 ${column.name} 建议`}
                  >
                    {accepted ? <><Check size={14} /> 已填入</> : saving ? <span role="status" aria-live="polite"><Loader2 size={14} className="spin" /> 保存中…</span> : "接受此项"}
                  </button>
                </div>
              </section>
            );
          })}
        </div>
        <footer className="kh-modal-footer">
          <div className="kh-footer-actions">
            <button type="button" onClick={requestClose} disabled={Boolean(savingColumnId)}>关闭</button>
          </div>
        </footer>
        <span className="kh-modal-resize-handle" aria-hidden="true" {...floating.resizeHandleProps} />
      </div>
    </div>
  );
}
