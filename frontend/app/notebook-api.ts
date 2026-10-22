import { requestJson } from "./api-client.ts";
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

export type NotebookQuestionSuggestion = Readonly<{
  label: string;
  question: string;
}>;

export type NotebookQuestionSuggestions = Readonly<{
  status: "ready" | "fallback";
  questions: NotebookQuestionSuggestion[];
  sampled: boolean;
}>;

export const fetchNotebookQuestionSuggestions = (id: string, signal: AbortSignal) =>
  requestJson<NotebookQuestionSuggestions>(`/notebooks/${id}/question-suggestions`, {
    ...options,
    method: "POST",
    signal,
  });

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

/** 删除整本笔记本(前端唯一的一份 DELETE 客户端):202 `{status: "pending_delete", delete_after}`
 *  ——这本笔记本进入一天的删除窗口,此刻起对所有人不可用,`delete_after`(UTC ISO)起永久删除,
 *  在此之前主人可以撤销。202 本身就是「已安排删除」:回答里读不出 `delete_after` 时返回
 *  `deleteAfter: null`,提示不写时刻,调用方随后重新拉取列表与区块拿到真实时刻——绝不把一次
 *  已经生效的删除报成失败,也不编一个时刻出来。 */
export async function deleteNotebook(id: string): Promise<{ deleteAfter: string | null }> {
  const body = await requestJson<{ delete_after?: unknown } | null>(
    `/notebooks/${encodeURIComponent(id)}`,
    { ...options, method: "DELETE" },
  );
  const deleteAfter = body?.delete_after;
  return {
    deleteAfter: typeof deleteAfter === "string" && Number.isFinite(Date.parse(deleteAfter))
      ? deleteAfter
      : null,
  };
}

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
