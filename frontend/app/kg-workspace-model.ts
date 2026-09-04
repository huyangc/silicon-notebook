import type {
  KnowledgeItem,
  KnowledgeRecord,
  PendingMerge,
  UnifiedGraphResp,
} from "./workspace-model";

export const KG_RANGE_DEFAULT = 80;

export const KG_RANGE_STEPS: ReadonlyArray<{ value: number; label: string }> = [
  { value: 80, label: "核心 80" },
  { value: 160, label: "核心 160" },
  { value: 320, label: "核心 320" },
  { value: 0, label: "全部" },
];

export const KG_SEARCH_DEBOUNCE_MS = 300;
export const KG_BACKGROUND_POLL_MS = 6000;
export const KG_MAINTENANCE_POLL_MS = 3000;
export const KG_BACKGROUND_POLL_CAP_MS = 20 * 60 * 1000;
export const MAINTENANCE_JOB_MISMATCH_SETTLE_STREAK = 2;

/** 图谱画布当下该渲染的那一种状态。 */
export type KgCanvasState = "loading" | "building" | "unavailable" | "empty" | "graph";

/**
 * 画布四态判定（批 3·W4 T-W4-3 之前是三态）。
 *
 * 第四态 `unavailable` 是后端新增的诚实降级信号：库规模超过在线折叠预算时，接口
 * 不再谎报 `viz_building: true`——它现在既不在后台建、也不会在这次请求里建，图谱
 * 预览要等下一次索引构建才有。此前这种库落进 `empty` 分支，被当成「没有匹配的
 * 节点。清空搜索后可查看完整图谱」——一句在这里永远兑现不了的话。
 *
 * 顺序即优先级：还没拿到响应 → 加载中；后端说在建 → 构建中；后端说没有预览且
 * 没人在建 → 不可用（后端把这两个标志造成互斥，所以这里的先后不承载判断）；
 * 剩下才轮到「渲染出来是空的」与正常出图。`visibleNodeCount` 是**过滤/搜索之后**
 * 的节点数，所以 `empty` 保留它原来的含义（搜到空集），不会被大库状态借走。
 */
export const kgCanvasState = (
  graph: UnifiedGraphResp | null,
  vizBuilding: boolean,
  visibleNodeCount: number,
): KgCanvasState => {
  if (graph === null) return "loading";
  if (vizBuilding) return "building";
  if (graph.viz_unavailable) return "unavailable";
  return visibleNodeCount === 0 ? "empty" : "graph";
};

export type KgWorkspaceOwner = {
  actorId: string;
  notebookId: string;
  generation: number;
  viewGeneration: number;
};

export type KgWorkspaceTransition = { generation: number };

export const sameKgOwner = (
  left: KgWorkspaceOwner | null,
  right: KgWorkspaceOwner,
): boolean => Boolean(
  left
  && left.actorId === right.actorId
  && left.notebookId === right.notebookId
  && left.generation === right.generation
  && left.viewGeneration === right.viewGeneration,
);

export const sameKgIdentity = (
  left: Pick<KgWorkspaceOwner, "actorId" | "notebookId"> | null,
  right: Pick<KgWorkspaceOwner, "actorId" | "notebookId">,
): boolean => Boolean(
  left
  && left.actorId === right.actorId
  && left.notebookId === right.notebookId,
);

export const knowledgeItemFromRecord = (record: KnowledgeRecord): KnowledgeItem => ({
  id: record.id,
  status: record.status,
  owner: record.owner,
  last_reviewed: record.last_reviewed,
  evidence: record.evidence,
  headline: record.headline,
  object_type: record.object_type,
  fields: record.fields,
});

const mergePairKey = (candidate: PendingMerge): string =>
  [candidate.canonical_a, candidate.canonical_b].sort().join("\0");

export const filterPendingMergeTombstones = (
  rows: readonly PendingMerge[],
  tombstoned: ReadonlySet<string>,
): PendingMerge[] => {
  if (tombstoned.size === 0) return [...rows];
  return rows.filter((row) => (
    !tombstoned.has(row.id)
    && !tombstoned.has(mergePairKey(row))
  ));
};

export const pendingMergeTombstoneKeys = (candidate: PendingMerge): string[] => [
  candidate.id,
  mergePairKey(candidate),
];
