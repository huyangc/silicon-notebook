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
 * 顺序即优先级：
 * 1. 还没拿到响应 → 加载中；
 * 2. 在建 → 构建中。它压过 `unavailable`：后端把两个标志造成互斥，所以常态下不
 *    会同时为真，但真同时为真时「正在建」是更有信息量、且会自己了结的那一个
 *    （单测里钉住了这条排序，防止两行被对调）；
 * 3. **画布上真有可见节点 → 出图**（批 3·W4 T-W4-3b 顺修 2）。原本这一行在
 *    `unavailable` 之后，于是「大库无折叠图产物」的卡片会盖掉从引用深链叠加出来
 *    的真实一跳邻域——`kg_neighbors` 对挂载 base 的邻域读并不依赖折叠图产物，那些
 *    节点是真的、可交互的。盖住它比这个改动之前的 `building` 态盖法更不诚实：至少
 *    「构建中」还会自己结束。代价如实说：这时候画布上不再有任何「预览不完整」的
 *    提示，用户看到的是叠加出的局部图，而不是全库预览；
 * 4. 后端说没有预览且没人在建，且此刻一个可见节点都没有 → 不可用；
 * 5. 剩下才是「渲染出来是空的」。`visibleNodeCount` 是**过滤/搜索之后**的节点数，
 *    所以 `empty` 保留它原来的含义（搜到空集）。
 *
 * 已知角落（T-W4-3b 顺修 3，只登记不重构）：`use-kg-graph` 的构建轮询在
 * `KG_BACKGROUND_POLL_CAP_MS`（20 分钟）封顶时会 `setVizBuilding(false)` 并弹一句
 * 「图谱索引仍在后台构建，请稍后重新打开查看」，但它**不**回写 `unifiedGraph`
 * （轮询只在响应不再 `viz_building` 时才 setUnifiedGraph），所以此刻
 * `graph.viz_building` 仍是 true 而参数 `vizBuilding` 已是 false。此函数按参数走，
 * 于是封顶后零可见节点的画布落进 `empty`，显示的是「没有匹配的节点。清空搜索后可
 * 查看完整图谱」——与那句 toast 不一致。判据不改读 `graph.viz_building`：那会让
 * 封顶失效（画布永远停在「构建中」），而封顶本身是有意的。现状文案以 toast 为准。
 */
export const kgCanvasState = (
  graph: UnifiedGraphResp | null,
  vizBuilding: boolean,
  visibleNodeCount: number,
): KgCanvasState => {
  if (graph === null) return "loading";
  if (vizBuilding) return "building";
  if (visibleNodeCount > 0) return "graph";
  return graph.viz_unavailable ? "unavailable" : "empty";
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
