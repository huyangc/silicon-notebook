import { requestBlob, requestJson, requestVoid } from "./api-client.ts";
import type {
  PaginatedSources,
  SourceElement,
  SourceSummary,
  UploadedSource,
} from "./workspace-model.ts";

const options = { tag: "api", unauthorized: "clear-and-reload" as const };

export const listSources = (
  notebookId: string,
  offset = 0,
  limit = 50,
  query = "",
) => requestJson<PaginatedSources>(
  `/notebooks/${notebookId}/sources?offset=${offset}&limit=${limit}&q=${encodeURIComponent(query)}`,
  options,
);

// 返回值里可能夹着**没有新建**的既有来源（同笔记本内容相同 → 沿用原条目），
// 每条用 reused 标出。别拿数组长度当「新增了几个」，交给 summarizeUpload 拆。
export const uploadSources = (notebookId: string, body: FormData) =>
  requestJson<UploadedSource[]>(`/notebooks/${notebookId}/sources`, {
    ...options,
    method: "POST",
    body,
  });

export const importUrlSources = (notebookId: string, urls: string[]) =>
  requestJson<{
    created: SourceSummary[];
    rejected: Array<{ url: string; reason: string }>;
  }>(`/notebooks/${notebookId}/sources/url`, {
    ...options,
    method: "POST",
    body: JSON.stringify({ urls }),
  });

export const detectSourceTypes = (
  items: Array<{ name: string; sample: string }>,
) => requestJson<Array<{ name: string; doc_type_id: string }>>(
  "/detect-doc-types",
  { ...options, method: "POST", body: JSON.stringify({ items }) },
);

export const getSource = (id: string) =>
  requestJson<SourceSummary>(`/sources/${id}`, options);

export const getSourceElements = (id: string) =>
  requestJson<SourceElement[]>(`/sources/${id}/elements`, options);

export const parseSource = (id: string) =>
  requestJson<SourceSummary>(`/sources/${id}/parse`, {
    ...options,
    method: "POST",
  });

export const deleteSource = (id: string) =>
  requestVoid(`/sources/${id}`, { ...options, method: "DELETE" });

/** Returns a Blob only; the component owns object-URL creation and revocation. */
export const fetchInternalAssetBlob = (url: string) => requestBlob(url, options);
