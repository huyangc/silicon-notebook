import { requestJson, requestVoid } from "./api-client.ts";
import type {
  DuplicateGroup,
  KnowledgeGraph,
  KnowledgeKind,
  KnowledgeTypeCount,
  ObjectSchema,
  OwnMemoryDuplicates,
  PaginatedKnowledge,
} from "./workspace-model.ts";

const options = { tag: "api", unauthorized: "clear-and-reload" as const };

export const listKnowledge = (
  nb: string,
  kind: KnowledgeKind,
  status: string,
  offset: number,
  limit: number,
) => requestJson<PaginatedKnowledge>(
  `/notebooks/${nb}/knowledge?type=${encodeURIComponent(kind)}&status=${encodeURIComponent(status)}&offset=${offset}&limit=${limit}`,
  options,
);

export const listKnowledgeTypes = (nb: string) =>
  requestJson<KnowledgeTypeCount[]>(`/notebooks/${nb}/knowledge-types`, options);

export const updateKnowledge = (nb: string, id: string, patch: unknown) =>
  requestVoid(`/notebooks/${nb}/knowledge/${id}`, {
    ...options,
    method: "PATCH",
    body: JSON.stringify(patch),
  });

export const findDuplicates = (nb: string, kind: KnowledgeKind) =>
  requestJson<DuplicateGroup[]>(
    `/notebooks/${nb}/duplicates?type=${encodeURIComponent(kind)}`,
    options,
  );

/** 只由调用者本人记忆支撑的重复组;共享 `/duplicates` 对所有人都不列这些对象。 */
export const findOwnMemoryDuplicates = (nb: string, kind: KnowledgeKind) =>
  requestJson<OwnMemoryDuplicates>(
    `/notebooks/${nb}/duplicates/own-memory?type=${encodeURIComponent(kind)}`,
    options,
  );

export const mergeKnowledge = (nb: string, sourceId: string, intoId: string) =>
  requestVoid(`/notebooks/${nb}/knowledge/${sourceId}/merge`, {
    ...options,
    method: "POST",
    body: JSON.stringify({ into_id: intoId }),
  });

export const listObjectSchemas = () => requestJson<ObjectSchema[]>("/object-schemas", options);

export const listNotebookObjectSchemas = (nb: string) =>
  requestJson<ObjectSchema[]>(`/notebooks/${nb}/object-schemas`, options);

export const createObjectSchema = (payload: unknown) =>
  requestVoid("/object-schemas", {
    ...options,
    method: "POST",
    body: JSON.stringify(payload),
  });

export const createNotebookObjectSchema = (nb: string, payload: unknown) =>
  requestVoid(`/notebooks/${nb}/object-schemas`, {
    ...options,
    method: "POST",
    body: JSON.stringify(payload),
  });

// 自定义对象类型名是用户内容,日志里只写路由形状。
const OBJECT_SCHEMA_LOG_PATH = "/object-schemas/{object_type}";
const notebookObjectSchemaLogPath = (nb: string) => `/notebooks/${nb}/object-schemas/{object_type}`;

export const updateObjectSchema = (type: string, patch: unknown) =>
  requestVoid(`/object-schemas/${encodeURIComponent(type)}`, {
    ...options,
    method: "PATCH",
    body: JSON.stringify(patch),
    logPath: OBJECT_SCHEMA_LOG_PATH,
  });

export const updateNotebookObjectSchema = (nb: string, type: string, patch: unknown) =>
  requestVoid(`/notebooks/${nb}/object-schemas/${encodeURIComponent(type)}`, {
    ...options,
    method: "PATCH",
    body: JSON.stringify(patch),
    logPath: notebookObjectSchemaLogPath(nb),
  });

export const deleteObjectSchema = (type: string) =>
  requestVoid(`/object-schemas/${encodeURIComponent(type)}`, {
    ...options,
    method: "DELETE",
    logPath: OBJECT_SCHEMA_LOG_PATH,
  });

export const deleteNotebookObjectSchema = (nb: string, type: string) =>
  requestVoid(`/notebooks/${nb}/object-schemas/${encodeURIComponent(type)}`, {
    ...options,
    method: "DELETE",
    logPath: notebookObjectSchemaLogPath(nb),
  });

export const proposeObjectSchemas = (nb: string) =>
  requestJson<ObjectSchema[]>(`/notebooks/${nb}/schema-proposals`, {
    ...options,
    method: "POST",
  });

export const getKnowledgeGraph = (nb: string) =>
  requestJson<KnowledgeGraph>(`/notebooks/${nb}/graph`, options);
