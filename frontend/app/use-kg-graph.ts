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
  pendingMergeTombstoneKeys,
  type KgWorkspaceOwner,
} from "./kg-workspace-model";
import { withoutDecidedMerge } from "./kg-merge-model";
import { shouldResumeReviewAll } from "./in-progress-resume";
import {
  buildKg,
  confirmMerge,
  deleteKg,
  fetchConceptDetail,
  fetchKgDeleteStatus,
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
  KG_DELETE_BUSY_MESSAGE,
  KG_DELETE_FAILED_MESSAGE,
  KG_DELETE_JOB_MISMATCH,
  KG_DELETE_POLL_MAX_ATTEMPTS,
  KG_DELETE_POLL_TIMED_OUT,
  KG_DELETE_RESULT_HOLD_MS,
  KG_DELETE_UNOBSERVED,
  kgDeletePollOutcome,
  kgDeleteTerminalSettlement,
  kgDeleteUnobservedOutcome,
  type KgDeleteResult,
} from "../features/kg-maintenance/kg-delete-status";
import {
  kgBuildTerminalToast,
  reconcileTrackedKgPoll,
} from "./kg-build-status";
import type { KgOwnerAuthority } from "./use-kg-owner";
import type {
  ConceptDetailResp,
  KgObject,
  KgSearchHit,
  MergeReviewJob,
  NotebookSummary,
  NodeContext,
  PendingMerge,
  UnifiedConceptNode,
  UnifiedEdge,
  UnifiedGraphResp,
  UnifiedKgStatus,
} from "./workspace-model";

// Unified graph view (open/range/search/node selection), pending-merge review
// with its actor+notebook tombstones, and the durable KG build / relink /
// rebuild / delete trackers. One of the three KG domain owners; the shared actor +
// notebook + generation gate arrives as `authority` (see `use-kg-owner.ts`).
type KgGraphPolicy = {
  canWriteKg: boolean;
  externalBuildPolling: boolean;
};

type KgGraphEffects = {
  notify: (message: string) => void;
  reportError: (error: unknown) => void;
  refreshNotebook: (notebookId: string, guard: () => boolean) => Promise<NotebookSummary>;
  /**
   * 「删除知识图谱」终态之后,KG 领域之外还读着旧图谱事实的那几块(来源列表的
   * 已分析/待分析徽标、看板「索引与构建」的状态)由 page 自己重拉。KG 领域不认识
   * 那些 owner,只在终态时按笔记本 + 守卫把这件事交出去。
   */
  refreshAfterKgDelete: (notebookId: string, guard: () => boolean) => Promise<void>;
  focusGraphNode: (nodeId: string) => void;
};

export type UseKgGraphOptions = {
  authority: KgOwnerAuthority;
  policy: KgGraphPolicy;
  effects: KgGraphEffects;
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
const NO_SEARCH_HITS: KgSearchHit[] = Object.freeze([] as KgSearchHit[]) as KgSearchHit[];
const NO_SELECTED_TYPES: string[] = Object.freeze([] as string[]) as string[];
const NO_PENDING_MERGES: PendingMerge[] = Object.freeze([] as PendingMerge[]) as PendingMerge[];

// R3 PR-B P1-2: `attached` across concept-detail hub-cluster pages can repeat
// the same adjacency object (it is not partitioned by member — see
// `loadMoreConceptMembers`'s merge below). Keeps the FIRST occurrence, same
// order otherwise.
function dedupeKgObjectsById(items: KgObject[]): KgObject[] {
  const seen = new Set<string>();
  const result: KgObject[] = [];
  for (const item of items) {
    if (seen.has(item.id)) continue;
    seen.add(item.id);
    result.push(item);
  }
  return result;
}

export function useKgGraph({ authority, policy, effects }: UseKgGraphOptions) {
  const { ownerVersion, currentOwner, owns, ownsIdentity, beginOperation, ownsOperation } = authority;
  const policyRef = useRef(policy);
  policyRef.current = policy;
  const effectsRef = useRef(effects);
  effectsRef.current = effects;

  const graphOpenRequestRef = useRef(0);
  const graphRangeRequestRef = useRef(0);
  const graphSearchRequestRef = useRef(0);
  const graphNodeRequestRef = useRef(0);
  const graphSearchTimerRef = useRef<number | null>(null);
  const mergeTombstonesRef = useRef(new Map<string, Set<string>>());
  const nodeNotebookRef = useRef(new Map<string, string>());
  const nodeContextObjectRef = useRef(new Map<string, string>());
  const submittingMaintenanceRef = useRef(new Set<string>());
  const expectedMaintenanceJobRef = useRef(new Map<string, string>());
  const pendingRebuildRef = useRef(new Set<string>());

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
  const [conceptMembersLoadingMore, setConceptMembersLoadingMore] = useState(false);
  // codex #639 R2 P2:同一概念的 merge/rebuild 刷新会落一个全新首页,但
  // canonical_id 不变——渐进披露(KgEvidenceList 的 resetKey)需要一个
  // 「每次首页落地都变化、load-more 追加期间稳定」的世代号。镜像自
  // conceptMembersEpochRef(ref 不触发子组件 effect,故需 state)。
  const [conceptDetailGeneration, setConceptDetailGeneration] = useState(0);
  // codex #639 R4 P2(AGENTS.md Interactive feedback):load-more 失败必须在
  // 按钮紧邻处给结果,不能只发页面横幅。失败置位、重试/新首页落地清零;
  // 且按契约原文「clear that state on its own timer」定时自动还原
  // (codex #639 R6 P2)——定时器带 epoch 守卫,新首页落地后过期回调不再清
  // (它清的是已被换代的状态,虽同值无害,守卫保持口径一致)。
  const [conceptMembersLoadError, setConceptMembersLoadError] = useState(false);
  const conceptMembersLoadErrorTimerRef = useRef<number | null>(null);
  const clearConceptMembersLoadErrorTimer = () => {
    if (conceptMembersLoadErrorTimerRef.current !== null) {
      window.clearTimeout(conceptMembersLoadErrorTimerRef.current);
      conceptMembersLoadErrorTimerRef.current = null;
    }
  };
  useEffect(() => clearConceptMembersLoadErrorTimer, []);
  // Hub-cluster member pagination (R3·T-B2). `conceptDetail` itself carries
  // the cursor (`next_cursor`) and the accumulated `members`/`attached`/
  // `evidence` — every place that lands a fresh FIRST page replaces the
  // whole object wholesale, which is what resets the "load more" accumulated
  // state (design review B8: the three call sites that (re)fetch a first
  // page — selectNode, decideMerge's post-decision refresh, and
  // refreshAfterRebuild — must not leave a stale cursor/accumulated list
  // pointing at a superseded page). `conceptMembersEpochRef` additionally
  // guards a load-more request in flight when one of those three call sites
  // lands a new first page (or clears the detail entirely) before it
  // resolves — the epoch bump discards the stale response.
  const conceptMembersEpochRef = useRef(0);
  const conceptDetailContextRef = useRef<{ notebookId: string; nodeId: string; sourceNotebookId: string } | null>(null);
  const [nodeContext, setNodeContext] = useState<NodeContext | null>(null);
  const [reviewBusy, setReviewBusy] = useState(false);
  const [decidingMerge, setDecidingMerge] = useState<{ id: string; confirm: boolean } | null>(null);
  const [reviewAllJob, setReviewAllJob] = useState<MergeReviewJob | null>(null);
  const [reviewAllStarting, setReviewAllStarting] = useState(false);
  const [reviewAllRunning, setReviewAllRunning] = useState(false);
  const [rebuildingNotebookIds, setRebuildingNotebookIds] = useState<Set<string>>(new Set());
  const [relinkingNotebookIds, setRelinkingNotebookIds] = useState<Set<string>>(new Set());
  const [deletingNotebookIds, setDeletingNotebookIds] = useState<Set<string>>(new Set());
  // 「删除知识图谱」的结果按 actor+notebook 分格保存,和忙碌位同一把键:切到 B 看不到 A 的
  // 结果,A→B→A 在保留期内回来仍能看到。每格自带计时器,到点只清自己那一格。
  const [deleteResults, setDeleteResults] = useState<Map<string, KgDeleteResult>>(new Map());
  const deleteResultTimersRef = useRef(new Map<string, number>());
  const [buildingKg, setBuildingKg] = useState(false);
  const [trackedKgJobId, setTrackedKgJobId] = useState<string | null>(null);
  const trackedKgJobIdRef = useRef<string | null>(null);
  trackedKgJobIdRef.current = trackedKgJobId;

  const mergeTombstones = (owner: Pick<KgWorkspaceOwner, "actorId" | "notebookId">): Set<string> =>
    mergeTombstonesRef.current.get(ownerKey(owner)) ?? new Set<string>();

  const filterPendingMerges = (owner: Pick<KgWorkspaceOwner, "actorId" | "notebookId">, rows: PendingMerge[]) =>
    filterPendingMergeTombstones(rows, mergeTombstones(owner));

  const publishError = (owner: KgWorkspaceOwner, error: unknown) => {
    if (owns(owner)) effectsRef.current.reportError(error);
  };

  // Every place that lands a FRESH first page (or clears the detail
  // entirely) must go through here: it bumps the epoch (discarding any
  // "load more" response still in flight against the superseded page) and
  // records the request context `loadMoreConceptMembers` needs to fetch the
  // next page (R3·T-B2, design review B8 — the three call sites at
  // selectNode / decideMerge's refresh / refreshAfterRebuild must reset the
  // accumulated "load more" state, not just replace `conceptDetail`'s
  // top-level fields).
  const setConceptDetailFirstPage = (
    context: { notebookId: string; nodeId: string; sourceNotebookId: string } | null,
    detail: ConceptDetailResp | null,
  ) => {
    conceptMembersEpochRef.current += 1;
    conceptDetailContextRef.current = detail ? context : null;
    setConceptMembersLoadingMore(false);
    clearConceptMembersLoadErrorTimer();
    setConceptMembersLoadError(false);
    setConceptDetailGeneration(conceptMembersEpochRef.current);
    setConceptDetail(detail);
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
    setConceptDetailFirstPage(null, null);
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
    graphOpenRequestRef.current += 1;
    graphRangeRequestRef.current += 1;
    graphSearchRequestRef.current += 1;
    graphNodeRequestRef.current += 1;
    clearVisibleState();
  };

  // The maintenance-state recovery probe the owner authority hands to this
  // domain the moment a new owner is established: adopt the notebook
  // snapshot frozen by `finishNotebookTransition`, resume a running
  // review-all job (write-capable members only), and re-claim a rebuild,
  // relink or KG delete the server is still running. Deliberately no Knowledge / schema /
  // unified-graph read — opening a notebook must not fetch content.
  const adoptOwner = (owner: KgWorkspaceOwner, notebookSnapshot: NotebookSummary | null) => {
    if (notebookSnapshot?.id === owner.notebookId && notebookSnapshot.kg_build?.status === "running") {
      setBuildingKg(true);
      setTrackedKgJobId(notebookSnapshot.kg_build.job_id);
    }
    if (policyRef.current.canWriteKg) {
      void fetchMergeReviewJob(owner.notebookId).then((job) => {
        if (!owns(owner) || !policyRef.current.canWriteKg) return;
        if (shouldResumeReviewAll(job)) {
          setReviewAllJob(job);
          setReviewAllRunning(true);
        }
      }).catch(() => {});
    }
    void Promise.all([
      fetchUnifiedKgRebuildStatus(owner.notebookId).catch(() => null),
      fetchRelinkStatus(owner.notebookId).catch(() => null),
      fetchKgDeleteStatus(owner.notebookId).catch(() => null),
    ]).then(([rebuild, relink, deletion]) => {
      if (!owns(owner)) return;
      if (rebuild && (rebuild.running || rebuild.status === "running")) {
        setRebuildingNotebookIds((current) => claimNotebookSlot(current, ownerKey(owner)));
      }
      if (relink && (relink.running || relink.status === "running")) {
        setRelinkingNotebookIds((current) => claimNotebookSlot(current, ownerKey(owner)));
      }
      if (deletion && (deletion.running || deletion.status === "running")) {
        // 亲眼见到在跑的那个删除就是这一格要等的任务:记下它的 job_id,终态回执只有对得上
        // 才报数字(同进程更早一次删除的 succeeded 不算)。
        expectedMaintenanceJobRef.current.set(maintenanceJobKey(owner, "delete"), deletion.job_id);
        setDeletingNotebookIds((current) => claimNotebookSlot(current, ownerKey(owner)));
      }
    });
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
    setConceptDetailFirstPage(null, null);
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
        // 批 3·W4 T-W4-3：这个状态不再是「正在构建、稍等即可」的短暂窗口——大库的
        // 折叠图产物只由索引构建发布，在线路径不会再自己去建。所以文案不能承诺
        // 一个没人会兑现的「完成后请重试」，只说清什么时候会有。
        effectsRef.current.notify("库规模较大，图谱预览尚未生成，暂时无法定位该引用节点；下一次索引构建后可用");
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
    setConceptDetailFirstPage(null, null);
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
          setConceptDetailFirstPage(
            { notebookId: owner.notebookId, nodeId, sourceNotebookId: resolvedSourceNotebookId },
            detail,
          );
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

  // Hub-cluster "load more members" (R3·T-B2). Appends the next page's
  // members/attached/evidence onto the already-loaded ones; `next_cursor`/
  // `canonical_name` are taken from the fresh page response (same values as
  // before except `next_cursor`, which advances or clears).
  // Guarded by `conceptMembersEpochRef` against a first page landing (via
  // `setConceptDetailFirstPage`, e.g. selecting a different node, or a
  // merge-decision/rebuild refresh) while this request is in flight — a
  // stale response must not get merged onto a page it no longer matches.
  const loadMoreConceptMembers = async () => {
    const owner = currentOwner();
    const context = conceptDetailContextRef.current;
    const cursor = conceptDetail?.next_cursor;
    if (!owner || !context || !cursor || conceptMembersLoadingMore
      || owner.notebookId !== context.notebookId) return;
    const epoch = conceptMembersEpochRef.current;
    setConceptMembersLoadingMore(true);
    clearConceptMembersLoadErrorTimer();
    setConceptMembersLoadError(false);
    try {
      const page = await fetchConceptDetail(
        context.notebookId, context.nodeId, context.sourceNotebookId, cursor,
      );
      if (!owns(owner) || epoch !== conceptMembersEpochRef.current) return;
      setConceptDetail((current) => (current ? {
        ...page,
        members: [...current.members, ...page.members],
        // R3 PR-B P1-2: `attached` is deduplicated by id across pages — a
        // hub cluster's adjacency objects are not partitioned by member, so
        // the same attached object can be reachable from more than one
        // member and reappear on a later page (React list key collisions,
        // inflated per-type counts in `relatedNodeGroups`). `evidence` is
        // intentionally NOT deduplicated here: each page's evidence already
        // came out unique per that page's own members (knowledge_query.py's
        // page-scoped evidence flattening), and two different members can
        // legitimately share the same evidence item text without it being a
        // "duplicate" the same way an attached object's identity is.
        attached: dedupeKgObjectsById([...current.attached, ...page.attached]),
        evidence: [...current.evidence, ...page.evidence],
        // R3 PR-B P1-1: the backend only recomputes `member_total` on the
        // first page — later pages answer `null` (see workspace-model.ts).
        // `...page` above would otherwise clobber the real total already
        // held in `current` with that `null`; keep the first page's value.
        member_total: page.member_total ?? current.member_total,
      } : current));
    } catch (error) {
      if (owns(owner) && epoch === conceptMembersEpochRef.current) {
        // 横幅照旧(全局错误通道),但本地失败态才是按钮紧邻反馈的载体
        // (codex #639 R4 P2 / AGENTS.md Interactive feedback)。
        setConceptMembersLoadError(true);
        clearConceptMembersLoadErrorTimer();
        conceptMembersLoadErrorTimerRef.current = window.setTimeout(() => {
          conceptMembersLoadErrorTimerRef.current = null;
          if (epoch === conceptMembersEpochRef.current) setConceptMembersLoadError(false);
        }, 4000);
        effectsRef.current.reportError(error);
      }
    } finally {
      if (epoch === conceptMembersEpochRef.current) setConceptMembersLoadingMore(false);
    }
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

  // 自动判重与删除互斥:删除会一并清掉待确认合并,判重期间删除则会把正在判的候选抽走。
  // 两侧各自早退(删除那侧见 startKgDelete),按钮同口径禁用。
  const reviewPendingMerges = async () => {
    const owner = currentOwner();
    if (!owner || !policyRef.current.canWriteKg || reviewBusy
      || deletingNotebookIds.has(ownerKey(owner))) return;
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
    if (!owner || !policyRef.current.canWriteKg || reviewAllStarting || reviewAllRunning
      || deletingNotebookIds.has(ownerKey(owner))) return;
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
    // 删除期间待确认合并正被一并清掉:此刻落决定只会对着正在消失的候选写,且确认分支
    // 连带的重新合并必然撞同一个维护槽。
    if (!owner || !policyRef.current.canWriteKg || decidingMerge
      || rebuildingNotebookIds.has(ownerKey(owner))
      || deletingNotebookIds.has(ownerKey(owner))) return;
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
          && selectedNodeIdRef.current === selection) {
          setConceptDetailFirstPage(
            { notebookId: owner.notebookId, nodeId: selected.id, sourceNotebookId: "" },
            detail,
          );
        }
      } else {
        setConceptDetailFirstPage(null, null);
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
        if (owns(owner) && selectedNodeIdRef.current === selection) {
          setConceptDetailFirstPage(
            { notebookId: owner.notebookId, nodeId: selected.id, sourceNotebookId: "" },
            detail,
          );
        }
      } else {
        setConceptDetailFirstPage(null, null);
      }
      if (!selected) setNodeContext(null);
    } catch (error) {
      publishError(owner, error);
    }
  };

  // 删除之后,画布上还挂着的一切都指向已不存在的对象:搜索命中(画布在搜索模式下直接画
  // 命中节点,点一下就报错)、选中节点与它的概念详情/上下文、类型过滤、展开的邻域。先按
  // 各自既有的清理路径作废(搜索与选点的请求序号 +1,迟到的响应一律丢弃),再按当前范围
  // 重拉图谱、待确认合并与合并状态。重新合并那条刷新会替仍在图里的选中概念重取详情,删除
  // 不走它:选中的对象此刻恰恰是被删掉的那批。笔记本摘要(kg_ready / kg_build)与 KG 领域
  // 外那几块(来源徽标、Knowledge 浏览器、看板状态)也读着旧事实——三件并行,各自吞自己的
  // 错,谁都不拖住释放忙碌位。调用方已核对过 owner 仍可见。
  // 图谱本身的读取同样要作废在途的那几条:删除期间「范围」下拉照常可用,一条比这次重拉
  // 更慢的范围响应(或恰在此刻发出的打开请求)会在刷新之后把删了一半的图重新画上去。所以
  // 范围与打开两个序号都 +1(它们各自的提交本来就核对序号),重拉图谱自己也占一个范围序号:
  // 用户在重拉途中换了范围,落地的是换范围那一次。被作废的范围请求不会再清自己的忙碌位,
  // 这里代它清掉。
  const refreshAfterDelete = async (owner: KgWorkspaceOwner) => {
    const guard = () => owns(owner);
    clearSearchTimer();
    graphSearchRequestRef.current += 1;
    graphNodeRequestRef.current += 1;
    graphOpenRequestRef.current += 1;
    const rangeRequestId = ++graphRangeRequestRef.current;
    setRangeBusy(false);
    setSearch("");
    setSearchHits([]);
    setSearchBusy(false);
    setSelectedTypes([]);
    setSelectedNodeId(null);
    setPendingFocusId(null);
    setConceptDetailFirstPage(null, null);
    setNodeContext(null);
    setExpandedNodes([]);
    setExpandedEdges([]);
    const reloadGraph = async () => {
      try {
        const [graph, merges, status] = await Promise.all([
          fetchUnifiedGraph(owner.notebookId, rangeLimitRef.current),
          fetchPendingMerges(owner.notebookId),
          fetchUnifiedKgStatus(owner.notebookId),
        ]);
        if (!owns(owner)) return;
        // 待确认合并与合并状态不随范围变化,照落;只有图谱本身让位给更晚的范围请求。
        setPendingMerges(filterPendingMerges(owner, merges));
        setUnifiedStatus(status);
        if (rangeRequestId !== graphRangeRequestRef.current) return;
        setUnifiedGraph(graph);
        setVizBuilding(Boolean(graph.viz_building));
      } catch (error) {
        publishError(owner, error);
      }
    };
    await Promise.all([
      reloadGraph(),
      effectsRef.current.refreshNotebook(owner.notebookId, guard).catch(() => null),
      effectsRef.current.refreshAfterKgDelete(owner.notebookId, guard).catch(() => {}),
    ]);
  };

  const clearDeleteResult = (key: string) => {
    const timer = deleteResultTimersRef.current.get(key);
    if (timer !== undefined) window.clearTimeout(timer);
    deleteResultTimersRef.current.delete(key);
    setDeleteResults((current) => {
      if (!current.has(key)) return current;
      const next = new Map(current);
      next.delete(key);
      return next;
    });
  };

  // 结果落在按钮旁,并按自己的计时器复原(AGENTS.md Interactive feedback)。计时器按格
  // 保存:同一格的新结果先撤掉旧计时器,旧计时器到点不会把新结果提前清掉。
  const showDeleteResult = (key: string, result: KgDeleteResult) => {
    const previous = deleteResultTimersRef.current.get(key);
    if (previous !== undefined) window.clearTimeout(previous);
    setDeleteResults((current) => new Map(current).set(key, result));
    deleteResultTimersRef.current.set(key, window.setTimeout(() => {
      deleteResultTimersRef.current.delete(key);
      setDeleteResults((current) => {
        if (current.get(key) !== result) return current;
        const next = new Map(current);
        next.delete(key);
        return next;
      });
    }, KG_DELETE_RESULT_HOLD_MS));
  };

  useEffect(() => {
    const timers = deleteResultTimersRef.current;
    return () => {
      for (const timer of timers.values()) window.clearTimeout(timer);
      timers.clear();
    };
  }, []);

  const maintenanceOwnerKey = (owner: Pick<KgWorkspaceOwner, "actorId" | "notebookId">) =>
    ownerKey(owner);
  const maintenanceJobKey = (
    owner: Pick<KgWorkspaceOwner, "actorId" | "notebookId">,
    kind: "rebuild" | "relink" | "delete",
  ) => `${maintenanceOwnerKey(owner)}\0${kind}`;

  const adoptRunningMaintenance = async (
    owner: KgWorkspaceOwner,
  ): Promise<"adopted" | "idle" | "unknown"> => {
    const [rebuildResult, relinkResult, deletionResult] = await Promise.allSettled([
      fetchUnifiedKgRebuildStatus(owner.notebookId),
      fetchRelinkStatus(owner.notebookId),
      fetchKgDeleteStatus(owner.notebookId),
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
    let deleteRunning = false;
    if (deletionResult.status === "fulfilled") {
      deleteRunning = Boolean(
        deletionResult.value.running || deletionResult.value.status === "running",
      );
      // 领养到的删除,期望的就是亲眼见到在跑的这一个 job_id(见 kgDeleteTerminalSettlement)。
      if (deleteRunning) {
        expectedMaintenanceJobRef.current.set(
          maintenanceJobKey(owner, "delete"), deletionResult.value.job_id,
        );
      }
      setDeletingNotebookIds((current) => deleteRunning
        ? claimNotebookSlot(current, key)
        : releaseNotebookClaim(current, key));
    }
    if (rebuildRunning || relinkRunning || deleteRunning) return "adopted";
    if (rebuildResult.status === "rejected" || relinkResult.status === "rejected"
      || deletionResult.status === "rejected") return "unknown";
    return "idle";
  };

  const startRelink = async () => {
    const owner = currentOwner();
    if (!owner || !policyRef.current.canWriteKg) return;
    const key = maintenanceOwnerKey(owner);
    const jobKey = maintenanceJobKey(owner, "relink");
    if (relinkingNotebookIds.has(key) || rebuildingNotebookIds.has(key) || buildingKg
      || deletingNotebookIds.has(key)
      || submittingMaintenanceRef.current.has(jobKey)) return;
    setRelinkingNotebookIds((current) => claimNotebookSlot(current, key));
    expectedMaintenanceJobRef.current.delete(jobKey);
    for (const attempt of [0, 1]) {
      if (!owns(owner) || !policyRef.current.canWriteKg) {
        setRelinkingNotebookIds((current) => releaseNotebookClaim(current, key));
        return;
      }
      // 409 之后领养探测若说「都没在跑」,会按服务端真相放掉这一格;重试的 POST 之前必须
      // 认领回来,否则重试成功时按钮既不忙碌、也没有轮询去等它的终态。
      if (attempt > 0) setRelinkingNotebookIds((current) => claimNotebookSlot(current, key));
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
        || deletingNotebookIds.has(key)
        || submittingMaintenanceRef.current.has(jobKey))) return "failed";
    setRebuildingNotebookIds((current) => claimNotebookSlot(current, key));
    expectedMaintenanceJobRef.current.delete(jobKey);
    for (const attempt of [0, 1]) {
      if (!owns(owner) || !policyRef.current.canWriteKg) {
        setRebuildingNotebookIds((current) => releaseNotebookClaim(current, key));
        return "denied";
      }
      // 同 startRelink:领养探测「都没在跑」时已放掉这一格,重试的 POST 之前认领回来。
      if (attempt > 0) setRebuildingNotebookIds((current) => claimNotebookSlot(current, key));
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

  // 「删除知识图谱」:破坏性后台任务,与补上关联/重新合并共用服务端一个维护槽。形态逐项
  // 镜像 startRelink——POST 之前先认领 actor+notebook 那一格(长任务按钮红线)、按种类
  // 记提交中标记与期望 job_id、409 只做一次有界的领养重试——只多两件事:
  // ① `notebookId` 是确认弹窗打开那一刻的笔记本。确认是异步的,点「确定」时当前 owner
  //    若已不是它,绝不能把删除落到另一本上。
  // ② 没能开始的原因也画在按钮旁(不只走横幅):被占槽时转述服务端点名占用者的 409 文案,
  //    其余失败说没完成;同一格的旧结果在认领时先让出。
  // 自动判重(单批 / 全部)与待确认合并的决定在飞时同样不删:删除会把它们正在处理的候选
  // 一并清掉。
  const startKgDelete = async (notebookId: string) => {
    const owner = currentOwner();
    if (!owner || !policyRef.current.canWriteKg || owner.notebookId !== notebookId) return;
    const key = maintenanceOwnerKey(owner);
    const jobKey = maintenanceJobKey(owner, "delete");
    if (deletingNotebookIds.has(key) || relinkingNotebookIds.has(key) || rebuildingNotebookIds.has(key)
      || buildingKg || submittingMaintenanceRef.current.has(jobKey)
      || reviewBusy || reviewAllStarting || reviewAllRunning || decidingMerge !== null) return;
    setDeletingNotebookIds((current) => claimNotebookSlot(current, key));
    clearDeleteResult(key);
    expectedMaintenanceJobRef.current.delete(jobKey);
    for (const attempt of [0, 1]) {
      if (!owns(owner) || !policyRef.current.canWriteKg) {
        setDeletingNotebookIds((current) => releaseNotebookClaim(current, key));
        return;
      }
      // 409 之后领养探测若说「都没在跑」,会放掉这一格;重试的 POST 之前认领回来,否则重试
      // 成功时服务端在删、按钮却重新可点,也没有轮询去等它的结果。
      if (attempt > 0) setDeletingNotebookIds((current) => claimNotebookSlot(current, key));
      submittingMaintenanceRef.current.add(jobKey);
      try {
        const started = await deleteKg(owner.notebookId);
        expectedMaintenanceJobRef.current.set(jobKey, started.job_id);
        if (owns(owner)) effectsRef.current.notify("已开始删除知识图谱；完成后会自动更新");
        return;
      } catch (error) {
        if (httpErrorStatus(error) === 409) {
          if (!owns(owner) || !policyRef.current.canWriteKg) {
            setDeletingNotebookIds((current) => releaseNotebookClaim(current, key));
            return;
          }
          const verdict = await adoptRunningMaintenance(owner);
          if (verdict === "idle" && attempt === 0) continue;
          // 这次点击没有开始删除。领养到了正在跑的那件事(可能正是另一个标签页发起的删除,
          // 那时它的终态结果稍后会覆盖这一行),或者三种维护状态都说没在跑、服务端仍回 409
          // (占着的是维护槽之外的分析任务)。两种情形都转述服务端点名占用者的 409 文案,
          // 不说「刚结束、再点一次」;只有后一种要自己放掉认领——前一种领养已经按服务端
          // 真相认领或释放过了。
          if (verdict === "idle") {
            setDeletingNotebookIds((current) => releaseNotebookClaim(current, key));
          }
          if (owns(owner)) {
            showDeleteResult(key, {
              tone: "neutral",
              text: toUserMessage(error, KG_DELETE_BUSY_MESSAGE),
            });
          }
          return;
        }
        if (owns(owner) && policyRef.current.canWriteKg) {
          effectsRef.current.reportError(error);
          showDeleteResult(key, {
            tone: "failed",
            text: toUserMessage(error, KG_DELETE_FAILED_MESSAGE),
          });
        }
        setDeletingNotebookIds((current) => releaseNotebookClaim(current, key));
        return;
      } finally {
        submittingMaintenanceRef.current.delete(jobKey);
      }
    }
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

  // 删除的有界轮询,逐项镜像上面那条 relink 轮询(单飞、身份感知、提交窗口内不结算、
  // job_id 连续对不上才收工、先刷新后释放)。差别在结算,且因为这是破坏性动作而更严:
  // 终态回执只有 job_id 正是本标签页提交成功或亲眼见过在跑的那一个,才以它的名义报数字
  // (kgDeleteTerminalSettlement)。一个都没期望过就静默收工——不报数字、不提示、不覆盖按钮
  // 旁已有的那一行;只有回执本身证明确有一次删除跑完(非空 job_id 的 succeeded / failed)
  // 才照删除后的样子刷新,idle 是纯释放(kgDeleteUnobservedOutcome)。期望过却对不上才说
  // 不知道。结果与提示在刷新完成之后才落地,让「已删除
  // N 个」出现时画布与来源列表已经是删除后的样子。
  useEffect(() => {
    const owner = currentOwner();
    if (!owner) return;
    const key = maintenanceOwnerKey(owner);
    if (!deletingNotebookIds.has(key)) return;
    const jobKey = maintenanceJobKey(owner, "delete");
    let stopped = false;
    let settled = false;
    let inFlight = false;
    let attempts = 0;
    let mismatchStreak = 0;
    const settle = async (outcome: ReturnType<typeof kgDeletePollOutcome>) => {
      if (outcome.refresh && owns(owner)) await refreshAfterDelete(owner);
      expectedMaintenanceJobRef.current.delete(jobKey);
      if (outcome.result) {
        if (owns(owner)) effectsRef.current.notify(outcome.result.text);
        showDeleteResult(key, outcome.result);
      }
      setDeletingNotebookIds((current) => releaseNotebookClaim(current, key));
    };
    const timer = window.setInterval(async () => {
      if (stopped || settled || inFlight) return;
      attempts += 1;
      if (attempts > KG_DELETE_POLL_MAX_ATTEMPTS) {
        settled = true;
        window.clearInterval(timer);
        await settle(expectedMaintenanceJobRef.current.get(jobKey)
          ? KG_DELETE_POLL_TIMED_OUT
          : KG_DELETE_UNOBSERVED);
        return;
      }
      inFlight = true;
      try {
        const status = await fetchKgDeleteStatus(owner.notebookId);
        if (stopped || settled || !ownsIdentity(owner)) return;
        const outcome = kgDeletePollOutcome(status);
        if (!outcome.done) {
          mismatchStreak = 0;
          // 领养探测失败而保留下来的认领没有期望 job_id;此刻亲眼见到在跑的删除就是要等的那个。
          if (status.job_id && !submittingMaintenanceRef.current.has(jobKey)
            && !expectedMaintenanceJobRef.current.get(jobKey)) {
            expectedMaintenanceJobRef.current.set(jobKey, status.job_id);
          }
          return;
        }
        if (submittingMaintenanceRef.current.has(jobKey)) return;
        const settlement = kgDeleteTerminalSettlement(
          status, expectedMaintenanceJobRef.current.get(jobKey),
        );
        if (settlement === "mismatch") {
          mismatchStreak += 1;
          if (mismatchStreak < MAINTENANCE_JOB_MISMATCH_SETTLE_STREAK) return;
        }
        settled = true;
        window.clearInterval(timer);
        await settle(settlement === "report"
          ? outcome
          : settlement === "mismatch" ? KG_DELETE_JOB_MISMATCH : kgDeleteUnobservedOutcome(status));
      } catch { /* transient status error; retain the claim */ }
      finally { inFlight = false; }
    }, KG_MAINTENANCE_POLL_MS);
    return () => { stopped = true; window.clearInterval(timer); };
  }, [deletingNotebookIds, ownerVersion]);

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
    // 删除期间服务端必然以「正在删除知识图谱」拒绝整理;调用方的按钮同口径禁用。
    if (!owner || !policyRef.current.canWriteKg || buildingKg
      || deletingNotebookIds.has(ownerKey(owner))) return;
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
    view: {
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
      conceptDetailGeneration,
      conceptMembersLoadingMore: visible && conceptMembersLoadingMore,
      conceptMembersLoadError: visible && conceptMembersLoadError,
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
      deleting: visible && Boolean(currentOwner()
        && busyForNotebook(deletingNotebookIds, maintenanceOwnerKey(currentOwner()!))),
      deleteResult: visible
        ? deleteResults.get(maintenanceOwnerKey(currentOwner()!)) ?? null
        : null,
      buildingKg: visible && buildingKg,
      trackedKgJobId: visible ? trackedKgJobId : null,
    },
    clearVisibleState,
    invalidate,
    adoptOwner,
    openGraph,
    closeGraph,
    openAnalysis,
    closeAnalysis,
    updateGraphSearch,
    changeRange,
    toggleType,
    clearTypes,
    selectNode,
    loadMoreConceptMembers,
    reviewPendingMerges,
    reviewAllMerges,
    decideMerge,
    startRelink,
    startRebuild,
    startKgDelete,
    startKgBuild,
    observeNotebook,
    observeKgBuild,
  };
}
