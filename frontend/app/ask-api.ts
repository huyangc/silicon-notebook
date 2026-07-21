import { performApiRequest, requestJson, requestVoid } from "./api-client";
import { humanizedError, logDiagnostic } from "./errors";
import { takeNdjsonLines, type AskStreamEvent, type ReasoningTraceStep } from "./ask-stream";
import type { AskResponse, ConversationDetail, ConversationSummary } from "./workspace-model";
import type { AskJobDetail } from "./ask-reconnect";

const options = { tag: "api", unauthorized: "clear-and-reload" as const };
export async function runAskStream<TResponse = AskResponse>(notebookId: string, payload: unknown, onProgress: (step: ReasoningTraceStep) => void | Promise<void>, signal?: AbortSignal, onStart?: (jobId: string) => void): Promise<TResponse> {
  const response = await performApiRequest(`/notebooks/${notebookId}/ask/stream`, { ...options, method: "POST", body: JSON.stringify(payload), signal });
  if (!response.ok) { const { throwHumanizedHttpError } = await import("./errors"); await throwHumanizedHttpError(response, "api"); }
  if (!response.body) throw new Error("Streaming response body is unavailable");
  const reader = response.body.getReader(); const decoder = new TextDecoder(); let buffer = ""; let finalResponse: TResponse | null = null;
  const yieldToPaint = () => new Promise<void>((resolve) => typeof window === "undefined" || typeof window.requestAnimationFrame !== "function" ? resolve() : window.requestAnimationFrame(() => resolve()));
  const consumeLine = async (line: string) => { const event = JSON.parse(line) as AskStreamEvent<TResponse>; if (event.event === "started") onStart?.(event.job_id); else if (event.event === "progress") { await onProgress(event.step); await yieldToPaint(); } else if (event.event === "final") finalResponse = event.response; else if (event.event === "cancelled") throw new DOMException("已中断回答", "AbortError"); else if (event.event === "error") { logDiagnostic("ask-stream", event.error); throw humanizedError("回答没能完成，请重试"); } else { const exhaustive: never = event; throw new Error(`unknown ask stream event: ${JSON.stringify(exhaustive)}`); } };
  while (true) { const { value, done } = await reader.read(); if (done) break; buffer += decoder.decode(value, { stream: true }); const parsed = takeNdjsonLines(buffer); buffer = parsed.remainder; for (const line of parsed.lines) await consumeLine(line); }
  buffer += decoder.decode(); if (buffer.trim()) await consumeLine(buffer.trim()); if (!finalResponse) throw new Error("Streaming response ended without a final answer"); return finalResponse;
}
export const cancelAskJob = (nb: string, id: string) => requestVoid(`/notebooks/${nb}/ask/jobs/${id}/cancel`, { ...options, method: "POST" });
export const getAskJob = (nb: string, id: string) => requestJson<AskJobDetail>(`/notebooks/${nb}/ask/jobs/${id}`, options);
export const listConversations = (nb: string) => requestJson<ConversationSummary[]>(`/notebooks/${nb}/conversations`, options);
export const getConversation = (id: string) => requestJson<ConversationDetail>(`/conversations/${id}`, options);
export const renameConversation = (id: string, title: string) => requestVoid(`/conversations/${id}`, { ...options, method: "PATCH", body: JSON.stringify({ title }) });
export const deleteConversation = (id: string) => requestVoid(`/conversations/${id}`, { ...options, method: "DELETE" });
export const bulkDeleteConversations = (nb: string, days: number) => requestJson<{ deleted: number }>(`/notebooks/${nb}/conversations?older_than_days=${days}`, { ...options, method: "DELETE" });
export const submitFeedback = (id: string, rating: "useful" | "not_useful", comment: string) => requestVoid(`/answers/${id}/feedback`, { ...options, method: "POST", body: JSON.stringify({ rating, comment }) });
export const fetchAnswerMemoryLinks = (nb: string, answerIds: string[], signal?: AbortSignal) => requestJson<{ links: Record<string, string> }>(`/notebooks/${nb}/answer-memory-links`, { ...options, method: "POST", body: JSON.stringify({ answer_ids: answerIds }), signal });
