"use client";

import { useRef, useState } from "react";
import { knowledgeItemFromRecord } from "./kg-workspace-model";
import type { KgWorkspaceOwner } from "./kg-workspace-model";
import {
  findDuplicates as findKnowledgeDuplicates,
  listKnowledge,
  listKnowledgeTypes,
  mergeKnowledge as mergeKnowledgeRecords,
  updateKnowledge as updateKnowledgeRecord,
} from "./knowledge-api";
import { fetchNodeContext } from "../features/kg-maintenance/kg-api";
import type { KgOwnerAuthority } from "./use-kg-owner";
import type {
  DuplicateGroup,
  KnowledgeItem,
  KnowledgeKind,
  KnowledgeTypeCount,
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
};

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
