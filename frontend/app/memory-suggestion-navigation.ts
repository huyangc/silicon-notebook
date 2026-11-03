import type { SuggestionAnswerTarget } from "./memory-suggestion-model";

export type MemorySourceAnswerTarget = SuggestionAnswerTarget & { actorId: string; epoch: number };

/** Use the existing workspace/session owners and fence each asynchronous hop. */
export async function navigateMemorySuggestionAnswer(target: SuggestionAnswerTarget, navigation: {
  actorId: string | null;
  signal: AbortSignal;
  currentActorId: () => string | null;
  currentNotebookId: () => string | null;
  currentEpoch: () => number;
  openNotebook: (id: string, actorId: string) => Promise<boolean>;
  openSession: (id: string) => Promise<boolean | { opened: boolean; epoch: number }>;
}): Promise<MemorySourceAnswerTarget | null> {
  const actorId = navigation.actorId;
  const owns = (epoch: number) => !navigation.signal.aborted && navigation.currentActorId() === actorId && navigation.currentEpoch() === epoch;
  if (!actorId || navigation.signal.aborted) return null;
  if (navigation.currentNotebookId() !== target.notebookId) {
    const opening = navigation.openNotebook(target.notebookId, actorId);
    const epoch = navigation.currentEpoch();
    try {
      if (!await opening || !owns(epoch) || navigation.currentNotebookId() !== target.notebookId) return null;
    } catch (cause) {
      if (!owns(epoch)) return null;
      throw cause;
    }
  }
  const opening = navigation.openSession(target.conversationId);
  const epoch = navigation.currentEpoch();
  try {
    const outcome = await opening;
    const opened = typeof outcome === "boolean" ? outcome : outcome.opened;
    const committedEpoch = typeof outcome === "boolean" ? epoch : outcome.epoch;
    return opened && owns(committedEpoch) && navigation.currentNotebookId() === target.notebookId ? { ...target, actorId, epoch: committedEpoch } : null;
  } catch (cause) {
    if (!owns(epoch)) return null;
    throw cause;
  }
}
