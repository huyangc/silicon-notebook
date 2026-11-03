import { COLLAB_KINDS, type CollabChange, type CollabKind } from "./collab-api.ts";

export const COLLAB_REFRESH_DEBOUNCE_MS = 2_000;
export const COLLAB_CHANGE_NOTICE_MS = 3_000;
export const COLLAB_DEFAULT_INTERVAL_SECONDS = 15;

export const COLLAB_KIND_LABELS: Readonly<Record<CollabKind, string>> = {
  sources: "来源", kg: "知识图谱", knowhow: "Knowhow 表", knowledge: "知识", catalog: "命令目录",
  notebook: "笔记本", understanding: "理解底座", scale_index: "检索索引",
};

export function collabKinds(changes: readonly CollabChange[], reset: boolean): CollabKind[] {
  return reset ? [...COLLAB_KINDS] : [...new Set(changes.map((change) => change.kind).filter((kind) => COLLAB_KINDS.includes(kind)))];
}

export function collabEpoch(cursor: string): string | null {
  const match = /^([^:]+):(\d+)$/.exec(cursor);
  return match ? match[1] : null;
}

/** A return to any previous process epoch is evidence of a rotating backend. */
export function collabEpochReturned(epochs: readonly string[], cursor: string): boolean {
  const epoch = collabEpoch(cursor);
  return epoch !== null && epochs.length > 1 && epochs[epochs.length - 1] !== epoch && epochs.includes(epoch);
}
