"use client";

import { useRef, useState } from "react";
import { knowledgeItemFromRecord } from "./kg-workspace-model";
import type { KgWorkspaceOwner } from "./kg-workspace-model";
import { httpErrorStatus, toUserMessage } from "./errors";
import {
  findDuplicates as findKnowledgeDuplicates,
  findOwnMemoryDuplicates,
  listKnowledge,
  listKnowledgeTypes,
  mergeKnowledge as mergeKnowledgeRecords,
  updateKnowledge as updateKnowledgeRecord,
} from "./knowledge-api";
import { fetchNodeContext } from "../features/kg-maintenance/kg-api";
import type { KgOwnerAuthority } from "./use-kg-owner";
import type {
  DuplicateGroup,
  DuplicatePartition,
  KnowledgeItem,
  KnowledgeKind,
  KnowledgeTypeCount,
  MergeOutcome,
  NodeContext,
  NotebookSummary,
} from "./workspace-model";
import { EMPTY_KNOWLEDGE } from "./workspace-model";

// Knowledge rows / types / status filter / paging / duplicates / per-item
// context. One of the three KG domain owners; the shared actor + notebook +
// generation gate arrives as `authority` (see `use-kg-owner.ts`) and this
// hook never reaches into another domain.
type KgKnowledgePolicy = {
  canGovernKnowledge: boolean;
  /** 合并本人的两条记忆对象只要读权(用户裁决 2026-10-22);镜像上收起。 */
  canMergeOwnMemory: boolean;
};

/** 合并结果的就地文案(见 mergeKnowledge)。 */
export const MERGE_DONE = "已合并，原条目已弃用";
export const MERGE_TARGET_GONE = "这条已不在了";

type KgKnowledgeEffects = {
  notify: (message: string) => void;
  reportError: (error: unknown) => void;
  refreshCollection: (guard: () => boolean) => Promise<void>;
  refreshNotebook: (notebookId: string, guard: () => boolean) => Promise<NotebookSummary>;
};

export type UseKgKnowledgeOptions = {
  authority: KgOwnerAuthority;
  policy: KgKnowledgePolicy;
  effects: KgKnowledgeEffects;
};

type KnowledgeRequest = {
  kind: KnowledgeKind;
  status: string;
  page: number;
};

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

export function useKgKnowledge({ authority, policy, effects }: UseKgKnowledgeOptions) {
  const { currentOwner, owns, beginOperation, ownsOperation } = authority;
  const policyRef = useRef(policy);
  policyRef.current = policy;
  const effectsRef = useRef(effects);
  effectsRef.current = effects;

  const knowledgeRequestRef = useRef(0);
  const typeRequestRef = useRef(0);
  const duplicateRequestRef = useRef(0);
  const knowledgeContextRequestsRef = useRef(new Map<string, object>());

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
  const [ownDuplicates, setOwnDuplicates] = useState<DuplicateGroup[] | null>(null);
  const [knowledgeContexts, setKnowledgeContexts] = useState<Record<string, NodeContext>>({});
  const [knowledgeBusy, setKnowledgeBusy] = useState<string | null>(null);

  const clearVisibleState = () => {
    setKnowledgeKind("concept");
    setKnowledge(EMPTY_KNOWLEDGE);
    setKnowledgeTypes([]);
    setKnowledgeStatusFilter("all");
    setKnowledgeTotal({});
    setKnowledgePage({});
    setDuplicates(null);
    setOwnDuplicates(null);
    setKnowledgeContexts({});
    knowledgeContextRequestsRef.current.clear();
    setKnowledgeBusy(null);
  };

  const invalidate = () => {
    knowledgeRequestRef.current += 1;
    typeRequestRef.current += 1;
    duplicateRequestRef.current += 1;
    clearVisibleState();
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
    setOwnDuplicates(null);
      setKnowledgeContexts({});
      knowledgeContextRequestsRef.current.clear();
      await loadKnowledgeFor(owner, { kind: nextKind, status: "all", page: 0 });
    } else if (knowledgeRef.current[currentKind] == null) {
      // 类型还在时状态筛选不会被复位(复位只发生在上面换类型那一支),所以重拉必须按当前
      // 筛选:作废之后(删除来源 / 删除知识图谱)重新进入,下拉仍写着「待审核」,列表却按
      // 「全部」拉回来,两者当着用户的面对不上。
      await loadKnowledgeFor(owner, { kind: currentKind, status: statusFilterRef.current, page: 0 });
    }
  };

  const selectKnowledgeKind = (kind: KnowledgeKind) => {
    const owner = currentOwner();
    if (!owner) return;
    setKnowledgeKind(kind);
    setKnowledgeStatusFilter("all");
    setDuplicates(null);
    setOwnDuplicates(null);
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
    // 引用同步清空:紧跟着的 enterKnowledge 判「这一类还没加载」读的是这个 ref,等不到
    // 下一次渲染——类型请求很快时,它会看到作废前的旧行而跳过重拉。
    knowledgeRef.current = EMPTY_KNOWLEDGE;
    setKnowledge(EMPTY_KNOWLEDGE);
    setKnowledgeTypes([]);
    setKnowledgeTotal({});
    setKnowledgePage({});
    setDuplicates(null);
    setOwnDuplicates(null);
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

  /** 哪个分区此刻允许查与合:共享组跟知识治理权走,本人记忆分区只要读权(镜像上都收)。 */
  const partitionAllowed = (partition: DuplicatePartition) => (
    partition === "shared"
      ? policyRef.current.canGovernKnowledge
      : policyRef.current.canMergeOwnMemory
  );

  const loadPartition = (notebookId: string, kind: KnowledgeKind, partition: DuplicatePartition) => (
    partition === "shared"
      ? findKnowledgeDuplicates(notebookId, kind)
      : findOwnMemoryDuplicates(notebookId, kind)
  );

  const setPartition = (partition: DuplicatePartition, rows: DuplicateGroup[]) => {
    if (partition === "shared") setDuplicates(rows);
    else setOwnDuplicates(rows);
  };

  // 「查重」:有知识治理权时拉共享组,只读成员不请求共享 `/duplicates`;两种人都拉本人
  // 记忆分区。两个请求并发,各自按 kind 与请求代次落定。
  const findDuplicates = async () => {
    const owner = currentOwner();
    if (!owner) return;
    const partitions = (["shared", "own"] as const).filter(partitionAllowed);
    if (partitions.length === 0) return;
    const kind = knowledgeKindRef.current;
    const requestId = ++duplicateRequestRef.current;
    const current = () => owns(owner) && requestId === duplicateRequestRef.current
      && knowledgeKindRef.current === kind;
    try {
      const results = await Promise.all(
        partitions.map((partition) => loadPartition(owner.notebookId, kind, partition)),
      );
      if (!current()) return;
      partitions.forEach((partition, index) => {
        if (partitionAllowed(partition)) setPartition(partition, results[index]);
      });
    } catch (error) {
      if (current()) effectsRef.current.reportError(error);
    }
  };

  /** 合并之后(或失败之后)重拉发起合并的那个分区;别的分区不受这次合并影响。 */
  const refreshPartition = async (
    owner: KgWorkspaceOwner, kind: KnowledgeKind, partition: DuplicatePartition,
    stillOurs: () => boolean,
  ) => {
    try {
      const rows = await loadPartition(owner.notebookId, kind, partition);
      if (stillOurs() && knowledgeKindRef.current === kind) setPartition(partition, rows);
    } catch (error) {
      if (stillOurs()) effectsRef.current.reportError(error);
    }
  };

  /**
   * 合并一条进同组第一条。结果交回调用方就地显示(按钮旁边),不再走页顶提示:
   * 成功 → 「已合并，原条目已弃用」;409 → 服务端原文并重拉该分区;404 → 「这条已不在了」
   * 并重拉该分区。共享分区的请求序列与以前相同(合并 → 列表 → 类型 → 共享查重)。
   */
  const mergeKnowledge = async (
    sourceId: string, intoId: string, partition: DuplicatePartition = "shared",
  ): Promise<MergeOutcome | null> => {
    const owner = currentOwner();
    if (!owner || !partitionAllowed(partition) || knowledgeBusy) return null;
    const operation = beginOperation("knowledge");
    const kind = knowledgeKindRef.current;
    const status = statusFilterRef.current;
    const stillOurs = () => owns(owner) && ownsOperation("knowledge", operation)
      && partitionAllowed(partition);
    setKnowledgeBusy(sourceId);
    try {
      try {
        await mergeKnowledgeRecords(owner.notebookId, sourceId, intoId);
      } catch (error) {
        if (!stillOurs()) return null;
        const httpStatus = httpErrorStatus(error);
        if (httpStatus === 409 || httpStatus === 404) {
          duplicateRequestRef.current += 1;
          await refreshPartition(owner, kind, partition, stillOurs);
          return {
            tone: "failed",
            text: httpStatus === 404 ? MERGE_TARGET_GONE : toUserMessage(error),
          };
        }
        return { tone: "failed", text: toUserMessage(error) };
      }
      if (!stillOurs()) return null;
      knowledgeRequestRef.current += 1;
      duplicateRequestRef.current += 1;
      const refreshed = await loadKnowledgeFor(owner, { kind, status, page: 0 });
      if (refreshed !== null && stillOurs()) {
        const types = await loadTypesFor(owner);
        if (types !== null && stillOurs()) {
          await refreshPartition(owner, kind, partition, stillOurs);
        }
      }
      return stillOurs() ? { tone: "done", text: MERGE_DONE } : null;
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

  const visible = Boolean(currentOwner());
  return {
    view: {
      kind: knowledgeKind,
      items: visible ? (knowledge[knowledgeKind] ?? null) : null,
      types: visible ? knowledgeTypes : NO_KNOWLEDGE_TYPES,
      statusFilter: visible ? knowledgeStatusFilter : "all",
      total: visible ? (knowledgeTotal[knowledgeKind] ?? 0) : 0,
      page: visible ? (knowledgePage[knowledgeKind] ?? 0) : 0,
      duplicates: visible ? duplicates : null,
      ownDuplicates: visible ? ownDuplicates : null,
      contexts: visible ? knowledgeContexts : EMPTY_KNOWLEDGE_CONTEXTS,
      busyId: visible ? knowledgeBusy : null,
    },
    clearVisibleState,
    invalidate,
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
  };
}
