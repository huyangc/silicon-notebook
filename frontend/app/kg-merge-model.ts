import type { PendingMerge } from "./workspace-model";


// What a pending-merge row shows for its two sides: the concept's name, or,
// when the backend has none to show (no published cluster carries the id, or
// every member is hidden from this viewer), the id without its `K-` prefix.
export function pendingMergeSides(candidate: PendingMerge): [string, string] {
  const side = (name: string | undefined, canonicalId: string) =>
    name || canonicalId.replace(/^K-/, "");
  return [
    side(candidate.name_a, candidate.canonical_a),
    side(candidate.name_b, candidate.canonical_b),
  ];
}


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
