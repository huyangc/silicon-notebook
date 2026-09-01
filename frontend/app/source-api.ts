import { requestBlob, requestJson, requestVoid } from "./api-client.ts";
import type {
  CheckupResponse,
  PaginatedSourceElements,
  PaginatedSources,
  RepairScheduledResult,
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
  signal?: AbortSignal,
) => requestJson<PaginatedSources>(
  `/notebooks/${notebookId}/sources?offset=${offset}&limit=${limit}&q=${encodeURIComponent(query)}`,
  { ...options, signal },
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

export const getSourceElementsPage = (
  id: string,
  offset = 0,
  limit = 40,
  anchorElementId = "",
) => requestJson<PaginatedSourceElements>(
  `/sources/${id}/elements-page?offset=${offset}&limit=${limit}&anchor_element_id=${encodeURIComponent(anchorElementId)}`,
  options,
);

// 参与集内的代理读取:路径里的 notebookId 是**当前 active 笔记本**（权限按它判），
// 来源本身可以属于它有效挂载的任一参考库。挂载参考库不等于获得该库的直接成员权限
// （红线），所以浏览器一律不去直连另一个库——由后端在 participant 集内解析并代理读取。
// 参与集首项恒为 active 自身，本库来源走的也是这条路径，调用方不必先判断跨不跨库。
export const getNotebookSource = (notebookId: string, id: string) =>
  requestJson<SourceSummary>(`/notebooks/${notebookId}/sources/${id}`, options);

export const getNotebookSourceElements = (notebookId: string, id: string) =>
  requestJson<SourceElement[]>(`/notebooks/${notebookId}/sources/${id}/elements`, options);

export const getNotebookSourceElementsPage = (
  notebookId: string,
  id: string,
  offset = 0,
  limit = 40,
  anchorElementId = "",
) => requestJson<PaginatedSourceElements>(
  `/notebooks/${notebookId}/sources/${id}/elements-page?offset=${offset}&limit=${limit}&anchor_element_id=${encodeURIComponent(anchorElementId)}`,
  options,
);

export const parseSource = (id: string) =>
  requestJson<SourceSummary>(`/sources/${id}/parse`, {
    ...options,
    method: "POST",
  });

export const deleteSource = (id: string) =>
  requestVoid(`/sources/${id}`, { ...options, method: "DELETE" });

/** Returns a Blob only; the component owns object-URL creation and revocation. */
export const fetchInternalAssetBlob = (url: string) => requestBlob(url, options);

// 流水线体检(P2):只读聚合 H2–H8。看板弹窗打开时拉取(与 fetchIndexStatus 同处)。
export const fetchCheckup = (notebookId: string) =>
  requestJson<CheckupResponse>(`/notebooks/${notebookId}/checkup`, options);

// 体检修复(H2/H3 空源·缺分块):批量重新解析。source_ids 从命中项的 sample 带来;
// 后端按 notebook 作用域过滤后逐个后台重跑既有摄取管线。
export const reparseSources = (notebookId: string, sourceIds: string[]) =>
  requestJson<RepairScheduledResult>(`/notebooks/${notebookId}/sources/reparse`, {
    ...options,
    method: "POST",
    body: JSON.stringify({ source_ids: sourceIds }),
  });

// 体检修复(H4/H5 缺向量):后台补齐该 notebook 缺失的检索向量(只补缺失、幂等)。
export const backfillVectors = (notebookId: string) =>
  requestJson<RepairScheduledResult>(`/notebooks/${notebookId}/backfill-vectors`, {
    ...options,
    method: "POST",
  });
