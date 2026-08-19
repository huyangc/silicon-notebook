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
  ConversationShareResponse,
  ConversationSummary,
  SearchHit,
} from "./workspace-model.ts";

const options = { tag: "api", unauthorized: "clear-and-reload" as const };

// The collection search box answers "which notebook contains X", so it calls
// `searchNotebook` once per visible notebook — and one of those may be a
// reference library with millions of knowledge objects.  Firing the whole fan-out
// at once put every one of those queries in the connection pool simultaneously.
export const SEARCH_FANOUT_LIMIT = 4;

// The permit pool is MODULE level, not per fan-out, and a permit is held for as
// long as the SERVER is busy — both halves are load-bearing, and each was a
// separate review finding.
//
// `search_notebook` is a synchronous FastAPI route running blocking SQL in a
// threadpool: it never observes a client disconnect, so `AbortController.abort()`
// stops the browser waiting but not the query or the connection it holds.
//
//   * Per-fan-out permits let rapid typing stack four more LIVE queries per
//     keystroke generation against a pool of ten (codex #460 round-1 P1).
//   * Module-level permits released on `abort` are barely better: the client
//     promise rejects at once, the permit comes back at once, and the next
//     generation takes it while the previous query is still executing — the
//     permit counted browser waits, not database work (round-2 P1).
//
// Hence the deliberate non-abort below.  Cancelling a fetch we cannot cancel
// server-side only buys the illusion of stopping; making the new generation wait
// for the permit is the honest version, because the server really is still busy.
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

// No `signal` parameter, deliberately: see the permit-pool note above.  Handing
// one to `fetch` would let a superseded generation return its permit while the
// query it started keeps running, which is exactly the bound this module claims
// to enforce and would not.
export const searchNotebook = (id: string, query: string) =>
  requestJson<{ hits: SearchHit[] }>(
    `/notebooks/${id}/search?q=${encodeURIComponent(query)}`,
    options,
  );

/** Search every notebook for `query`, with at most `SEARCH_FANOUT_LIMIT`
 * searches running ON THE SERVER — counted across every live fan-out.
 *
 * `signal` is a gate on ISSUING a request, never a cancellation of one already
 * issued.  A superseded generation therefore stops sending anything within a
 * microtask, while whatever it already handed the server keeps its permit until
 * the server answers.  The next generation waits for those permits, and that
 * wait is the honest behaviour: the database really is still busy, and letting
 * a fresh fan-out start four more would just queue them inside a ten-connection
 * pool instead of here.
 *
 * `search` is injected so the concurrency bound is testable without a network.
 */
export async function searchNotebooksBounded(
  notebookIds: readonly string[],
  query: string,
  signal?: AbortSignal,
  search: (id: string, query: string) => Promise<{ hits: SearchHit[] }> = searchNotebook,
): Promise<Record<string, SearchHit[]>> {
  const hits: Record<string, SearchHit[]> = {};
  await Promise.all(notebookIds.map(async (id) => {
    await acquireSearchPermit();
    try {
      // Checked AFTER the wait, not before it: the abort almost always lands
      // while this task sits in the queue, and this is the last moment at which
      // skipping is still free — one line later the server owns the work.
      signal?.throwIfAborted();
      hits[id] = (await search(id, query)).hits;
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

// 会话公开分享（T5）。三个端点都在**主 router**（带 router 级鉴权 + 行级 created_by
// 门），与匿名公开页端点分属两侧——链接口令就是凭证，发放/回读/撤销都是写级动作。
// 「分享」与「更新到最新」是同一个 POST：幂等复用链接口令，同时把水位推到 client
// 披露到的那条答案。`expectedThroughId` 是弹窗据以算披露的那批 turns 里**最新**一条
// 的 answer_id：服务端把水位钉死在它上,发布的快照 == 披露的快照,关闭「披露到 X、实际
// 公开到更新的 Y」的 TOCTOU(codex #522 R2 P1)。空串回退「当前最新」(旧行为)。
export const shareConversation = (nb: string, cid: string, expectedThroughId = "") =>
  requestJson<ConversationShareResponse>(
    `/notebooks/${nb}/conversations/${cid}/share`,
    {
      ...options,
      method: "POST",
      body: JSON.stringify({ expected_through_id: expectedThroughId }),
    },
  );

export const getConversationShare = (nb: string, cid: string) =>
  requestJson<ConversationShareResponse>(
    `/notebooks/${nb}/conversations/${cid}/share`,
    options,
  );

export const unshareConversation = (nb: string, cid: string) =>
  requestVoid(`/notebooks/${nb}/conversations/${cid}/share`, {
    ...options,
    method: "DELETE",
  });

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
