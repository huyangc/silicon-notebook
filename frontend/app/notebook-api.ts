import { requestJson, requestVoid } from "./api-client.ts";
import type {
  NotebookAnalytics,
  NotebookContentOverview,
  NotebookSummary,
} from "./workspace-model.ts";

const options = { tag: "api", unauthorized: "clear-and-reload" as const };

export type IndexingPipelineOption = Readonly<{
  pipeline_id?: string | null;
  label: string;
  description: string;
  version: string;
  overrides_chunking?: boolean;
  overrides_kg_extraction?: boolean;
  available?: boolean;
  selected?: boolean;
}>;

export type IndexingPipelineResponse = Readonly<{
  pipeline_id?: string | null;
  version: string;
  available: boolean;
  missing: boolean;
  pending: boolean;
  options: IndexingPipelineOption[];
  changed?: boolean;
  warning_count?: number;
  rebuild_status?: string;
  job_id?: string | null;
}>;

export const listNotebooks = () =>
  requestJson<NotebookSummary[]>("/notebooks", options);

export const createNotebook = (payload: unknown) =>
  requestJson<NotebookSummary>("/notebooks", {
    ...options,
    method: "POST",
    body: JSON.stringify(payload),
  });

// `signal` 只加在**多步写序列**用到的那几条上:一次保存要连打 PATCH → 管线 → PUT
// bases → GET,用户中途关掉弹窗后必须能把在飞那一条掐掉——否则它可能在用户重开、
// 重存之后才落到服务端,拿旧值盖掉新值。其余读接口没有这个形态,不加参数。
export const getNotebook = (id: string, signal?: AbortSignal) =>
  requestJson<NotebookSummary>(`/notebooks/${id}`, { ...options, signal });

export const updateNotebook = (id: string, patch: unknown, signal?: AbortSignal) =>
  requestJson<NotebookSummary>(`/notebooks/${id}`, {
    ...options,
    method: "PATCH",
    body: JSON.stringify(patch),
    signal,
  });

export const fetchNotebookIndexingPipeline = (id: string) =>
  requestJson<IndexingPipelineResponse>(`/notebooks/${id}/indexing-pipeline`, options);

export const setNotebookIndexingPipeline = (
  id: string,
  pipelineId: string | null,
  signal?: AbortSignal,
) =>
  requestJson<IndexingPipelineResponse>(`/notebooks/${id}/indexing-pipeline`, {
    ...options,
    method: "PATCH",
    body: JSON.stringify({ pipeline_id: pipelineId }),
    signal,
  });

export const deleteNotebook = (id: string) =>
  requestVoid(`/notebooks/${id}`, { ...options, method: "DELETE" });

export const fetchNotebookAnalytics = (id: string) =>
  requestJson<NotebookAnalytics>(`/notebooks/${id}/analytics`, options);

export const fetchNotebookContentOverview = (id: string) =>
  requestJson<NotebookContentOverview>(
    `/notebooks/${id}/analytics/content-overview`,
    options,
  );

export const backfillPaperMetadata = (id: string) =>
  requestJson<{ queued: number }>(`/notebooks/${id}/paper-meta/backfill`, {
    ...options,
    method: "POST",
  });
