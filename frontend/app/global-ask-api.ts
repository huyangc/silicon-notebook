import { requestJson, requestVoid } from "./api-client.ts";
import { httpErrorStatus } from "./errors.ts";
import { requestTaskStream } from "./request-task-stream.ts";
import type { AnswerAnchorLike, CitationLike } from "./answer-formatting.ts";
import type { ReasoningTraceStep } from "./ask-stream.ts";
import type { AskIntentConfirmation, QueryIntentContract } from "./ask-intent-model.ts";
import type { AskRetrievalEffortId } from "./ask-retrieval-effort.ts";
import type { ConversationShareApi, ShareTurnsResult } from "./conversation-share-api.ts";
import { SHARE_SNAPSHOT_MAX_TURNS, type ShareTurn } from "./conversation-share-disclosure.ts";
import type { AskResponse, ConversationShareResponse } from "./workspace-model.ts";

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
/**
 * 全局回答的身份就是它的作业。
 *
 * 全局回答不落任何笔记本的 answers 表,而 `answer_id` 是那张表铸的——所以较早完成的
 * 全局作业存下来的 `answer.answer_id` 是空串(后端现在在完成时用 `job_id` 补上,
 * 但已经落库的旧作业不会回填)。共享的 `AnswerView` 以它为键:按回答重置引用小卡片,
 * 并且只在它非空时才提供按回答的动作(分享)。在**读入口**统一补一次,下游就不用各自
 * 判断「这条全局回答有没有 id」。
 */
export function withAnswerIdentity(job: GlobalJob): GlobalJob {
  if (!job.answer || job.answer.answer_id) return job;
  return { ...job, answer: { ...job.answer, answer_id: job.job_id } };
}

export const getGlobalConversation = (id: string, offset = 0) =>
  requestJson<GlobalConversationDetail>(`${root}/conversations/${encodeURIComponent(id)}?limit=${GLOBAL_ASK_PAGE_SIZE}&offset=${offset}`, options)
    .then((detail) => ({ ...detail, turns: detail.turns.map(withAnswerIdentity) }));
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
  /** 「编辑后重发」：这次提问要替换的那条已停止的作业（只能是会话里最新的一条）。
   *  后端在建新作业的同一个事务里删掉它。 */
  replaces_job_id?: string;
}) => requestJson<GlobalJob>(`${root}/ask`, { ...options, method: "POST", body: JSON.stringify(input) })
  .then(withAnswerIdentity);

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
  requestJson<GlobalJob>(`${root}/jobs/${encodeURIComponent(id)}`, options).then(withAnswerIdentity);
/** 停止一条作业。`discard`：还没有任何过程输出就停止——问题弹回输入框，服务端
 *  连这条记录（以及它刚开出来的空会话）一起丢掉，与笔记本内问答同一条规则。 */
/** 「停止并丢弃」之后向服务端确认这条作业**真的不在了**。
 *
 *  `cancel?discard=true` 只丢弃被那次调用停下来的作业：别的标签页先一步停了它、
 *  或它已经不是会话里最新的一条时，服务端照样回 `cancelled` 却什么都没删。本地
 *  在确认之前不许当它已经消失——否则会把一条仍然存在的记录、连同它所在的会话，
 *  从这个视图里摘掉。404 才算确认（`true`），读到了就是还在（`false`）；读不到
 *  （网络错、5xx）先重读一次，仍读不到回 `null`——调用方按「没确认」保留记录，由
 *  之后的提交失败重拉去对账（会话已不存在时那里会退回「还没有会话」）。 */
export async function globalJobIsGone(id: string): Promise<boolean | null> {
  for (let attempt = 0; attempt < 2; attempt += 1) {
    try {
      await requestJson<GlobalJob>(`${root}/jobs/${encodeURIComponent(id)}`, options);
      return false;
    } catch (cause) {
      if (httpErrorStatus(cause) === 404) return true;
    }
  }
  return null;
}
export const cancelGlobalJob = (id: string, discard = false) =>
  requestJson<GlobalJob>(`${root}/jobs/${encodeURIComponent(id)}/cancel${discard ? "?discard=true" : ""}`, { ...options, method: "POST" }).then(withAnswerIdentity);
export const submitGlobalFeedback = (jobId: string, rating: "useful" | "not_useful") =>
  requestJson<GlobalJob>(`${root}/jobs/${encodeURIComponent(jobId)}/feedback`, {
    ...options, method: "POST", body: JSON.stringify({ rating }),
  }).then(withAnswerIdentity);

// --- 回答投影：新形状 `job.answer` 优先，旧形状 `job.response` 兜底 ------------
//
// 与后端 `backend/app/models/global_ask.py` 的 `global_answer_text` /
// `global_answer_citations` 同一条判据（`answer` 非空即用它，否则退回 `response`，
// 都没有才是空）。抽成三个具名函数而不是在每个消费方就地写一遍三元：这个 fallback
// 一旦分家，就会出现「引用卡按新形状读、分享披露按旧形状读」这种只错一半的情形。
export const globalAnswerAnchors = (job: GlobalJob): AnswerAnchorLike[] =>
  job.answer ? job.answer.anchors : job.response ? job.response.anchors : [];
export const globalAnswerCitations = (job: GlobalJob): CitationLike[] =>
  job.answer ? job.answer.citations : job.response ? job.response.citations : [];

/**
 * 全局会话的轮次 → 分享披露逻辑吃的那一份 `ShareTurn`。
 *
 * 两条刻意的映射：
 *  · **只取 `status === "done"`**。运行中/失败/取消/中断的作业没有可公开的回答，
 *    后端快照也不会包含它们；把它们算进披露会凭空多报轮数与引用。
 *  · `answer_id` 位放的是 **`job_id`**。全局侧没有「答案行 id」这种东西，分享水位
 *    的边界就是作业本身（`expected_through_id` 送的也是 job_id）——两处必须同源，
 *    否则弹窗算出来的边界与服务端钉住的边界说的是两件事。
 */
export function globalShareTurns(turns: GlobalJob[]): ShareTurn[] {
  return turns
    .filter((job) => job.status === "done")
    .map((job) => ({
      answer_id: job.job_id,
      created_at: job.created_at,
      response: { anchors: globalAnswerAnchors(job), citations: globalAnswerCitations(job) },
    }));
}

/** 取全一条全局会话的轮次最多翻几页。上界与后端公开快照的 `MAX_TURNS` 同源——超出
 *  那个规模的会话，公开页本身也投影不全（`truncated_turns`），披露没有可给的确数。 */
const SHARE_TURN_MAX_PAGES = Math.ceil(SHARE_SNAPSHOT_MAX_TURNS / GLOBAL_ASK_PAGE_SIZE);

/**
 * 取**全**一条全局会话的轮次，拿不全就如实说。
 *
 * ⚠ 页序：`offset 0` 是**最新**一页，后续 offset 是更早的页（与
 * `use-global-ask.loadMoreTurns` 同一拼法——它把新取到的整页拼在已有序列**之前**）。
 * 每页内部是升序，所以完整的升序序列 = 把各页**倒序**首尾相接。只读第一页会丢掉**最早**
 * 的那些轮次，而披露的每个数字都声称覆盖链接的全部内容：那会给出一个偏小的确数，
 * 且边界作业就在第一页里时连 `unresolved` 降级都不会触发（评审 P1）。
 *
 * 任何一页失败 → 整条 reject，由弹窗既有的 catch 走兜底文案；翻到上界仍 `has_more`
 * → `complete: false`。两种情形都**不给**半份序列。
 */
async function loadGlobalShareTurns(conversationId: string): Promise<ShareTurnsResult> {
  const pages: GlobalJob[][] = [];
  let offset = 0;
  for (let page = 0; page < SHARE_TURN_MAX_PAGES; page += 1) {
    const detail = await getGlobalConversation(conversationId, offset);
    pages.push(detail.turns || []);
    if (!detail.has_more || detail.next_offset === null) {
      // 翻页期间新作业落库会让 offset 整体后移，同一条可能被相邻两页各取一次；
      // 按 job_id 去重、保留先出现的那条（升序序列里更早的位置），与
      // `loadMoreTurns` 的去重口径一致。
      const seen = new Set<string>();
      const ordered: GlobalJob[] = [];
      for (const job of pages.reverse().flat()) {
        if (seen.has(job.job_id)) continue;
        seen.add(job.job_id);
        ordered.push(job);
      }
      return { turns: globalShareTurns(ordered), complete: true };
    }
    offset = detail.next_offset;
  }
  return { turns: [], complete: false };
}

/**
 * 全局会话的分享接线（与单库 `notebookConversationShareApi` **同形**，弹窗因此是同
 * 一份实现）。三个端点与单库逐字段同形、错误语义相同：零可分享回答 409、水位过期
 * 409、非本人 404；公开链接仍然是同一个 `/c/{token}` 页面。
 */
export function globalConversationShareApi(conversationId: string): ConversationShareApi {
  const path = `${root}/conversations/${encodeURIComponent(conversationId)}/share`;
  return {
    key: `global:${conversationId}`,
    load: () => requestJson<ConversationShareResponse>(path, options),
    loadTurns: () => loadGlobalShareTurns(conversationId),
    share: (expectedThroughId) => requestJson<ConversationShareResponse>(path, {
      ...options, method: "POST", body: JSON.stringify({ expected_through_id: expectedThroughId }),
    }),
    unshare: () => requestVoid(path, { ...options, method: "DELETE" }),
  };
}

// 引用原文全文的读取端点（`GET /global-ask/jobs/{job}/citations/{element}`）在后端
// 保留，MCP 侧仍在用。浏览器这一侧不再有调用方：引用小卡片直接用回答里已经带着的
// anchors/citations（snippet / quoted_span），不为一张卡片再多打一次全文。
