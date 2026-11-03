import { requestJson } from "./api-client.ts";

export const COLLAB_KINDS = ["sources", "kg", "knowhow", "knowledge", "catalog", "notebook", "understanding", "scale_index"] as const;
export type CollabKind = typeof COLLAB_KINDS[number];
export type CollabChange = { kind: CollabKind; actor_name: string; at: string };
export type CollabViewer = { user_id: string; display_name: string; role: string };
export type CollabPulseResult = {
  cursor: string;
  reset: boolean;
  changes: CollabChange[];
  viewers: CollabViewer[];
  other_reader_count: number;
  next_interval_s: number;
};
export type CollabActivity = { id: string; kind: CollabKind; capability: string; actor_id: string; actor_name: string; target: string; created_at: string };
export type CollabActivityResult = { items: CollabActivity[] };

export function postCollabPulse(notebookId: string, since: string, signal: AbortSignal): Promise<CollabPulseResult> {
  return requestJson(`/notebooks/${encodeURIComponent(notebookId)}/pulse`, {
    method: "POST", body: JSON.stringify({ since, visible: true }), signal, tag: "collab-pulse",
  });
}

export function getCollabActivity(notebookId: string, signal: AbortSignal): Promise<CollabActivityResult> {
  return requestJson(`/notebooks/${encodeURIComponent(notebookId)}/activity`, { signal, tag: "collab-activity" });
}
