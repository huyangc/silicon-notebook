import { requestJson, requestVoid } from "./api-client.ts";
import type { AnswerAnchorLike, CitationLike } from "./answer-formatting.ts";

export type GlobalScope = { mode: "all" } | { mode: "include"; notebook_ids: string[] };
export type GlobalSkippedNotebook = { notebook_id: string; reason: string };
export type GlobalAnswer = {
  answer_id: string;
  question: string;
  answer: string;
  grounded: boolean;
  anchors: AnswerAnchorLike[];
  citations: CitationLike[];
  created_at: string;
  notebook_scope: GlobalScope;
  resolved_notebook_ids: string[];
  searched_notebook_ids: string[];
  cited_notebook_ids: string[];
  skipped_notebooks: GlobalSkippedNotebook[];
  degraded_notebook_ids?: string[];
  completeness_notice: string;
};
export type GlobalJob = {
  job_id: string;
  conversation_id: string;
  status: "running" | "done" | "failed" | "cancelled" | "interrupted";
  question: string;
  created_at: string;
  notebook_scope: GlobalScope;
  resolved_notebook_ids: string[];
  searched_notebook_ids: string[];
  cited_notebook_ids: string[];
  skipped_notebooks: GlobalSkippedNotebook[];
  degraded_notebook_ids?: string[];
  error: string | null;
  response: GlobalAnswer | null;
};
export type GlobalConversation = {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
  notebook_scope: GlobalScope;
  submitted_via: "web" | "mcp";
};
export type GlobalConversationDetail = GlobalConversation & {
  turns: GlobalJob[];
  has_more: boolean;
  next_offset: number | null;
};

export const GLOBAL_ASK_PAGE_SIZE = 50;
const options = { tag: "global-ask" };
const root = "/global-ask";

export const listGlobalConversations = (offset = 0) =>
  requestJson<GlobalConversation[]>(`${root}/conversations?limit=${GLOBAL_ASK_PAGE_SIZE}&offset=${offset}`, options);
export const getGlobalConversation = (id: string, offset = 0) =>
  requestJson<GlobalConversationDetail>(`${root}/conversations/${encodeURIComponent(id)}?limit=${GLOBAL_ASK_PAGE_SIZE}&offset=${offset}`, options);
export const renameGlobalConversation = (id: string, title: string) =>
  requestJson<GlobalConversation>(`${root}/conversations/${encodeURIComponent(id)}`, {
    ...options, method: "PATCH", body: JSON.stringify({ title }),
  });
export const deleteGlobalConversation = (id: string) =>
  requestVoid(`${root}/conversations/${encodeURIComponent(id)}`, { ...options, method: "DELETE" });
export const askGlobal = (input: {
  question: string;
  notebook_scope: GlobalScope;
  conversation_id?: string;
  client_request_id: string;
}) => requestJson<GlobalJob>(`${root}/ask`, { ...options, method: "POST", body: JSON.stringify(input) });
export const getGlobalJob = (id: string) =>
  requestJson<GlobalJob>(`${root}/jobs/${encodeURIComponent(id)}`, options);
export const cancelGlobalJob = (id: string) =>
  requestJson<GlobalJob>(`${root}/jobs/${encodeURIComponent(id)}/cancel`, { ...options, method: "POST" });
// 引用原文全文的读取端点（`GET /global-ask/jobs/{job}/citations/{element}`）在后端
// 保留，MCP 侧仍在用。浏览器这一侧不再有调用方：引用小卡片直接用回答里已经带着的
// anchors/citations（snippet / quoted_span），不为一张卡片再多打一次全文。
