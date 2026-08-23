"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import { httpErrorStatus, toUserMessage } from "./errors";
import { prepareKgFocus } from "./kg-focus";
import {
  KG_BACKGROUND_POLL_CAP_MS,
  KG_BACKGROUND_POLL_MS,
  KG_MAINTENANCE_POLL_MS,
  MAINTENANCE_JOB_MISMATCH_SETTLE_STREAK,
  KG_RANGE_DEFAULT,
  KG_SEARCH_DEBOUNCE_MS,
  filterPendingMergeTombstones,
  knowledgeItemFromRecord,
  pendingMergeTombstoneKeys,
  sameKgIdentity,
  sameKgOwner,
  type KgWorkspaceOwner,
  type KgWorkspaceTransition,
} from "./kg-workspace-model";
import {
  createNotebookObjectSchema,
  createObjectSchema,
  deleteNotebookObjectSchema,
  deleteObjectSchema,
  findDuplicates as findKnowledgeDuplicates,
  listKnowledge,
  listKnowledgeTypes,
  listNotebookObjectSchemas,
  listObjectSchemas,
  mergeKnowledge as mergeKnowledgeRecords,
  proposeObjectSchemas,
  updateKnowledge as updateKnowledgeRecord,
  updateNotebookObjectSchema,
  updateObjectSchema,
} from "./knowledge-api";
import { withoutDecidedMerge } from "./kg-merge-model";
import { shouldResumeReviewAll } from "./in-progress-resume";
import type { SchemaView } from "./schema-manager";
import {
  buildKg,
  confirmMerge,
  fetchConceptDetail,
  fetchKgNeighbors,
  fetchKgSearch,
  fetchMergeReviewJob,
  fetchNodeContext,
  fetchPendingMerges,
  fetchRelinkStatus,
  fetchUnifiedGraph,
  fetchUnifiedKgStatus,
  fetchUnifiedKgRebuildStatus,
  rebuildKg,
  rebuildUnifiedKg,
  relinkKg,
  rejectMerge,
  reviewAllMerges as reviewAllMergesRequest,
  reviewMerges,
} from "../features/kg-maintenance/kg-api";
import {
  REBUILD_POLL_MAX_ATTEMPTS,
  REBUILD_POLL_TIMED_OUT,
  busyForNotebook,
  claimNotebookSlot,
  rebuildPollOutcome,
  releaseNotebookClaim,
} from "../features/kg-maintenance/kg-rebuild-status";
import {
  RELINK_POLL_MAX_ATTEMPTS,
  RELINK_POLL_TIMED_OUT,
  relinkPollOutcome,
} from "../features/kg-maintenance/kg-relink-status";
import {
  kgBuildTerminalToast,
  reconcileTrackedKgPoll,
} from "./kg-build-status";
import type {
  ConceptDetailResp,
  DuplicateGroup,
  KgSearchHit,
  KnowledgeItem,
  KnowledgeKind,
  KnowledgeTypeCount,
  MergeReviewJob,
  NotebookSummary,
  NodeContext,
  ObjectSchema,
  PendingMerge,
  UnifiedConceptNode,
  UnifiedEdge,
  UnifiedGraphResp,
  UnifiedKgStatus,
} from "./workspace-model";
import { EMPTY_KNOWLEDGE } from "./workspace-model";

type KgWorkspacePolicy = {
  canGovernKnowledge: boolean;
  canManageNotebookSchemas: boolean;
  canManageGlobalSchemas: boolean;
  canWriteKg: boolean;
  externalBuildPolling: boolean;
};

type KgWorkspaceEffects = {
  notify: (message: string) => void;
  reportError: (error: unknown) => void;
  refreshCollection: (guard: () => boolean) => Promise<void>;
  refreshNotebook: (notebookId: string, guard: () => boolean) => Promise<NotebookSummary>;
  focusGraphNode: (nodeId: string) => void;
};

export type UseKgWorkspaceOptions = {
  actorId: string | null;
  notebookId: string | null;
  policy: KgWorkspacePolicy;
  effects: KgWorkspaceEffects;
};

type KnowledgeRequest = {
  kind: KnowledgeKind;
  status: string;
  page: number;
};

const ownerKey = (owner: Pick<KgWorkspaceOwner, "actorId" | "notebookId">): string =>
  `${owner.actorId}\0${owner.notebookId}`;

// Hidden-state (owner not visible) fallback values must be **stable
// references**. The returned view is read by page.tsx effects/useMemo that
// depend on these fields; handing back a brand-new `[]`/`{}` on every render
// makes those dependencies "change" every render and can drive a
// setState-in-effect loop (see use-ask-session.ts for the traced incident).
// Freezing also turns any accidental in-place write into an immediate
// dev-time throw. Declared with the same mutable type as the state they
// stand in for so the ternary branches unify.
const NO_KNOWLEDGE_TYPES: KnowledgeTypeCount[] =
  Object.freeze([] as KnowledgeTypeCount[]) as KnowledgeTypeCount[];
const EMPTY_KNOWLEDGE_CONTEXTS: Record<string, NodeContext> =
  Object.freeze({} as Record<string, NodeContext>) as Record<string, NodeContext>;
const NO_SEARCH_HITS: KgSearchHit[] = Object.freeze([] as KgSearchHit[]) as KgSearchHit[];
const NO_SELECTED_TYPES: string[] = Object.freeze([] as string[]) as string[];
const NO_PENDING_MERGES: PendingMerge[] = Object.freeze([] as PendingMerge[]) as PendingMerge[];

export type KgWorkspace = ReturnType<typeof useKgWorkspace>;

export function useKgWorkspace({
  actorId,
  notebookId,
  policy,
  effects,
}: UseKgWorkspaceOptions) {
  const policyRef = useRef(policy);
  policyRef.current = policy;
  const effectsRef = useRef(effects);
  effectsRef.current = effects;
  const actorIdRef = useRef(actorId);
  actorIdRef.current = actorId;
  const notebookIdRef = useRef(notebookId);
  notebookIdRef.current = notebookId;

  const generationRef = useRef(0);
  const viewGenerationRef = useRef(0);
  const transitionGenerationRef = useRef(0);
  const transitionSuspendedRef = useRef(false);
  const pendingNotebookSnapshotRef = useRef<NotebookSummary | null>(null);
  const ownerRef = useRef<KgWorkspaceOwner | null>(null);
  const [ownerVersion, forceOwnerRender] = useState(0);

  const knowledgeRequestRef = useRef(0);
  const typeRequestRef = useRef(0);
  const duplicateRequestRef = useRef(0);
  const knowledgeContextRequestsRef = useRef(new Map<string, object>());
  const schemaRequestRef = useRef(0);
  const graphOpenRequestRef = useRef(0);
  const graphRangeRequestRef = useRef(0);
  const graphSearchRequestRef = useRef(0);
  const graphNodeRequestRef = useRef(0);
  const graphSearchTimerRef = useRef<number | null>(null);
  const operationTokensRef = useRef(new Map<string, object>());
  const mergeTombstonesRef = useRef(new Map<string, Set<string>>());
  const nodeNotebookRef = useRef(new Map<string, string>());
  const nodeContextObjectRef = useRef(new Map<string, string>());
  const submittingMaintenanceRef = useRef(new Set<string>());
  const expectedMaintenanceJobRef = useRef(new Map<string, string>());
  const pendingRebuildRef = useRef(new Set<string>());

  const [knowledgeKind, setKnowledgeKind] = useState<KnowledgeKind>("concept");
  const knowledgeKindRef = useRef(knowledgeKind);
  knowledgeKindRef.current = knowledgeKind;
  const [knowledge, setKnowledge] = useState<Record<string, KnowledgeItem[] | null>>(EMPTY_KNOWLEDGE);
  const knowledgeRef = useRef(knowledge);
  knowledgeRef.current = knowledge;
  const [knowledgeTypes, setKnowledgeTypes] = useState<KnowledgeTypeCount[]>([]);
  const [knowledgeStatusFilter, setKnowledgeStatusFilter] = useState("all");
  const statusFilterRef = useRef(knowledgeStatusFilter);
  statusFilterRef.current = knowledgeStatusFilter;
  const [knowledgeTotal, setKnowledgeTotal] = useState<Record<string, number>>({});
  const [knowledgePage, setKnowledgePage] = useState<Record<string, number>>({});
  const [duplicates, setDuplicates] = useState<DuplicateGroup[] | null>(null);
  const [knowledgeContexts, setKnowledgeContexts] = useState<Record<string, NodeContext>>({});
  const [knowledgeBusy, setKnowledgeBusy] = useState<string | null>(null);

  const [schemaModalOpen, setSchemaModalOpen] = useState(false);
  const [schemas, setSchemas] = useState<ObjectSchema[] | null>(null);
  const [schemaBusy, setSchemaBusy] = useState(false);
  const [schemaView, setSchemaView] = useState<SchemaView>("notebook");
  const schemaViewRef = useRef(schemaView);
  schemaViewRef.current = schemaView;

  const [graphOpen, setGraphOpen] = useState(false);
  const graphOpenRef = useRef(graphOpen);
  graphOpenRef.current = graphOpen;
  const [analysisOpen, setAnalysisOpen] = useState(false);
  const [unifiedGraph, setUnifiedGraph] = useState<UnifiedGraphResp | null>(null);
  const unifiedGraphRef = useRef<UnifiedGraphResp | null>(null);
  unifiedGraphRef.current = unifiedGraph;
  const [vizBuilding, setVizBuilding] = useState(false);
  const [search, setSearch] = useState("");
  const [searchHits, setSearchHits] = useState<KgSearchHit[]>([]);
  const [searchBusy, setSearchBusy] = useState(false);
  const [expandedNodes, setExpandedNodes] = useState<UnifiedConceptNode[]>([]);
  const [expandedEdges, setExpandedEdges] = useState<UnifiedEdge[]>([]);
  const [rangeLimit, setRangeLimit] = useState(KG_RANGE_DEFAULT);
  const rangeLimitRef = useRef(rangeLimit);
  rangeLimitRef.current = rangeLimit;
  const [rangeBusy, setRangeBusy] = useState(false);
  const [selectedTypes, setSelectedTypes] = useState<string[]>([]);
  const [pendingMerges, setPendingMerges] = useState<PendingMerge[]>([]);
  const [unifiedStatus, setUnifiedStatus] = useState<UnifiedKgStatus | null>(null);
  const [selectedNodeId, setSelectedNodeId] = useState<string | null>(null);
  const selectedNodeIdRef = useRef(selectedNodeId);
  selectedNodeIdRef.current = selectedNodeId;
  const [pendingFocusId, setPendingFocusId] = useState<string | null>(null);
  const [conceptDetail, setConceptDetail] = useState<ConceptDetailResp | null>(null);
  const [nodeContext, setNodeContext] = useState<NodeContext | null>(null);
  const [reviewBusy, setReviewBusy] = useState(false);
  const [decidingMerge, setDecidingMerge] = useState<{ id: string; confirm: boolean } | null>(null);
  const [reviewAllJob, setReviewAllJob] = useState<MergeReviewJob | null>(null);
  const [reviewAllStarting, setReviewAllStarting] = useState(false);
  const [reviewAllRunning, setReviewAllRunning] = useState(false);
  const [rebuildingNotebookIds, setRebuildingNotebookIds] = useState<Set<string>>(new Set());
  const [relinkingNotebookIds, setRelinkingNotebookIds] = useState<Set<string>>(new Set());
  const [buildingKg, setBuildingKg] = useState(false);
  const [trackedKgJobId, setTrackedKgJobId] = useState<string | null>(null);
  const trackedKgJobIdRef = useRef<string | null>(null);
  trackedKgJobIdRef.current = trackedKgJobId;

  const currentOwner = (): KgWorkspaceOwner | null => {
    const owner = ownerRef.current;
    if (!owner || !actorIdRef.current || !notebookIdRef.current) return null;
    if (owner.actorId !== actorIdRef.current || owner.notebookId !== notebookIdRef.current) return null;
    return owner;
  };

  const owns = (owner: KgWorkspaceOwner): boolean => Boolean(
    actorIdRef.current === owner.actorId
    && notebookIdRef.current === owner.notebookId
    && sameKgOwner(ownerRef.current, owner),
  );

  const ownsIdentity = (owner: Pick<KgWorkspaceOwner, "actorId" | "notebookId">): boolean => Boolean(
    actorIdRef.current === owner.actorId
    && notebookIdRef.current === owner.notebookId
    && sameKgIdentity(ownerRef.current, owner),
  );

  const beginOperation = (kind: string): object => {
    const token = {};
    operationTokensRef.current.set(kind, token);
    return token;
  };
  const ownsOperation = (kind: string, token: object): boolean =>
    operationTokensRef.current.get(kind) === token;

  const mergeTombstones = (owner: Pick<KgWorkspaceOwner, "actorId" | "notebookId">): Set<string> =>
    mergeTombstonesRef.current.get(ownerKey(owner)) ?? new Set<string>();

  const filterPendingMerges = (owner: Pick<KgWorkspaceOwner, "actorId" | "notebookId">, rows: PendingMerge[]) =>
    filterPendingMergeTombstones(rows, mergeTombstones(owner));

  const publishError = (owner: KgWorkspaceOwner, error: unknown) => {
    if (owns(owner)) effectsRef.current.reportError(error);
  };

  const clearSearchTimer = () => {
    if (graphSearchTimerRef.current !== null) {
      window.clearTimeout(graphSearchTimerRef.current);
      graphSearchTimerRef.current = null;
    }
  };

  const clearVisibleState = () => {
    clearSearchTimer();
    graphOpenRef.current = false;
    setKnowledgeKind("concept");
    setKnowledge(EMPTY_KNOWLEDGE);
    setKnowledgeTypes([]);
    setKnowledgeStatusFilter("all");
    setKnowledgeTotal({});
    setKnowledgePage({});
    setDuplicates(null);
    setKnowledgeContexts({});
    knowledgeContextRequestsRef.current.clear();
    setKnowledgeBusy(null);
    setSchemaModalOpen(false);
    setSchemas(null);
    setSchemaBusy(false);
    setSchemaView("notebook");
    setGraphOpen(false);
    setAnalysisOpen(false);
    setUnifiedGraph(null);
    setVizBuilding(false);
    setSearch("");
    setSearchHits([]);
    setSearchBusy(false);
    setExpandedNodes([]);
    setExpandedEdges([]);
    setRangeLimit(KG_RANGE_DEFAULT);
    setRangeBusy(false);
    setSelectedTypes([]);
    setPendingMerges([]);
    setUnifiedStatus(null);
    setSelectedNodeId(null);
    setPendingFocusId(null);
    setConceptDetail(null);
    setNodeContext(null);
    setReviewBusy(false);
    setDecidingMerge(null);
    setReviewAllJob(null);
    setReviewAllStarting(false);
    setReviewAllRunning(false);
    setBuildingKg(false);
    setTrackedKgJobId(null);
  };

  const invalidate = () => {
    generationRef.current += 1;
    viewGenerationRef.current += 1;
    ownerRef.current = null;
    pendingNotebookSnapshotRef.current = null;
    knowledgeRequestRef.current += 1;
    typeRequestRef.current += 1;
    duplicateRequestRef.current += 1;
    schemaRequestRef.current += 1;
    graphOpenRequestRef.current += 1;
    graphRangeRequestRef.current += 1;
    graphSearchRequestRef.current += 1;
    graphNodeRequestRef.current += 1;
    clearVisibleState();
    forceOwnerRender((value) => value + 1);
  };

  useEffect(() => {
    if (!actorId || !notebookId) {
      if (ownerRef.current) invalidate();
      return;
    }
    if (transitionSuspendedRef.current) return;
    const current = ownerRef.current;
    if (current && current.actorId === actorId && current.notebookId === notebookId) return;
    const owner: KgWorkspaceOwner = {
      actorId,
      notebookId,
      generation: ++generationRef.current,
      viewGeneration: ++viewGenerationRef.current,
    };
    ownerRef.current = owner;
    clearVisibleState();
    const notebookSnapshot = pendingNotebookSnapshotRef.current;
    pendingNotebookSnapshotRef.current = null;
    if (notebookSnapshot?.id === notebookId && notebookSnapshot.kg_build?.status === "running") {
      setBuildingKg(true);
      setTrackedKgJobId(notebookSnapshot.kg_build.job_id);
    }
    forceOwnerRender((value) => value + 1);
    if (policyRef.current.canWriteKg) {
      void fetchMergeReviewJob(notebookId).then((job) => {
        if (!owns(owner) || !policyRef.current.canWriteKg) return;
        if (shouldResumeReviewAll(job)) {
          setReviewAllJob(job);
          setReviewAllRunning(true);
        }
      }).catch(() => {});
    }
    void Promise.all([
      fetchUnifiedKgRebuildStatus(notebookId).catch(() => null),
      fetchRelinkStatus(notebookId).catch(() => null),
    ]).then(([rebuild, relink]) => {
      if (!owns(owner)) return;
      if (rebuild && (rebuild.running || rebuild.status === "running")) {
        setRebuildingNotebookIds((current) => claimNotebookSlot(current, ownerKey(owner)));
      }
      if (relink && (relink.running || relink.status === "running")) {
        setRelinkingNotebookIds((current) => claimNotebookSlot(current, ownerKey(owner)));
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [actorId, notebookId, ownerVersion]);

  const beginNotebookTransition = (): KgWorkspaceTransition => {
    transitionSuspendedRef.current = true;
    pendingNotebookSnapshotRef.current = null;
    const transition = { generation: ++transitionGenerationRef.current };
    invalidate();
    return transition;
  };

  const finishNotebookTransition = (
    transition: KgWorkspaceTransition,
    notebook?: NotebookSummary | null,
  ) => {
    if (transition.generation !== transitionGenerationRef.current) return;
    pendingNotebookSnapshotRef.current = notebook ?? null;
    transitionSuspendedRef.current = false;
    forceOwnerRender((value) => value + 1);
  };

  const activateActor = (nextActorId: string) => {
    if (!nextActorId || (actorIdRef.current && actorIdRef.current !== nextActorId)) invalidate();
    actorIdRef.current = nextActorId;
  };

  const leaveWorkspace = () => {
    transitionSuspendedRef.current = false;
    transitionGenerationRef.current += 1;
    invalidate();
  };

  const loadKnowledgeFor = async (
    owner: KgWorkspaceOwner,
    request: KnowledgeRequest,
  ): Promise<KnowledgeItem[] | null> => {
    const requestId = ++knowledgeRequestRef.current;
    const status = request.status === "all" ? "" : request.status;
    try {
      const response = await listKnowledge(
        owner.notebookId,
        request.kind,
        status,
        request.page * 50,
        50,
      );
      if (!owns(owner) || requestId !== knowledgeRequestRef.current) return null;
      const items = response.items.map(knowledgeItemFromRecord);
      setKnowledge((current) => ({ ...current, [request.kind]: items }));
      setKnowledgeTotal((current) => ({ ...current, [request.kind]: response.total_count }));
      setKnowledgePage((current) => ({ ...current, [request.kind]: request.page }));
      return items;
    } catch (error) {
      if (owns(owner) && requestId === knowledgeRequestRef.current) {
        effectsRef.current.reportError(error);
      }
      return null;
    }
  };

  const loadTypesFor = async (owner: KgWorkspaceOwner): Promise<KnowledgeTypeCount[] | null> => {
    const requestId = ++typeRequestRef.current;
    try {
      const types = await listKnowledgeTypes(owner.notebookId);
      if (!owns(owner) || requestId !== typeRequestRef.current) return null;
      setKnowledgeTypes(types);
      return types;
    } catch (error) {
      if (owns(owner) && requestId === typeRequestRef.current) {
        effectsRef.current.reportError(error);
      }
      return null;
    }
  };

  const enterKnowledge = async () => {
    const owner = currentOwner();
    if (!owner) return;
    const types = await loadTypesFor(owner);
    if (!types || types.length === 0 || !owns(owner)) return;
    const currentKind = knowledgeKindRef.current;
    const available = types.map((type) => type.object_type);
    if (!available.includes(currentKind)) {
      const nextKind = types[0].object_type;
      setKnowledgeKind(nextKind);
      setKnowledgeStatusFilter("all");
      setDuplicates(null);
      setKnowledgeContexts({});
      knowledgeContextRequestsRef.current.clear();
      await loadKnowledgeFor(owner, { kind: nextKind, status: "all", page: 0 });
    } else if (knowledgeRef.current[currentKind] == null) {
      await loadKnowledgeFor(owner, { kind: currentKind, status: "all", page: 0 });
    }
  };

  const selectKnowledgeKind = (kind: KnowledgeKind) => {
    const owner = currentOwner();
    if (!owner) return;
    setKnowledgeKind(kind);
    setKnowledgeStatusFilter("all");
    setDuplicates(null);
    setKnowledgeContexts({});
    knowledgeContextRequestsRef.current.clear();
    void loadKnowledgeFor(owner, { kind, status: "all", page: 0 });
  };

  const selectKnowledgeStatus = (status: string) => {
    const owner = currentOwner();
    if (!owner) return;
    const kind = knowledgeKindRef.current;
    setKnowledgeStatusFilter(status);
    void loadKnowledgeFor(owner, { kind, status, page: 0 });
  };

  const goToKnowledgePage = (page: number) => {
    const owner = currentOwner();
    if (!owner || !Number.isInteger(page) || page < 0) return;
    void loadKnowledgeFor(owner, {
      kind: knowledgeKindRef.current,
      status: statusFilterRef.current,
      page,
    });
  };

  const refreshKnowledge = () => {
    const owner = currentOwner();
    if (!owner) return;
    void loadKnowledgeFor(owner, {
      kind: knowledgeKindRef.current,
      status: statusFilterRef.current,
      page: 0,
    });
  };

  const invalidateKnowledge = () => {
    knowledgeRequestRef.current += 1;
    typeRequestRef.current += 1;
    duplicateRequestRef.current += 1;
    setKnowledge(EMPTY_KNOWLEDGE);
    setKnowledgeTypes([]);
    setKnowledgeTotal({});
    setKnowledgePage({});
    setDuplicates(null);
    setKnowledgeContexts({});
    knowledgeContextRequestsRef.current.clear();
  };

  const updateKnowledge = async (id: string, patch: { status?: string; owner?: string }) => {
    const owner = currentOwner();
    if (!owner || !policyRef.current.canGovernKnowledge || knowledgeBusy) return;
    const operation = beginOperation("knowledge");
    const kind = knowledgeKindRef.current;
    const status = statusFilterRef.current;
    setKnowledgeBusy(id);
    try {
      await updateKnowledgeRecord(owner.notebookId, id, patch);
      if (!owns(owner) || !ownsOperation("knowledge", operation)
        || !policyRef.current.canGovernKnowledge) return;
      const rows = await loadKnowledgeFor(owner, { kind, status, page: 0 });
      if (rows === null) return;
      if (!owns(owner) || !ownsOperation("knowledge", operation)
        || !policyRef.current.canGovernKnowledge) return;
      const types = await loadTypesFor(owner);
      if (types === null) return;
      if (!owns(owner) || !ownsOperation("knowledge", operation)
        || !policyRef.current.canGovernKnowledge) return;
      await effectsRef.current.refreshCollection(
        () => owns(owner) && ownsOperation("knowledge", operation)
          && policyRef.current.canGovernKnowledge,
      ).catch(() => {});
      if (!owns(owner) || !ownsOperation("knowledge", operation)
        || !policyRef.current.canGovernKnowledge) return;
      await effectsRef.current.refreshNotebook(
        owner.notebookId,
        () => owns(owner) && ownsOperation("knowledge", operation)
          && policyRef.current.canGovernKnowledge,
      ).catch(() => {});
      if (owns(owner) && ownsOperation("knowledge", operation)
        && policyRef.current.canGovernKnowledge) effectsRef.current.notify("知识已更新");
    } catch (error) {
      if (owns(owner) && ownsOperation("knowledge", operation)
        && policyRef.current.canGovernKnowledge) {
        effectsRef.current.reportError(error);
      }
    } finally {
      if (owns(owner) && ownsOperation("knowledge", operation)) setKnowledgeBusy(null);
    }
  };

  const findDuplicates = async () => {
    const owner = currentOwner();
    if (!owner || !policyRef.current.canGovernKnowledge) return;
    const kind = knowledgeKindRef.current;
    const requestId = ++duplicateRequestRef.current;
    try {
      const rows = await findKnowledgeDuplicates(owner.notebookId, kind);
      if (owns(owner) && requestId === duplicateRequestRef.current && knowledgeKindRef.current === kind) {
        setDuplicates(rows);
      }
    } catch (error) {
      if (owns(owner) && requestId === duplicateRequestRef.current
        && knowledgeKindRef.current === kind) {
        effectsRef.current.reportError(error);
      }
    }
  };

  const mergeKnowledge = async (sourceId: string, intoId: string) => {
    const owner = currentOwner();
    if (!owner || !policyRef.current.canGovernKnowledge || knowledgeBusy) return;
    const operation = beginOperation("knowledge");
    const kind = knowledgeKindRef.current;
    const status = statusFilterRef.current;
    setKnowledgeBusy(sourceId);
    try {
      await mergeKnowledgeRecords(owner.notebookId, sourceId, intoId);
      if (!owns(owner) || !ownsOperation("knowledge", operation)
        || !policyRef.current.canGovernKnowledge) return;
      knowledgeRequestRef.current += 1;
      duplicateRequestRef.current += 1;
      const refreshed = await loadKnowledgeFor(owner, { kind, status, page: 0 });
      if (refreshed === null) return;
      if (!owns(owner) || !ownsOperation("knowledge", operation)
        || !policyRef.current.canGovernKnowledge) return;
      const types = await loadTypesFor(owner);
      if (types === null) return;
      if (!owns(owner) || !ownsOperation("knowledge", operation)
        || !policyRef.current.canGovernKnowledge) return;
      const rows = await findKnowledgeDuplicates(owner.notebookId, kind);
      if (owns(owner) && ownsOperation("knowledge", operation)
        && policyRef.current.canGovernKnowledge) {
        setDuplicates(rows);
        effectsRef.current.notify("已合并，原条目已弃用");
      }
    } catch (error) {
      if (owns(owner) && ownsOperation("knowledge", operation)
        && policyRef.current.canGovernKnowledge) {
        effectsRef.current.reportError(error);
      }
    } finally {
      if (owns(owner) && ownsOperation("knowledge", operation)) setKnowledgeBusy(null);
    }
  };

  const loadKnowledgeContext = async (itemId: string) => {
    const owner = currentOwner();
    if (!owner || knowledgeContexts[itemId]) return;
    const kind = knowledgeKindRef.current;
    const request = {};
    knowledgeContextRequestsRef.current.set(itemId, request);
    try {
      const context = await fetchNodeContext(owner.notebookId, itemId);
      if (owns(owner) && knowledgeKindRef.current === kind
        && knowledgeContextRequestsRef.current.get(itemId) === request) {
        setKnowledgeContexts((current) => ({ ...current, [itemId]: context }));
      }
    } catch {
      if (owns(owner) && knowledgeKindRef.current === kind
        && knowledgeContextRequestsRef.current.get(itemId) === request) {
        const item = knowledgeRef.current[kind]?.find((row) => row.id === itemId);
        setKnowledgeContexts((current) => ({
          ...current,
          [itemId]: {
            id: itemId,
            object_type: item?.object_type ?? "",
            name: "",
            section_path: "",
            occurrences: [],
            definition: null,
            steps: null,
          },
        }));
      }
    }
  };

  const loadSchemasFor = async (owner: KgWorkspaceOwner, view: SchemaView): Promise<boolean> => {
    const requestId = ++schemaRequestRef.current;
    try {
      const rows = view === "global"
        ? await listObjectSchemas()
        : await listNotebookObjectSchemas(owner.notebookId);
      if (owns(owner) && requestId === schemaRequestRef.current && schemaViewRef.current === view) {
        setSchemas(rows);
        return true;
      }
    } catch (error) {
      if (owns(owner) && requestId === schemaRequestRef.current && schemaViewRef.current === view) {
        effectsRef.current.reportError(error);
      }
    }
    return false;
  };

  const openSchemas = () => {
    const owner = currentOwner();
    if (!owner) return;
    setSchemaView("notebook");
    setSchemas(null);
    setSchemaModalOpen(true);
    void loadSchemasFor(owner, "notebook");
  };

  const closeSchemas = () => {
    schemaRequestRef.current += 1;
    setSchemaModalOpen(false);
    setSchemas(null);
    setSchemaBusy(false);
  };

  const selectSchemaView = (view: SchemaView) => {
    const owner = currentOwner();
    if (!owner || (view === "global" && !policyRef.current.canManageGlobalSchemas)) return;
    setSchemaView(view);
    setSchemas(null);
    void loadSchemasFor(owner, view);
  };

  const canWriteSchema = (view: SchemaView): boolean => view === "global"
    ? policyRef.current.canManageGlobalSchemas
    : policyRef.current.canManageNotebookSchemas;

  const mutateSchema = async (
    mutation: (owner: KgWorkspaceOwner, view: SchemaView) => Promise<void>,
    success: (view: SchemaView) => string,
  ) => {
    const owner = currentOwner();
    const view = schemaViewRef.current;
    if (!owner || !schemaModalOpen || !canWriteSchema(view) || schemaBusy) return;
    const operation = beginOperation("schema");
    setSchemaBusy(true);
    try {
      await mutation(owner, view);
      if (!owns(owner) || !ownsOperation("schema", operation)
        || schemaViewRef.current !== view || !canWriteSchema(view)) return;
      schemaRequestRef.current += 1;
      const reloaded = await loadSchemasFor(owner, view);
      if (owns(owner) && ownsOperation("schema", operation)
        && reloaded
        && schemaViewRef.current === view && canWriteSchema(view)) {
        effectsRef.current.notify(success(view));
      }
    } catch (error) {
      if (owns(owner) && ownsOperation("schema", operation)
        && schemaViewRef.current === view && canWriteSchema(view)) {
        effectsRef.current.reportError(error);
      }
    } finally {
      if (owns(owner) && ownsOperation("schema", operation)) setSchemaBusy(false);
    }
  };

  const patchSchema = (objectType: string, patch: Partial<ObjectSchema> & { status?: string }) =>
    mutateSchema(async (owner, view) => {
      if (view === "global") await updateObjectSchema(objectType, patch);
      else await updateNotebookObjectSchema(owner.notebookId, objectType, patch);
    }, () => "类型已更新");

  const createSchema = (payload: {
    object_type: string;
    plural: string;
    label: string;
    fields: string[];
    primary: string;
    list_fields: string[];
    description: string;
  }) => mutateSchema(async (owner, view) => {
    if (view === "global") await createObjectSchema(payload);
    else await createNotebookObjectSchema(owner.notebookId, payload);
  }, () => "已新增类型");

  const deleteSchema = (objectType: string) => mutateSchema(async (owner, view) => {
    if (view === "global") await deleteObjectSchema(objectType);
    else await deleteNotebookObjectSchema(owner.notebookId, objectType);
  }, (view) => view === "notebook" ? "类型已更新" : "类型已删除");

  const induceSchemas = async () => {
    const owner = currentOwner();
    if (!owner || !schemaModalOpen || !policyRef.current.canManageNotebookSchemas || schemaBusy) return;
    const operation = beginOperation("schema");
    setSchemaBusy(true);
    try {
      const proposals = await proposeObjectSchemas(owner.notebookId);
      if (!owns(owner) || !ownsOperation("schema", operation)
        || schemaViewRef.current !== "notebook" || !policyRef.current.canManageNotebookSchemas) return;
      schemaRequestRef.current += 1;
      const reloaded = await loadSchemasFor(owner, "notebook");
      if (owns(owner) && ownsOperation("schema", operation)
        && reloaded
        && schemaViewRef.current === "notebook" && policyRef.current.canManageNotebookSchemas) {
        effectsRef.current.notify(
        proposals.length
          ? `归纳出 ${proposals.length} 个候选类型`
          : "未发现可补充的新类型（或模型服务暂不可用）",
        );
      }
    } catch (error) {
      if (owns(owner) && ownsOperation("schema", operation)
        && schemaViewRef.current === "notebook" && policyRef.current.canManageNotebookSchemas) {
        effectsRef.current.reportError(error);
      }
    } finally {
      if (owns(owner) && ownsOperation("schema", operation)) setSchemaBusy(false);
    }
  };

  const mergedGraph = useMemo((): UnifiedGraphResp | null => {
    if (!unifiedGraph) return null;
    if (expandedNodes.length === 0 && expandedEdges.length === 0) return unifiedGraph;
    const existingNodeIds = new Set(unifiedGraph.nodes.map((node) => node.id));
    const existingEdgeKeys = new Set(unifiedGraph.edges.map((edge) =>
      `${edge.source_object_id}→${edge.target_object_id}→${edge.edge_type}`));
    return {
      ...unifiedGraph,
      nodes: [...unifiedGraph.nodes, ...expandedNodes.filter((node) => !existingNodeIds.has(node.id))],
      edges: [...unifiedGraph.edges, ...expandedEdges.filter((edge) =>
        !existingEdgeKeys.has(`${edge.source_object_id}→${edge.target_object_id}→${edge.edge_type}`))],
    };
  }, [unifiedGraph, expandedNodes, expandedEdges]);

  const openGraph = async (
    targetNodeId?: string,
    sourceNotebookId?: string,
  ) => {
    const owner = currentOwner();
    if (!owner) return;
    const requestId = ++graphOpenRequestRef.current;
    const graphOwner = owner;
    graphOpenRef.current = true;
    setGraphOpen(true);
    setAnalysisOpen(false);
    setSelectedNodeId(null);
    setConceptDetail(null);
    setNodeContext(null);
    setSearch("");
    setSearchHits([]);
    setSearchBusy(false);
    setExpandedNodes([]);
    setExpandedEdges([]);
    setSelectedTypes([]);
    setRangeLimit(KG_RANGE_DEFAULT);
    try {
      const [graph, merges, status] = await Promise.all([
        fetchUnifiedGraph(graphOwner.notebookId, KG_RANGE_DEFAULT),
        fetchPendingMerges(graphOwner.notebookId),
        fetchUnifiedKgStatus(graphOwner.notebookId),
      ]);
      if (!owns(graphOwner) || requestId !== graphOpenRequestRef.current || !graphOpenRef.current) return;
      let neighborhood = null;
      if (targetNodeId) {
        try {
          neighborhood = await fetchKgNeighbors(
            graphOwner.notebookId,
            targetNodeId,
            50,
            sourceNotebookId || graphOwner.notebookId,
          );
        } catch { /* core graph remains usable */ }
      }
      if (!owns(graphOwner) || requestId !== graphOpenRequestRef.current || !graphOpenRef.current) return;
      const focus = prepareKgFocus(graph, targetNodeId, neighborhood);
      const resolvedSourceNotebookId = neighborhood?.source_notebook_id
        || sourceNotebookId
        || graphOwner.notebookId;
      const notebookMap = new Map<string, string>();
      for (const node of neighborhood?.nodes ?? []) notebookMap.set(node.id, resolvedSourceNotebookId);
      if (focus.focusId) notebookMap.set(focus.focusId, resolvedSourceNotebookId);
      nodeNotebookRef.current = notebookMap;
      const contextMap = new Map<string, string>();
      if (focus.focusId && focus.contextObjectId) contextMap.set(focus.focusId, focus.contextObjectId);
      nodeContextObjectRef.current = contextMap;
      setUnifiedGraph(graph);
      setPendingMerges(filterPendingMerges(graphOwner, merges));
      setUnifiedStatus(status);
      setExpandedNodes(focus.expandedNodes);
      setExpandedEdges(focus.expandedEdges);
      setPendingFocusId(focus.focusId);
      setVizBuilding(Boolean(graph.viz_building));
      if (targetNodeId && neighborhood?.locating_unavailable) {
        effectsRef.current.notify("图谱索引正在构建，暂时无法定位该引用节点；完成后请重试");
      } else if (targetNodeId && !focus.focusId) {
        effectsRef.current.notify("知识图谱已打开，但引用节点定位失败，请重试");
      }
    } catch (error) {
      if (owns(graphOwner) && requestId === graphOpenRequestRef.current && graphOpenRef.current) {
        effectsRef.current.reportError(error);
      }
    }
  };

  const closeGraph = () => {
    graphOpenRequestRef.current += 1;
    graphRangeRequestRef.current += 1;
    graphSearchRequestRef.current += 1;
    graphNodeRequestRef.current += 1;
    clearSearchTimer();
    graphOpenRef.current = false;
    setGraphOpen(false);
    setAnalysisOpen(false);
    setSearchBusy(false);
  };

  const openAnalysis = () => { if (graphOpenRef.current && currentOwner()) setAnalysisOpen(true); };
  const closeAnalysis = () => setAnalysisOpen(false);

  const updateGraphSearch = (value: string) => {
    const owner = currentOwner();
    if (!owner || !graphOpenRef.current) return;
    setSearch(value);
    clearSearchTimer();
    const requestId = ++graphSearchRequestRef.current;
    if (!value.trim()) {
      setSearchHits([]);
      setSearchBusy(false);
      return;
    }
    setSearchBusy(true);
    const query = value.trim();
    graphSearchTimerRef.current = window.setTimeout(async () => {
      graphSearchTimerRef.current = null;
      try {
        const response = await fetchKgSearch(owner.notebookId, query);
        if (owns(owner) && graphOpenRef.current && requestId === graphSearchRequestRef.current) {
          setSearchHits(response.hits);
        }
      } catch (error) {
        if (owns(owner) && requestId === graphSearchRequestRef.current) {
          effectsRef.current.reportError(error);
          setSearchHits([]);
        }
      } finally {
        if (owns(owner) && requestId === graphSearchRequestRef.current) setSearchBusy(false);
      }
    }, KG_SEARCH_DEBOUNCE_MS);
  };

  const changeRange = async (limit: number) => {
    const owner = currentOwner();
    if (!owner || !graphOpenRef.current) return;
    const requestId = ++graphRangeRequestRef.current;
    setRangeLimit(limit);
    setRangeBusy(true);
    try {
      const graph = await fetchUnifiedGraph(owner.notebookId, limit);
      if (owns(owner) && graphOpenRef.current && requestId === graphRangeRequestRef.current) {
        setUnifiedGraph(graph);
        setVizBuilding(Boolean(graph.viz_building));
      }
    } catch (error) {
      if (owns(owner) && graphOpenRef.current && requestId === graphRangeRequestRef.current) {
        effectsRef.current.reportError(error);
      }
    } finally {
      if (owns(owner) && requestId === graphRangeRequestRef.current) setRangeBusy(false);
    }
  };

  const toggleType = (type: string) => {
    if (!currentOwner() || !graphOpenRef.current) return;
    const allTypes = Array.from(new Set((mergedGraph?.nodes ?? []).map((node) => node.object_type)));
    if (allTypes.length === 0) return;
    setSelectedTypes((current) => {
      const next = current.includes(type)
        ? current.filter((item) => item !== type)
        : [...current, type];
      return next.length === allTypes.length ? [] : next;
    });
  };

  const clearTypes = () => { if (currentOwner()) setSelectedTypes([]); };

  const selectNode = async (nodeId: string) => {
    const owner = currentOwner();
    if (!owner || !graphOpenRef.current) return;
    const requestId = ++graphNodeRequestRef.current;
    const sourceNotebookId = nodeNotebookRef.current.get(nodeId) || owner.notebookId;
    setSelectedNodeId(nodeId);
    setConceptDetail(null);
    setNodeContext(null);
    effectsRef.current.focusGraphNode(nodeId);
    let resolvedSourceNotebookId = sourceNotebookId;
    try {
      const neighbors = await fetchKgNeighbors(owner.notebookId, nodeId, 50, sourceNotebookId);
      if (!owns(owner) || !graphOpenRef.current || requestId !== graphNodeRequestRef.current
        || selectedNodeIdRef.current !== nodeId) return;
      resolvedSourceNotebookId = neighbors.source_notebook_id || sourceNotebookId;
      if (neighbors.focus_id && neighbors.focus_object_id
        && (!nodeContextObjectRef.current.has(neighbors.focus_id)
          || neighbors.focus_object_id !== neighbors.focus_id)) {
        nodeContextObjectRef.current.set(neighbors.focus_id, neighbors.focus_object_id);
      }
      for (const node of neighbors.nodes) {
        if (!nodeNotebookRef.current.has(node.id)) nodeNotebookRef.current.set(node.id, resolvedSourceNotebookId);
      }
      setExpandedNodes((current) => {
        const existing = new Set(current.map((node) => node.id));
        const fresh = neighbors.nodes.filter((node) => !existing.has(node.id));
        return fresh.length ? [...current, ...fresh] : current;
      });
      setExpandedEdges((current) => {
        const existing = new Set(current.map((edge) =>
          `${edge.source_object_id}→${edge.target_object_id}→${edge.edge_type}`));
        const fresh = neighbors.edges.filter((edge) =>
          !existing.has(`${edge.source_object_id}→${edge.target_object_id}→${edge.edge_type}`));
        return fresh.length ? [...current, ...fresh] : current;
      });
    } catch { /* neighbor expansion is best effort */ }
    if (!owns(owner) || requestId !== graphNodeRequestRef.current || selectedNodeIdRef.current !== nodeId) return;
    const selected = mergedGraph?.nodes.find((node) => node.id === nodeId);
    if (selected?.object_type === "concept") {
      try {
        const detail = await fetchConceptDetail(owner.notebookId, nodeId, resolvedSourceNotebookId);
        if (owns(owner) && requestId === graphNodeRequestRef.current && selectedNodeIdRef.current === nodeId) {
          setConceptDetail(detail);
        }
      } catch (error) {
        if (owns(owner) && requestId === graphNodeRequestRef.current) publishError(owner, error);
      }
    }
    if (!owns(owner) || requestId !== graphNodeRequestRef.current || selectedNodeIdRef.current !== nodeId) return;
    const contextObjectId = nodeContextObjectRef.current.get(nodeId) || nodeId;
    try {
      const context = await fetchNodeContext(owner.notebookId, contextObjectId, resolvedSourceNotebookId);
      if (owns(owner) && requestId === graphNodeRequestRef.current && selectedNodeIdRef.current === nodeId) {
        setNodeContext(context);
      }
    } catch { /* node context is best effort */ }
  };

  useEffect(() => {
    if (!pendingFocusId || !graphOpen || !unifiedGraph) return;
    const focusId = pendingFocusId;
    setPendingFocusId(null);
    void selectNode(focusId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pendingFocusId, graphOpen, unifiedGraph]);

  useEffect(() => {
    const owner = currentOwner();
    if (!owner || !graphOpen || !vizBuilding) return;
    let stopped = false;
    let inFlight = false;
    const timer = window.setInterval(async () => {
      if (stopped || inFlight || !owns(owner)) return;
      inFlight = true;
      try {
        const graph = await fetchUnifiedGraph(owner.notebookId, rangeLimitRef.current);
        if (!owns(owner) || !graphOpenRef.current || stopped || graph.viz_building) return;
        setUnifiedGraph(graph);
        setVizBuilding(false);
        try {
          const status = await fetchUnifiedKgStatus(owner.notebookId);
          if (owns(owner) && graphOpenRef.current && !stopped) setUnifiedStatus(status);
        } catch { /* status is best effort */ }
      } catch { /* transient error; keep polling */ }
      finally { inFlight = false; }
    }, KG_BACKGROUND_POLL_MS);
    const cap = window.setTimeout(() => {
      if (!stopped && owns(owner)) {
        stopped = true;
        window.clearInterval(timer);
        setVizBuilding(false);
        effectsRef.current.notify("图谱索引仍在后台构建，请稍后重新打开查看");
      }
    }, KG_BACKGROUND_POLL_CAP_MS);
    return () => {
      stopped = true;
      window.clearInterval(timer);
      window.clearTimeout(cap);
    };
  }, [graphOpen, vizBuilding]);

  useEffect(() => {
    const owner = currentOwner();
    if (!owner || !reviewAllRunning || !policyRef.current.canWriteKg) return;
    let stopped = false;
    let inFlight = false;
    const timer = window.setInterval(async () => {
      if (stopped || inFlight || !owns(owner) || !policyRef.current.canWriteKg) return;
      inFlight = true;
      try {
        const job = await fetchMergeReviewJob(owner.notebookId);
        if (!owns(owner) || stopped || !policyRef.current.canWriteKg) return;
        setReviewAllJob(job);
        if (job.status !== "running") {
          stopped = true;
          window.clearInterval(timer);
          setReviewAllRunning(false);
          const [merges, status] = await Promise.all([
            fetchPendingMerges(owner.notebookId),
            fetchUnifiedKgStatus(owner.notebookId),
          ]);
          if (!owns(owner) || !policyRef.current.canWriteKg) return;
          setPendingMerges(filterPendingMerges(owner, merges));
          setUnifiedStatus(status);
          effectsRef.current.notify(job.status === "failed"
            ? `全部自动判重中止：${toUserMessage(job.error ? new Error(job.error) : null, "出了点问题")}（已处理 ${job.done}）`
            : `全部自动判重完成：已处理 ${job.done} 项`);
        }
      } catch { /* transient error; keep polling */ }
      finally { inFlight = false; }
    }, KG_BACKGROUND_POLL_MS);
    const cap = window.setTimeout(() => {
      if (!stopped && owns(owner) && policyRef.current.canWriteKg) {
        stopped = true;
        window.clearInterval(timer);
        setReviewAllRunning(false);
        effectsRef.current.notify("自动判重仍在后台进行，请稍后查看");
      }
    }, KG_BACKGROUND_POLL_CAP_MS);
    return () => {
      stopped = true;
      window.clearInterval(timer);
      window.clearTimeout(cap);
    };
  }, [reviewAllRunning, ownerVersion, policy.canWriteKg]);

  const reviewPendingMerges = async () => {
    const owner = currentOwner();
    if (!owner || !policyRef.current.canWriteKg || reviewBusy) return;
    const operation = beginOperation("review");
    setReviewBusy(true);
    effectsRef.current.notify("正在自动判重（约 1 分钟，请稍候）…");
    try {
      const summary = await reviewMerges(owner.notebookId);
      if (!owns(owner) || !ownsOperation("review", operation)
        || !policyRef.current.canWriteKg) return;
      effectsRef.current.notify(`已判重 ${summary.reviewed} 项：合并 ${summary.confirmed}，分开 ${summary.rejected}，保留 ${summary.unsure}`);
      const [merges, status] = await Promise.all([
        fetchPendingMerges(owner.notebookId),
        fetchUnifiedKgStatus(owner.notebookId),
      ]);
      if (owns(owner) && ownsOperation("review", operation) && policyRef.current.canWriteKg) {
        setPendingMerges(filterPendingMerges(owner, merges));
        setUnifiedStatus(status);
      }
    } catch (error) {
      if (owns(owner) && ownsOperation("review", operation) && policyRef.current.canWriteKg) {
        effectsRef.current.reportError(error);
      }
    } finally {
      if (owns(owner) && ownsOperation("review", operation)) setReviewBusy(false);
    }
  };

  const reviewAllMerges = async () => {
    const owner = currentOwner();
    if (!owner || !policyRef.current.canWriteKg || reviewAllStarting || reviewAllRunning) return;
    const operation = beginOperation("review-all");
    setReviewAllStarting(true);
    try {
      await reviewAllMergesRequest(owner.notebookId);
      if (!owns(owner) || !ownsOperation("review-all", operation)
        || !policyRef.current.canWriteKg) return;
      setReviewAllJob({ status: "running", total: pendingMerges.length, done: 0, error: "" });
      setReviewAllRunning(true);
    } catch (error) {
      if (owns(owner) && ownsOperation("review-all", operation) && policyRef.current.canWriteKg) {
        effectsRef.current.reportError(error);
      }
    } finally {
      if (owns(owner) && ownsOperation("review-all", operation)) setReviewAllStarting(false);
    }
  };

  const recordMergeDecision = (owner: KgWorkspaceOwner, candidate: PendingMerge) => {
    const key = ownerKey(owner);
    const tombstones = new Set(mergeTombstonesRef.current.get(key) ?? []);
    for (const value of pendingMergeTombstoneKeys(candidate)) tombstones.add(value);
    mergeTombstonesRef.current.set(key, tombstones);
    if (ownsIdentity(owner)) setPendingMerges((current) => withoutDecidedMerge(current, candidate));
  };

  const decideMerge = async (candidate: PendingMerge, confirm: boolean) => {
    const owner = currentOwner();
    if (!owner || !policyRef.current.canWriteKg || decidingMerge
      || rebuildingNotebookIds.has(ownerKey(owner))) return;
    const operation = beginOperation("merge-decision");
    setDecidingMerge({ id: candidate.id, confirm });
    try {
      if (confirm) await confirmMerge(owner.notebookId, candidate.id);
      else await rejectMerge(owner.notebookId, candidate.id);
      recordMergeDecision(owner, candidate);
      if (!owns(owner) || !ownsOperation("merge-decision", operation)
        || !policyRef.current.canWriteKg) return;
      if (confirm) await launchRebuild(owner, { allowClaimed: true, decision: true });
      if (!owns(owner) || !ownsOperation("merge-decision", operation)) return;
      const merges = await fetchPendingMerges(owner.notebookId);
      if (!owns(owner) || !ownsOperation("merge-decision", operation)) return;
      setPendingMerges(filterPendingMerges(owner, merges));
      const selection = selectedNodeIdRef.current;
      const selected = selection ? mergedGraph?.nodes.find((node) => node.id === selection) : null;
      if (selected?.object_type === "concept") {
        const detail = await fetchConceptDetail(owner.notebookId, selected.id).catch(() => null);
        if (owns(owner) && ownsOperation("merge-decision", operation)
          && selectedNodeIdRef.current === selection) setConceptDetail(detail);
      } else {
        setConceptDetail(null);
      }
      if (!selected) setNodeContext(null);
    } catch (error) {
      if (owns(owner) && ownsOperation("merge-decision", operation)
        && policyRef.current.canWriteKg) effectsRef.current.reportError(error);
    } finally {
      if (owns(owner) && ownsOperation("merge-decision", operation)) setDecidingMerge(null);
    }
  };

  const refreshAfterRelink = async () => {
    const owner = currentOwner();
    if (!owner) return;
    try {
      const [graph, status] = await Promise.all([
        fetchUnifiedGraph(owner.notebookId, rangeLimitRef.current),
        fetchUnifiedKgStatus(owner.notebookId),
      ]);
      if (!owns(owner)) return;
      setUnifiedGraph(graph);
      setExpandedNodes([]);
      setExpandedEdges([]);
      setUnifiedStatus(status);
      setVizBuilding(Boolean(graph.viz_building));
    } catch (error) {
      publishError(owner, error);
    }
  };

  const refreshAfterRebuild = async () => {
    const owner = currentOwner();
    if (!owner) return;
    const selection = selectedNodeIdRef.current;
    try {
      const [graph, merges, status] = await Promise.all([
        fetchUnifiedGraph(owner.notebookId, rangeLimitRef.current),
        fetchPendingMerges(owner.notebookId),
        fetchUnifiedKgStatus(owner.notebookId),
      ]);
      if (!owns(owner)) return;
      setUnifiedGraph(graph);
      setExpandedNodes([]);
      setExpandedEdges([]);
      setPendingMerges(filterPendingMerges(owner, merges));
      setUnifiedStatus(status);
      setVizBuilding(Boolean(graph.viz_building));
      const selected = selection ? graph.nodes.find((node) => node.id === selection) : null;
      if (selected?.object_type === "concept") {
        const detail = await fetchConceptDetail(owner.notebookId, selected.id).catch(() => null);
        if (owns(owner) && selectedNodeIdRef.current === selection) setConceptDetail(detail);
      } else {
        setConceptDetail(null);
      }
      if (!selected) setNodeContext(null);
    } catch (error) {
      publishError(owner, error);
    }
  };

  const maintenanceOwnerKey = (owner: Pick<KgWorkspaceOwner, "actorId" | "notebookId">) =>
    ownerKey(owner);
  const maintenanceJobKey = (
    owner: Pick<KgWorkspaceOwner, "actorId" | "notebookId">,
    kind: "rebuild" | "relink",
  ) => `${maintenanceOwnerKey(owner)}\0${kind}`;

  const adoptRunningMaintenance = async (
    owner: KgWorkspaceOwner,
  ): Promise<"adopted" | "idle" | "unknown"> => {
    const [rebuildResult, relinkResult] = await Promise.allSettled([
      fetchUnifiedKgRebuildStatus(owner.notebookId),
      fetchRelinkStatus(owner.notebookId),
    ]);
    if (!ownsIdentity(owner)) return "unknown";
    const key = maintenanceOwnerKey(owner);
    let rebuildRunning = false;
    if (rebuildResult.status === "fulfilled") {
      rebuildRunning = Boolean(
        rebuildResult.value.running || rebuildResult.value.status === "running",
      );
      setRebuildingNotebookIds((current) => rebuildRunning
        ? claimNotebookSlot(current, key)
        : releaseNotebookClaim(current, key));
    }
    let relinkRunning = false;
    if (relinkResult.status === "fulfilled") {
      relinkRunning = Boolean(
        relinkResult.value.running || relinkResult.value.status === "running",
      );
      setRelinkingNotebookIds((current) => relinkRunning
        ? claimNotebookSlot(current, key)
        : releaseNotebookClaim(current, key));
    }
    if (rebuildRunning || relinkRunning) return "adopted";
    if (rebuildResult.status === "rejected" || relinkResult.status === "rejected") return "unknown";
    return "idle";
  };

  const startRelink = async () => {
    const owner = currentOwner();
    if (!owner || !policyRef.current.canWriteKg) return;
    const key = maintenanceOwnerKey(owner);
    const jobKey = maintenanceJobKey(owner, "relink");
    if (relinkingNotebookIds.has(key) || rebuildingNotebookIds.has(key) || buildingKg
      || submittingMaintenanceRef.current.has(jobKey)) return;
    setRelinkingNotebookIds((current) => claimNotebookSlot(current, key));
    expectedMaintenanceJobRef.current.delete(jobKey);
    for (const attempt of [0, 1]) {
      if (!owns(owner) || !policyRef.current.canWriteKg) {
        setRelinkingNotebookIds((current) => releaseNotebookClaim(current, key));
        return;
      }
      submittingMaintenanceRef.current.add(jobKey);
      try {
        const started = await relinkKg(owner.notebookId);
        expectedMaintenanceJobRef.current.set(jobKey, started.job_id);
        if (owns(owner)) effectsRef.current.notify("已开始补上关联；完成后会自动更新");
        return;
      } catch (error) {
        if (httpErrorStatus(error) === 409) {
          if (!owns(owner) || !policyRef.current.canWriteKg) {
            setRelinkingNotebookIds((current) => releaseNotebookClaim(current, key));
            return;
          }
          const verdict = await adoptRunningMaintenance(owner);
          if (verdict !== "idle") return;
          if (attempt === 0) continue;
          if (owns(owner)) effectsRef.current.notify("当前有其他整理任务刚结束，请再点一次");
          setRelinkingNotebookIds((current) => releaseNotebookClaim(current, key));
          return;
        }
        if (owns(owner) && policyRef.current.canWriteKg) effectsRef.current.reportError(error);
        setRelinkingNotebookIds((current) => releaseNotebookClaim(current, key));
        return;
      } finally {
        submittingMaintenanceRef.current.delete(jobKey);
      }
    }
  };

  const launchRebuild = async (
    owner: KgWorkspaceOwner,
    options: { allowClaimed?: boolean; decision?: boolean; pendingRetry?: boolean } = {},
  ): Promise<"started" | "adopted" | "waiting" | "denied" | "failed"> => {
    if (!owns(owner) || !policyRef.current.canWriteKg) return "denied";
    const key = maintenanceOwnerKey(owner);
    const jobKey = maintenanceJobKey(owner, "rebuild");
    if (!options.allowClaimed
      && (rebuildingNotebookIds.has(key) || relinkingNotebookIds.has(key) || buildingKg
        || submittingMaintenanceRef.current.has(jobKey))) return "failed";
    setRebuildingNotebookIds((current) => claimNotebookSlot(current, key));
    expectedMaintenanceJobRef.current.delete(jobKey);
    for (const attempt of [0, 1]) {
      if (!owns(owner) || !policyRef.current.canWriteKg) {
        setRebuildingNotebookIds((current) => releaseNotebookClaim(current, key));
        return "denied";
      }
      submittingMaintenanceRef.current.add(jobKey);
      try {
        const started = await rebuildUnifiedKg(owner.notebookId);
        expectedMaintenanceJobRef.current.set(jobKey, started.job_id);
        pendingRebuildRef.current.delete(key);
        if (owns(owner) && !options.decision) {
          effectsRef.current.notify("已开始重新合并；完成后会自动更新");
        }
        return "started";
      } catch (error) {
        if (httpErrorStatus(error) === 409) {
          if (options.decision) {
            pendingRebuildRef.current.add(key);
            if (!options.pendingRetry && owns(owner)) {
              effectsRef.current.notify("合并已记录，将在当前任务完成后自动重新合并");
            }
          }
          // The bounded rebuild poll already owns retry cadence for a pending
          // decision. A 409 here means the shared maintenance slot is still
          // occupied: retain the claim/marker and let the next 3s tick retry.
          // Do not add the two adoption-status reads used by a fresh command;
          // that would change the established retry request budget.
          if (options.pendingRetry) return "waiting";
          if (!owns(owner) || !policyRef.current.canWriteKg) {
            setRebuildingNotebookIds((current) => releaseNotebookClaim(current, key));
            return "denied";
          }
          const verdict = await adoptRunningMaintenance(owner);
          if (options.decision && verdict !== "idle") {
            setRebuildingNotebookIds((current) => claimNotebookSlot(current, key));
          }
          if (verdict !== "idle") return "adopted";
          if (attempt === 0) continue;
          if (owns(owner)) effectsRef.current.notify("当前有其他整理任务刚结束，请再点一次");
          setRebuildingNotebookIds((current) => releaseNotebookClaim(current, key));
          return "failed";
        }
        if (owns(owner) && policyRef.current.canWriteKg) effectsRef.current.reportError(error);
        if (options.pendingRetry) return "waiting";
        setRebuildingNotebookIds((current) => releaseNotebookClaim(current, key));
        return "failed";
      } finally {
        submittingMaintenanceRef.current.delete(jobKey);
      }
    }
    return "failed";
  };

  const startRebuild = async () => {
    const owner = currentOwner();
    if (!owner) return;
    await launchRebuild(owner);
  };

  useEffect(() => {
    const owner = currentOwner();
    if (!owner) return;
    const key = maintenanceOwnerKey(owner);
    if (!relinkingNotebookIds.has(key)) return;
    let stopped = false;
    let settled = false;
    let inFlight = false;
    let attempts = 0;
    let mismatchStreak = 0;
    const settle = async (outcome: ReturnType<typeof relinkPollOutcome>) => {
      if (outcome.toast && owns(owner)) effectsRef.current.notify(outcome.toast);
      if (outcome.refresh && owns(owner)) await refreshAfterRelink();
      expectedMaintenanceJobRef.current.delete(maintenanceJobKey(owner, "relink"));
      setRelinkingNotebookIds((current) => releaseNotebookClaim(current, key));
    };
    const timer = window.setInterval(async () => {
      if (stopped || settled || inFlight) return;
      attempts += 1;
      if (attempts > RELINK_POLL_MAX_ATTEMPTS) {
        settled = true;
        window.clearInterval(timer);
        await settle(RELINK_POLL_TIMED_OUT);
        return;
      }
      inFlight = true;
      try {
        const status = await fetchRelinkStatus(owner.notebookId);
        if (stopped || settled || !ownsIdentity(owner)) return;
        const outcome = relinkPollOutcome(status);
        if (!outcome.done) { mismatchStreak = 0; return; }
        const jobKey = maintenanceJobKey(owner, "relink");
        if (submittingMaintenanceRef.current.has(jobKey)) return;
        const expected = expectedMaintenanceJobRef.current.get(jobKey);
        if (expected && status.job_id !== expected) {
          mismatchStreak += 1;
          if (mismatchStreak < MAINTENANCE_JOB_MISMATCH_SETTLE_STREAK) return;
        }
        settled = true;
        window.clearInterval(timer);
        await settle(outcome);
      } catch { /* transient status error; retain the claim */ }
      finally { inFlight = false; }
    }, KG_MAINTENANCE_POLL_MS);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [relinkingNotebookIds, ownerVersion]);

  useEffect(() => {
    const owner = currentOwner();
    if (!owner) return;
    const key = maintenanceOwnerKey(owner);
    if (!rebuildingNotebookIds.has(key)) return;
    let stopped = false;
    let settled = false;
    let inFlight = false;
    let attempts = 0;
    let mismatchStreak = 0;
    let lastTerminalToastReceipt: string | null = null;
    const settle = async (
      outcome: ReturnType<typeof rebuildPollOutcome>,
      terminalReceipt: string,
    ) => {
      if (outcome.toast && owns(owner) && lastTerminalToastReceipt !== terminalReceipt) {
        lastTerminalToastReceipt = terminalReceipt;
        effectsRef.current.notify(outcome.toast);
      }
      if (pendingRebuildRef.current.has(key) && attempts <= REBUILD_POLL_MAX_ATTEMPTS) {
        try {
          const launch = await launchRebuild(owner, {
            allowClaimed: true,
            decision: true,
            pendingRetry: true,
          });
          if (launch === "started" || launch === "adopted" || launch === "waiting") {
            settled = false;
            if (launch === "started") attempts = 0;
            mismatchStreak = 0;
            return false;
          }
        } catch { /* the explicit pending marker remains retryable */ }
      }
      if (outcome.refresh && owns(owner)) await refreshAfterRebuild();
      expectedMaintenanceJobRef.current.delete(maintenanceJobKey(owner, "rebuild"));
      setRebuildingNotebookIds((current) => releaseNotebookClaim(current, key));
      return true;
    };
    const timer = window.setInterval(async () => {
      if (stopped || settled || inFlight) return;
      attempts += 1;
      if (attempts > REBUILD_POLL_MAX_ATTEMPTS) {
        settled = true;
        window.clearInterval(timer);
        await settle(REBUILD_POLL_TIMED_OUT, "timeout");
        return;
      }
      inFlight = true;
      try {
        const status = await fetchUnifiedKgRebuildStatus(owner.notebookId);
        if (stopped || settled || !ownsIdentity(owner)) return;
        const outcome = rebuildPollOutcome(status);
        if (!outcome.done) { mismatchStreak = 0; return; }
        const jobKey = maintenanceJobKey(owner, "rebuild");
        if (submittingMaintenanceRef.current.has(jobKey)) return;
        const expected = expectedMaintenanceJobRef.current.get(jobKey);
        if (expected && status.job_id !== expected) {
          mismatchStreak += 1;
          if (mismatchStreak < MAINTENANCE_JOB_MISMATCH_SETTLE_STREAK) return;
        }
        settled = true;
        const finished = await settle(outcome, `${status.job_id}\0${status.status}`);
        if (finished) window.clearInterval(timer);
      } catch { /* transient status error; retain the claim */ }
      finally { inFlight = false; }
    }, KG_MAINTENANCE_POLL_MS);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [rebuildingNotebookIds, ownerVersion]);

  const observeNotebook = (notebook: NotebookSummary | null) => {
    const owner = currentOwner();
    if (!owner || !notebook || notebook.id !== owner.notebookId) return;
    if (notebook.kg_build?.status === "running") {
      setBuildingKg(true);
      setTrackedKgJobId(notebook.kg_build.job_id);
    } else {
      setBuildingKg(false);
      setTrackedKgJobId(null);
    }
  };

  const observeKgBuild = (
    job: NotebookSummary["kg_build"],
    building: boolean,
  ) => {
    if (!currentOwner()) return;
    setBuildingKg(building || job?.status === "running");
    setTrackedKgJobId(job?.status === "running" ? job.job_id : null);
  };

  const startKgBuild = async (rebuild = false) => {
    const owner = currentOwner();
    if (!owner || !policyRef.current.canWriteKg || buildingKg) return;
    const operation = beginOperation("kg-build");
    setBuildingKg(true);
    try {
      const started = rebuild ? await rebuildKg(owner.notebookId) : await buildKg(owner.notebookId);
      if (!owns(owner) || !ownsOperation("kg-build", operation)) return;
      setTrackedKgJobId(started.job_id);
      effectsRef.current.notify(rebuild
        ? "已开始全部重新分析；完成后会自动更新"
        : "已开始整理知识图谱；完成后会自动更新");
      const notebook = await effectsRef.current.refreshNotebook(
        owner.notebookId,
        () => owns(owner) && ownsOperation("kg-build", operation),
      ).catch(() => null);
      if (!notebook || !owns(owner)) return;
      const tracked = reconcileTrackedKgPoll(started.job_id, notebook.kg_build);
      if (tracked.terminal) {
        setBuildingKg(false);
        setTrackedKgJobId(null);
        const message = kgBuildTerminalToast(notebook.kg_build);
        if (message) effectsRef.current.notify(message);
      }
    } catch (error) {
      if (owns(owner) && ownsOperation("kg-build", operation)) {
        effectsRef.current.reportError(error);
      }
      if (owns(owner) && ownsOperation("kg-build", operation)) setBuildingKg(false);
    }
  };

  useEffect(() => {
    const owner = currentOwner();
    if (!owner || !buildingKg || policy.externalBuildPolling) return;
    let stopped = false;
    let inFlight = false;
    const timer = window.setInterval(async () => {
      if (stopped || inFlight || !owns(owner)) return;
      inFlight = true;
      try {
        const notebook = await effectsRef.current.refreshNotebook(owner.notebookId, () => owns(owner));
        if (stopped || !owns(owner)) return;
        const tracked = reconcileTrackedKgPoll(trackedKgJobIdRef.current, notebook.kg_build);
        if (tracked.terminal || !notebook.kg_build || notebook.kg_build.status !== "running") {
          setBuildingKg(false);
          setTrackedKgJobId(null);
          const message = kgBuildTerminalToast(notebook.kg_build);
          if (message) effectsRef.current.notify(message);
        } else if (tracked.trackedJobId !== trackedKgJobIdRef.current) {
          setTrackedKgJobId(tracked.trackedJobId);
        }
      } catch { /* transient error; keep polling */ }
      finally { inFlight = false; }
    }, KG_BACKGROUND_POLL_MS);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [buildingKg, ownerVersion, policy.externalBuildPolling]);

  const visible = Boolean(currentOwner());
  return {
    knowledge: {
      kind: knowledgeKind,
      items: visible ? (knowledge[knowledgeKind] ?? null) : null,
      types: visible ? knowledgeTypes : NO_KNOWLEDGE_TYPES,
      statusFilter: visible ? knowledgeStatusFilter : "all",
      total: visible ? (knowledgeTotal[knowledgeKind] ?? 0) : 0,
      page: visible ? (knowledgePage[knowledgeKind] ?? 0) : 0,
      duplicates: visible ? duplicates : null,
      contexts: visible ? knowledgeContexts : EMPTY_KNOWLEDGE_CONTEXTS,
      busyId: visible ? knowledgeBusy : null,
    },
    schema: {
      open: visible && schemaModalOpen,
      schemas: visible ? schemas : null,
      busy: visible && schemaBusy,
      view: visible ? schemaView : "notebook" as SchemaView,
    },
    graph: {
      open: visible && graphOpen,
      analysisOpen: visible && analysisOpen,
      graph: visible ? unifiedGraph : null,
      merged: visible ? mergedGraph : null,
      vizBuilding: visible && vizBuilding,
      search: visible ? search : "",
      searchHits: visible ? searchHits : NO_SEARCH_HITS,
      searchBusy: visible && searchBusy,
      rangeLimit: visible ? rangeLimit : KG_RANGE_DEFAULT,
      rangeBusy: visible && rangeBusy,
      selectedTypes: visible ? selectedTypes : NO_SELECTED_TYPES,
      pendingMerges: visible ? pendingMerges : NO_PENDING_MERGES,
      status: visible ? unifiedStatus : null,
      selectedNodeId: visible ? selectedNodeId : null,
      conceptDetail: visible ? conceptDetail : null,
      nodeContext: visible ? nodeContext : null,
      reviewBusy: visible && reviewBusy,
      decidingMerge: visible ? decidingMerge : null,
      reviewAllJob: visible ? reviewAllJob : null,
      reviewAllStarting: visible && reviewAllStarting,
      reviewAllRunning: visible && reviewAllRunning,
      rebuilding: visible && Boolean(currentOwner()
        && busyForNotebook(rebuildingNotebookIds, maintenanceOwnerKey(currentOwner()!))),
      relinking: visible && Boolean(currentOwner()
        && busyForNotebook(relinkingNotebookIds, maintenanceOwnerKey(currentOwner()!))),
      buildingKg: visible && buildingKg,
      trackedKgJobId: visible ? trackedKgJobId : null,
    },
    activateActor,
    beginNotebookTransition,
    finishNotebookTransition,
    leaveWorkspace,
    enterKnowledge,
    selectKnowledgeKind,
    selectKnowledgeStatus,
    goToKnowledgePage,
    refreshKnowledge,
    invalidateKnowledge,
    updateKnowledge,
    findDuplicates,
    mergeKnowledge,
    loadKnowledgeContext,
    openSchemas,
    closeSchemas,
    selectSchemaView,
    patchSchema,
    createSchema,
    deleteSchema,
    induceSchemas,
    openGraph,
    closeGraph,
    openAnalysis,
    closeAnalysis,
    updateGraphSearch,
    changeRange,
    toggleType,
    clearTypes,
    selectNode,
    reviewPendingMerges,
    reviewAllMerges,
    decideMerge,
    startRelink,
    startRebuild,
    startKgBuild,
    observeNotebook,
    observeKgBuild,
  };
}
