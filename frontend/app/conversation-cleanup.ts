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
