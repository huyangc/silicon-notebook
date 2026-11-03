"use client";

import { useCallback, useEffect, useRef, useState } from "react";
import { postCollabPulse, type CollabChange, type CollabKind, type CollabViewer } from "./collab-api";
import { COLLAB_CHANGE_NOTICE_MS, COLLAB_DEFAULT_INTERVAL_SECONDS, COLLAB_REFRESH_DEBOUNCE_MS, COLLAB_KIND_LABELS, collabEpoch, collabEpochReturned, collabKinds } from "./collab-refresh";
import { httpErrorStatus } from "./errors";
import type { NotebookSummary } from "./workspace-model";

type Owner = Readonly<{ actorId: string; notebookId: string; workspaceEpoch: number }>;
type Ticket = Readonly<{ owner: Owner; generation: number }>;
type Scope = Ticket & { initialCursor: string; enabled: boolean };
type Effects = {
  isMounted: (kind: CollabKind) => boolean;
  canRefresh: (kind: CollabKind) => boolean;
  refresh: (kind: CollabKind, isCurrent: () => boolean) => Promise<void | "delegated">;
  accessLost: () => void;
};
type View = Readonly<{
  enabled: boolean;
  unavailable: boolean;
  viewers: readonly CollabViewer[];
  otherReaderCount: number;
  pending: readonly CollabKind[];
  busy: boolean;
  notice: string;
}>;
const EMPTY: View = Object.freeze({ enabled: false, unavailable: false, viewers: Object.freeze([]), otherReaderCount: 0, pending: Object.freeze([]), busy: false, notice: "" });
const documentVisible = () => document.visibilityState !== "hidden";

export function useCollabPulse({ actorId, notebookId, notebook, sessionSignal, effects }: {
  actorId: string | null;
  notebookId: string | null;
  notebook: NotebookSummary | null;
  sessionSignal: AbortSignal;
  effects: Effects;
}) {
  const effectsRef = useRef(effects);
  useEffect(() => { effectsRef.current = effects; });
  const actorRef = useRef(actorId);
  const generation = useRef(0);
  const ticketRef = useRef<Ticket | null>(null);
  const activeRef = useRef<Scope | null>(null);
  const controllerRef = useRef<AbortController | null>(null);
  const refreshTimers = useRef(new Map<CollabKind, ReturnType<typeof setTimeout>>());
  const noticeTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  const refreshInFlight = useRef(false);
  const refreshQueue = useRef(new Set<CollabKind>());
  const invalidationRevisions = useRef(new Map<CollabKind, symbol>());
  const cursorRef = useRef("");
  const [scope, setScope] = useState<Scope | null>(null);
  const [view, setView] = useState<View>(EMPTY);

  const invalidate = useCallback(() => {
    generation.current += 1;
    ticketRef.current = null;
    activeRef.current = null;
    controllerRef.current?.abort();
    for (const timer of refreshTimers.current.values()) clearTimeout(timer);
    refreshTimers.current.clear();
    if (noticeTimer.current) clearTimeout(noticeTimer.current);
    noticeTimer.current = null;
    cursorRef.current = "";
    refreshInFlight.current = false;
    refreshQueue.current = new Set();
    invalidationRevisions.current.clear();
    setScope(null);
    setView(EMPTY);
  }, []);
  const activateActor = useCallback((nextActorId: string) => {
    if (actorRef.current !== nextActorId) invalidate();
    actorRef.current = nextActorId;
  }, [invalidate]);
  const beginNotebookTransition = useCallback((owner: Owner): Ticket | null => {
    invalidate();
    if (actorRef.current !== owner.actorId) return null;
    const ticket = { owner, generation: generation.current };
    ticketRef.current = ticket;
    return ticket;
  }, [invalidate]);
  const finishNotebookTransition = useCallback((ticket: Ticket, snapshot: NotebookSummary | null) => {
    if (ticketRef.current !== ticket || ticket.generation !== generation.current) return;
    ticketRef.current = null;
    if (!snapshot || snapshot.id !== ticket.owner.notebookId) return;
    const next = { ...ticket, initialCursor: snapshot.pulse_cursor ?? "", enabled: snapshot.collab_pulse === true };
    activeRef.current = next;
    cursorRef.current = next.initialCursor;
    setScope(next);
    setView({ ...EMPTY, enabled: next.enabled });
  }, []);

  // Only a detail's definite boolean changes the gate; PATCH/list nulls retain it.
  useEffect(() => {
    const active = activeRef.current;
    if (!active || notebook?.id !== active.owner.notebookId || notebook.collab_pulse == null || notebook.collab_pulse === active.enabled) return;
    const next = { ...active, enabled: notebook.collab_pulse };
    activeRef.current = next;
    setScope(next);
    setView((current) => ({ ...current, enabled: next.enabled }));
  }, [notebook]);

  const runRefresh = useCallback(async (kinds: readonly CollabKind[], owner: Scope, manual: boolean) => {
    const isCurrent = () => activeRef.current?.generation === owner.generation && !sessionSignal.aborted;
    if (!isCurrent() || (manual && refreshInFlight.current)) return;
    const queue = refreshQueue.current;
    for (const kind of kinds) queue.add(kind);
    if (!queue.size || refreshInFlight.current || !documentVisible()) return;
    refreshInFlight.current = true;
    setView((current) => ({ ...current, busy: true, notice: manual ? "" : current.notice }));
    let delegated = false;
    let refreshed = 0;
    try {
      while (queue.size) {
        if (!isCurrent() || !documentVisible()) break;
        const kind = queue.values().next().value!;
        queue.delete(kind);
        if (!effectsRef.current.isMounted(kind)) {
          // Opening that domain later performs its ordinary authoritative read.
          // Do not turn an unopened panel into persistent work or extra I/O.
          setView((current) => ({ ...current, pending: current.pending.filter((item) => item !== kind) }));
          continue;
        }
        if (!effectsRef.current.canRefresh(kind)) {
          setView((current) => ({ ...current, pending: [...new Set([...current.pending, kind])] }));
          continue;
        }
        try {
          const revision = invalidationRevisions.current.get(kind);
          delegated = await effectsRef.current.refresh(kind, isCurrent) === "delegated" || delegated;
          refreshed += 1;
          // A later reset may require an explicit refresh without adding a
          // queued read. This read can acknowledge only the invalidation it
          // started with, never that newer reminder whose cursor is already
          // accepted. Check when applying state as well as after the await.
          if (isCurrent()) setView((current) => invalidationRevisions.current.get(kind) === revision
            ? { ...current, pending: current.pending.filter((item) => item !== kind) }
            : current);
        } catch {
          if (isCurrent()) setView((current) => ({ ...current, pending: [...new Set([...current.pending, kind])], notice: "刷新没成功，请重试" }));
        }
      }
      if (manual && isCurrent()) setView((current) => ({ ...current, notice: current.pending.length ? "仍有内容待刷新，请结束编辑后重试" : delegated ? "已通知面板刷新" : refreshed ? "已刷新" : "当前没有需要刷新的已打开面板" }));
    } finally {
      // An old owner cannot release a newer owner's in-flight latch.
      if (isCurrent()) {
        refreshInFlight.current = false;
        setView((current) => ({ ...current, busy: false }));
      }
    }
  }, [sessionSignal]);

  useEffect(() => {
    if (!scope?.enabled || scope.owner.actorId !== actorId || scope.owner.notebookId !== notebookId || sessionSignal.aborted) return;
    const pendingTimers = refreshTimers.current;
    let stopped = false;
    let timer: ReturnType<typeof setTimeout> | null = null;
    let inFlight = false;
    let interval = COLLAB_DEFAULT_INTERVAL_SECONDS;
    let consecutiveResets = 0;
    const epochs: string[] = [];
    const originalEpoch = collabEpoch(cursorRef.current);
    if (originalEpoch) epochs.push(originalEpoch);
    const current = () => !stopped && activeRef.current === scope && !sessionSignal.aborted;
    function receive(changes: readonly CollabChange[], reset: boolean) {
      const invalidatedKinds = collabKinds(changes, reset);
      const kinds = invalidatedKinds.filter((kind) => effectsRef.current.isMounted(kind));
      for (const kind of kinds) invalidationRevisions.current.set(kind, Symbol());
      if (reset && consecutiveResets > 1) {
        // Only a new explicit refresh may acknowledge a consecutive reset.
        // Older automatic work that has not started must not adopt its token;
        // an in-flight read keeps its original token and may finish safely.
        for (const kind of invalidatedKinds) {
          const pending = refreshTimers.current.get(kind);
          if (pending) clearTimeout(pending);
          refreshTimers.current.delete(kind);
          refreshQueue.current.delete(kind);
        }
        setView((value) => ({ ...value, pending: [...new Set([...value.pending, ...kinds])] }));
        return;
      }
      for (const kind of kinds) {
        const previous = refreshTimers.current.get(kind);
        if (previous) clearTimeout(previous);
        refreshTimers.current.set(kind, setTimeout(() => {
          refreshTimers.current.delete(kind);
          if (current()) void runRefresh([kind], scope!, false);
        }, COLLAB_REFRESH_DEBOUNCE_MS));
      }
      const change = changes[changes.length - 1];
      if (change) setView((value) => ({ ...value, notice: `${change.actor_name || "协作者"}刚更新了${COLLAB_KIND_LABELS[change.kind] ?? "内容"}` }));
    }
    async function poll() {
      if (!current() || !documentVisible() || inFlight) return;
      inFlight = true;
      const controller = new AbortController();
      controllerRef.current = controller;
      try {
        const result = await postCollabPulse(scope!.owner.notebookId, cursorRef.current, controller.signal);
        if (!current()) return;
        // A response issued while visible may finish after the tab hides. Do
        // not advance past its changes or start panel reads in the background;
        // the immediate foreground pulse will replay them from the old cursor.
        if (!documentVisible()) return;
        if (collabEpochReturned(epochs, result.cursor)) {
          stopped = true;
          setView((value) => ({ ...value, unavailable: true }));
          console.info("[collab-pulse] disabled: epoch_returned");
          return;
        }
        const epoch = collabEpoch(result.cursor);
        if (epoch && epochs[epochs.length - 1] !== epoch) epochs.push(epoch);
        if (!epoch || !Number.isFinite(result.next_interval_s) || result.next_interval_s <= 0) throw new TypeError("invalid pulse response");
        cursorRef.current = result.cursor;
        interval = result.next_interval_s;
        consecutiveResets = result.reset ? consecutiveResets + 1 : 0;
        setView((value) => ({ ...value, unavailable: false, viewers: result.viewers, otherReaderCount: result.other_reader_count }));
        receive(result.changes, result.reset);
      } catch (error) {
        if (!current() || controller.signal.aborted) return;
        if (httpErrorStatus(error) === 404) {
          stopped = true;
          effectsRef.current.accessLost();
        } else setView((value) => ({ ...value, unavailable: true }));
      } finally {
        inFlight = false;
        if (controllerRef.current === controller) controllerRef.current = null;
        if (current() && documentVisible()) timer = setTimeout(() => { void poll(); }, interval * 1_000);
      }
    }
    function visibility() {
      if (timer) clearTimeout(timer);
      timer = null;
      if (documentVisible()) {
        // These kinds were already acknowledged by our cursor before hiding.
        // Drain them independently of the next pulse's possibly empty delta.
        if (current()) void runRefresh([], scope!, false);
        void poll();
      } else {
        for (const [kind, pending] of pendingTimers) {
          clearTimeout(pending);
          refreshQueue.current.add(kind);
        }
        pendingTimers.clear();
      }
    }
    function abort() { controllerRef.current?.abort(); }
    document.addEventListener("visibilitychange", visibility);
    sessionSignal.addEventListener("abort", abort);
    void runRefresh([], scope, false);
    void poll();
    return () => {
      stopped = true;
      if (timer) clearTimeout(timer);
      controllerRef.current?.abort();
      for (const pending of pendingTimers.values()) clearTimeout(pending);
      pendingTimers.clear();
      document.removeEventListener("visibilitychange", visibility);
      sessionSignal.removeEventListener("abort", abort);
    };
  }, [scope, actorId, notebookId, sessionSignal, runRefresh]);

  useEffect(() => {
    if (!view.notice) return;
    noticeTimer.current = setTimeout(() => setView((value) => ({ ...value, notice: "" })), COLLAB_CHANGE_NOTICE_MS);
    return () => { if (noticeTimer.current) clearTimeout(noticeTimer.current); };
  }, [view.notice]);
  useEffect(() => {
    const timers = refreshTimers.current;
    return () => {
      activeRef.current = null;
      controllerRef.current?.abort();
      for (const timer of timers.values()) clearTimeout(timer);
    };
  }, []);
  const refreshPending = useCallback(async () => {
    const owner = activeRef.current;
    if (owner) await runRefresh(view.pending, owner, true);
  }, [runRefresh, view.pending]);
  const ownerVisible = scope?.owner.actorId === actorId && scope?.owner.notebookId === notebookId;
  return { view: ownerVisible ? view : EMPTY, activateActor, beginNotebookTransition, finishNotebookTransition, leaveWorkspace: invalidate, refreshPending };
}
