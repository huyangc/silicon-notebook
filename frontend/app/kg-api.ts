import { requestJson } from "./api-client.ts";
import type { ScaleIndexStatus } from "./scale-index.ts";
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
} from "./workspace-model.ts";

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

export const rebuildUnifiedKg = (nb: string) =>
  requestJson<{ clusters: number }>(`/notebooks/${nb}/unified-kg/rebuild`, {
    ...options,
    method: "POST",
  });

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

export const relinkKg = (nb: string) =>
  requestJson<{ isolated_before: number; edges_added: number; isolated_after: number }>(
    `/notebooks/${nb}/kg/relink`,
    { ...options, method: "POST" },
  );

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

export const fetchKgNeighbors = (nb: string, id: string, cap = 50) =>
  requestJson<KgNeighborsResp>(
    `/notebooks/${nb}/objects/${encodeURIComponent(id)}/neighbors?cap=${cap}`,
    options,
  );

export const fetchConceptDetail = (nb: string, id: string) =>
  requestJson<ConceptDetailResp>(
    `/notebooks/${nb}/concepts/${encodeURIComponent(id)}/detail`,
    options,
  );

export const fetchNodeContext = (nb: string, id: string) =>
  requestJson<NodeContext>(
    `/notebooks/${nb}/objects/${encodeURIComponent(id)}/context`,
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
