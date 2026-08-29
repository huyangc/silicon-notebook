"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import { searchNotebooksBounded } from "./collection-search.ts";
import {
  listBases,
  listMountable,
  mountedByCount,
  setBases,
  type MountedBase,
  type NotebookRef,
} from "./notebook-bases.ts";
import {
  createNotebook,
  deleteNotebook,
  fetchNotebookIndexingPipeline,
  getNotebook,
  listNotebooks,
  setNotebookIndexingPipeline,
  type IndexingPipelineResponse,
  updateNotebook,
} from "./notebook-api.ts";
import {
  indexingPipelineIdsEqual,
  normalizeIndexingPipelineId,
} from "./indexing-pipeline-settings.ts";
import { defaultNotebookPayload } from "./notebook-creation.ts";
import type { NotebookSummary, SearchHit } from "./workspace-model.ts";

type CollectionOwner = Readonly<{ actorId: string; generation: number }>;

export type CollectionListRead = Readonly<{
  owner: CollectionOwner;
  issue: number;
  deleteGeneration: number;
}>;

export type NotebookEditorPatch = Readonly<{
  name: FormDataEntryValue | null;
  purpose: FormDataEntryValue | null;
  primary_domain: FormDataEntryValue | null;
  target_users: string;
  access_scope: string;
  expected_questions: string[];
  source_types: string[];
  taxonomy: string[];
  indexing_pipeline_id?: string | null;
}>;

type CollectionEffects = {
  reportError(error: unknown): void;
  notify(message: string): void;
  refreshComposite(guard: () => boolean): Promise<void>;
  onNotebookCreated(notebook: NotebookSummary): Promise<void>;
  onNotebookUpdated(notebook: NotebookSummary, bases: MountedBase[]): void;
  onNotebookDeleted(notebookId: string): void;
  captureNavigationEpoch(): number;
  reconcileAccess(rows: NotebookSummary[], navigationEpoch: number): Promise<void>;
};

type CollectionOptions = {
  actorId: string | null;
  effects: CollectionEffects;
};

type MenuPosition = { left: number; top: number };

type EditorView = {
  owner: CollectionOwner;
  target: NotebookSummary;
  canManageContent: boolean;
  canConfigureNotebook: boolean;
  mountable: NotebookRef[];
  mountedIds: string[];
  mountEdges: MountedBase[];
  indexingPipeline: IndexingPipelineResponse | null;
  selectedPipelineId: string;
  busy: boolean;
};

type EditorOperation = Readonly<{ notebookId: string }>;

type DeleteView = {
  owner: CollectionOwner;
  target: NotebookSummary;
  mountedByCount: number;
  busy: boolean;
};

const ACCESS_REVALIDATE_MIN_INTERVAL_MS = 30_000;

function ownerKey(actorId: string): string {
  return actorId;
}

// Shape produced by the `visibleRows` search/sort projection below — named
// so the stable-empty fallback constant can be typed without repeating the
// anonymous object literal inline.
type NotebookSearchRow = { notebook: NotebookSummary; index: number; hits: SearchHit[] };

// Hidden-state (owner not visible) fallback values must be **stable
// references**. Consumers of the returned view depend on these fields in
// effects/useMemo; handing back a brand-new `[]`/`{}` on every render makes
// those dependencies "change" every render and can drive a setState-in-effect
// loop (see use-ask-session.ts for the traced incident). Freezing also turns
// any accidental in-place write into an immediate dev-time throw. Declared
// with the same mutable type as the state they stand in for so the ternary
// branches unify.
const NO_ROWS: NotebookSummary[] = Object.freeze([] as NotebookSummary[]) as NotebookSummary[];
const NO_VISIBLE_ROWS: NotebookSearchRow[] =
  Object.freeze([] as NotebookSearchRow[]) as NotebookSearchRow[];
const EMPTY_SEARCH_HITS: Record<string, SearchHit[]> =
  Object.freeze({} as Record<string, SearchHit[]>) as Record<string, SearchHit[]>;

export function useNotebookCollection({ actorId, effects }: CollectionOptions) {
  const effectsRef = useRef(effects);
  effectsRef.current = effects;
  const actorIdRef = useRef(actorId);
  const previousActorIdRef = useRef(actorId);
  const pendingActorIdRef = useRef<string | null>(null);
  const actorDetachedRef = useRef(false);
  const actorGenerationRef = useRef(0);

  const [rows, setRows] = useState<NotebookSummary[]>([]);
  const rowsRef = useRef(rows);
  rowsRef.current = rows;
  const rowsOwnerRef = useRef<CollectionOwner | null>(null);
  const [searchHits, setSearchHits] = useState<Record<string, SearchHit[]>>({});
  const [filter, setFilterState] = useState("mine");
  const [viewMode, setViewModeState] = useState("grid");
  const [sortMode, setSortModeState] = useState("recent");
  const [searchQuery, setSearchQueryState] = useState("");
  const searchQueryRef = useRef(searchQuery);
  searchQueryRef.current = searchQuery;
  const [sortOpen, setSortOpen] = useState(false);
  const [menuNotebookId, setMenuNotebookId] = useState<string | null>(null);
  const [menuPosition, setMenuPosition] = useState<MenuPosition | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);
  const [editor, setEditor] = useState<EditorView | null>(null);
  const [deletion, setDeletion] = useState<DeleteView | null>(null);
  const [creating, setCreating] = useState(false);
  const [uiOwnerGeneration, setUiOwnerGeneration] = useState(actorId ? 0 : -1);

  const listIssuedRef = useRef(0);
  const listPublishedRef = useRef(0);
  const searchGenerationRef = useRef(0);
  const editorOperationRef = useRef<EditorOperation | null>(null);
  const deleteOperationRef = useRef<object | null>(null);
  const createOperationRef = useRef<{ owner: CollectionOwner; token: object } | null>(null);
  // Tie the single-flight seat to the editor operation that acquired it.  A
  // successful save closes the editor before the post-write collection refresh
  // finishes, so a bare boolean can leak forever when the user reopens settings
  // and replaces editorOperationRef before the old finally block runs.
  const editorSavingRef = useRef<EditorOperation | null>(null);
  const editorRevokedOperationRef = useRef<EditorOperation | null>(null);
  const deletingRef = useRef(new Set<string>());
  const renamingRef = useRef(new Map<string, object>());
  const tombstonesRef = useRef(new Map<string, Set<string>>());
  const deleteGenerationRef = useRef(new Map<string, number>());

  const clearVisibleState = () => {
    rowsRef.current = [];
    rowsOwnerRef.current = null;
    setRows([]);
    setSearchHits({});
    setMenuNotebookId(null);
    setMenuPosition(null);
    setEditor(null);
    setDeletion(null);
    setCreating(false);
  };

  const resetActorLocalView = () => {
    setSearchHits({});
    setFilterState("mine");
    setViewModeState("grid");
    setSortModeState("recent");
    setSearchQueryState("");
    setSortOpen(false);
    setMenuNotebookId(null);
    setMenuPosition(null);
    setEditor(null);
    setDeletion(null);
    setCreating(false);
  };

  const invalidateActor = (nextActorId: string | null) => {
    actorIdRef.current = nextActorId;
    actorGenerationRef.current += 1;
    listIssuedRef.current += 1;
    searchGenerationRef.current += 1;
    editorOperationRef.current = null;
    deleteOperationRef.current = null;
    createOperationRef.current = null;
    editorSavingRef.current = null;
    editorRevokedOperationRef.current = null;
  };

  // Authentication changes are synchronous authority boundaries.  The pending
  // bridge mirrors the other workspace owners: fetchMe may bind the actor just
  // before React publishes currentUser, and the intervening null render must not
  // roll that authority back.
  if (!actorDetachedRef.current) {
    if (pendingActorIdRef.current === actorId) pendingActorIdRef.current = null;
    if (previousActorIdRef.current !== actorId) {
      if (!(pendingActorIdRef.current && actorId === null)) {
        pendingActorIdRef.current = null;
        previousActorIdRef.current = actorId;
        invalidateActor(actorId);
      }
    } else if (!pendingActorIdRef.current) {
      actorIdRef.current = actorId;
    }
  }

  useEffect(() => {
    if (actorIdRef.current !== actorId) return;
    resetActorLocalView();
    setUiOwnerGeneration(actorGenerationRef.current);
    if (actorId === null) clearVisibleState();
  }, [actorId]);

  const captureOwner = (): CollectionOwner | null => {
    const currentActor = actorIdRef.current;
    return currentActor
      ? Object.freeze({ actorId: currentActor, generation: actorGenerationRef.current })
      : null;
  };

  const owns = (owner: CollectionOwner): boolean => (
    actorIdRef.current === owner.actorId
    && actorGenerationRef.current === owner.generation
  );

  const ownsIdentity = (owner: Pick<CollectionOwner, "actorId">): boolean => (
    actorIdRef.current === owner.actorId
  );

  const deletedIds = (owner: Pick<CollectionOwner, "actorId">): Set<string> => (
    tombstonesRef.current.get(ownerKey(owner.actorId)) ?? new Set<string>()
  );

  const deleteGeneration = (owner: Pick<CollectionOwner, "actorId">): number => (
    deleteGenerationRef.current.get(ownerKey(owner.actorId)) ?? 0
  );

  const filterDeleted = (
    owner: Pick<CollectionOwner, "actorId">,
    values: NotebookSummary[],
  ): NotebookSummary[] => {
    const deleted = deletedIds(owner);
    return deleted.size === 0 ? values : values.filter((row) => !deleted.has(row.id));
  };

  const currentRow = (id: string): NotebookSummary | null => (
    rowsOwnerRef.current && owns(rowsOwnerRef.current)
      ? rowsRef.current.find((row) => row.id === id) ?? null
      : null
  );

  const rowIsOwner = (id: string): boolean => {
    const row = currentRow(id);
    return Boolean(row && (row.access ?? "owner") !== "reader");
  };

  const rowCanManageContent = (id: string): boolean => {
    const row = currentRow(id);
    return Boolean(row && (
      (row.access ?? "owner") !== "reader"
      || row.can_manage_content === true
    ));
  };

  const rowExplicitlyDeniesManageContent = (id: string): boolean => {
    const row = currentRow(id);
    return Boolean(
      row
      && (row.access ?? "owner") === "reader"
      && row.can_manage_content !== true
    );
  };

  const editorOperationMayContinue = (
    owner: CollectionOwner,
    operation: EditorOperation,
    notebookId: string,
  ): boolean => {
    if (!owns(owner) || editorOperationRef.current !== operation) return false;
    if (
      editorRevokedOperationRef.current !== operation
      && !rowExplicitlyDeniesManageContent(notebookId)
    ) return true;
    editorRevokedOperationRef.current = operation;
    effectsRef.current.notify(
      "权限已变更，已停止继续操作；此前已提交的修改不会撤销。",
    );
    return false;
  };

  const rowCanRename = (id: string): boolean => {
    return rowCanManageContent(id);
  };

  const rowCanConfigure = (id: string): boolean => {
    return rowIsOwner(id);
  };

  const refreshCompositeAfterCommit = async (owner: CollectionOwner): Promise<boolean> => {
    try {
      await effectsRef.current.refreshComposite(() => owns(owner));
      return owns(owner);
    } catch (error) {
      // The write has already committed.  Callers disclose an unrefreshed
      // projection without reclassifying the durable action as failed.
      //
      // The cause still has to stay observable.  This catch also covers the
      // composite's post-await state writes, so a programming error in them
      // would otherwise reach the user as a bare "list not refreshed" and reach
      // nobody else at all: reportError is deliberately not called here (it
      // would contradict the success toast the caller is about to publish).
      console.error("[collection] composite refresh after a committed write failed", error);
      return false;
    }
  };

  function activateActor(nextActorId: string) {
    if (!nextActorId || actorIdRef.current === nextActorId || actorIdRef.current !== null) return;
    actorDetachedRef.current = false;
    pendingActorIdRef.current = nextActorId;
    previousActorIdRef.current = nextActorId;
    invalidateActor(nextActorId);
  }

  function leaveActor() {
    actorDetachedRef.current = true;
    pendingActorIdRef.current = null;
    previousActorIdRef.current = null;
    invalidateActor(null);
    clearVisibleState();
  }

  function beginListRead(): CollectionListRead | null {
    const owner = captureOwner();
    if (!owner) return null;
    return Object.freeze({
      owner,
      issue: ++listIssuedRef.current,
      deleteGeneration: deleteGeneration(owner),
    });
  }

  function commitListSnapshot(read: CollectionListRead | null, snapshot: NotebookSummary[]): boolean {
    if (!read || !owns(read.owner) || read.issue <= listPublishedRef.current) return false;
    listPublishedRef.current = read.issue;
    if (read.deleteGeneration === deleteGeneration(read.owner)) {
      const present = new Set(snapshot.map((row) => row.id));
      const tombstones = tombstonesRef.current.get(ownerKey(read.owner.actorId));
      if (tombstones) {
        for (const id of tombstones) {
          if (!present.has(id)) tombstones.delete(id);
        }
        if (tombstones.size === 0) tombstonesRef.current.delete(ownerKey(read.owner.actorId));
      }
    }
    const next = filterDeleted(read.owner, snapshot);
    rowsOwnerRef.current = read.owner;
    rowsRef.current = next;
    setRows(next);
    const activeEditorOperation = editorOperationRef.current;
    if (
      activeEditorOperation
      && editorSavingRef.current === activeEditorOperation
      && rowExplicitlyDeniesManageContent(activeEditorOperation.notebookId)
    ) {
      // Once this operation observes an explicit revocation it cannot be made
      // writable again by a later incomplete/omitted collection projection.
      editorRevokedOperationRef.current = activeEditorOperation;
    }
    const ownerIds = new Set(next
      .filter((row) => (row.access ?? "owner") !== "reader")
      .map((row) => row.id));
    const editorVisibleIds = new Set(next
      .filter((row) => (row.access ?? "owner") !== "reader" || row.can_manage_content === true)
      .map((row) => row.id));
    setEditor((current) => {
      if (!current || editorVisibleIds.has(current.target.id)) return current;
      // The list is only a projection and may be replaced while a multi-step
      // save is in flight.  Keep the disabled editor visible and let the API
      // authorization on each remaining write decide; otherwise this snapshot
      // silently cancels the operation between PATCH and PUT.
      if (
        editorOperationRef.current
        && editorSavingRef.current === editorOperationRef.current
      ) return current;
      editorOperationRef.current = null;
      editorSavingRef.current = null;
      editorRevokedOperationRef.current = null;
      return null;
    });
    setDeletion((current) => {
      if (!current || ownerIds.has(current.target.id)) return current;
      const key = `${current.owner.actorId}\0${current.target.id}`;
      // As with editor saves, a list projection is not cancellation authority
      // for a request that has already reached the server.  Keep the disabled
      // confirmation visible so either success or failure has somewhere to land.
      if (deletingRef.current.has(key)) return current;
      deleteOperationRef.current = null;
      return null;
    });
    return true;
  }

  async function refreshAfterAccessChange(
    navigationEpoch = effectsRef.current.captureNavigationEpoch(),
    retry = true,
  ): Promise<void> {
    const read = beginListRead();
    if (!read) return;
    let snapshot: NotebookSummary[];
    try {
      snapshot = await listNotebooks();
    } catch (error) {
      if (owns(read.owner)) throw error;
      return;
    }
    if (!owns(read.owner)) return;
    commitListSnapshot(read, snapshot);
    if (read.issue !== listIssuedRef.current) {
      if (retry) await refreshAfterAccessChange(navigationEpoch, false);
      return;
    }
    await effectsRef.current.reconcileAccess(filterDeleted(read.owner, snapshot), navigationEpoch);
  }

  useEffect(() => {
    if (!actorId) return;
    let lastAt = 0;
    const revalidate = () => {
      if (document.visibilityState !== "visible") return;
      const now = Date.now();
      if (now - lastAt < ACCESS_REVALIDATE_MIN_INTERVAL_MS) return;
      lastAt = now;
      void refreshAfterAccessChange().catch((error) => {
        const owner = captureOwner();
        if (owner && owns(owner)) effectsRef.current.reportError(error);
      });
    };
    document.addEventListener("visibilitychange", revalidate);
    window.addEventListener("focus", revalidate);
    return () => {
      document.removeEventListener("visibilitychange", revalidate);
      window.removeEventListener("focus", revalidate);
    };
  }, [actorId]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    const owner = captureOwner();
    const query = searchQuery.trim();
    const generation = ++searchGenerationRef.current;
    if (!owner || !query) {
      setSearchHits({});
      return;
    }
    const controller = new AbortController();
    const timer = window.setTimeout(() => {
      if (!rowsOwnerRef.current || !owns(rowsOwnerRef.current)) return;
      const ids = filterDeleted(owner, rowsRef.current).map((row) => row.id);
      searchNotebooksBounded(ids, query, controller.signal)
        .then((hits) => {
          if (!owns(owner) || generation !== searchGenerationRef.current
            || searchQueryRef.current.trim() !== query) return;
          const deleted = deletedIds(owner);
          const filtered = Object.fromEntries(
            Object.entries(hits).filter(([id]) => !deleted.has(id)),
          );
          setSearchHits(filtered);
        })
        .catch((error) => {
          if (!controller.signal.aborted && owns(owner)
            && generation === searchGenerationRef.current) {
            effectsRef.current.reportError(error);
          }
        });
    }, 250);
    return () => {
      controller.abort();
      window.clearTimeout(timer);
    };
  }, [actorId, rows, searchQuery]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    if (!menuNotebookId) return;
    const close = () => {
      setMenuNotebookId(null);
      setMenuPosition(null);
    };
    const pointer = (event: PointerEvent) => {
      if (event.target instanceof Node && menuRef.current?.contains(event.target)) return;
      close();
    };
    const key = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") close();
    };
    window.addEventListener("pointerdown", pointer);
    window.addEventListener("keydown", key);
    window.addEventListener("resize", close);
    window.addEventListener("scroll", close, true);
    return () => {
      window.removeEventListener("pointerdown", pointer);
      window.removeEventListener("keydown", key);
      window.removeEventListener("resize", close);
      window.removeEventListener("scroll", close, true);
    };
  }, [menuNotebookId]);

  const visibleRows = useMemo(() => {
    if (!rowsOwnerRef.current || !owns(rowsOwnerRef.current)) return [];
    const query = searchQuery.trim();
    const enriched = rows
      .map((notebook, index) => ({ notebook, index, hits: searchHits[notebook.id] ?? [] }))
      .filter(({ notebook, hits }) => {
        if (filter === "featured") {
          const featured = Object.values(notebook.counts ?? {}).some((count) => (count ?? 0) > 0);
          if (!featured) return false;
        }
        return !query || hits.length > 0;
      });
    enriched.sort((left, right) => {
      if (sortMode === "name") return left.notebook.name.localeCompare(right.notebook.name, "zh-Hans-CN");
      if (sortMode === "sources") {
        return (right.notebook.counts.sources ?? 0) - (left.notebook.counts.sources ?? 0);
      }
      return left.index - right.index;
    });
    return enriched;
  }, [filter, rows, searchHits, searchQuery, sortMode]);

  function selectFilter(next: string) {
    setFilterState(next);
  }

  function selectView(next: string) {
    setViewModeState(next);
  }

  function selectSort(next: string) {
    setSortModeState(next);
    setSortOpen(false);
  }

  function updateSearchQuery(next: string) {
    setSearchQueryState(next);
  }

  function openMenu(notebookId: string, rect: DOMRect) {
    if (!captureOwner() || !currentRow(notebookId)) return;
    const menuWidth = 180;
    const menuHeight = 116;
    setMenuPosition({
      left: Math.min(window.innerWidth - menuWidth - 12, Math.max(12, rect.right - menuWidth)),
      top: Math.min(window.innerHeight - menuHeight - 12, rect.bottom + 8),
    });
    setMenuNotebookId(notebookId);
  }

  function closeMenu() {
    setMenuNotebookId(null);
    setMenuPosition(null);
  }

  async function createDefaultNotebook(): Promise<void> {
    const owner = captureOwner();
    if (!owner || createOperationRef.current) return;
    const token = {};
    createOperationRef.current = { owner, token };
    setCreating(true);
    try {
      let notebook: NotebookSummary;
      try {
        notebook = await createNotebook(defaultNotebookPayload());
      } catch (error) {
        if (owns(owner)) effectsRef.current.reportError(error);
        return;
      }
      if (!owns(owner)) return;
      const refreshed = await refreshCompositeAfterCommit(owner);
      if (!owns(owner)) return;
      try {
        await effectsRef.current.onNotebookCreated(notebook);
      } catch (error) {
        if (owns(owner)) {
          effectsRef.current.reportError(error);
          // Only point at the list when the list actually has the notebook: on
          // an unrefreshed projection it is not there, so "reopen it from the
          // list" would name a row that does not exist yet.
          effectsRef.current.notify(
            refreshed
              ? "笔记本已创建，但暂时没能打开；请从列表重新打开。"
              : "笔记本已创建，但暂时没能打开、列表也暂未刷新；请稍后刷新页面。",
          );
        }
        return;
      }
      if (!refreshed && owns(owner)) {
        effectsRef.current.notify("笔记本已创建，但列表暂未刷新；请稍后刷新页面。");
      }
    } finally {
      if (createOperationRef.current?.token === token) {
        createOperationRef.current = null;
        if (owns(owner)) setCreating(false);
      }
    }
  }

  async function renameNotebook(notebookId: string, name: string): Promise<NotebookSummary | null> {
    const owner = captureOwner();
    if (!owner || !rowCanRename(notebookId)) return null;
    const key = `${owner.actorId}\0${notebookId}`;
    if (renamingRef.current.has(key)) return null;
    const token = {};
    renamingRef.current.set(key, token);
    try {
      const updated = await updateNotebook(notebookId, { name });
      if (!owns(owner) || renamingRef.current.get(key) !== token) {
        return null;
      }
      const refreshed = await refreshCompositeAfterCommit(owner);
      if (!owns(owner) || renamingRef.current.get(key) !== token) {
        return null;
      }
      effectsRef.current.notify(
        refreshed
          ? "笔记本名称已更新"
          : "笔记本名称已更新，但列表暂未刷新；请稍后刷新页面。",
      );
      if (!refreshed) return updated;
      const authoritativeRow = currentRow(notebookId);
      if (!authoritativeRow) return null;
      // PATCH returns the access projection from request time.  A concurrent
      // refresh may already have revoked or changed that access, so carry only
      // the refreshed authority fields onto the committed detail before the
      // shell replaces currentNotebook with it.  Do not spread the whole list
      // row: several detail-only fields use sentinels in collection responses.
      return {
        ...updated,
        access: authoritativeRow.access,
        shared_from: authoritativeRow.shared_from,
        is_shared: authoritativeRow.is_shared,
        granted_via: authoritativeRow.granted_via,
        can_manage_content: authoritativeRow.can_manage_content,
      };
    } catch (error) {
      if (owns(owner) && renamingRef.current.get(key) === token) throw error;
      return null;
    } finally {
      if (renamingRef.current.get(key) === token) renamingRef.current.delete(key);
    }
  }

  async function openEditor(notebookId: string): Promise<boolean> {
    const owner = captureOwner();
    if (!owner || !rowCanManageContent(notebookId)) return false;
    const operation: EditorOperation = { notebookId };
    editorOperationRef.current = operation;
    // Replacing the operation orphans any save still in flight for the previous
    // one: its remaining steps stop at the next editorOperationMayContinue and
    // its editor view is already gone.  Release the single-flight seat with it,
    // or the editor this call is opening has a silently dead save button until
    // the orphan settles — saveEditor reads the seat as a plain latch, not as
    // "a save for *this* operation".
    editorSavingRef.current = null;
    editorRevokedOperationRef.current = null;
    try {
      const target = currentRow(notebookId);
      if (!target) return false;
      const canManageContent = rowCanManageContent(notebookId);
      const canConfigureNotebook = rowCanConfigure(notebookId);
      const indexingPipelinePromise = fetchNotebookIndexingPipeline(notebookId);
      const [indexingPipeline, mountable, mountEdges] = canConfigureNotebook
        ? await Promise.all([
          indexingPipelinePromise,
          listMountable(notebookId),
          listBases(notebookId),
        ])
        : await Promise.all([
          indexingPipelinePromise,
          Promise.resolve([] as NotebookRef[]),
          Promise.resolve([] as MountedBase[]),
        ]);
      if (
        !owns(owner)
        || editorOperationRef.current !== operation
        || !rowCanManageContent(notebookId)
      ) return false;
      setEditor({
        owner,
        target,
        canManageContent,
        canConfigureNotebook,
        mountable,
        mountEdges,
        mountedIds: mountEdges.map((edge) => edge.id),
        indexingPipeline,
        selectedPipelineId: normalizeIndexingPipelineId(indexingPipeline.pipeline_id),
        busy: false,
      });
      closeMenu();
      return true;
    } catch (error) {
      if (owns(owner) && editorOperationRef.current === operation) effectsRef.current.reportError(error);
      return false;
    }
  }

  function closeEditor() {
    // Dismissing the dialog is explicit cancellation: the remaining write steps
    // stop at the next checkpoint.  It is also the only exit the settings
    // dialog has while a save is in flight (the modal takes no Escape and no
    // backdrop click), so the outcome of the steps that already reached the
    // server has to be said out loud rather than left to a silently vanished
    // dialog.
    const abandonedSave = Boolean(
      editorSavingRef.current
      && editorSavingRef.current === editorOperationRef.current
    );
    editorOperationRef.current = null;
    editorSavingRef.current = null;
    editorRevokedOperationRef.current = null;
    setEditor(null);
    if (abandonedSave) {
      effectsRef.current.notify(
        "已停止等待保存结果；此前已提交的修改不会撤销，未提交的部分不会继续。",
      );
    }
  }

  function toggleMountedBase(notebookId: string, selected: boolean) {
    setEditor((current) => current ? {
      ...current,
      mountedIds: selected
        ? [...current.mountedIds.filter((id) => id !== notebookId), notebookId]
        : current.mountedIds.filter((id) => id !== notebookId),
    } : current);
  }

  function selectIndexingPipeline(pipelineId: string | null) {
    setEditor((current) => current ? {
      ...current,
      selectedPipelineId: normalizeIndexingPipelineId(pipelineId),
    } : current);
  }

  async function applyIndexingPipelineSelection(
    current: EditorView,
    desiredPipelineId: string,
  ): Promise<IndexingPipelineResponse | null> {
    if (
      !current.canManageContent
      || indexingPipelineIdsEqual(current.indexingPipeline?.pipeline_id, desiredPipelineId)
    ) return current.indexingPipeline;
    return setNotebookIndexingPipeline(current.target.id, desiredPipelineId || null);
  }

  async function saveEditor(patch: NotebookEditorPatch): Promise<void> {
    const owner = captureOwner();
    const current = editor;
    if (!owner || !current || !owns(current.owner) || current.busy || editorSavingRef.current
      || !rowCanManageContent(current.target.id)) return;
    const operation = editorOperationRef.current;
    // Two notebook ids are in play once the seat is bound to an operation: the
    // write steps below key off `current.target.id`, while the sticky-revocation
    // bookkeeping in commitListSnapshot keys off `operation.notebookId`.  They
    // can only diverge while an openEditor for a *different* notebook is in
    // flight, and letting the pair drift would mark this save revoked from the
    // other notebook's access.  Refuse instead of running on a split identity.
    if (!operation || operation.notebookId !== current.target.id) return;
    editorSavingRef.current = operation;
    setEditor((value) => value ? { ...value, busy: true } : value);
    try {
      const { indexing_pipeline_id: indexingPipelineId, ...notebookPatch } = patch;
      await updateNotebook(current.target.id, notebookPatch);
      if (!editorOperationMayContinue(owner, operation, current.target.id)) return;
      const pipelineResult = await applyIndexingPipelineSelection(
        current,
        normalizeIndexingPipelineId(indexingPipelineId),
      );
      if (!editorOperationMayContinue(owner, operation, current.target.id)) return;
      const bases = current.canConfigureNotebook
        ? await setBases(current.target.id, current.mountedIds)
        : current.mountEdges;
      if (!editorOperationMayContinue(owner, operation, current.target.id)) return;
      const updated = await getNotebook(current.target.id);
      if (!editorOperationMayContinue(owner, operation, current.target.id)) return;
      // The server-side write sequence is complete.  Release the save seat
      // before hiding the dialog and awaiting the slower collection refresh so
      // an immediate reopen starts with a fresh, usable operation.
      if (editorSavingRef.current === operation) editorSavingRef.current = null;
      setEditor(null);
      effectsRef.current.onNotebookUpdated(updated, bases);
      const refreshed = await refreshCompositeAfterCommit(owner);
      if (owns(owner)) {
        if (pipelineResult?.changed) {
          const warningCount = pipelineResult.warning_count ?? 0;
          const message = (
            warningCount > 0
              ? `索引管线已切换，正在重建全库索引；${warningCount} 项插件分块已回退到内建。`
              : "索引管线已切换，正在重建全库索引。"
          );
          effectsRef.current.notify(
            refreshed ? message : `${message} 列表暂未刷新，请稍后刷新页面。`,
          );
        } else {
          effectsRef.current.notify(
            refreshed
              ? "笔记本信息已更新"
              : "笔记本信息已更新，但列表暂未刷新；请稍后刷新页面。",
          );
        }
      }
    } catch (error) {
      if (owns(owner) && editorOperationRef.current === operation) effectsRef.current.reportError(error);
    } finally {
      if (editorSavingRef.current === operation) editorSavingRef.current = null;
      if (editorRevokedOperationRef.current === operation) editorRevokedOperationRef.current = null;
      if (owns(owner) && editorOperationRef.current === operation) {
        setEditor((value) => {
          if (!value) return value;
          if (!rowCanManageContent(value.target.id)) {
            editorOperationRef.current = null;
            return null;
          }
          return { ...value, busy: false };
        });
      }
    }
  }

  async function restartIndexingPipeline(
    pipelineId: string | null,
    successMessage: string,
  ): Promise<void> {
    const owner = captureOwner();
    const current = editor;
    if (!owner || !current || !owns(current.owner) || current.busy || editorSavingRef.current
      || !rowCanManageContent(current.target.id)) return;
    const operation = editorOperationRef.current;
    // Same split-identity refusal as saveEditor — see the comment there.
    if (!operation || operation.notebookId !== current.target.id) return;
    editorSavingRef.current = operation;
    setEditor((value) => value ? { ...value, busy: true } : value);
    try {
      await setNotebookIndexingPipeline(current.target.id, pipelineId);
      if (!editorOperationMayContinue(owner, operation, current.target.id)) return;
      const updated = await getNotebook(current.target.id);
      if (!editorOperationMayContinue(owner, operation, current.target.id)) return;
      // 成功即关弹窗(镜像 saveEditor):弹窗没有活状态轮询,留着只会把一次性的
      // pending 响应冻在屏上——后台重建早已结束,界面还写着「重建中」并禁用整组
      // 单选,直到用户自己关掉重开(codex #602 R1 P2)。toast 已说明重建在进行;
      // 重开设置时会重新取一次实时投影。
      if (editorSavingRef.current === operation) editorSavingRef.current = null;
      setEditor(null);
      effectsRef.current.onNotebookUpdated(updated, current.mountEdges);
      const refreshed = await refreshCompositeAfterCommit(owner);
      if (owns(owner)) {
        effectsRef.current.notify(
          refreshed
            ? successMessage
            : `${successMessage} 列表暂未刷新，请稍后刷新页面。`,
        );
      }
    } catch (error) {
      if (owns(owner) && editorOperationRef.current === operation) {
        effectsRef.current.reportError(error);
      }
    } finally {
      if (editorSavingRef.current === operation) editorSavingRef.current = null;
      if (editorRevokedOperationRef.current === operation) editorRevokedOperationRef.current = null;
      if (owns(owner) && editorOperationRef.current === operation) {
        setEditor((value) => {
          if (!value) return value;
          if (!rowCanManageContent(value.target.id)) {
            editorOperationRef.current = null;
            return null;
          }
          return { ...value, busy: false };
        });
      }
    }
  }

  async function revertIndexingPipelineToBuiltin(): Promise<void> {
    await restartIndexingPipeline(
      null,
      "已切回内建索引管线，正在重建全库索引。",
    );
  }

  async function retryIndexingPipelineRebuild(): Promise<void> {
    const pipelineId = normalizeIndexingPipelineId(
      editor?.indexingPipeline?.pipeline_id,
    );
    await restartIndexingPipeline(
      pipelineId || null,
      "已重新提交当前索引管线，正在重建全库索引。",
    );
  }

  async function openDelete(notebookId: string): Promise<boolean> {
    const owner = captureOwner();
    if (!owner || !rowIsOwner(notebookId)) return false;
    const operation = {};
    deleteOperationRef.current = operation;
    try {
      const { count } = await mountedByCount(notebookId);
      if (!owns(owner) || deleteOperationRef.current !== operation || !rowIsOwner(notebookId)) return false;
      const target = currentRow(notebookId);
      if (!target) return false;
      setDeletion({ owner, target, mountedByCount: count, busy: false });
      closeMenu();
      return true;
    } catch (error) {
      if (owns(owner) && deleteOperationRef.current === operation) effectsRef.current.reportError(error);
      return false;
    }
  }

  function closeDelete() {
    // Unlike an editor save, a DELETE already on the wire is not stopped by
    // dismissing the confirmation — its success path is gated on the actor, not
    // on this dialog, and still removes the row and toasts.  Dismissing while it
    // is in flight therefore has to say the request outlives the box.
    const inFlightDelete = Boolean(
      deletion
      && deletingRef.current.has(`${deletion.owner.actorId}\0${deletion.target.id}`)
    );
    deleteOperationRef.current = null;
    setDeletion(null);
    if (inFlightDelete) {
      effectsRef.current.notify("删除请求仍在进行；结果稍后会反映在列表里。");
    }
  }

  async function confirmDelete(): Promise<void> {
    const owner = captureOwner();
    const current = deletion;
    if (!owner || !current || !owns(current.owner) || !rowIsOwner(current.target.id)) return;
    const key = `${owner.actorId}\0${current.target.id}`;
    if (deletingRef.current.has(key)) return;
    const operation = deleteOperationRef.current;
    if (!operation) return;
    deletingRef.current.add(key);
    setDeletion((value) => value ? { ...value, busy: true } : value);
    try {
      await deleteNotebook(current.target.id);
      let tombstones = tombstonesRef.current.get(ownerKey(owner.actorId));
      if (!tombstones) {
        tombstones = new Set<string>();
        tombstonesRef.current.set(ownerKey(owner.actorId), tombstones);
      }
      tombstones.add(current.target.id);
      deleteGenerationRef.current.set(
        ownerKey(owner.actorId),
        deleteGeneration(owner) + 1,
      );
      if (ownsIdentity(owner)) {
        const next = rowsRef.current.filter((row) => row.id !== current.target.id);
        rowsRef.current = next;
        setRows(next);
        setSearchHits((hits) => {
          const copy = { ...hits };
          delete copy[current.target.id];
          return copy;
        });
        effectsRef.current.onNotebookDeleted(current.target.id);
        setDeletion(null);
        await effectsRef.current.refreshComposite(() => ownsIdentity(owner)).catch(() => undefined);
      }
      if (owns(owner)) effectsRef.current.notify("笔记本已删除");
    } catch (error) {
      // Reported on the same authority the success branch uses (the actor, not
      // the dialog operation).  A DELETE cannot be withdrawn by closing the
      // confirmation, so gating the failure on a still-open dialog would make
      // success speak and failure stay silent for the very same request.
      if (owns(owner)) effectsRef.current.reportError(error);
    } finally {
      deletingRef.current.delete(key);
      if (owns(owner) && deleteOperationRef.current === operation) {
        setDeletion((value) => {
          if (!value) return value;
          if (!rowIsOwner(value.target.id)) {
            deleteOperationRef.current = null;
            return null;
          }
          return { ...value, busy: false };
        });
      }
    }
  }

  const visible = Boolean(
    actorIdRef.current && uiOwnerGeneration === actorGenerationRef.current
  );
  const rowsVisible = Boolean(rowsOwnerRef.current && owns(rowsOwnerRef.current));
  const menuNotebook = visible && menuNotebookId
    ? rows.find((row) => row.id === menuNotebookId) ?? null
    : null;
  const editorSaveInFlight = Boolean(
    editorOperationRef.current
    && editorSavingRef.current === editorOperationRef.current
  );
  const deletionInFlight = Boolean(
    deletion
    && deletingRef.current.has(`${deletion.owner.actorId}\0${deletion.target.id}`)
  );

  return {
    rows: rowsVisible ? rows : NO_ROWS,
    visibleRows: rowsVisible ? visibleRows : NO_VISIBLE_ROWS,
    searchHits: rowsVisible ? searchHits : EMPTY_SEARCH_HITS,
    filter: visible ? filter : "mine",
    viewMode: visible ? viewMode : "grid",
    sortMode: visible ? sortMode : "recent",
    searchQuery: visible ? searchQuery : "",
    sortOpen: visible && sortOpen,
    menu: {
      notebook: menuNotebook,
      position: menuNotebook ? menuPosition : null,
      ref: menuRef,
    },
    editor: visible && editor && owns(editor.owner)
      && (editorSaveInFlight || rowCanManageContent(editor.target.id))
      ? editor
      : null,
    deletion: visible && deletion && owns(deletion.owner)
      && (deletionInFlight || rowIsOwner(deletion.target.id))
      ? deletion
      : null,
    creating: visible && creating && Boolean(
      createOperationRef.current && owns(createOperationRef.current.owner)
    ),
    activateActor,
    leaveActor,
    beginListRead,
    commitListSnapshot,
    refreshAfterAccessChange,
    selectFilter,
    selectView,
    selectSort,
    updateSearchQuery,
    toggleSort: () => setSortOpen((value) => !value),
    closeSort: () => setSortOpen(false),
    openMenu,
    closeMenu,
    createDefaultNotebook,
    renameNotebook,
    openEditor,
    closeEditor,
    toggleMountedBase,
    selectIndexingPipeline,
    saveEditor,
    revertIndexingPipelineToBuiltin,
    retryIndexingPipelineRebuild,
    openDelete,
    closeDelete,
    confirmDelete,
  };
}
