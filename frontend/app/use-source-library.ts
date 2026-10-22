"use client";

import { useCallback, useEffect, useMemo, useRef, useState } from "react";
import { httpErrorStatus, toUserMessage } from "./errors.ts";
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
import { PROMOTION_SOURCE_TYPE, sourceIsPending } from "./source-management.ts";
import { pageDocumentCount } from "./document-limit.ts";
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
/**
 * One followed source: the status last read, the `updated_at` read just before
 * the reparse was submitted (null when that read failed), and whether this run
 * has been seen to start (a non-terminal status read since the submission).
 */
type FollowedSource = { seen: string; baseline: string | null; started: boolean };
const NO_TRACKED_SOURCES: ReadonlyMap<string, FollowedSource> = new Map();
const NO_SOURCE_IDS: ReadonlySet<string> = new Set();
/** What a submitted reparse came to: queued, nothing to do (gone / excluded), or the request failed. */
export type SourceReparseOutcome = "started" | "none" | "failed";

/**
 * One read of a followed source -> its next follow state, or null once THIS
 * run is over. The reparse endpoint only hands `process_source` to the KG job
 * pool and returns without touching the source's status, so while the pool is
 * busy the first reads after the submission can still return the previous
 * run's `extracted` / `failed`; that stale terminal state is not this run
 * ending. Over: the source is gone (404); or a terminal read after this run was
 * seen to start; or a terminal read whose `updated_at` moved past the
 * pre-submission baseline (the whole run fell between two reads). Without a
 * baseline only "seen to start" counts; the poll's attempt cap bounds the rest.
 */
function followedSourceStep(entry: FollowedSource, item: SourceSummary | null): FollowedSource | null {
  if (!item) return null;
  if (sourceIsPending(item)) return { ...entry, seen: item.parse_status, started: true };
  if (entry.started) return null;
  if (entry.baseline !== null && item.updated_at && item.updated_at !== entry.baseline) return null;
  return { ...entry, seen: item.parse_status };
}

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
  // 文档数量上限的计数（不含收录来源）；与 notebookSourceTotal 同步维护。
  const [notebookDocumentCount, setNotebookDocumentCount] = useState(0);
  const [sourcesPage, setSourcesPage] = useState(0);
  const [sourcesPageLoading, setSourcesPageLoading] = useState(false);
  const [sourcesCollapsed, setSourcesCollapsedState] = useState(false);
  const [sourceQuery, setSourceQueryState] = useState("");
  const [sourceDetail, setSourceDetail] = useState<SourceSummary | null>(null);
  const [deletingSourceIds, setDeletingSourceIds] = useState<Set<string>>(() => new Set());
  // The source detail's own (synchronous) reparse: the source it is for, null when none is in flight.
  const [reparsingSourceId, setReparsingSourceId] = useState<string | null>(null);
  const [sourceElements, setSourceElements] = useState<SourceElement[]>([]);
  const [sourceElementsTotal, setSourceElementsTotal] = useState(0);
  const [sourceElementStartOffset, setSourceElementStartOffset] = useState(0);
  const [sourceElementsLoading, setSourceElementsLoading] = useState(false);
  const [highlightedElementId, setHighlightedElementId] = useState("");
  const [ownerSerial, setOwnerSerial] = useState(0);
  // Content consumers revalidate after mutations, independently of list search
  // and pagination. This is a browser refresh token, not a server content hash.
  const [contentRevision, setContentRevision] = useState(0);
  // Sources asked to be followed to a terminal parse state even when they are
  // not on the current page (id -> last seen parse_status; "" before the first
  // read). The page-scoped poll below only ever sees `sources`, so a reparse
  // started from elsewhere (the KG analysis findings) would otherwise never
  // move `contentRevision` and nothing downstream would revalidate.
  const [trackedSources, setTrackedSources] = useState<ReadonlyMap<string, FollowedSource>>(NO_TRACKED_SOURCES);
  // Bumped whenever new ids are followed: part of the poll effect's deps, so new
  // follow work restarts a poller that went idle (attempt cap reached, or a round
  // that found nothing) even when `hasPending` itself did not flip.
  const [trackSerial, setTrackSerial] = useState(0);
  // Followed reparses (`submitFollowedReparse`) whose baseline read or request has
  // not returned yet; on "started" the follow set takes each one over.
  const [submittingSourceIds, setSubmittingSourceIds] = useState<ReadonlySet<string>>(NO_SOURCE_IDS);

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
  const committedListWindowRef = useRef<{ page: number; q: string } | null>(null);
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
  const trackedSourcesRef = useRef<ReadonlyMap<string, FollowedSource>>(trackedSources);
  trackedSourcesRef.current = trackedSources;
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
    committedListWindowRef.current = null;
    setSources([]);
    setSourceScopeSelection(defaultSourceScopeSelection());
    setSourcesTotal(0);
    setNotebookSourceTotal(0);
    setNotebookDocumentCount(0);
    setSourcesPage(0);
    setSourceQueryState("");
    setSourceDetail(null);
    setDeletingSourceIds(new Set());
    setSourceElements([]);
    setSourceElementsTotal(0);
    setSourceElementStartOffset(0);
    setSourceElementsLoading(false);
    setHighlightedElementId("");
    setReparsingSourceId(null);
    setSourcesPageLoading(false);
    setTrackedSources(NO_TRACKED_SOURCES);
    setSubmittingSourceIds(NO_SOURCE_IDS);
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
    committedListWindowRef.current = { page: 0, q: "" };
    setContentRevision((value) => value + 1);
    setSources(filtered.items);
    setSourceScopeSelection(defaultSourceScopeSelection());
    setSourcesTotal(visibleTotal);
    setNotebookSourceTotal(visibleTotal);
    setNotebookDocumentCount(pageDocumentCount(
      input.page, new Set(filtered.items.map((item) => item.id)),
    ));
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
    setTrackedSources(NO_TRACKED_SOURCES);
    setSubmittingSourceIds(NO_SOURCE_IDS);
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
    const q = (input.q ?? sourceQueryRef.current).trim();
    const isCurrent = () => sourcePageRequestIsCurrent(
      requestId,
      pageRequestRef.current,
      notebookId,
      ownerRef.current?.notebookId ?? null,
      owns(owner) && (!input.guard || input.guard()),
    );
    // `isCurrent()` going false at any exit point below can mean two very
    // different things, and only one of them is this call's job to clean up:
    //   - Superseded: a later `loadSourcesPage` invocation (or an
    //     owner/actor transition — beginTransition / activateActor /
    //     commitNotebookSnapshot / the render-time actor-change branch) has
    //     already bumped `pageRequestRef.current` and taken over
    //     `pageAbortRef`. That successor owns the busy window now and will
    //     clear it itself (or the transition's own resetVisibleState /
    //     commitNotebookSnapshot already did) — this call must stay silent
    //     and must not touch `pageAbortRef`, which no longer points at its
    //     own controller.
    //   - Rejected, not superseded: the owner/notebook is unchanged and
    //     `requestId` still equals `pageRequestRef.current` (no successor
    //     ran), but the caller-supplied `guard()` vetoed the result. Nobody
    //     else is coming to release busy for this request — page.tsx's
    //     poll-completion call sites hit exactly this: their own effect's
    //     cleanup flips `cancelled` (and their `guard` reads it) before this
    //     response lands, with no follow-up `loadSourcesPage` call to take
    //     over. This call must clear busy itself here or it leaks `true`
    //     forever (spinner stuck, Pagination controls stuck disabled).
    // `requestId === pageRequestRef.current` is exactly the "no successor
    // exists" test, so it's what gates every release below — never the
    // stale/inverted `isCurrent()` check that conflated the two cases.
    const releaseBusyIfWindowHolder = () => {
      if (requestId === pageRequestRef.current) {
        setSourcesPageLoading(false);
        pageAbortRef.current = null;
      }
    };
    // `clampSourcePage` below can trigger a second `listSources` call for the
    // same logical page load; both calls share one busy window, set once
    // here and released via `releaseBusyIfWindowHolder()` at every exit path
    // so it never flickers off and back on between the two requests.
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
      // A genuine cancellation (this request's own controller aborted by one
      // of the sites named above) must never surface as a reportable error.
      // Invariant: every abort site bumps `pageRequestRef.current` (here) or
      // invalidates `ownerRef` (there) before/while aborting, so `isCurrent()`
      // is already false by the time an abort-triggered rejection reaches
      // here — the `throw` branch below can only fire for a genuine request
      // failure. This explicit shape check is a second, independent line of
      // defense: if a future abort site ever forgot to keep that invariant
      // intact, the cancellation would still be swallowed here instead of
      // reaching a caller as an error.
      // `controller.signal.aborted` is checked first and wins regardless of
      // the rejection's shape: undici raises a plain `TypeError: terminated`
      // (not a DOMException) when the request is aborted mid body-stream-read,
      // and the unmount cleanup effect below is exactly a site that aborts
      // without bumping `pageRequestRef` or invalidating `ownerRef` — relying
      // on the DOMException shape alone there would let that abort mis-fire
      // the `throw` branch instead of being swallowed here.
      if (
        controller.signal.aborted
        || (error instanceof DOMException && error.name === "AbortError")
      ) {
        releaseBusyIfWindowHolder();
        return;
      }
      if (isCurrent()) {
        releaseBusyIfWindowHolder();
        throw error;
      }
      releaseBusyIfWindowHolder();
      return;
    }
    if (!isCurrent()) {
      releaseBusyIfWindowHolder();
      return;
    }
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
        // Same invariant, `controller.signal.aborted` precedence, and
        // defense-in-depth AbortError check as above, applied to this
        // second, clamp-triggered call.
        if (
          controller.signal.aborted
          || (error instanceof DOMException && error.name === "AbortError")
        ) {
          releaseBusyIfWindowHolder();
          return;
        }
        if (isCurrent()) {
          releaseBusyIfWindowHolder();
          throw error;
        }
        releaseBusyIfWindowHolder();
        return;
      }
      if (!isCurrent()) {
        releaseBusyIfWindowHolder();
        return;
      }
    }
    const deleted = deletedIdsRef.current.get(ownerKey(owner.actorId, notebookId));
    const filtered = filterDeletedSourceItems(result.items, deleted);
    const visibleTotal = Math.max(0, result.total_count - filtered.removedCount);
    const previousById = new Map(sourcesRef.current.map((source) => [source.id, source]));
    const previousWindow = committedListWindowRef.current;
    const membershipChanged = previousWindow?.page === pageNum && previousWindow.q === q
      && (previousById.size !== filtered.items.length || filtered.items.some((source) => !previousById.has(source.id)));
    if (membershipChanged || filtered.items.some((source) => {
      const previous = previousById.get(source.id);
      return previous && (
        previous.title !== source.title || previous.summary !== source.summary
        || previous.parse_status !== source.parse_status || previous.element_count !== source.element_count
      );
    })) setContentRevision((value) => value + 1);
    committedListWindowRef.current = { page: pageNum, q };
    setSources(filtered.items);
    setSourcesTotal(visibleTotal);
    if (!q) {
      setNotebookSourceTotal(visibleTotal);
      setNotebookDocumentCount(pageDocumentCount(
        result, new Set(filtered.items.map((item) => item.id)),
      ));
    }
    setSourcesPage(pageNum);
    setSourcesPageLoading(false);
    pageAbortRef.current = null;
  }

  function setSourceQuery(value: string) {
    sourceQueryRef.current = value;
    setSourceQueryState(value);
  }

  async function searchSources() {
    const q = sourceQueryRef.current.trim();
    setSourceQuery(q);
    await loadSourcesPage({ page: 0, q });
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

  /**
   * Replace the whole source selection at once (「就此追问」 restores the scope a
   * report was generated under). Copied, so the caller's set can never alias
   * the selection this hook owns.
   */
  function applySourceScopeSelection(selection: SourceScopeSelection) {
    setSourceScopeSelection({ allSelected: selection.allSelected, ids: new Set(selection.ids) });
  }

  function commitUploadedSources(
    owner: SourceLibraryOwner | null,
    uploaded: readonly SourceSummary[],
    addedCount: number,
  ): boolean {
    if (!owner || !owns(owner)) return false;
    setContentRevision((value) => value + 1);
    setSources((previous) => [
      ...previous.filter((source) => !uploaded.some((item) => item.id === source.id)),
      ...uploaded,
    ]);
    setSourcesTotal((total) => total + addedCount);
    setNotebookSourceTotal((total) => total + addedCount);
    setNotebookDocumentCount((count) => count + addedCount);
    return true;
  }

  function commitUrlSources(
    owner: SourceLibraryOwner | null,
    created: readonly SourceSummary[],
  ): boolean {
    if (!owner || !owns(owner)) return false;
    setContentRevision((value) => value + 1);
    setSources((previous) => [
      ...previous.filter((source) => !created.some((item) => item.id === source.id)),
      ...created,
    ]);
    setNotebookSourceTotal((total) => total + created.length);
    setNotebookDocumentCount((count) => count + created.length);
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
    if (!detail || !owner || reparsingSourceId !== null || !canWriteRef.current) return;
    if (crossLibrarySourceNotebookId(detail.notebook_id, owner.notebookId)) return;
    // A reparse of this source submitted elsewhere (the KG analysis findings) has not
    // finished yet: submitting it again would run the same expensive parse twice.
    if (reparsingSourceIdsRef.current.has(detail.id)) return;
    setReparsingSourceId(detail.id);
    try {
      const updated = await parseSource(detail.id);
      if (!owns(owner)) return;
      setContentRevision((value) => value + 1);
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
      if (owns(owner)) setReparsingSourceId(null);
    }
  }

  // 返回值 = 这次点击有没有真的把来源删掉(DELETE 成功)。调用方用它把结果写在按钮旁
  // (图谱分析的问题清单);失败的原因仍由 reportError 报出,这里不重复。
  async function deleteSource(source: Pick<SourceSummary, "id" | "notebook_id" | "type">): Promise<boolean> {
    const ownerAtStart = currentOwner();
    if (
      !ownerAtStart
      || !owns(ownerAtStart)
      || source.notebook_id !== ownerAtStart.notebookId
      || deletingIdsRef.current.has(source.id)
      || !canWriteRef.current
    ) return false;
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
      if (!refreshOwner || !activeOwner) return true;
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
      setContentRevision((value) => value + 1);
      setSources((previous) => previous.filter((item) => item.id !== source.id));
      setSourceScopeSelection((previous) => removeSourceFromSelection(previous, source.id));
      if (wasVisible) setSourcesTotal((total) => Math.max(0, total - 1));
      setNotebookSourceTotal((total) => Math.max(0, total - 1));
      // 收录来源不占文档名额，删掉它不改变上限计数
      if (source.type !== PROMOTION_SOURCE_TYPE) {
        setNotebookDocumentCount((count) => Math.max(0, count - 1));
      }
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
      return true;
    } catch (error) {
      const current = ownerRef.current;
      if (current?.actorId === actorAtStart && current.notebookId === source.notebook_id) {
        effectsRef.current.reportError(error);
      }
      return false;
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
    (source) => sourceIsPending(source),
  ) || (ownerIsActive && trackedSources.size > 0);
  // The one reparse placeholder every reparse entry reads: a source holds it from the
  // moment a followed reparse is submitted until the follow judges THIS run over (or
  // drops it: the source is gone, the attempt budget ran out, the owner changed,
  // `untrackSources`), and while the source detail's own reparse is in flight.
  const reparsingSourceIds = useMemo<ReadonlySet<string>>(() => {
    if (!ownerIsActive) return NO_SOURCE_IDS;
    if (trackedSources.size === 0 && submittingSourceIds.size === 0 && reparsingSourceId === null) {
      return NO_SOURCE_IDS;
    }
    const ids = new Set<string>([...trackedSources.keys(), ...submittingSourceIds]);
    if (reparsingSourceId !== null) ids.add(reparsingSourceId);
    return ids;
  }, [ownerIsActive, trackedSources, submittingSourceIds, reparsingSourceId]);
  const reparsingSourceIdsRef = useRef(reparsingSourceIds);
  reparsingSourceIdsRef.current = reparsingSourceIds;

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
      (source) => sourceIsPending(source),
    );
    if (first.length) {
      effectsRef.current.setStatusText(
        `正在处理来源（已 ${Math.round((pollCountRef.current * SOURCE_POLL_INITIAL_MS) / 1000)}s · ${first.length} 个）`,
      );
    }
    const tick = async () => {
      if (cancelled || !owns(owner)) return;
      const pending = sourcesRef.current.filter(
        (source) => sourceIsPending(source),
      );
      const pendingIds = new Set(pending.map((source) => source.id));
      // Followed sources still pending on the current page are read with the page
      // below; every other followed source (off the page, or on it with a stale
      // terminal status) is read here. Both results settle the follow set.
      const tracked = [...trackedSourcesRef.current].filter(([id]) => !pendingIds.has(id));
      if (pending.length === 0 && tracked.length === 0) {
        pollCountRef.current = 0;
        return;
      }
      if (pollCountRef.current > SOURCE_POLL_MAX_ATTEMPTS) {
        effectsRef.current.setStatusText("处理超时：部分来源长时间未完成，请稍后重试");
        // The follows end with the attempt budget, and their reparse placeholders with them.
        if (trackedSourcesRef.current.size > 0) setTrackedSources(NO_TRACKED_SOURCES);
        return;
      }
      pollCountRef.current += 1;
      const elapsedSec = Math.round(
        (pollCountRef.current * SOURCE_POLL_INITIAL_MS) / 1000,
      );
      if (pending.length > 0) {
        effectsRef.current.setStatusText(
          `正在处理来源（已 ${elapsedSec}s · ${pending.length} 个）`,
        );
      }
      try {
        const [updated, trackedUpdated] = await Promise.all([
          Promise.all(pending.map((source) => getSource(source.id))),
          // A followed source deleted meanwhile is done being followed.
          Promise.all(tracked.map(([id]) => getSource(id).then(
            (item): SourceSummary | null => item,
            (error: unknown) => {
              if (httpErrorStatus(error) === 404) return null;
              throw error;
            },
          ))),
        ]);
        if (cancelled || !owns(owner)) return;
        // Every followed source read this round (off the page, or with the page)
        // steps its follow state; null = this run is over.
        const steps = new Map<string, FollowedSource | null>();
        tracked.forEach(([id, entry], index) => steps.set(id, followedSourceStep(entry, trackedUpdated[index])));
        for (const item of updated) {
          const entry = trackedSourcesRef.current.get(item.id);
          // Read with the page means its page row was pending: a run is underway.
          if (entry) steps.set(item.id, followedSourceStep({ ...entry, started: true }, item));
        }
        // Off-page followed sources move contentRevision when this run starts,
        // advances or ends -- not on a stale terminal read before it started.
        const trackedChanged = tracked.some(([id, entry], index) => {
          const item = trackedUpdated[index];
          return steps.get(id) === null
            || (item !== null && item.parse_status !== entry.seen && (entry.seen !== "" || sourceIsPending(item)));
        });
        const trackedReachedExtracted = tracked.some(([id], index) => (
          steps.get(id) === null && trackedUpdated[index]?.parse_status === "extracted"
        ));
        if (steps.size > 0) {
          setTrackedSources((previous) => {
            let moved = false;
            const next = new Map(previous);
            for (const [id, step] of steps) {
              const current = next.get(id);
              if (!current) continue;
              if (step === null) {
                next.delete(id);
                moved = true;
              } else if (step.seen !== current.seen || step.started !== current.started) {
                next.set(id, step);
                moved = true;
              }
            }
            if (!moved) return previous;
            return next.size === 0 ? NO_TRACKED_SOURCES : next;
          });
        }
        const reachedExtracted = trackedReachedExtracted || updated.some((item) => {
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
        if (trackedChanged || updated.some((item) => pending.find((source) => source.id === item.id)?.parse_status !== item.parse_status)) {
          setContentRevision((value) => value + 1);
        }
        let changed = false;
        setSources((previous) => {
          const next = previous.map((source) => {
            const item = updated.find((candidate) => candidate.id === source.id)
              ?? trackedUpdated.find((candidate) => candidate?.id === source.id)
              ?? undefined;
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
        if (reachedExtracted || justFailed) {
          const guard = () => owns(owner);
          await effectsRef.current.refreshCollection(guard);
          await effectsRef.current.refreshNotebook(owner.notebookId, guard);
          if (reachedExtracted && owns(owner)) {
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
  }, [hasPending, ownerSerial, trackSerial]);

  // Follow these sources until the reparse just submitted for each is over,
  // even off the current page (each state change moves `contentRevision`).
  // `baselines` carries each source's `updated_at` read just before the
  // submission (see followedSourceStep). The shared poll's attempt cap bounds
  // it (and then drops the follow), and an owner change or `untrackSources`
  // stops it. While followed, a source holds the reparse placeholder.
  const trackSourceProgress = useCallback((
    sourceIds: readonly string[],
    baselines: Readonly<Record<string, string | null | undefined>> = {},
  ) => {
    if (currentOwner() === null) return;
    const added = sourceIds.filter((id) => !trackedSourcesRef.current.has(id));
    if (added.length === 0) return;
    setTrackedSources((previous) => {
      const next = new Map(previous);
      for (const id of added) {
        if (!next.has(id)) next.set(id, { seen: "", baseline: baselines[id] || null, started: false });
      }
      return next;
    });
    // New work: a fresh attempt budget, and wake the poller even if it is idle.
    pollCountRef.current = 0;
    setTrackSerial((value) => value + 1);
  }, []);
  const untrackSources = useCallback(() => setTrackedSources(NO_TRACKED_SOURCES), []);
  // The baseline a follow compares against: `updated_at` read just before the
  // reparse is submitted (null when that read fails -- the follow then only
  // trusts "seen to start"). `submitFollowedReparse` reads it; source reads stay here.
  const readFollowBaseline = useCallback(async (sourceId: string): Promise<string | null> => {
    try {
      return (await getSource(sourceId)).updated_at || null;
    } catch {
      return null;
    }
  }, []);
  // A reparse submitted from outside the source detail (the KG analysis findings),
  // followed to the end of THIS run. The source holds the reparse placeholder from the
  // submission on -- the baseline read and the request included -- so no entry can
  // submit it again meanwhile; on "started" the follow takes it over in the same
  // render, otherwise it is released.
  async function submitFollowedReparse(
    sourceId: string,
    submit: () => Promise<SourceReparseOutcome>,
  ): Promise<SourceReparseOutcome> {
    const owner = currentOwner();
    if (owner) setSubmittingSourceIds((previous) => new Set(previous).add(sourceId));
    try {
      const baseline = await readFollowBaseline(sourceId);
      const outcome = await submit();
      if (outcome === "started" && owner && owns(owner)) {
        trackSourceProgress([sourceId], { [sourceId]: baseline });
      }
      return outcome;
    } finally {
      setSubmittingSourceIds((previous) => {
        if (!previous.has(sourceId)) return previous;
        const next = new Set(previous);
        next.delete(sourceId);
        return next.size === 0 ? NO_SOURCE_IDS : next;
      });
    }
  }

  // Unmount is not one of the abort/invalidation sites `abortInFlightSourcesPage`
  // is called from elsewhere in this hook (see its own comment) — none of them
  // fire once the component is gone. Without this, a source-page request still
  // in flight when the hook unmounts runs to completion server-side for nothing.
  useEffect(() => () => {
    pageAbortRef.current?.abort();
  }, []);

  return {
    trackSourceProgress,
    untrackSources,
    readFollowBaseline,
    submitFollowedReparse,
    followedSourceCount: ownerIsActive ? trackedSources.size : 0,
    reparsingSourceIds,
    sources: visibleSources,
    contentRevision: ownerIsActive ? contentRevision : 0,
    sourceScopeSelection: ownerIsActive
      ? sourceScopeSelection
      : inactiveScopeSelectionRef.current,
    sourcesTotal: ownerIsActive ? sourcesTotal : 0,
    notebookSourceTotal: ownerIsActive ? notebookSourceTotal : 0,
    notebookDocumentCount: ownerIsActive ? notebookDocumentCount : 0,
    sourcesPage: ownerIsActive ? sourcesPage : 0,
    sourcesPageLoading: ownerIsActive ? sourcesPageLoading : false,
    sourcesCollapsed,
    sourceQuery: ownerIsActive ? sourceQuery : "",
    sourceDetail: ownerIsActive ? sourceDetail : null,
    deletingSourceIds: ownerIsActive ? deletingSourceIds : inactiveDeletingIdsRef.current,
    reparsingSource: ownerIsActive ? reparsingSourceId !== null : false,
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
    searchSources,
    setSourceQuery,
    setSourcesCollapsed,
    selectAllSources,
    clearSourceSelection,
    toggleSource,
    applySourceScopeSelection,
    commitUploadedSources,
    commitUrlSources,
    openSourceById,
    closeSourceDetail,
    loadSourceElementPage,
    reparseSource,
    deleteSource,
  } as const;
}
