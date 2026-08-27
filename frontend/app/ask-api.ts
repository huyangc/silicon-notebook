import {
  performApiRequest,
  requestJson,
  requestVoid,
} from "./api-client.ts";
import { countCodePoints } from "./input-limits.ts";
import {
  humanizedError,
  logDiagnostic,
  pluginEngineFailureMessage,
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
} from "./workspace-model.ts";
import { requestTaskStream } from "./request-task-stream.ts";

// Compatibility exports: collection search is no longer owned by the Ask API,
// but existing external imports keep the same public surface during F5.
export {
  SEARCH_FANOUT_LIMIT,
  searchNotebook,
  searchNotebooksBounded,
} from "./collection-search.ts";

const options = { tag: "api", unauthorized: "clear-and-reload" as const };

export const fetchAskModes = () => requestJson<unknown>("/ask-modes", options);

// --- 输入护栏 ---------------------------------------------------------------

/**
 * 提问的长度上限。
 *
 * **与 `backend/app/models/ask.py` 的 `ASK_QUESTION_MAX_CHARS` 同值**，改一侧就要
 * 改另一侧。
 *
 * 两侧都要有，是「数值上限与截断」红线的要求：用户编辑的数据不得静默截断——前端
 * 显示同一护栏（超限当场说清并拦住提交），API 超限**明确拒绝**（后端 422，不裁短
 * 了存）。少了前端这半，用户会敲完一长串才在提交时吃一个 422，而且不知道边界在哪。
 *
 * 这条对问答尤其承重：会话公开分享页把每轮 `question` **原样**发给匿名访客（旧的
 * 2,000 字公开截断在 codex #522 R1 被拿掉，因为静默截断用户自撰的问题正是红线要
 * 防的），所以「不截断」只有在提交那一刻就挡住超长问题时才成立——与深度报告那侧
 * codex #525 R1 P2 是同一条。
 */
/**
 * 会话标题的长度上限（`conversationTitleMaxChars`）。
 *
 * **与 `backend/app/models/ask.py` 的 `CONVERSATION_TITLE_MAX_CHARS` 同值**，改一侧
 * 就要改另一侧。
 *
 * 与提问同一条红线的另一半：会话公开分享页把标题也**原样**发给匿名访客（旧的 400
 * 字公开截断在 codex #522 R2 被拿掉），所以「不截断」同样只有在重命名那一刻就挡住
 * 超长标题时才是**有界**的。重命名是标题唯一能超过服务端自动取的前 60 字的途径。
 *
 * 取 200 而不是 4,000：那是给**问题正文**定的尺，用它给一行**标签**定界等于宣称我们
 * 打算服务 4,000 字的会话名。200 与后端同源，也是 `QueryIntentTopic.title` 的既有口径。
 */
export const ASK_INPUT_LIMITS = {
  questionMaxChars: 4000,
  conversationTitleMaxChars: 200,
} as const;

/**
 * 超限时的提示文案；没超返回 `null`。
 *
 * **超出的文字一个字都不删**——护栏是「拦住提交」，不是「替用户裁剪」。在 `onChange`
 * 里按上限夹一刀等于用户粘进来 10,000 字、当场只剩 4,000 而且不说一声，正是「用户
 * 编辑的数据不得静默截断」要防的（codex #525 R3）。留着原文，用户自己精简。
 *
 * 按**码点**数，与后端 Pydantic `max_length` 同一把尺（见 `countCodePoints`）。
 */
export const askQuestionLimitHint = (question: string): string | null => {
  const used = countCodePoints(question);
  const max = ASK_INPUT_LIMITS.questionMaxChars;
  if (used > max) return `提问超出 ${max} 字上限（当前 ${used} 字），请精简后再提问`;
  return null;
};

/**
 * 会话标题超限时的提示文案；没超返回 `null`。`askQuestionLimitHint` 的平移。
 *
 * 同样**一个字都不删**：拦住保存，让用户自己改短（codex #525 R3）。同样按**码点**数，
 * 与后端 Pydantic `max_length` 同一把尺——刻意不使用 `<input maxLength>`，它数的是
 * UTF-16 code unit，含非 BMP 字符时会比 API 更早停手（codex #525 R2）。
 *
 * 顺带的可见后果：护栏上线**之前**改过的超长标题，一点开重命名就会当场显示这句话。
 * 那是对的——那份草稿此刻确实提交不了，说清楚比让保存键莫名变灰好。
 */
export const conversationTitleLimitHint = (title: string): string | null => {
  const used = countCodePoints(title);
  const max = ASK_INPUT_LIMITS.conversationTitleMaxChars;
  if (used > max) return `标题超出 ${max} 字上限（当前 ${used} 字），请精简后再保存`;
  return null;
};

export const previewAskIntent = (
  notebookId: string,
  question: string,
  conversationId?: string | null,
  signal?: AbortSignal,
  sourceScope?: SourceScopePayload,
  // 预检必须与执行用**同一份**参考库上限：预检读语料目录、执行读证据，两者用不同
  // 范围就会出现「预检说搜得到、执行搜不到」。省略即历史行为（全部挂载库参与）。
  baseScope?: BaseScopePayload,
  onHeartbeat?: (elapsedMs: number) => void | Promise<void>,
) => requestTaskStream<QueryIntentContract>(
  `/notebooks/${notebookId}/ask/intent/stream`,
  {
    ...options,
    method: "POST",
    body: JSON.stringify({
      question,
      conversation_id: conversationId || undefined,
      source_scope: sourceScope,
      base_scope: baseScope,
    }),
    signal,
  },
  {
    onHeartbeat: (elapsedMs) => onHeartbeat?.(elapsedMs),
    fallbackMessage: "问题理解没能完成，请重试",
  },
);

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
      const diagnostic = event.error;
      logDiagnostic("ask-stream", diagnostic);
      throw humanizedError(
        pluginEngineFailureMessage(diagnostic) ?? "回答没能完成，请重试",
      );
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
