import type {
  KnowledgeItem,
  KnowledgeRecord,
  PendingMerge,
} from "./workspace-model";

export const KG_RANGE_DEFAULT = 80;

export const KG_RANGE_STEPS: ReadonlyArray<{ value: number; label: string }> = [
  { value: 80, label: "核心 80" },
  { value: 160, label: "核心 160" },
  { value: 320, label: "核心 320" },
  { value: 0, label: "全部" },
];

export const KG_SEARCH_DEBOUNCE_MS = 300;
export const KG_BACKGROUND_POLL_MS = 6000;
export const KG_MAINTENANCE_POLL_MS = 3000;
export const KG_BACKGROUND_POLL_CAP_MS = 20 * 60 * 1000;
export const MAINTENANCE_JOB_MISMATCH_SETTLE_STREAK = 2;

export type KgWorkspaceOwner = {
  actorId: string;
  notebookId: string;
  generation: number;
  viewGeneration: number;
};

export type KgWorkspaceTransition = { generation: number };

export const sameKgOwner = (
  left: KgWorkspaceOwner | null,
  right: KgWorkspaceOwner,
): boolean => Boolean(
  left
  && left.actorId === right.actorId
  && left.notebookId === right.notebookId
  && left.generation === right.generation
  && left.viewGeneration === right.viewGeneration,
);

export const sameKgIdentity = (
  left: Pick<KgWorkspaceOwner, "actorId" | "notebookId"> | null,
  right: Pick<KgWorkspaceOwner, "actorId" | "notebookId">,
): boolean => Boolean(
  left
  && left.actorId === right.actorId
  && left.notebookId === right.notebookId,
);

export const knowledgeItemFromRecord = (record: KnowledgeRecord): KnowledgeItem => ({
  id: record.id,
  status: record.status,
  owner: record.owner,
  last_reviewed: record.last_reviewed,
  evidence: record.evidence,
  headline: record.headline,
  object_type: record.object_type,
  fields: record.fields,
});

const mergePairKey = (candidate: PendingMerge): string =>
  [candidate.canonical_a, candidate.canonical_b].sort().join("\0");

export const filterPendingMergeTombstones = (
  rows: readonly PendingMerge[],
  tombstoned: ReadonlySet<string>,
): PendingMerge[] => {
  if (tombstoned.size === 0) return [...rows];
  return rows.filter((row) => (
    !tombstoned.has(row.id)
    && !tombstoned.has(mergePairKey(row))
  ));
};

export const pendingMergeTombstoneKeys = (candidate: PendingMerge): string[] => [
  candidate.id,
  mergePairKey(candidate),
];
