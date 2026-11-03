import type { MemoryRecord } from "./workspace-model";

export type SuggestionAnswerTarget = { notebookId: string; conversationId: string; answerId: string };
export type SuggestionDraft = Pick<MemoryRecord, "title" | "content_md" | "tags">;

function object(value: unknown): Record<string, unknown> {
  return value && typeof value === "object" && !Array.isArray(value) ? value as Record<string, unknown> : {};
}

export function memorySuggestionReason(memory: MemoryRecord): string {
  if (memory.suggested_signal === "clarified") return "你在问题理解时确认了这一点";
  if (memory.suggested_signal === "corrected") return "你在追问中纠正了回答";
  if (memory.suggested_signal === "repeated") {
    const count = object(memory.provenance.suggestion).cluster_size;
    return Number.isSafeInteger(count) && Number(count) > 0
      ? `你问过 ${count} 次相近的问题` : "你问过相近的问题";
  }
  return "";
}

export function memorySuggestionConflictsWithSources(memory: MemoryRecord): boolean {
  return memory.suggested_signal === "corrected"
    && object(memory.provenance.suggestion).conflicts_with_sources === true;
}

export function memorySuggestionRefines(memory: MemoryRecord): string | null {
  const id = object(memory.provenance.suggestion).refines;
  return typeof id === "string" && id && id !== memory.id ? id : null;
}

export function memorySuggestionAnswer(memory: MemoryRecord): SuggestionAnswerTarget | null {
  // Imported citations are archives, never authority to open a local conversation.
  if (!memory.suggested_signal || memory.provenance.imported_from || memory.provenance.sync_origin) return null;
  const { conversation_id: conversationId, answer_id: answerId } = memory.provenance;
  return typeof conversationId === "string" && conversationId && typeof answerId === "string" && answerId
    ? { notebookId: memory.notebook_id, conversationId, answerId } : null;
}
