import type { PendingMerge } from "./workspace-model";


// Shown for a side the backend has no name for (no published cluster carries
// the id yet, or an older response without the field).
export const UNNAMED_MERGE_SIDE = "未命名概念";

// What a pending-merge row shows for its two sides: the concept's name, or
// `UNNAMED_MERGE_SIDE`. Never the canonical id: it is the type prefix plus a
// member's normalised name (possibly a member hidden from this viewer), kept
// only as the row's handle.
export function pendingMergeSides(candidate: PendingMerge): [string, string] {
  const side = (name: string | undefined) => name || UNNAMED_MERGE_SIDE;
  return [side(candidate.name_a), side(candidate.name_b)];
}


// A pending merge whose two names differ only in how they are written
// (spaces, hyphens, underscores, dots, brackets): the row carries the
// 「同名不同写法」 tag. Any other kind -- or none, from an older backend -- is
// a candidate found by meaning and shows no tag.
export function isSpellingVariant(candidate: PendingMerge): boolean {
  return candidate.kind === "spelling_variant";
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
