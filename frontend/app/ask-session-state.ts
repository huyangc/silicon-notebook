import type { ConversationSummary } from "./workspace-model.ts";


export type StartedConversation = {
  conversationId: string;
  question: string;
  startedAt: string;
};


/**
 * Publish a durable Ask conversation as soon as the stream starts.
 *
 * The server has already created/touched the conversation before emitting the
 * started event.  This optimistic projection makes that activity visible
 * without waiting for the model response; the normal list reload immediately
 * afterwards replaces it with the authoritative server summary.
 */
export function recordStartedConversation(
  sessions: ConversationSummary[],
  started: StartedConversation,
): ConversationSummary[] {
  const existing = sessions.find((session) => session.id === started.conversationId);
  const active: ConversationSummary = existing
    ? { ...existing, updated_at: started.startedAt }
    : {
        id: started.conversationId,
        title: started.question.trim().slice(0, 60),
        updated_at: started.startedAt,
        turn_count: 0,
        used_reasoning: false,
      };
  return [active, ...sessions.filter((session) => session.id !== started.conversationId)];
}


/**
 * A stale-but-valid list may be used only when a newer refresh failed. Keep
 * every locally published durable conversation ahead of that fallback,
 * including an existing session whose optimistic activity timestamp/order is
 * newer than the fallback snapshot.
 */
export function mergeSessionListFallback(
  current: ConversationSummary[],
  fallback: ConversationSummary[],
  optimisticConversationIds: ReadonlySet<string>,
): ConversationSummary[] {
  const optimistic = current.filter((session) => optimisticConversationIds.has(session.id));
  return [
    ...optimistic,
    ...fallback.filter((session) => !optimisticConversationIds.has(session.id)),
  ];
}
