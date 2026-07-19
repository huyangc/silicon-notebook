import type { PendingMerge } from "./workspace-model";


export function mergePairKey(candidate: PendingMerge): string {
  return [candidate.canonical_a, candidate.canonical_b].sort().join("\u0000");
}


export function withoutDecidedMerge(
  candidates: readonly PendingMerge[],
  decided: PendingMerge,
): PendingMerge[] {
  const decidedPairKey = mergePairKey(decided);
  return candidates.filter(
    (candidate) => (
      candidate.id !== decided.id
      && mergePairKey(candidate) !== decidedPairKey
    ),
  );
}
