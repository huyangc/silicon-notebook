// Pure helpers for the conversation history "batch cleanup" feature.
// Cleanup is keyed on a conversation's LAST ACTIVITY (`updated_at`), not its
// creation time: a stale conversation is one not touched for N days.

export const CLEANUP_PRESETS = [3, 7, 30] as const;

const DAY_MS = 86_400_000;

/** Conversations whose last activity is strictly older than `days` days.
 *  `updated_at` is naive-local ISO; parsed with `new Date`, same basis as `Date.now()`. */
export function conversationsOlderThan<T extends { updated_at: string }>(
  sessions: T[],
  days: number,
  nowMs: number = Date.now(),
): T[] {
  const cutoff = nowMs - days * DAY_MS;
  return sessions.filter((s) => new Date(s.updated_at).getTime() < cutoff);
}

/** Reconcile only ids the server actually deleted after its lock/revalidation. */
export function reconcileConversationCleanup<T extends { id: string }>(
  sessions: T[],
  currentConversationId: string | null,
  deletedIds: string[],
): { sessions: T[]; currentDeleted: boolean } {
  const confirmed = new Set(deletedIds);
  return {
    sessions: sessions.filter((session) => !confirmed.has(session.id)),
    currentDeleted: currentConversationId != null && confirmed.has(currentConversationId),
  };
}

export function conversationCleanupToast(deleted: number): string {
  return deleted > 0
    ? `已删除 ${deleted} 条会话`
    : "没有删除会话；这些会话可能已有新活动或问答正在进行";
}

export type ConversationCleanupResult = {
  deleted: number;
  deleted_ids: string[];
};

/**
 * Finish a delayed cleanup only while its notebook/session resource is still
 * current. The caller owns the epoch check; this coordinator keeps every UI
 * effect (local reconciliation, reload, and notification) behind that check.
 */
export async function runOwnedConversationCleanup(
  response: Promise<ConversationCleanupResult>,
  isCurrent: () => boolean,
  applyCurrent: (result: ConversationCleanupResult) => void | Promise<void>,
  reloadCurrent: () => void | Promise<void>,
  notifyCurrent: (result: ConversationCleanupResult) => void,
): Promise<boolean> {
  try {
    const result = await response;
    if (!isCurrent()) return false;
    await applyCurrent(result);
    if (!isCurrent()) return false;
    await reloadCurrent();
    if (!isCurrent()) return false;
    notifyCurrent(result);
    return true;
  } catch (error) {
    if (isCurrent()) throw error;
    return false;
  }
}
