import { requestJson, requestVoid } from "./api-client.ts";
import { requestTaskStream } from "./request-task-stream.ts";
import type { AnswerAnchorLike, CitationLike } from "./answer-formatting.ts";
import type { ReasoningTraceStep } from "./ask-stream.ts";
import type { AskIntentConfirmation, QueryIntentContract } from "./ask-intent-model.ts";
import type { AskRetrievalEffortId } from "./ask-retrieval-effort.ts";
import type { AskResponse } from "./workspace-model.ts";

export type GlobalScope = { mode: "all" } | { mode: "include"; notebook_ids: string[] };

/** 一次全局提问最多参与的笔记本数。后端 `GLOBAL_ASK_MAX_NOTEBOOKS` 的硬上限(`le=8`)
 *  的镜像:超过它的范围后端直接 422,所以界面必须在提交之前就把范围收进这个数。 */
export const GLOBAL_ASK_MAX_NOTEBOOKS = 8;

/** 「全部」在可读笔记本超过上限时不是一个可提交的范围。此时预选来源最多的那几个
 *  (列表没有「最近使用」这类字段;来源多的库是更可能被问到的库),同数按列表原序。
 *  不超过上限时原样返回传入的范围。 */
export function submittableGlobalScope(
  scope: GlobalScope,
  notebooks: ReadonlyArray<{ id: string; counts: Record<string, number> }>,
): GlobalScope {
  if (scope.mode !== "all" || notebooks.length <= GLOBAL_ASK_MAX_NOTEBOOKS) return scope;
  const ranked = notebooks
    .map((item, index) => ({ id: item.id, sources: item.counts.sources ?? 0, index }))
    .sort((left, right) => right.sources - left.sources || left.index - right.index)
    .slice(0, GLOBAL_ASK_MAX_NOTEBOOKS)
    .sort((left, right) => left.index - right.index);
  return { mode: "include", notebook_ids: ranked.map((item) => item.id) };
}
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
  /** 本轮使用的引擎 id（后端默认 "chunk"）。 */
  mode: string;
  /** 运行中逐步追加的推理轨迹；完成后以 `answer.reasoning_trace` 为准，
   *  它可能被清空。chunk 引擎没有轨迹。 */
  trace?: ReasoningTraceStep[];
  /** 全局问答直接调单库引擎之后的标准回答。**新作业只有它**。 */
  answer?: AskResponse | null;
  /** 旧的跨库合成回答。**只有历史轮次才有**；新作业恒为 null。 */
  response: GlobalAnswer | null;
  /** 这条回答收到的反馈:"" 未反馈,否则 "useful" / "not_useful"。首次写入
   *  为准——一旦非空就不会再变,与按钮「点过就禁用」的界面契约一致。旧作业
   *  没有这个字段,读到时按未反馈处理。 */
  feedback?: string;
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
  /** 省略即后端默认 "chunk"。未知或扩展引擎后端 422（全局问答不挂部署扩展引擎）。 */
  mode?: string;
  /** 「逐步推理」的问题理解结果。预检说需要澄清时由用户在审阅卡里补齐后回传。 */
  intent?: AskIntentConfirmation;
  retrieval_effort?: AskRetrievalEffortId;
}) => requestJson<GlobalJob>(`${root}/ask`, { ...options, method: "POST", body: JSON.stringify(input) });

/**
 * 全局问答的问题理解预检。与单库的 `/notebooks/{id}/ask/intent/stream` 完全同形
 * （同一个 task-stream 传输、同一份 `QueryIntentContract`），所以这里逐字照搬
 * `previewAskIntent` 的写法，只换端点与请求体里的范围字段。
 */
export const previewGlobalAskIntent = (
  question: string,
  conversationId?: string | null,
  notebookScope?: GlobalScope,
  signal?: AbortSignal,
  onHeartbeat?: (elapsedMs: number) => void | Promise<void>,
) => requestTaskStream<QueryIntentContract>(
  `${root}/intent/stream`,
  {
    ...options,
    method: "POST",
    body: JSON.stringify({
      question,
      conversation_id: conversationId || undefined,
      notebook_scope: notebookScope,
    }),
    signal,
  },
  {
    onHeartbeat: (elapsedMs) => onHeartbeat?.(elapsedMs),
    fallbackMessage: "问题理解没能完成，请重试",
  },
);
export const getGlobalJob = (id: string) =>
  requestJson<GlobalJob>(`${root}/jobs/${encodeURIComponent(id)}`, options);
export const cancelGlobalJob = (id: string) =>
  requestJson<GlobalJob>(`${root}/jobs/${encodeURIComponent(id)}/cancel`, { ...options, method: "POST" });
export const submitGlobalFeedback = (jobId: string, rating: "useful" | "not_useful") =>
  requestJson<GlobalJob>(`${root}/jobs/${encodeURIComponent(jobId)}/feedback`, {
    ...options, method: "POST", body: JSON.stringify({ rating }),
  });
// 引用原文全文的读取端点（`GET /global-ask/jobs/{job}/citations/{element}`）在后端
// 保留，MCP 侧仍在用。浏览器这一侧不再有调用方：引用小卡片直接用回答里已经带着的
// anchors/citations（snippet / quoted_span），不为一张卡片再多打一次全文。
