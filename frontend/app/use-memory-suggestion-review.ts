"use client";

import { useEffect, useRef, useState } from "react";
import { requestJson } from "./api-client";
import { httpErrorStatus, humanizedError, toUserMessage } from "./errors";
import { confirmMemoryBody, MEMORY_PAGE_SIZE, memoryListPath, validateMemoryDraft } from "./memory-model";
import { memorySuggestionRefines, type SuggestionDraft } from "./memory-suggestion-model";
import type { RootModalCoordinator, RootModalLease } from "./use-root-modal-coordinator";
import type { MemoryRecord, PaginatedMemories } from "./workspace-model";

export const MEMORY_SUGGESTION_RESULT_MS = 5000;
type Refined = Pick<MemoryRecord, "id" | "title" | "status">;
type Pending = {
  lease: RootModalLease<"memory-suggestion-adopt">;
  memory: MemoryRecord;
  draft: SuggestionDraft;
  editorOwner?: object;
  eligible: boolean;
  loading: boolean;
  loadError: string;
  refined: Refined | null;
  refinedHint: string;
  refinedError: boolean;
  refinedLoading: boolean;
};
type Receipt = { notebookId: string; message: string; failed: boolean; retryRefines?: Refined };
type AdoptionCompletion = { memoryId: string; issue: number; editorOwner?: object };
type Modals = Pick<RootModalCoordinator, "captureActorOwner" | "issue" | "publish" | "owns" | "requestClose">;

async function readRefined(pending: Pending, signal: AbortSignal): Promise<MemoryRecord | null> {
  const id = memorySuggestionRefines(pending.memory);
  if (!id) return null;
  try {
    const memory = await requestJson<MemoryRecord>(`/memories/${encodeURIComponent(id)}`, { tag: "memory", signal });
    return memory.id === id && memory.notebook_id === pending.memory.notebook_id && memory.created_by === pending.memory.created_by ? memory : null;
  } catch (cause) {
    if (httpErrorStatus(cause) === 403 || httpErrorStatus(cause) === 404) return null;
    throw cause;
  }
}

function refinedFields(hasRefines: boolean, result: PromiseSettledResult<MemoryRecord | null>) {
  const old = result.status === "fulfilled" ? result.value : null;
  const refinedError = result.status === "rejected";
  return {
    refined: old?.status === "confirmed" ? old : null,
    refinedLoading: false,
    refinedError,
    refinedHint: !hasRefines ? "" : refinedError ? "旧记忆暂时未能读取；仍可单独采纳这条建议" : old?.status === "deprecated" ? `《${old.title}》已经弃用，无需再次处理` : old?.status === "confirmed" ? "" : "旧记忆已不可用；仍可单独采纳这条建议",
  };
}

/** Page-owned mutation state; root owns only the presentation lease. */
export function useMemorySuggestionReview({ actorId, sessionSignal, modals }: {
  actorId: string | null;
  sessionSignal: AbortSignal;
  modals: Modals;
}) {
  const ownerRef = useRef({ actorId, sessionSignal });
  if (ownerRef.current.actorId !== actorId || ownerRef.current.sessionSignal !== sessionSignal) ownerRef.current = { actorId, sessionSignal };
  const owner = ownerRef.current;
  const [state, setState] = useState<{ owner: typeof owner; pending: Pending | null; receipts: Record<string, Receipt>; revision: number; adoptionCompleted: AdoptionCompletion | null }>({ owner, pending: null, receipts: {}, revision: 0, adoptionCompleted: null });
  const current = state.owner === owner ? state : { owner, pending: null, receipts: {}, revision: 0, adoptionCompleted: null };
  const pendingRef = useRef(current.pending);
  pendingRef.current = current.pending;
  const modalsRef = useRef(modals);
  modalsRef.current = modals;
  const mounted = useRef(true);
  const details = useRef<AbortController | null>(null);
  const operation = useRef<{ owner: typeof owner; controller: AbortController } | null>(null);
  const [busy, setBusy] = useState<typeof operation.current>(null);
  const timers = useRef(new Map<string, ReturnType<typeof setTimeout>>());
  useEffect(() => {
    mounted.current = true;
    const ownedTimers = timers.current;
    return () => { mounted.current = false; details.current?.abort(); operation.current?.controller.abort(); ownedTimers.forEach(clearTimeout); };
  }, []);
  useEffect(() => {
    const abort = () => {
      if (ownerRef.current.sessionSignal === sessionSignal) details.current?.abort();
      if (operation.current?.owner.sessionSignal === sessionSignal) operation.current.controller.abort();
    };
    sessionSignal.addEventListener("abort", abort, { once: true });
    return () => { sessionSignal.removeEventListener("abort", abort); abort(); };
  }, [sessionSignal]);
  useEffect(() => {
    if (state.owner !== owner && state.pending && modalsRef.current.owns(state.pending.lease)) {
      modalsRef.current.requestClose("memory-suggestion-adopt", "button");
    }
  }, [owner, state.owner, state.pending]);
  const isOwner = () => mounted.current && ownerRef.current === owner && !sessionSignal.aborted && Boolean(actorId);
  const change = (update: (value: typeof state) => typeof state) => setState((previous) => update(previous.owner === owner ? previous : { owner, pending: null, receipts: {}, revision: 0, adoptionCompleted: null }));

  function receipt(id: string, value: Receipt) {
    if (!isOwner()) return;
    const oldTimer = timers.current.get(id);
    if (oldTimer) clearTimeout(oldTimer);
    change((previous) => ({ ...previous, receipts: { ...previous.receipts, [id]: value } }));
    const timer = setTimeout(() => {
      if (timers.current.get(id) === timer) timers.current.delete(id);
      if (!isOwner()) return;
      change((previous) => {
        if (previous.receipts[id] !== value) return previous;
        const receipts = { ...previous.receipts };
        if (value.retryRefines) receipts[id] = { ...value, message: "" };
        else delete receipts[id];
        return { ...previous, receipts };
      });
    }, MEMORY_SUGGESTION_RESULT_MS);
    timers.current.set(id, timer);
  }

  function clear() {
    details.current?.abort();
    details.current = null;
    pendingRef.current = null;
    change((previous) => ({ ...previous, pending: null }));
  }

  async function load(pending: Pending, knownEligible?: boolean) {
    details.current?.abort();
    const controller = new AbortController();
    details.current = controller;
    const stillCurrent = () => isOwner() && !controller.signal.aborted && pendingRef.current?.lease.issue === pending.lease.issue && modals.owns(pending.lease);
    const refines = memorySuggestionRefines(pending.memory);
    const [eligibility, refined] = await Promise.allSettled([
      knownEligible !== undefined ? Promise.resolve(knownEligible) : requestJson<PaginatedMemories>(memoryListPath({ scope: "notebook", notebookId: pending.memory.notebook_id, status: "all", origin: "all", query: "", offset: 0, limit: MEMORY_PAGE_SIZE }), { tag: "memory", signal: controller.signal }).then((page) => {
        if (typeof page.kg_extract_eligible !== "boolean") throw humanizedError("未能读取知识图谱设置，请重试");
        return page.kg_extract_eligible;
      }),
      readRefined(pending, controller.signal),
    ]);
    if (!stillCurrent()) return;
    const next: Pending = { ...pending, loading: false,
      eligible: eligibility.status === "fulfilled" ? eligibility.value : false,
      loadError: eligibility.status === "rejected" ? toUserMessage(eligibility.reason, "未能读取知识图谱设置，请重试") : "",
      ...refinedFields(Boolean(refines), refined),
    };
    pendingRef.current = next;
    change((previous) => ({ ...previous, pending: next }));
  }

  async function reloadRefined() {
    const pending = pendingRef.current;
    if (!pending || !pending.refinedError || pending.refinedLoading || pending.loading || operation.current?.owner === owner || !isOwner() || !modals.owns(pending.lease)) return;
    details.current?.abort();
    const controller = new AbortController();
    details.current = controller;
    const loading = { ...pending, refinedLoading: true };
    pendingRef.current = loading;
    change((previous) => ({ ...previous, pending: loading }));
    const [result] = await Promise.allSettled([readRefined(pending, controller.signal)]);
    if (!isOwner() || controller.signal.aborted || pendingRef.current?.lease.issue !== pending.lease.issue || !modals.owns(pending.lease)) return;
    const next = { ...pending, ...refinedFields(true, result) };
    pendingRef.current = next;
    change((previous) => ({ ...previous, pending: next }));
  }

  function open(memory: MemoryRecord, draft: SuggestionDraft = memory, knownEligible?: boolean, editorOwner?: object) {
    if (!isOwner() || operation.current?.owner === owner || memory.status !== "candidate" || !memory.suggested_signal) return;
    const validation = validateMemoryDraft(draft);
    if (validation) { receipt(memory.id, { notebookId: memory.notebook_id, message: validation, failed: true }); return; }
    const lease = modals.issue("memory-suggestion-adopt", modals.captureActorOwner());
    if (!lease || !modals.publish(lease)) return;
    const pending: Pending = { lease, memory, draft: { title: draft.title, content_md: draft.content_md, tags: [...draft.tags] }, editorOwner, eligible: false, loading: true, loadError: "", refined: null, refinedHint: "", refinedError: false, refinedLoading: false };
    pendingRef.current = pending;
    change((previous) => ({ ...previous, pending }));
    void load(pending, knownEligible);
  }

  async function deprecate(refined: Refined, signal: AbortSignal) {
    const updated = await requestJson<MemoryRecord>(`/memories/${encodeURIComponent(refined.id)}/deprecate`, { method: "POST", tag: "memory", signal });
    if (updated.id !== refined.id || updated.status !== "deprecated") throw humanizedError("旧记忆尚未弃用，请重试");
  }

  async function confirm(extractKg: boolean, deprecateOld: boolean) {
    const pending = pendingRef.current;
    if (!pending || pending.loading || pending.loadError || !modals.owns(pending.lease) || !isOwner() || operation.current?.owner === owner) return;
    const active = { owner, controller: new AbortController() };
    operation.current = active;
    setBusy(active);
    let adopted = false;
    const refined = deprecateOld ? pending.refined : null;
    try {
      const memory = await requestJson<MemoryRecord>(`/memories/${encodeURIComponent(pending.memory.id)}/confirm`, { method: "POST", tag: "memory", signal: active.controller.signal,
        body: JSON.stringify(confirmMemoryBody({ ...pending.draft, eligible: pending.eligible, extractKg })),
      });
      if (!isOwner() || active.controller.signal.aborted) return;
      if (memory.id !== pending.memory.id || memory.status !== "confirmed") throw humanizedError("尚未确认采纳结果，请刷新后核对");
      adopted = true;
      change((previous) => ({ ...previous, revision: previous.revision + 1, adoptionCompleted: { memoryId: memory.id, issue: pending.lease.issue, editorOwner: pending.editorOwner } }));
      if (refined) await deprecate(refined, active.controller.signal);
      if (!isOwner() || active.controller.signal.aborted) return;
      if (refined) change((previous) => ({ ...previous, revision: previous.revision + 1 }));
      receipt(memory.id, { notebookId: pending.memory.notebook_id, message: refined ? "已采纳，旧记忆已弃用" : "已采纳", failed: false });
      if (modals.owns(pending.lease)) modals.requestClose("memory-suggestion-adopt", "button");
    } catch (cause) {
      if (!isOwner() || active.controller.signal.aborted) return;
      receipt(pending.memory.id, adopted
        ? { notebookId: pending.memory.notebook_id, message: "已采纳，但旧记忆未能弃用", failed: true, ...(refined ? { retryRefines: refined } : {}) }
        : { notebookId: pending.memory.notebook_id, message: toUserMessage(cause, "采纳失败，请稍后重试"), failed: true });
      if (adopted && modals.owns(pending.lease)) modals.requestClose("memory-suggestion-adopt", "button");
    } finally {
      if (operation.current === active) operation.current = null;
      if (mounted.current) setBusy((value) => value === active ? null : value);
    }
  }

  async function retryRefines(id: string) {
    const previous = current.receipts[id];
    const refined = previous?.retryRefines;
    if (!refined || !isOwner() || operation.current?.owner === owner) return;
    const active = { owner, controller: new AbortController() };
    operation.current = active;
    setBusy(active);
    try {
      const actual = await requestJson<MemoryRecord>(`/memories/${encodeURIComponent(refined.id)}`, { tag: "memory", signal: active.controller.signal });
      if (!isOwner() || active.controller.signal.aborted) return;
      if (actual.id !== refined.id || actual.notebook_id !== previous.notebookId || actual.created_by !== actorId || !["confirmed", "deprecated"].includes(actual.status)) throw humanizedError("未能确认旧记忆状态，请重试");
      if (actual.status === "confirmed") await deprecate(refined, active.controller.signal);
      if (!isOwner() || active.controller.signal.aborted) return;
      receipt(id, { notebookId: previous.notebookId, message: "旧记忆已弃用", failed: false });
      change((previous) => ({ ...previous, revision: previous.revision + 1 }));
    } catch (cause) {
      if (isOwner() && !active.controller.signal.aborted) receipt(id, { notebookId: previous.notebookId, message: toUserMessage(cause, "旧记忆未能弃用，请重试"), failed: true, retryRefines: refined });
    } finally {
      if (operation.current === active) operation.current = null;
      if (mounted.current) setBusy((value) => value === active ? null : value);
    }
  }

  return {
    view: { pending: current.pending && modals.owns(current.pending.lease) ? current.pending : null, busy: busy?.owner === owner, receipts: current.receipts, revision: current.revision, adoptionCompleted: current.adoptionCompleted },
    commands: { open, clear, confirm, retryRefines, reloadRefined, changed: () => { if (isOwner()) change((previous) => ({ ...previous, revision: previous.revision + 1 })); }, reload: () => {
      if (!current.pending || current.pending.loading || current.pending.refinedLoading || operation.current?.owner === owner) return;
      const pending = { ...current.pending, loading: true, loadError: "" };
      pendingRef.current = pending;
      change((previous) => ({ ...previous, pending }));
      void load(pending);
    }, close: () => { if (current.pending && modals.owns(current.pending.lease)) modals.requestClose("memory-suggestion-adopt", "button"); } },
  };
}

export type MemorySuggestionReview = ReturnType<typeof useMemorySuggestionReview>;
