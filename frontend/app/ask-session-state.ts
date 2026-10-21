import type { ConversationDetail, ConversationSummary } from "./workspace-model.ts";


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


/**
 * Put an opened conversation's detail onto its history row (inserting the row
 * when the list does not have it yet).
 *
 * The row is rebuilt ON the existing one: whatever the list endpoint returned
 * that the detail does not carry survives, and the fields the detail does
 * carry win. `linked_report` follows the detail exactly (both are "the report
 * on the last answered turn"): present there replaces the row's, absent there
 * means the last turn had none, so a badge from an older list read must not
 * linger. Rebuilding from the detail alone once dropped the badge from the
 * very conversation the user had just opened.
 */
export function upsertSessionFromDetail(
  sessions: ConversationSummary[],
  detail: Pick<
    ConversationDetail,
    "id" | "title" | "updated_at" | "turn_count" | "used_reasoning" | "linked_report" | "turns"
  >,
): ConversationSummary[] {
  const existing = sessions.find((session) => session.id === detail.id);
  const base: Partial<ConversationSummary> = { ...existing };
  delete base.linked_report;
  const summary: ConversationSummary = {
    ...base,
    id: detail.id,
    title: detail.title,
    updated_at: detail.updated_at,
    turn_count: detail.turn_count,
    used_reasoning: detail.used_reasoning ?? Boolean(
      detail.turns[detail.turns.length - 1]?.response.reasoning_trace?.length,
    ),
    ...(detail.linked_report ? { linked_report: detail.linked_report } : {}),
  };
  return existing
    ? sessions.map((session) => (session.id === detail.id ? summary : session))
    : [summary, ...sessions];
}
