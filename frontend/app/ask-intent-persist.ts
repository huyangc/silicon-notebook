// 推理模式「问题理解 / 澄清确认」阶段的浏览器侧持久化 —— 单测于
// ask-intent-persist.test.mjs。
//
// 这一段刻意没有服务端痕迹（意图预检不建 conversation、不建 job，见产品文档），
// 所以同一页面内的离开/返回靠 use-ask-session 的内存记录接回，但整页刷新会把内存
// 记录一起抹掉。这里把「正在理解的问题」与「等待补充的澄清契约」存进 **sessionStorage**：
// 同一标签页刷新、或从别处导航回来后，笔记本恢复时能续上——理解阶段重新发起一次
// 理解请求，澄清阶段直接重开确认卡（不再调模型）。
//
// 一条记录 = 一次提交（`id` 随机），不是一本笔记本：同一本笔记本里可以同时脱开多条
// （在 A 会话留下澄清、切到 B 会话再提一问），刷新后按 actor+notebook（+会话）各自找回。
//
// 用 sessionStorage 而不是 localStorage 是刻意的：它按标签页隔离。但浏览器在「复制
// 标签页」时会把 sessionStorage 一起复制，所以续上之前还要拿 Web Locks（按记录 id
// 命名、标签页关闭即释放）当跨标签页的所有权闸：拿不到锁 = 另一个标签页正在处理这
// 条记录，本标签页删掉自己的副本、不续。没有 Web Locks 的环境退化为只靠 sessionStorage。
//
// 交接给 durable run、用户取消/中断、切到自动模式、会话被删除、预检失败退回草稿时
// 清除；detach 不清除。

import type { AskIntentConfirmation, QueryIntentContract } from "./ask-intent-model.ts";
import type { AskRetrievalEffortId } from "./ask-retrieval-effort.ts";
import type { BaseScopePayload, SourceScopePayload } from "./source-scope.ts";

export const PENDING_INTENT_STORAGE_KEY = "silicon_notebook_pending_intent";
const LOCK_PREFIX = "silicon_notebook_pending_intent:";

export type PersistedIntentRun = {
  version: 1;
  id: string;
  savedAt: number;
  actorId: string;
  notebookId: string;
  conversationIdAtStart: string | null;
  question: string;
  askedAt: string;
  retrievalEffort: AskRetrievalEffortId;
  sourceScope: SourceScopePayload;
  baseScope: BaseScopePayload;
  // "handoff": the intent is settled and the durable /ask/stream POST is in
  // flight but the server has not acknowledged `started` yet — the only copy of
  // the question is still this record, so it stays until `started` (then the
  // job/conversation own it) or until the POST ends without ever starting.
  phase: "preview" | "review" | "handoff";
  contract: QueryIntentContract | null;
  understandingMs: number;
  confirmation: AskIntentConfirmation | null;
};

export type IntentRunStorage = Pick<Storage, "getItem" | "setItem" | "removeItem">;

// 取 storage 的 thunk 而不是值：sessionStorage 的属性 getter 在隐私模式 / 禁用站点
// 数据的浏览器里会直接抛，读写两侧都要能在没有它时安静退化成「不持久化」。
export function sessionIntentStorage(): IntentRunStorage | null {
  try {
    if (typeof window === "undefined") return null;
    return window.sessionStorage;
  } catch {
    return null;
  }
}

export function newIntentRunId(): string {
  try {
    if (typeof crypto !== "undefined" && typeof crypto.randomUUID === "function") {
      return crypto.randomUUID();
    }
  } catch {
    // fall through
  }
  return `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 10)}`;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isStringArray(value: unknown): boolean {
  return Array.isArray(value) && value.every((item) => typeof item === "string");
}

function isScope(value: unknown, listKey: "source_ids" | "notebook_ids"): boolean {
  return isRecord(value)
    && (value.mode === "include" || value.mode === "exclude")
    && isStringArray(value[listKey]);
}

/**
 * 只放行 ask-intent-review 与轨迹合成真正会读的那些字段形状。一条坏契约会在恢复时
 * 直接炸掉 `intentClarifyStep`（读 `ambiguities.length`），并且每次恢复都炸、直到
 * 用户手清 storage —— 所以宁可整条丢弃。
 */
const RESULT_SCOPES = new Set(["ranked", "complete", "aggregate", "hybrid"]);

function isOptional(value: unknown, check: (item: unknown) => boolean): boolean {
  return value === undefined || check(value);
}

/**
 * Every field of `QueryIntentContract` (ask-intent-model.ts), required and
 * optional alike, so a stale or hand-edited entry can neither break the review
 * card nor be sent back to the backend as a confirmed intent (422).
 */
export function isQueryIntentContractShape(value: unknown): value is QueryIntentContract {
  if (!isRecord(value)) return false;
  if (typeof value.objective !== "string" || typeof value.resolved_question !== "string") return false;
  if (typeof value.intent_type !== "string" || typeof value.expected_output !== "string") return false;
  if (typeof value.result_scope !== "string" || !RESULT_SCOPES.has(value.result_scope)) return false;
  if (typeof value.completeness_required !== "boolean") return false;
  if (typeof value.needs_clarification !== "boolean" || typeof value.confirmed !== "boolean") return false;
  if (typeof value.confidence !== "number" || !Number.isFinite(value.confidence)) return false;
  if (!Array.isArray(value.ambiguities)) return false;
  for (const item of value.ambiguities) {
    if (!isRecord(item)) return false;
    if (typeof item.id !== "string" || typeof item.question !== "string") return false;
    if (!isOptional(item.reason, (v) => typeof v === "string")) return false;
    if (!isOptional(item.required, (v) => typeof v === "boolean")) return false;
    if (!isOptional(item.options, isStringArray)) return false;
  }
  if (!Array.isArray(value.mandatory_topics)) return false;
  for (const item of value.mandatory_topics) {
    if (!isRecord(item)) return false;
    if (typeof item.id !== "string" || typeof item.title !== "string") return false;
    if (typeof item.question !== "string" || !isStringArray(item.retrieval_queries)) return false;
  }
  for (const key of ["entities", "comparison_axes", "constraints", "excluded_topics", "assumptions"]) {
    if (!isStringArray(value[key])) return false;
  }
  if (!isOptional(value.clarification_answers, (answers) => (
    Array.isArray(answers) && answers.every((item) => (
      isRecord(item)
      && typeof item.id === "string"
      && typeof item.question === "string"
      && typeof item.answer === "string"
    ))
  ))) return false;
  return true;
}

export function isAskIntentConfirmationShape(value: unknown): value is AskIntentConfirmation {
  if (!isRecord(value)) return false;
  if (!isQueryIntentContractShape(value.contract)) return false;
  if (typeof value.resolved_question !== "string") return false;
  if (!Array.isArray(value.answers)) return false;
  for (const item of value.answers) {
    if (!isRecord(item) || typeof item.id !== "string" || typeof item.answer !== "string") return false;
  }
  return isOptional(value.understanding_ms, (v) => typeof v === "number" && Number.isFinite(v));
}

/** 只接受形状完整的条目；坏条目整条丢弃，绝不把半截状态续回界面。 */
export function isPersistedIntentRun(value: unknown): value is PersistedIntentRun {
  if (!isRecord(value) || value.version !== 1) return false;
  if (typeof value.id !== "string" || !value.id) return false;
  if (typeof value.savedAt !== "number" || !Number.isFinite(value.savedAt)) return false;
  if (typeof value.actorId !== "string" || !value.actorId) return false;
  if (typeof value.notebookId !== "string" || !value.notebookId) return false;
  if (value.conversationIdAtStart !== null && typeof value.conversationIdAtStart !== "string") return false;
  if (typeof value.question !== "string" || !value.question.trim()) return false;
  if (typeof value.askedAt !== "string") return false;
  if (typeof value.retrievalEffort !== "string") return false;
  if (!isScope(value.sourceScope, "source_ids") || !isScope(value.baseScope, "notebook_ids")) return false;
  if (value.phase !== "preview" && value.phase !== "review" && value.phase !== "handoff") return false;
  if (typeof value.understandingMs !== "number") return false;
  if (value.phase === "handoff") return isAskIntentConfirmationShape(value.confirmation);
  if (value.confirmation !== null && value.confirmation !== undefined) return false;
  if (value.phase === "review") return isQueryIntentContractShape(value.contract);
  return value.contract === null || isQueryIntentContractShape(value.contract);
}

export function readPersistedIntentRuns(
  store: IntentRunStorage | null = sessionIntentStorage(),
): PersistedIntentRun[] {
  if (!store) return [];
  try {
    const raw = store.getItem(PENDING_INTENT_STORAGE_KEY);
    if (!raw) return [];
    const parsed: unknown = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.filter(isPersistedIntentRun);
  } catch {
    return [];
  }
}

function writeAll(store: IntentRunStorage, runs: readonly PersistedIntentRun[]): void {
  try {
    if (runs.length === 0) store.removeItem(PENDING_INTENT_STORAGE_KEY);
    else store.setItem(PENDING_INTENT_STORAGE_KEY, JSON.stringify(runs));
  } catch {
    // Quota / disabled storage: the in-memory record still drives this page;
    // only the reload-resume convenience is lost.
  }
}

// `savedAt` orders submissions (newest resumes with the notebook). Two
// submissions in the same millisecond must still order, so it is monotonic.
let lastSavedAt = 0;
function nextSavedAt(): number {
  lastSavedAt = Math.max(Date.now(), lastSavedAt + 1);
  return lastSavedAt;
}

/**
 * 按记录 id 覆盖（同一次提交从 preview 进到 review 是更新，不是新增，保留原来的
 * 提交次序）；新记录取一个单调递增的 `savedAt`。
 */
export function savePersistedIntentRun(
  run: PersistedIntentRun,
  store: IntentRunStorage | null = sessionIntentStorage(),
): void {
  if (!store) return;
  const runs = readPersistedIntentRuns(store);
  const existing = runs.find((item) => item.id === run.id);
  const others = runs.filter((item) => item.id !== run.id);
  writeAll(store, [...others, { ...run, savedAt: existing ? existing.savedAt : nextSavedAt() }]);
}

/** 该 actor+notebook 的全部条目，最近保存的在前。 */
export function findPersistedIntentRuns(
  actorId: string,
  notebookId: string,
  store: IntentRunStorage | null = sessionIntentStorage(),
): PersistedIntentRun[] {
  return readPersistedIntentRuns(store)
    .filter((run) => run.actorId === actorId && run.notebookId === notebookId)
    .sort((a, b) => b.savedAt - a.savedAt);
}

export function removePersistedIntentRun(
  id: string,
  store: IntentRunStorage | null = sessionIntentStorage(),
): void {
  if (!store) return;
  const runs = readPersistedIntentRuns(store);
  const kept = runs.filter((run) => run.id !== id);
  if (kept.length !== runs.length) writeAll(store, kept);
}

/** 登出：该 actor 的全部条目一起清掉，不给下一个登录者续上别人的问题。 */
export function clearPersistedIntentRuns(
  actorId: string,
  store: IntentRunStorage | null = sessionIntentStorage(),
): void {
  if (!store) return;
  const runs = readPersistedIntentRuns(store);
  const kept = runs.filter((run) => run.actorId !== actorId);
  if (kept.length !== runs.length) writeAll(store, kept);
}

// ---------------------------------------------------------------------------
// 跨标签页所有权闸（Web Locks）。锁按记录 id 命名，标签页关闭自动释放。
// ---------------------------------------------------------------------------

export type IntentRunLocks = Pick<LockManager, "request">;

export function navigatorIntentLocks(): IntentRunLocks | null {
  try {
    if (typeof navigator === "undefined") return null;
    const locks = navigator.locks;
    return locks && typeof locks.request === "function" ? locks : null;
  } catch {
    return null;
  }
}

const heldIntentLocks = new Map<string, () => void>();

/**
 * 试着成为这条记录的唯一处理者。拿到锁就一直持有到 `releaseIntentRun` 或标签页关闭；
 * `ifAvailable` 拿不到（另一个标签页正持有）返回 false。没有 Web Locks 时放行。
 */
export function claimIntentRun(
  id: string,
  locks: IntentRunLocks | null = navigatorIntentLocks(),
): Promise<boolean> {
  if (!locks) return Promise.resolve(true);
  if (heldIntentLocks.has(id)) return Promise.resolve(true);
  return new Promise<boolean>((resolve) => {
    let settled = false;
    const settle = (value: boolean) => {
      if (settled) return;
      settled = true;
      resolve(value);
    };
    try {
      void locks.request(`${LOCK_PREFIX}${id}`, { ifAvailable: true }, (lock) => {
        if (!lock) {
          settle(false);
          return Promise.resolve();
        }
        return new Promise<void>((release) => {
          heldIntentLocks.set(id, release);
          settle(true);
        });
      }).catch(() => settle(true));
    } catch {
      settle(true);
    }
  });
}

export function releaseIntentRun(id: string): void {
  const release = heldIntentLocks.get(id);
  heldIntentLocks.delete(id);
  release?.();
}
