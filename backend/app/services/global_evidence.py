"""Budgeted peer evidence selection without comparing unlike retrieval scores."""
import heapq
import math


def peer_evidence(pools, limit, *, min_relevance=0, relative_relevance=0):
    """Reserve one qualified hit per library, then spend on local rank/confidence.

    Exact duplicate text cannot consume another slot. No user-authored text is
    cut: this selects whole evidence chunks for a disclosed bounded projection.
    """
    lanes = []
    for hits in pools:
        ranked = sorted(
            ((float(hit.relevance or hit.score), index, hit) for index, hit in enumerate(hits)
             if math.isfinite(float(hit.relevance or hit.score))),
            key=lambda item: (-item[0], item[1]),
        )
        if not ranked:
            continue
        peak = ranked[0][0]
        floor = max(min_relevance, peak * relative_relevance)
        qualified = [item for item in ranked if item[0] >= floor]
        if qualified:
            lanes.append((peak, qualified))
    selected, seen = [], set()
    remaining = []
    for library, (peak, hits) in enumerate(lanes):
        reserved = False
        for rank, (score, _, hit) in enumerate(hits, start=1):
            identity = hit.text
            if not reserved and identity not in seen and len(selected) < limit:
                seen.add(identity)
                selected.append(hit)
                reserved = True
            else:
                # Scores from different producers are never directly compared.
                # Confidence is relative to this library's best evidence; local
                # rank rewards a coherent relevant tail over weak filler.
                priority = (score / peak if peak > 0 else 1) / rank
                heapq.heappush(remaining, (-priority, rank, library, hit))
    while remaining and len(selected) < limit:
        _, _, _, hit = heapq.heappop(remaining)
        identity = hit.text
        if identity not in seen:
            seen.add(identity)
            selected.append(hit)
    return selected
