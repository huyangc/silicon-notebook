"""Budgeted peer evidence selection without comparing unlike retrieval scores."""
import heapq
import math


def peer_evidence(pools, limit, *, min_relevance=0, relative_relevance=0,
                  peer_floor=0.0):
    """Reserve one qualified hit per library, then spend on local rank/confidence.

    Exact duplicate text cannot consume another slot. No user-authored text is
    cut: this selects whole evidence chunks for a disclosed bounded projection.

    ``peer_floor`` -- 0 (the default, and what global ask passes by never
    passing it) keeps the historical INCOMPARABLE-SCORES contract below
    verbatim: every library with any qualified hit gets a reserved slot, and
    remaining capacity is spent on confidence RELATIVE TO ITS OWN best hit.
    That is the only safe reading when each pool came from a different
    producer, which is global ask's situation.

    A caller whose pools all came from ONE producer -- federated chunk recall
    drives the same ``_retrieve_chunks`` and therefore the same 0..1 ``_fuse``
    scale for every library -- may instead declare scores cross-comparable by
    passing ``peer_floor > 0``. Then, with ``best`` = the highest peak across
    all libraries:

    * a library whose own peak is below ``best * peer_floor`` gets NO reserved
      slot (its hits still compete for the remaining capacity, they just cannot
      pre-empt a strong library's evidence); and
    * remaining capacity ranks by ``(score / best) / rank`` instead of
      ``(score / own peak) / rank``, so an irrelevant library's rank-1 no longer
      ties with a strong library's rank-1.

    Without it, a focused question against one strong library plus N mounted
    reference libraries that happen to hold nothing relevant loses ~N slots of
    real evidence to their guaranteed-but-worthless first hits.
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
    comparable = peer_floor > 0
    best = max((peak for peak, _ in lanes), default=0.0)
    admission = best * peer_floor if comparable else 0.0
    for library, (peak, hits) in enumerate(lanes):
        # In comparable mode a library too far below the best evidence anywhere
        # starts out as if it had already spent its reserved slot.
        reserved = comparable and peak < admission
        for rank, (score, _, hit) in enumerate(hits, start=1):
            identity = hit.text
            if not reserved and identity not in seen and len(selected) < limit:
                seen.add(identity)
                selected.append(hit)
                reserved = True
            else:
                # Default: scores from different producers are never directly
                # compared, so confidence is relative to this library's best
                # evidence and local rank rewards a coherent relevant tail over
                # weak filler. In comparable mode the denominator becomes the
                # global best instead, which is what makes a weak library's
                # rank-1 rank below a strong library's rank-2.
                reference = best if comparable else peak
                priority = (score / reference if reference > 0 else 1) / rank
                heapq.heappush(remaining, (-priority, rank, library, hit))
    while remaining and len(selected) < limit:
        _, _, _, hit = heapq.heappop(remaining)
        identity = hit.text
        if identity not in seen:
            seen.add(identity)
            selected.append(hit)
    return selected
