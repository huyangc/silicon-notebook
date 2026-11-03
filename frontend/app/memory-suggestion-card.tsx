"use client";

import { useEffect, useRef, useState } from "react";
import { AnswerMarkdown } from "./answer-markdown";
import { requestJson } from "./api-client";
import { FloatingModalCard } from "./floating-modal-card";
import { toUserMessage } from "./errors";
import { MEMORY_PAGE_SIZE, memoryListPath } from "./memory-model";
import { memorySuggestionAnswer, memorySuggestionConflictsWithSources, memorySuggestionReason, type SuggestionAnswerTarget } from "./memory-suggestion-model";
import { useDialogFocus } from "./use-dialog-focus";
import type { RootModalView } from "./use-root-modal-coordinator";
import { MEMORY_SUGGESTION_RESULT_MS, type MemorySuggestionReview } from "./use-memory-suggestion-review";
import type { MemoryRecord, MemoryStatus, PaginatedMemories } from "./workspace-model";

export function MemorySuggestionSummary({ memory, onOpenAnswer, sessionSignal }: {
  memory: MemoryRecord;
  onOpenAnswer?: (target: SuggestionAnswerTarget) => Promise<boolean>;
  sessionSignal: AbortSignal;
}) {
  const target = memorySuggestionAnswer(memory);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const active = useRef(true);
  const inFlight = useRef(false);
  useEffect(() => { active.current = true; return () => { active.current = false; }; }, []);
  useEffect(() => {
    if (!error) return;
    const timer = setTimeout(() => setError(""), MEMORY_SUGGESTION_RESULT_MS);
    return () => clearTimeout(timer);
  }, [error]);
  async function open() {
    if (!target || !onOpenAnswer || inFlight.current || sessionSignal.aborted) return;
    inFlight.current = true;
    setBusy(true);
    setError("");
    try {
      if (!await onOpenAnswer(target) && active.current && !sessionSignal.aborted) setError("原回答未能打开，请重试");
    } catch (cause) {
      if (active.current && !sessionSignal.aborted) setError(toUserMessage(cause, "原回答未能打开，请重试"));
    } finally {
      inFlight.current = false;
      if (active.current && !sessionSignal.aborted) setBusy(false);
    }
  }
  return <div className="memory-suggestion-reason">
    {memorySuggestionConflictsWithSources(memory) && <span className="memory-badge tone-warning">与资料不一致</span>}
    <p>{memorySuggestionReason(memory)}{target && onOpenAnswer && <> · <button type="button" className="link-button inline-action" disabled={busy || sessionSignal.aborted} onClick={() => { void open(); }}>{busy ? "正在打开…" : "查看原回答"}</button></>}</p>
    {error && <span role="alert">{error}</span>}
  </div>;
}

export function MemorySuggestionReceipt({ memoryId, review }: { memoryId: string; review: MemorySuggestionReview }) {
  const receipt = review.view.receipts[memoryId];
  if (!receipt) return null;
  return <div className="memory-suggestion-result">
    {receipt.message && <span role={receipt.failed ? "alert" : "status"}>{receipt.message}</span>}
    {receipt.retryRefines && <p>旧记忆《{receipt.retryRefines.title}》尚未弃用。 <button type="button" className="link-button inline-action" disabled={review.view.busy} onClick={() => { void review.commands.retryRefines(memoryId); }}>{review.view.busy ? "正在处理…" : "重试弃用旧记忆"}</button></p>}
  </div>;
}

export function MemorySuggestionAdoptionModal({ review, modalView }: { review: MemorySuggestionReview; modalView: RootModalView }) {
  const pending = review.view.pending;
  const [extractKg, setExtractKg] = useState(true);
  const [deprecateOld, setDeprecateOld] = useState(false);
  const container = useRef<HTMLDivElement>(null);
  const cancel = useRef<HTMLButtonElement>(null);
  useEffect(() => { setExtractKg(true); setDeprecateOld(false); }, [pending?.lease.issue]);
  useDialogFocus({ containerRef: container, initialFocusRef: cancel, active: Boolean(pending && modalView.open && modalView.topmost) });
  if (!pending || !modalView.open) return null;
  const receipt = review.view.receipts[pending.memory.id];
  return <div ref={container} className="utility-modal utility-modal-top" role="dialog" aria-modal={modalView.topmost} aria-hidden={!modalView.topmost} inert={modalView.topmost ? undefined : true} aria-label="采纳记忆建议" style={{ zIndex: modalView.zIndex }}>
    <FloatingModalCard storageKey="memory.suggestion-adopt.window" className="utility-modal-card memory-confirm-card">
      {(floating) => <>
        <h3 {...floating.dragHandleProps}>采纳记忆建议</h3>
        <strong>{pending.draft.title}</strong>
        <div className="memory-markdown"><AnswerMarkdown answer={pending.draft.content_md} onReferenceClick={() => undefined} /></div>
        {pending.draft.tags.length > 0 && <div className="memory-tags">{pending.draft.tags.map((tag) => <span key={tag}>#{tag}</span>)}</div>}
        {pending.loading ? <p role="status">正在读取采纳选项…</p> : pending.loadError ? <p role="alert">{pending.loadError} <button type="button" className="link-button inline-action" onClick={review.commands.reload}>重试</button></p> : pending.eligible && <label className="agent-check"><input type="checkbox" checked={extractKg} disabled={review.view.busy} onChange={(event) => setExtractKg(event.target.checked)} />同时整理进知识图谱</label>}
        {pending.refined && <label className="agent-check"><input type="checkbox" checked={deprecateOld} disabled={review.view.busy || pending.loading} onChange={(event) => setDeprecateOld(event.target.checked)} />同时弃用《{pending.refined.title}》</label>}
        {pending.refinedHint && <p role={pending.refinedError ? "alert" : undefined}>{pending.refinedHint}{pending.refinedError && <> <button type="button" className="link-button inline-action" disabled={review.view.busy || pending.loading || pending.refinedLoading} onClick={() => { void review.commands.reloadRefined(); }}>{pending.refinedLoading ? "正在读取旧记忆…" : "重试读取旧记忆"}</button></>}</p>}
        {receipt?.message && <p role={receipt.failed ? "alert" : "status"}>{receipt.message}</p>}
        <div className="memory-dialog-actions">
          <button ref={cancel} type="button" onClick={review.commands.close}>取消</button>
          <button type="button" className="primary" disabled={review.view.busy || pending.loading || Boolean(pending.loadError)} onClick={() => { void review.commands.confirm(extractKg, deprecateOld); }}>{review.view.busy ? "正在采纳…" : "确认采纳"}</button>
        </div>
      </>}
    </FloatingModalCard>
  </div>;
}

/** Owner-wide totals, independent of the current list/search/page. */
export function MemorySuggestionStats({ sessionSignal, revision }: { sessionSignal: AbortSignal; revision: number }) {
  const [counts, setCounts] = useState<{ adopted: number; ignored: number } | null>(null);
  const [error, setError] = useState(false);
  const [retry, setRetry] = useState(0);
  useEffect(() => {
    const controller = new AbortController();
    const abort = () => controller.abort();
    sessionSignal.addEventListener("abort", abort, { once: true });
    setCounts(null);
    setError(false);
    if (!sessionSignal.aborted) {
      const total = async (status: MemoryStatus): Promise<number> => {
        const page = await requestJson<PaginatedMemories>(memoryListPath({ scope: "global", notebookId: null, status, origin: "suggested", query: "", offset: 0, limit: MEMORY_PAGE_SIZE }), { tag: "memory", signal: controller.signal });
        if (!Number.isSafeInteger(page.total_count) || page.total_count < 0) throw new Error("invalid suggestion total");
        return page.total_count;
      };
      Promise.all([total("confirmed"), total("deprecated"), total("rejected")]).then(([confirmed, deprecated, rejected]) => {
        if (!controller.signal.aborted) setCounts({ adopted: confirmed + deprecated, ignored: rejected });
      }).catch(() => { if (!controller.signal.aborted) setError(true); });
    }
    return () => { controller.abort(); sessionSignal.removeEventListener("abort", abort); };
  }, [sessionSignal, revision, retry]);
  return <p className="memory-suggestion-stats">{counts ? `系统建议：已采纳 ${counts.adopted}、已忽略 ${counts.ignored}（含过期）` : error ? <>系统建议数量暂不可用。 <button type="button" className="link-button inline-action" onClick={() => setRetry((value) => value + 1)}>重试</button></> : "正在读取系统建议数量…"}</p>;
}
