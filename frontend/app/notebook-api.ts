import { requestJson, requestVoid } from "./api-client";
import type { NotebookAnalytics, NotebookContentOverview, NotebookSummary } from "./workspace-model";

const options = { tag: "api", unauthorized: "clear-and-reload" as const };
export const listNotebooks = () => requestJson<NotebookSummary[]>("/notebooks", options);
export const createNotebook = (payload: unknown) => requestJson<NotebookSummary>("/notebooks", { ...options, method: "POST", body: JSON.stringify(payload) });
export const getNotebook = (id: string) => requestJson<NotebookSummary>(`/notebooks/${id}`, options);
export const updateNotebook = (id: string, patch: unknown) => requestJson<NotebookSummary>(`/notebooks/${id}`, { ...options, method: "PATCH", body: JSON.stringify(patch) });
export const deleteNotebook = (id: string) => requestVoid(`/notebooks/${id}`, { ...options, method: "DELETE" });
export const fetchNotebookAnalytics = (id: string) => requestJson<NotebookAnalytics>(`/notebooks/${id}/analytics`, options);
export const fetchNotebookContentOverview = (id: string) => requestJson<NotebookContentOverview>(`/notebooks/${id}/analytics/content-overview`, options);
export const searchNotebook = (id: string, query: string) => requestJson<{ hits: import("./workspace-model").SearchHit[] }>(`/notebooks/${id}/search?q=${encodeURIComponent(query)}`, options);
export const backfillPaperMetadata = (id: string) => requestJson<{ queued: number }>(`/notebooks/${id}/paper-meta/backfill`, { ...options, method: "POST" });
