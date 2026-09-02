// 推理模式「问题理解 / 澄清确认」阶段的浏览器侧持久化 —— 单测于
// ask-intent-persist.test.mjs。
//
// 这一段刻意没有服务端痕迹（意图预检不建 conversation、不建 job，见产品文档），
// 所以同一页面内的离开/返回靠 use-ask-session 的内存记录接回，但整页刷新会把内存
// 记录一起抹掉。这里把「正在理解的问题」与「等待补充的澄清契约」按 actor+notebook
// 存进 **sessionStorage**：同一标签页刷新、或从别处导航回来后，笔记本恢复时能续上——
// 理解阶段重新发起一次理解请求，澄清阶段直接重开确认卡（不再调模型）。
//
// 用 sessionStorage 而不是 localStorage 是刻意的：它按标签页隔离。两个标签页同开
// 一本笔记本时若共享条目，会各自续上同一次理解、意图清晰时各起一个 durable job，
// 用户得到两份回答。标签页关闭即丢，是可接受的边界（durable job 那半已在服务端）。
//
// 交接给 durable run、用户取消/中断、切到自动模式、会话被删除时清除；detach 不清除。

import type { QueryIntentContract } from "./ask-intent-model.ts";
import type { AskRetrievalEffortId } from "./ask-retrieval-effort.ts";
import type { BaseScopePayload, SourceScopePayload } from "./source-scope.ts";

export const PENDING_INTENT_STORAGE_KEY = "silicon_notebook_pending_intent";

export type PersistedIntentRun = {
  version: 1;
  actorId: string;
  notebookId: string;
  conversationIdAtStart: string | null;
  question: string;
  askedAt: string;
  retrievalEffort: AskRetrievalEffortId;
  sourceScope: SourceScopePayload;
  baseScope: BaseScopePayload;
  phase: "preview" | "review";
  contract: QueryIntentContract | null;
  understandingMs: number;
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

function isRecord(value: unknown): value is Record<string, unknown> {
  return typeof value === "object" && value !== null && !Array.isArray(value);
}

function isScope(value: unknown, listKey: "source_ids" | "notebook_ids"): boolean {
  return isRecord(value)
    && (value.mode === "include" || value.mode === "exclude")
    && Array.isArray(value[listKey])
    && (value[listKey] as unknown[]).every((item) => typeof item === "string");
}

/** 只接受形状完整的条目；坏条目整条丢弃，绝不把半截状态续回界面。 */
export function isPersistedIntentRun(value: unknown): value is PersistedIntentRun {
  if (!isRecord(value) || value.version !== 1) return false;
  if (typeof value.actorId !== "string" || !value.actorId) return false;
  if (typeof value.notebookId !== "string" || !value.notebookId) return false;
  if (value.conversationIdAtStart !== null && typeof value.conversationIdAtStart !== "string") return false;
  if (typeof value.question !== "string" || !value.question.trim()) return false;
  if (typeof value.askedAt !== "string") return false;
  if (typeof value.retrievalEffort !== "string") return false;
  if (!isScope(value.sourceScope, "source_ids") || !isScope(value.baseScope, "notebook_ids")) return false;
  if (value.phase !== "preview" && value.phase !== "review") return false;
  if (typeof value.understandingMs !== "number") return false;
  if (value.phase === "review") return isRecord(value.contract);
  return value.contract === null || isRecord(value.contract);
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

function sameIdentity(run: PersistedIntentRun, actorId: string, notebookId: string): boolean {
  return run.actorId === actorId && run.notebookId === notebookId;
}

/** 一个 actor+notebook 至多一条（提交时 `intentChecking` 已阻止第二次提交）。 */
export function savePersistedIntentRun(
  run: PersistedIntentRun,
  store: IntentRunStorage | null = sessionIntentStorage(),
): void {
  if (!store) return;
  const others = readPersistedIntentRuns(store).filter((item) => (
    !sameIdentity(item, run.actorId, run.notebookId)
  ));
  writeAll(store, [...others, run]);
}

export function findPersistedIntentRun(
  actorId: string,
  notebookId: string,
  store: IntentRunStorage | null = sessionIntentStorage(),
): PersistedIntentRun | null {
  return readPersistedIntentRuns(store).find((run) => sameIdentity(run, actorId, notebookId)) ?? null;
}

export function removePersistedIntentRun(
  actorId: string,
  notebookId: string,
  store: IntentRunStorage | null = sessionIntentStorage(),
): void {
  if (!store) return;
  const runs = readPersistedIntentRuns(store);
  const kept = runs.filter((run) => !sameIdentity(run, actorId, notebookId));
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
