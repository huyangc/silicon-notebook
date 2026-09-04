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
  large_library_locked?: boolean;
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

export const getNotebook = (id: string) =>
  requestJson<NotebookSummary>(`/notebooks/${id}`, options);

export const updateNotebook = (id: string, patch: unknown) =>
  requestJson<NotebookSummary>(`/notebooks/${id}`, {
    ...options,
    method: "PATCH",
    body: JSON.stringify(patch),
  });

export const fetchNotebookIndexingPipeline = (id: string) =>
  requestJson<IndexingPipelineResponse>(`/notebooks/${id}/indexing-pipeline`, options);

export const setNotebookIndexingPipeline = (id: string, pipelineId: string | null) =>
  requestJson<IndexingPipelineResponse>(`/notebooks/${id}/indexing-pipeline`, {
    ...options,
    method: "PATCH",
    body: JSON.stringify({ pipeline_id: pipelineId }),
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
