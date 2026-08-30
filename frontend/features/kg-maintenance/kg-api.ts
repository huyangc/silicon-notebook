import { requestJson } from "../../app/api-client.ts";
import type { UnifiedKgRebuildStatus } from "./kg-rebuild-status.ts";
import type { RelinkStatus } from "./kg-relink-status.ts";
import type { ScaleIndexStatus } from "../../app/scale-index.ts";
import type {
  ConceptDetailResp,
  KgBuildJobStatus,
  KgNeighborsResp,
  KgSearchResp,
  MergeReviewJob,
  MergeReviewSummary,
  NodeContext,
  PendingMerge,
  UnifiedGraphResp,
  UnifiedKgStatus,
} from "../../app/workspace-model.ts";

const options = { tag: "api", unauthorized: "clear-and-reload" as const };

export type KgBuildStartResponse = {
  status: string;
  notebook_id: string;
  job_id: string;
};

export type IndexStatus = {
  kg: {
    ready: boolean;
    building: boolean;
    pending_sources: number;
    job: KgBuildJobStatus | null;
  };
  unified_kg: { dirty: boolean; building: boolean; last_rebuild_at: string };
  scale_index: ScaleIndexStatus;
};

// 「重新合并」现在是后台任务:POST 只认领任务槽并返回 job_id,聚类数要等
// unified-kg/rebuild/status 报出终态。绝不能拿这里的返回值假装拿到了结果。
export const rebuildUnifiedKg = (nb: string) =>
  requestJson<KgBuildStartResponse>(`/notebooks/${nb}/unified-kg/rebuild`, {
    ...options,
    method: "POST",
  });

export const fetchUnifiedKgRebuildStatus = (nb: string) =>
  requestJson<UnifiedKgRebuildStatus>(
    `/notebooks/${nb}/unified-kg/rebuild/status`,
    options,
  );

export const buildKg = (nb: string) =>
  requestJson<KgBuildStartResponse>(`/notebooks/${nb}/kg/build`, {
    ...options,
    method: "POST",
  });

export const rebuildKg = (nb: string) =>
  requestJson<KgBuildStartResponse>(`/notebooks/${nb}/kg/rebuild`, {
    ...options,
    method: "POST",
  });

// 「补上关联」现在是后台任务:POST 只认领任务槽并返回 job_id,真正的数字要等
// relink/status 报出终态。绝不能拿这里的返回值假装拿到了统计。
export const relinkKg = (nb: string) =>
  requestJson<KgBuildStartResponse>(
    `/notebooks/${nb}/kg/relink`,
    { ...options, method: "POST" },
  );

export const fetchRelinkStatus = (nb: string) =>
  requestJson<RelinkStatus>(`/notebooks/${nb}/kg/relink/status`, options);

export const rebuildScaleIndex = (
  nb: string,
  when: "now" | "idle" = "now",
  mode: "auto" | "fold" | "full" = "auto",
) => requestJson<{ status: string; notebook_id: string }>(
  `/notebooks/${nb}/scale-index/rebuild`,
  { ...options, method: "POST", body: JSON.stringify({ when, mode }) },
);

export const cancelScaleIndex = (nb: string) =>
  requestJson<{ cancelled: boolean; state: string; reason: string }>(
    `/notebooks/${nb}/scale-index/cancel`,
    { ...options, method: "POST" },
  );

export const fetchScaleIndexStatus = (nb: string) =>
  requestJson<ScaleIndexStatus>(`/notebooks/${nb}/scale-index/status`, options);

export const fetchIndexStatus = (nb: string) =>
  requestJson<IndexStatus>(`/notebooks/${nb}/index-status`, options);

export const fetchUnifiedGraph = (nb: string, limit = 0) =>
  requestJson<UnifiedGraphResp>(
    `/notebooks/${nb}/unified-kg?level=object${limit > 0 ? `&limit=${limit}` : ""}`,
    options,
  );

export const fetchKgSearch = (nb: string, query: string, k = 30) =>
  requestJson<KgSearchResp>(
    `/notebooks/${nb}/kg/search?q=${encodeURIComponent(query)}&k=${k}`,
    options,
  );

export const fetchKgNeighbors = (
  nb: string,
  id: string,
  cap = 50,
  sourceNotebookId = "",
) =>
  requestJson<KgNeighborsResp>(
    `/notebooks/${nb}/objects/${encodeURIComponent(id)}/neighbors?cap=${cap}${sourceNotebookId ? `&source_notebook_id=${encodeURIComponent(sourceNotebookId)}` : ""}`,
    options,
  );

// `after` is the hub-cluster member keyset cursor (R3·T-B2): the `id` of the
// last member on the previous page, or "" for the first page. Omitting it
// (the common call shape below) is the first page — backward compatible.
export const fetchConceptDetail = (nb: string, id: string, sourceNotebookId = "", after = "") => {
  const params = new URLSearchParams();
  if (sourceNotebookId) params.set("source_notebook_id", sourceNotebookId);
  if (after) params.set("after", after);
  const query = params.toString();
  return requestJson<ConceptDetailResp>(
    `/notebooks/${nb}/concepts/${encodeURIComponent(id)}/detail${query ? `?${query}` : ""}`,
    options,
  );
};

export const fetchNodeContext = (nb: string, id: string, sourceNotebookId = "") =>
  requestJson<NodeContext>(
    `/notebooks/${nb}/objects/${encodeURIComponent(id)}/context${sourceNotebookId ? `?source_notebook_id=${encodeURIComponent(sourceNotebookId)}` : ""}`,
    options,
  );

export const fetchPendingMerges = (nb: string) =>
  requestJson<PendingMerge[]>(`/notebooks/${nb}/unified-kg/pending-merges`, options);

export const fetchUnifiedKgStatus = (nb: string) =>
  requestJson<UnifiedKgStatus>(`/notebooks/${nb}/unified-kg/status`, options);

export const confirmMerge = (nb: string, id: string) =>
  requestJson<{ ok: boolean }>(
    `/notebooks/${nb}/unified-kg/merges/${encodeURIComponent(id)}/confirm`,
    { ...options, method: "POST" },
  );

export const rejectMerge = (nb: string, id: string) =>
  requestJson<{ ok: boolean }>(
    `/notebooks/${nb}/unified-kg/merges/${encodeURIComponent(id)}/reject`,
    { ...options, method: "POST" },
  );

export const reviewMerges = (nb: string) =>
  requestJson<MergeReviewSummary>(`/notebooks/${nb}/unified-kg/merges/review`, {
    ...options,
    method: "POST",
    body: JSON.stringify({ limit: 50 }),
  });

export const reviewAllMerges = (nb: string) =>
  requestJson<{ status: string }>(`/notebooks/${nb}/unified-kg/merges/review-all`, {
    ...options,
    method: "POST",
  });

export const fetchMergeReviewJob = (nb: string) =>
  requestJson<MergeReviewJob>(`/notebooks/${nb}/unified-kg/merges/review-job`, options);
