import {
  performApiRequest,
  requestJson,
  requestVoid,
} from "./api-client.ts";
import {
  humanizedError,
  logDiagnostic,
  throwHumanizedHttpError,
} from "./errors.ts";
import {
  takeNdjsonLines,
  type AskStreamEvent,
  type ReasoningTraceStep,
} from "./ask-stream.ts";
import type { AskJobDetail } from "./ask-reconnect.ts";
import type { QueryIntentContract } from "./ask-intent-model.ts";
import type { BaseScopePayload, SourceScopePayload } from "./source-scope.ts";
import type {
  AskResponse,
  ConversationDetail,
  ConversationSummary,
  SearchHit,
} from "./workspace-model.ts";

const options = { tag: "api", unauthorized: "clear-and-reload" as const };

// The collection search box answers "which notebook contains X", so it calls
// `searchNotebook` once per visible notebook — and one of those may be a
// reference library with millions of knowledge objects.  Firing the whole fan-out
// at once put every one of those queries in the connection pool simultaneously.
export const SEARCH_FANOUT_LIMIT = 4;

// The permit pool is MODULE level, not per fan-out, and that is the whole point.
// `AbortController.abort()` cancels the browser's fetch; it does NOT cancel the
// work, because `search_notebook` is a synchronous FastAPI route doing blocking
// SQL in a threadpool and never observes the disconnect.  So a per-call pool of
// four still lets rapid typing stack four more live queries per keystroke
// generation, against a connection pool of ten (codex #460 round-1 P1).  Sharing
// the permits makes the bound hold across generations: four in flight, total,
// no matter how many fan-outs are alive.
let searchPermitsHeld = 0;
const searchPermitWaiters: (() => void)[] = [];

async function acquireSearchPermit(): Promise<void> {
  if (searchPermitsHeld < SEARCH_FANOUT_LIMIT) {
    searchPermitsHeld += 1;
    return;
  }
  await new Promise<void>((resolve) => searchPermitWaiters.push(resolve));
  // The permit was handed over by `releaseSearchPermit`, which deliberately
  // leaves the count alone: transferring rather than re-incrementing is what
  // stops two waiters from both claiming the same freed slot.
}

function releaseSearchPermit(): void {
  const next = searchPermitWaiters.shift();
  if (next) next();
  else searchPermitsHeld -= 1;
}

export const searchNotebook = (id: string, query: string, signal?: AbortSignal) =>
  requestJson<{ hits: SearchHit[] }>(
    `/notebooks/${id}/search?q=${encodeURIComponent(query)}`,
    { ...options, signal },
  );

/** Search every notebook for `query`, holding at most `SEARCH_FANOUT_LIMIT`
 * searches in flight — counted across every live fan-out, not per call.
 *
 * Each notebook waits for a module-level permit before it issues anything, so a
 * superseded generation cannot add its own four on top of the current one's.
 * A generation whose signal has already aborted returns its permit without
 * issuing a request at all, which is what lets a fresh fan-out take the slots
 * back within a microtask instead of behind the old queue.
 *
 * `search` is injected so the concurrency bound is testable without a network.
 */
export async function searchNotebooksBounded(
  notebookIds: readonly string[],
  query: string,
  signal?: AbortSignal,
  search: (
    id: string, query: string, signal?: AbortSignal,
  ) => Promise<{ hits: SearchHit[] }> = searchNotebook,
): Promise<Record<string, SearchHit[]>> {
  const hits: Record<string, SearchHit[]> = {};
  await Promise.all(notebookIds.map(async (id) => {
    await acquireSearchPermit();
    try {
      // Re-checked AFTER the wait, not before it: the abort almost always lands
      // while this task sits in the queue, and issuing then would spend a real
      // query on a generation whose answer is already discarded.
      signal?.throwIfAborted();
      hits[id] = (await search(id, query, signal)).hits;
    } finally {
      releaseSearchPermit();
    }
  }));
  return hits;
}

export const previewAskIntent = (
  notebookId: string,
  question: string,
  conversationId?: string | null,
  signal?: AbortSignal,
  sourceScope?: SourceScopePayload,
  // 预检必须与执行用**同一份**参考库上限：预检读语料目录、执行读证据，两者用不同
  // 范围就会出现「预检说搜得到、执行搜不到」。省略即历史行为（全部挂载库参与）。
  baseScope?: BaseScopePayload,
) => requestJson<QueryIntentContract>(`/notebooks/${notebookId}/ask/intent`, {
  ...options,
  method: "POST",
  body: JSON.stringify({
    question,
    conversation_id: conversationId || undefined,
    source_scope: sourceScope,
    base_scope: baseScope,
  }),
  signal,
});

export async function runAskStream<TResponse = AskResponse>(
  notebookId: string,
  payload: unknown,
  onProgress: (step: ReasoningTraceStep) => void | Promise<void>,
  signal?: AbortSignal,
  onStart?: (jobId: string, conversationId: string) => void | Promise<void>,
): Promise<TResponse> {
  const response = await performApiRequest(`/notebooks/${notebookId}/ask/stream`, {
    ...options,
    method: "POST",
    body: JSON.stringify(payload),
    signal,
  });
  if (!response.ok) await throwHumanizedHttpError(response, "api");
  if (!response.body) throw new Error("Streaming response body is unavailable");

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let finalResponse: TResponse | null = null;

  const yieldToPaint = () => new Promise<void>((resolve) => {
    if (typeof window === "undefined" || typeof window.requestAnimationFrame !== "function") {
      resolve();
      return;
    }
    window.requestAnimationFrame(() => resolve());
  });

  const consumeLine = async (line: string) => {
    const event = JSON.parse(line) as AskStreamEvent<TResponse>;
    if (event.event === "started") {
      await onStart?.(event.job_id, event.conversation_id);
    }
    else if (event.event === "progress") {
      await onProgress(event.step);
      await yieldToPaint();
    } else if (event.event === "final") finalResponse = event.response;
    else if (event.event === "cancelled") {
      throw new DOMException("已中断回答", "AbortError");
    } else if (event.event === "error") {
      logDiagnostic("ask-stream", event.error);
      throw humanizedError("回答没能完成，请重试");
    } else {
      const exhaustive: never = event;
      throw new Error(`unknown ask stream event: ${JSON.stringify(exhaustive)}`);
    }
  };

  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += decoder.decode(value, { stream: true });
    const parsed = takeNdjsonLines(buffer);
    buffer = parsed.remainder;
    for (const line of parsed.lines) await consumeLine(line);
  }
  buffer += decoder.decode();
  if (buffer.trim()) await consumeLine(buffer.trim());
  if (!finalResponse) throw new Error("Streaming response ended without a final answer");
  return finalResponse;
}

export const cancelAskJob = (nb: string, id: string) =>
  requestVoid(`/notebooks/${nb}/ask/jobs/${id}/cancel`, {
    ...options,
    method: "POST",
  });

export const getAskJob = (nb: string, id: string) =>
  requestJson<AskJobDetail>(`/notebooks/${nb}/ask/jobs/${id}`, options);

export const listConversations = (nb: string) =>
  requestJson<ConversationSummary[]>(`/notebooks/${nb}/conversations`, options);

export const getConversation = (id: string) =>
  requestJson<ConversationDetail>(`/conversations/${id}`, options);

export const renameConversation = (id: string, title: string) =>
  requestVoid(`/conversations/${id}`, {
    ...options,
    method: "PATCH",
    body: JSON.stringify({ title }),
  });

export const deleteConversation = (id: string) =>
  requestVoid(`/conversations/${id}`, { ...options, method: "DELETE" });

export const bulkDeleteConversations = (nb: string, days: number) =>
  requestJson<{ deleted: number; deleted_ids: string[] }>(
    `/notebooks/${nb}/conversations?older_than_days=${days}`,
    { ...options, method: "DELETE" },
  );

export const submitFeedback = (
  id: string,
  rating: "useful" | "not_useful",
  comment: string,
) => requestVoid(`/answers/${id}/feedback`, {
  ...options,
  method: "POST",
  body: JSON.stringify({ rating, comment }),
});

export const fetchAnswerMemoryLinks = (
  nb: string,
  answerIds: string[],
  signal?: AbortSignal,
) => requestJson<{ links: Record<string, string> }>(
  `/notebooks/${nb}/answer-memory-links`,
  {
    ...options,
    method: "POST",
    body: JSON.stringify({ answer_ids: answerIds }),
    signal,
  },
);
