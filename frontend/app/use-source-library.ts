"use client";

import { useEffect, useRef, useState } from "react";
import { toUserMessage } from "./errors.ts";
import {
  deleteSource as deleteSourceRequest,
  getNotebookSource,
  getNotebookSourceElementsPage,
  getSource,
  listSources,
  parseSource,
} from "./source-api.ts";
import {
  claimSourceDeleteRefresh,
  filterDeletedSourceItems,
  ownsSourceDeleteRefresh,
} from "./source-delete-state.ts";
import { clampSourcePage, sourcePageRequestIsCurrent } from "./source-page-state.ts";
import { sourceElementDomId } from "./source-detail-state.ts";
import {
  crossLibrarySourceNotebookId,
  defaultSourceScopeSelection,
  removeSourceFromSelection,
  toggleSourceSelection,
  type SourceScopeSelection,
} from "./source-scope.ts";
import type {
  PaginatedSourceElements,
  PaginatedSources,
  SourceElement,
  SourceSummary,
} from "./workspace-model.ts";
import { SOURCES_PAGE_SIZE } from "./workspace-model.ts";

const SOURCE_ELEMENT_PAGE_SIZE = 40;
const SOURCE_POLL_INITIAL_MS = 1500;
const SOURCE_POLL_MAX_MS = 15000;
const SOURCE_POLL_MAX_ATTEMPTS = 120;

// Hidden-state (owner not active) fallback values must be **stable references**.
// Consumers of the returned view depend on these fields in effects/useMemo;
// a fresh `[]` on every render makes those dependencies "change" every
// render, which can drive a setState-in-effect loop (see use-ask-session.ts
// for the traced incident). Freezing also turns any accidental in-place
// write into an immediate dev-time throw. Declared with the same mutable
// type as the state they stand in for so the ternary branches unify.
const NO_SOURCES: SourceSummary[] = Object.freeze([] as SourceSummary[]) as SourceSummary[];
const NO_SOURCE_ELEMENTS: SourceElement[] = Object.freeze([] as SourceElement[]) as SourceElement[];

export type SourceLibraryOwner = Readonly<{
  actorId: string;
  notebookId: string;
  workspaceEpoch: number;
  generation: number;
}>;

type SourceLibraryEffects = {
  setStatusText(message: string): void;
  reportError(error: unknown): void;
  setToast(message: string): void;
  invalidateKnowledge(): void;
  refreshCollection(guard: () => boolean): Promise<void>;
  refreshNotebook(notebookId: string, guard: () => boolean): Promise<void>;
  refreshCheckup(notebookId: string, guard: () => boolean): Promise<void> | void;
};

type UseSourceLibraryOptions = {
  actorId: string | null;
  canWriteSources: boolean;
  effects: SourceLibraryEffects;
};

function ownerKey(actorId: string, notebookId: string): string {
  return `${actorId}\u0000${notebookId}`;
}

function sameOwner(
  current: SourceLibraryOwner | null,
  expected: SourceLibraryOwner,
): boolean {
  return Boolean(
    current
      && current.actorId === expected.actorId
      && current.notebookId === expected.notebookId
      && current.workspaceEpoch === expected.workspaceEpoch
      && current.generation === expected.generation,
  );
}

export function useSourceLibrary({
  actorId,
  canWriteSources,
  effects,
}: UseSourceLibraryOptions) {
  const [sources, setSources] = useState<SourceSummary[]>([]);
  const [sourceScopeSelection, setSourceScopeSelection] = useState<SourceScopeSelection>(
    defaultSourceScopeSelection,
  );
  const [sourcesTotal, setSourcesTotal] = useState(0);
  const [notebookSourceTotal, setNotebookSourceTotal] = useState(0);
  const [sourcesPage, setSourcesPage] = useState(0);
  const [sourcesPageLoading, setSourcesPageLoading] = useState(false);
  const [sourcesCollapsed, setSourcesCollapsedState] = useState(false);
  const [sourceQuery, setSourceQueryState] = useState("");
  const [sourceDetail, setSourceDetail] = useState<SourceSummary | null>(null);
  const [deletingSourceIds, setDeletingSourceIds] = useState<Set<string>>(() => new Set());
  const [reparsingSource, setReparsingSource] = useState(false);
  const [sourceElements, setSourceElements] = useState<SourceElement[]>([]);
  const [sourceElementsTotal, setSourceElementsTotal] = useState(0);
  const [sourceElementStartOffset, setSourceElementStartOffset] = useState(0);
  const [sourceElementsLoading, setSourceElementsLoading] = useState(false);
  const [highlightedElementId, setHighlightedElementId] = useState("");
  const [ownerSerial, setOwnerSerial] = useState(0);

  const effectsRef = useRef(effects);
  effectsRef.current = effects;
  const actorIdRef = useRef(actorId);
  const canWriteRef = useRef(canWriteSources);
  canWriteRef.current = canWriteSources;
  const ownerRef = useRef<SourceLibraryOwner | null>(null);
  const generationRef = useRef(0);
  const sourcesRef = useRef<SourceSummary[]>(sources);
  const sourceDetailRef = useRef<SourceSummary | null>(sourceDetail);
  const sourcesPageRef = useRef(sourcesPage);
  const sourceQueryRef = useRef(sourceQuery);
  const pageRequestRef = useRef(0);
  // Holds the AbortController for whichever `listSources` call is currently
  // in flight, so a superseding request (or an owner/actor transition that
  // abandons the request outright) can cancel the network call for real —
  // `pageRequestRef` alone only ever discarded the stale *response*, it never
  // stopped the request from running to completion server-side.
  const pageAbortRef = useRef<AbortController | null>(null);
  const detailRequestRef = useRef(0);
  const deletingIdsRef = useRef<Set<string>>(new Set());
  const deletingIdsByOwnerRef = useRef<Map<string, Set<string>>>(new Map());
  const deleteRefreshGenerationsRef = useRef<Map<string, number>>(new Map());
  const deletedIdsRef = useRef<Map<string, Set<string>>>(new Map());
  const pollCountRef = useRef(0);
  const previousActorIdRef = useRef(actorId);
  const pendingActorIdRef = useRef<string | null>(null);
  const inactiveScopeSelectionRef = useRef(defaultSourceScopeSelection());
  const inactiveDeletingIdsRef = useRef(new Set<string>());

  // Every site that bumps `pageRequestRef.current` outside of
  // `loadSourcesPage` itself is abandoning whatever source-list request is
  // currently in flight for the owner being replaced — call this alongside
  // each one so that abandonment is a real network cancellation, not just a
  // response that gets silently discarded on arrival.
  function abortInFlightSourcesPage() {
    pageAbortRef.current?.abort();
    pageAbortRef.current = null;
  }

  // Actor changes are an authority boundary, not a later cleanup concern. Invalidate
  // the owner synchronously during render so a continuation that resolves before the
  // normal effect phase cannot commit the previous user's source state.
  if (pendingActorIdRef.current === actorId) pendingActorIdRef.current = null;
  if (previousActorIdRef.current !== actorId) {
    // During authenticated bootstrap, activateActor binds the fetched principal
    // before React publishes currentUser. A render with the still-null prop must not
    // roll that authority back; any non-null conflicting prop remains authoritative.
    if (!(pendingActorIdRef.current && actorId === null)) {
      pendingActorIdRef.current = null;
      previousActorIdRef.current = actorId;
      actorIdRef.current = actorId;
      generationRef.current += 1;
      ownerRef.current = null;
      abortInFlightSourcesPage();
      pageRequestRef.current += 1;
      detailRequestRef.current += 1;
    }
  } else if (!pendingActorIdRef.current) {
    actorIdRef.current = actorId;
  }

  sourcesRef.current = sources;
  sourceDetailRef.current = sourceDetail;
  sourcesPageRef.current = sourcesPage;
  sourceQueryRef.current = sourceQuery;

  const currentOwner = () => (
    ownerRef.current?.actorId === actorIdRef.current ? ownerRef.current : null
  );
  const owns = (owner: SourceLibraryOwner) => (
    actorIdRef.current === owner.actorId && sameOwner(ownerRef.current, owner)
  );
  const currentDeleteKey = (notebookId: string) => {
    const currentActor = actorIdRef.current;
    return currentActor ? ownerKey(currentActor, notebookId) : "";
  };

  function resetVisibleState() {
    setSources([]);
    setSourceScopeSelection(defaultSourceScopeSelection());
    setSourcesTotal(0);
    setNotebookSourceTotal(0);
    setSourcesPage(0);
    setSourceQueryState("");
    setSourceDetail(null);
    setDeletingSourceIds(new Set());
    setSourceElements([]);
    setSourceElementsTotal(0);
    setSourceElementStartOffset(0);
    setSourceElementsLoading(false);
    setHighlightedElementId("");
    setReparsingSource(false);
    setSourcesPageLoading(false);
    pollCountRef.current = 0;
  }

  function beginTransition() {
    generationRef.current += 1;
    ownerRef.current = null;
    abortInFlightSourcesPage();
    pageRequestRef.current += 1;
    detailRequestRef.current += 1;
    setOwnerSerial((value) => value + 1);
    resetVisibleState();
  }

  function activateActor(nextActorId: string) {
    if (
      !nextActorId
      || actorIdRef.current === nextActorId
      || actorIdRef.current !== null
    ) return;
    actorIdRef.current = nextActorId;
    previousActorIdRef.current = nextActorId;
    pendingActorIdRef.current = nextActorId;
    generationRef.current += 1;
    ownerRef.current = null;
    abortInFlightSourcesPage();
    pageRequestRef.current += 1;
    detailRequestRef.current += 1;
  }

  function commitNotebookSnapshot(input: {
    actorId: string;
    notebookId: string;
    workspaceEpoch: number;
    page: PaginatedSources;
  }): SourceLibraryOwner | null {
    if (!input.actorId || actorIdRef.current !== input.actorId) return null;
    const generation = ++generationRef.current;
    const owner: SourceLibraryOwner = Object.freeze({
      actorId: input.actorId,
      notebookId: input.notebookId,
      workspaceEpoch: input.workspaceEpoch,
      generation,
    });
    ownerRef.current = owner;
    abortInFlightSourcesPage();
    pageRequestRef.current += 1;
    detailRequestRef.current += 1;
    const deleted = deletedIdsRef.current.get(ownerKey(input.actorId, input.notebookId));
    const filtered = filterDeletedSourceItems(input.page.items, deleted);
    const visibleTotal = Math.max(0, input.page.total_count - filtered.removedCount);
    setSources(filtered.items);
    setSourceScopeSelection(defaultSourceScopeSelection());
    setSourcesTotal(visibleTotal);
    setNotebookSourceTotal(visibleTotal);
    setSourcesPage(0);
    setSourceQueryState("");
    setSourceDetail(null);
    setDeletingSourceIds(new Set(
      deletingIdsByOwnerRef.current.get(ownerKey(input.actorId, input.notebookId)) ?? [],
    ));
    setSourceElements([]);
    setSourceElementsTotal(0);
    setSourceElementStartOffset(0);
    setSourceElementsLoading(false);
    setHighlightedElementId("");
    setSourcesPageLoading(false);
    pollCountRef.current = 0;
    setOwnerSerial((value) => value + 1);
    return owner;
  }

  function captureOwner(): SourceLibraryOwner | null {
    return currentOwner();
  }

  function currentPageRequest(): { page: number; q: string } {
    return currentOwner()
      ? { page: sourcesPageRef.current, q: sourceQueryRef.current }
      : { page: 0, q: "" };
  }

  function deleteGeneration(notebookId: string): number {
    const key = currentDeleteKey(notebookId);
    return key ? deleteRefreshGenerationsRef.current.get(key) ?? 0 : 0;
  }

  async function loadSourcesPage(input: {
    notebookId?: string;
    page?: number;
    q?: string;
    guard?: () => boolean;
  } = {}) {
    const owner = ownerRef.current;
    const notebookId = input.notebookId ?? owner?.notebookId;
    if (!owner || !notebookId || owner.notebookId !== notebookId) return;
    // Supersede whatever source-list request is still in flight: abort it for
    // real (the browser stops the network call, instead of just having its
    // response discarded on arrival) before starting this one.
    abortInFlightSourcesPage();
    const controller = new AbortController();
    pageAbortRef.current = controller;
    const requestId = ++pageRequestRef.current;
    let pageNum = input.page ?? 0;
    const q = input.q ?? sourceQueryRef.current;
    const isCurrent = () => sourcePageRequestIsCurrent(
      requestId,
      pageRequestRef.current,
      notebookId,
      ownerRef.current?.notebookId ?? null,
      owns(owner) && (!input.guard || input.guard()),
    );
    // `clampSourcePage` below can trigger a second `listSources` call for the
    // same logical page load; both calls share one busy window, set once
    // here and cleared once at every exit path so it never flickers off and
    // back on between the two requests.
    setSourcesPageLoading(true);
    let result: PaginatedSources;
    try {
      result = await listSources(
        notebookId,
        pageNum * SOURCES_PAGE_SIZE,
        SOURCES_PAGE_SIZE,
        q,
        controller.signal,
      );
    } catch (error) {
      // Invariant: the only way this request's fetch can reject with an
      // AbortError is via `pageAbortRef.current?.abort()` — called only from
      // this same function's next invocation, or from an owner/actor
      // transition (beginTransition / activateActor / commitNotebookSnapshot
      // / the render-time actor-change branch). Every one of those sites also
      // bumps `pageRequestRef.current` (here) or invalidates `ownerRef`
      // (there) before or as it aborts, so `isCurrent()` is always already
      // false by the time an abort-triggered rejection reaches here. That
      // means the `throw` branch below can only ever fire for a genuine
      // request failure, never for our own cancellation — no separate
      // AbortError check is needed to keep it from surfacing as an error.
      if (isCurrent()) {
        setSourcesPageLoading(false);
        pageAbortRef.current = null;
        throw error;
      }
      return;
    }
    if (!isCurrent()) return;
    const clamped = clampSourcePage(pageNum, result.total_count, SOURCES_PAGE_SIZE);
    if (clamped !== pageNum) {
      pageNum = clamped;
      try {
        result = await listSources(
          notebookId,
          pageNum * SOURCES_PAGE_SIZE,
          SOURCES_PAGE_SIZE,
          q,
          controller.signal,
        );
      } catch (error) {
        // Same invariant as above applies to this second, clamp-triggered call.
        if (isCurrent()) {
          setSourcesPageLoading(false);
          pageAbortRef.current = null;
          throw error;
        }
        return;
      }
      if (!isCurrent()) return;
    }
    const deleted = deletedIdsRef.current.get(ownerKey(owner.actorId, notebookId));
    const filtered = filterDeletedSourceItems(result.items, deleted);
    const visibleTotal = Math.max(0, result.total_count - filtered.removedCount);
    setSources(filtered.items);
    setSourcesTotal(visibleTotal);
    if (!q) setNotebookSourceTotal(visibleTotal);
    setSourcesPage(pageNum);
    setSourcesPageLoading(false);
    pageAbortRef.current = null;
  }

  function setSourceQuery(value: string) {
    setSourceQueryState(value);
  }

  function setSourcesCollapsed(value: boolean) {
    setSourcesCollapsedState(value);
  }

  function selectAllSources() {
    setSourceScopeSelection(defaultSourceScopeSelection());
  }

  function clearSourceSelection() {
    setSourceScopeSelection({ allSelected: false, ids: new Set() });
  }

  function toggleSource(sourceId: string) {
    setSourceScopeSelection((previous) => toggleSourceSelection(previous, sourceId));
  }

  function commitUploadedSources(
    owner: SourceLibraryOwner | null,
    uploaded: readonly SourceSummary[],
    addedCount: number,
  ): boolean {
    if (!owner || !owns(owner)) return false;
    setSources((previous) => [
      ...previous.filter((source) => !uploaded.some((item) => item.id === source.id)),
      ...uploaded,
    ]);
    setSourcesTotal((total) => total + addedCount);
    setNotebookSourceTotal((total) => total + addedCount);
    return true;
  }

  function commitUrlSources(
    owner: SourceLibraryOwner | null,
    created: readonly SourceSummary[],
  ): boolean {
    if (!owner || !owns(owner)) return false;
    setSources((previous) => [
      ...previous.filter((source) => !created.some((item) => item.id === source.id)),
      ...created,
    ]);
    setNotebookSourceTotal((total) => total + created.length);
    return true;
  }

  async function openSourceById(sourceId: string, elementId = "") {
    const owner = ownerRef.current;
    if (!owner) return false;
    const requestGeneration = ++detailRequestRef.current;
    setSourceElementsLoading(false);
    let detail: SourceSummary;
    let elementPage: PaginatedSourceElements;
    try {
      [detail, elementPage] = await Promise.all([
        getNotebookSource(owner.notebookId, sourceId),
        getNotebookSourceElementsPage(
          owner.notebookId,
          sourceId,
          0,
          SOURCE_ELEMENT_PAGE_SIZE,
          elementId,
        ),
      ]);
    } catch (error) {
      if (owns(owner)) throw error;
      return false;
    }
    const deleted = deletedIdsRef.current.get(ownerKey(owner.actorId, detail.notebook_id));
    if (
      detailRequestRef.current !== requestGeneration
      || !owns(owner)
      || deleted?.has(sourceId)
    ) return false;
    setSourceDetail(detail);
    setSourceElements(elementPage.items);
    setSourceElementsTotal(elementPage.total_count);
    setSourceElementStartOffset(elementPage.offset);
    setSourceElementsLoading(false);
    setHighlightedElementId(elementId);
    return true;
  }

  function closeSourceDetail() {
    detailRequestRef.current += 1;
    setSourceDetail(null);
    setSourceElements([]);
    setSourceElementsTotal(0);
    setSourceElementStartOffset(0);
    setSourceElementsLoading(false);
    setHighlightedElementId("");
  }

  async function loadSourceElementPage(direction: "previous" | "next") {
    const detail = sourceDetailRef.current;
    const owner = ownerRef.current;
    if (!detail || !owner || sourceElementsLoading) return;
    const offset = direction === "previous"
      ? Math.max(0, sourceElementStartOffset - SOURCE_ELEMENT_PAGE_SIZE)
      : sourceElementStartOffset + sourceElements.length;
    const requestGeneration = detailRequestRef.current;
    setSourceElementsLoading(true);
    try {
      const page = await getNotebookSourceElementsPage(
        owner.notebookId,
        detail.id,
        offset,
        SOURCE_ELEMENT_PAGE_SIZE,
      );
      if (
        detailRequestRef.current !== requestGeneration
        || sourceDetailRef.current?.id !== detail.id
        || !owns(owner)
      ) return;
      setSourceElements((current) => direction === "previous"
        ? [...page.items, ...current]
        : [...current, ...page.items]);
      if (direction === "previous") setSourceElementStartOffset(page.offset);
      setSourceElementsTotal(page.total_count);
    } finally {
      if (
        detailRequestRef.current === requestGeneration
        && sourceDetailRef.current?.id === detail.id
      ) setSourceElementsLoading(false);
    }
  }

  async function reparseSource() {
    const detail = sourceDetailRef.current;
    const owner = ownerRef.current;
    if (!detail || !owner || reparsingSource || !canWriteRef.current) return;
    if (crossLibrarySourceNotebookId(detail.notebook_id, owner.notebookId)) return;
    setReparsingSource(true);
    try {
      const updated = await parseSource(detail.id);
      if (!owns(owner)) return;
      setSources((previous) => previous.map((source) => (
        source.id === updated.id ? updated : source
      )));
      await openSourceById(updated.id);
      if (!owns(owner)) return;
      const guard = () => owns(owner);
      await effectsRef.current.refreshCollection(guard);
      if (!owns(owner)) return;
      await effectsRef.current.refreshNotebook(owner.notebookId, guard);
      if (owns(owner)) effectsRef.current.setToast("Source 已重新解析");
    } catch (error) {
      if (owns(owner)) effectsRef.current.reportError(error);
    } finally {
      if (owns(owner)) setReparsingSource(false);
    }
  }

  async function deleteSource(source: SourceSummary) {
    const ownerAtStart = currentOwner();
    if (
      !ownerAtStart
      || !owns(ownerAtStart)
      || source.notebook_id !== ownerAtStart.notebookId
      || deletingIdsRef.current.has(source.id)
      || !canWriteRef.current
    ) return;
    const actorAtStart = ownerAtStart.actorId;
    const pendingKey = ownerKey(ownerAtStart.actorId, ownerAtStart.notebookId);
    deletingIdsRef.current.add(source.id);
    const pendingForOwner = deletingIdsByOwnerRef.current.get(pendingKey) ?? new Set<string>();
    pendingForOwner.add(source.id);
    deletingIdsByOwnerRef.current.set(pendingKey, pendingForOwner);
    setDeletingSourceIds((previous) => new Set(previous).add(source.id));
    try {
      await deleteSourceRequest(source.id);
      const key = ownerKey(actorAtStart, source.notebook_id);
      const deleted = deletedIdsRef.current.get(key) ?? new Set<string>();
      deleted.add(source.id);
      deletedIdsRef.current.set(key, deleted);
      if (sourceDetailRef.current?.id === source.id) closeSourceDetail();
      const generation = (deleteRefreshGenerationsRef.current.get(key) ?? 0) + 1;
      deleteRefreshGenerationsRef.current.set(key, generation);
      const activeOwner = ownerRef.current;
      const refreshOwner = activeOwner && activeOwner.actorId === actorAtStart
        ? claimSourceDeleteRefresh(
          source.notebook_id,
          activeOwner.notebookId,
          activeOwner.workspaceEpoch,
          generation,
        )
        : null;
      if (!refreshOwner || !activeOwner) return;
      const owner = activeOwner;
      const isCurrent = () => {
        const current = ownerRef.current;
        return owns(owner) && ownsSourceDeleteRefresh(
          refreshOwner,
          current?.notebookId ?? null,
          current?.workspaceEpoch ?? -1,
          deleteRefreshGenerationsRef.current.get(key) ?? 0,
        );
      };
      const wasVisible = sourcesRef.current.some((item) => item.id === source.id);
      setSources((previous) => previous.filter((item) => item.id !== source.id));
      setSourceScopeSelection((previous) => removeSourceFromSelection(previous, source.id));
      if (wasVisible) setSourcesTotal((total) => Math.max(0, total - 1));
      setNotebookSourceTotal((total) => Math.max(0, total - 1));
      effectsRef.current.invalidateKnowledge();
      effectsRef.current.setToast("来源已删除");
      void Promise.allSettled([
        loadSourcesPage({
          notebookId: source.notebook_id,
          page: sourcesPageRef.current,
          q: sourceQueryRef.current,
          guard: isCurrent,
        }),
        effectsRef.current.refreshCollection(isCurrent),
        effectsRef.current.refreshNotebook(source.notebook_id, isCurrent),
        effectsRef.current.refreshCheckup(source.notebook_id, isCurrent),
      ]);
    } catch (error) {
      const current = ownerRef.current;
      if (current?.actorId === actorAtStart && current.notebookId === source.notebook_id) {
        effectsRef.current.reportError(error);
      }
    } finally {
      deletingIdsRef.current.delete(source.id);
      const pending = deletingIdsByOwnerRef.current.get(pendingKey);
      pending?.delete(source.id);
      if (pending && pending.size === 0) deletingIdsByOwnerRef.current.delete(pendingKey);
      const activeOwner = currentOwner();
      if (activeOwner && ownerKey(activeOwner.actorId, activeOwner.notebookId) === pendingKey) {
        setDeletingSourceIds(new Set(pending ?? []));
      }
    }
  }

  useEffect(() => {
    if (!sourceDetail || !highlightedElementId) return;
    const scrollTimer = window.setTimeout(() => {
      document.getElementById(sourceElementDomId(highlightedElementId))
        ?.scrollIntoView({ block: "center" });
    }, 80);
    const clearTimer = window.setTimeout(() => setHighlightedElementId(""), 2600);
    return () => {
      window.clearTimeout(scrollTimer);
      window.clearTimeout(clearTimer);
    };
  }, [highlightedElementId, sourceDetail, sourceElements]);

  const ownerIsActive = currentOwner() !== null;
  const visibleSources = ownerIsActive ? sources : NO_SOURCES;
  const hasPending = visibleSources.some(
    (source) => !["extracted", "failed"].includes(source.parse_status),
  );

  useEffect(() => {
    const owner = ownerRef.current;
    if (!owner || !hasPending) {
      pollCountRef.current = 0;
      return;
    }
    let cancelled = false;
    let timer: number | undefined;
    let delay = SOURCE_POLL_INITIAL_MS;
    const first = sourcesRef.current.filter(
      (source) => !["extracted", "failed"].includes(source.parse_status),
    );
    if (first.length) {
      effectsRef.current.setStatusText(
        `正在处理来源（已 ${Math.round((pollCountRef.current * SOURCE_POLL_INITIAL_MS) / 1000)}s · ${first.length} 个）`,
      );
    }
    const tick = async () => {
      if (cancelled || !owns(owner)) return;
      const pending = sourcesRef.current.filter(
        (source) => !["extracted", "failed"].includes(source.parse_status),
      );
      if (pending.length === 0) {
        pollCountRef.current = 0;
        return;
      }
      if (pollCountRef.current > SOURCE_POLL_MAX_ATTEMPTS) {
        effectsRef.current.setStatusText("处理超时：部分来源长时间未完成，请稍后重试");
        return;
      }
      pollCountRef.current += 1;
      const elapsedSec = Math.round(
        (pollCountRef.current * SOURCE_POLL_INITIAL_MS) / 1000,
      );
      effectsRef.current.setStatusText(
        `正在处理来源（已 ${elapsedSec}s · ${pending.length} 个）`,
      );
      try {
        const updated = await Promise.all(pending.map((source) => getSource(source.id)));
        if (cancelled || !owns(owner)) return;
        const reachedExtracted = updated.some((item) => {
          const previous = pending.find((source) => source.id === item.id);
          return previous
            && previous.parse_status !== "extracted"
            && item.parse_status === "extracted";
        });
        const justFailed = updated.find((item) => {
          const previous = pending.find((source) => source.id === item.id);
          return previous
            && previous.parse_status !== "failed"
            && item.parse_status === "failed";
        });
        let changed = false;
        setSources((previous) => {
          const next = previous.map((source) => {
            const item = updated.find((candidate) => candidate.id === source.id);
            if (item && item.parse_status !== source.parse_status) changed = true;
            return item ?? source;
          });
          return changed ? next : previous;
        });
        if (justFailed) {
          const failureHint = justFailed.error_message
            ? toUserMessage(new Error(justFailed.error_message), "")
            : "";
          effectsRef.current.setStatusText(
            `来源处理失败：${justFailed.file_name || justFailed.title}${failureHint ? ` — ${failureHint}` : ""}`,
          );
        }
        if (reachedExtracted) {
          const guard = () => owns(owner);
          await effectsRef.current.refreshCollection(guard);
          await effectsRef.current.refreshNotebook(owner.notebookId, guard);
          if (owns(owner)) {
            try {
              void Promise.resolve(
                effectsRef.current.refreshCheckup(owner.notebookId, guard),
              ).catch(() => undefined);
            } catch {
              // Checkup is a fail-open derived refresh and must not delay polling.
            }
          }
        }
      } catch (error) {
        if (owns(owner)) effectsRef.current.reportError(error);
      }
      if (!cancelled && owns(owner)) {
        delay = Math.min(Math.round(delay * 1.5), SOURCE_POLL_MAX_MS);
        timer = window.setTimeout(tick, delay);
      }
    };
    timer = window.setTimeout(tick, delay);
    return () => {
      cancelled = true;
      if (timer !== undefined) window.clearTimeout(timer);
    };
  }, [hasPending, ownerSerial]);

  return {
    sources: visibleSources,
    sourceScopeSelection: ownerIsActive
      ? sourceScopeSelection
      : inactiveScopeSelectionRef.current,
    sourcesTotal: ownerIsActive ? sourcesTotal : 0,
    notebookSourceTotal: ownerIsActive ? notebookSourceTotal : 0,
    sourcesPage: ownerIsActive ? sourcesPage : 0,
    sourcesPageLoading: ownerIsActive ? sourcesPageLoading : false,
    sourcesCollapsed,
    sourceQuery: ownerIsActive ? sourceQuery : "",
    sourceDetail: ownerIsActive ? sourceDetail : null,
    deletingSourceIds: ownerIsActive ? deletingSourceIds : inactiveDeletingIdsRef.current,
    reparsingSource: ownerIsActive ? reparsingSource : false,
    sourceElements: ownerIsActive ? sourceElements : NO_SOURCE_ELEMENTS,
    sourceElementsTotal: ownerIsActive ? sourceElementsTotal : 0,
    sourceElementStartOffset: ownerIsActive ? sourceElementStartOffset : 0,
    sourceElementsLoading: ownerIsActive ? sourceElementsLoading : false,
    highlightedElementId: ownerIsActive ? highlightedElementId : "",
    hasPending,
    activateActor,
    beginTransition,
    commitNotebookSnapshot,
    captureOwner,
    currentPageRequest,
    deleteGeneration,
    loadSourcesPage,
    setSourceQuery,
    setSourcesCollapsed,
    selectAllSources,
    clearSourceSelection,
    toggleSource,
    commitUploadedSources,
    commitUrlSources,
    openSourceById,
    closeSourceDetail,
    loadSourceElementPage,
    reparseSource,
    deleteSource,
  } as const;
}
