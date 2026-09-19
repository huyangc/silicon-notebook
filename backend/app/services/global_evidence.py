"""Budgeted peer evidence selection without comparing unlike retrieval scores."""
from collections import deque


def peer_evidence(pools, limit):
    """Round-robin notebooks and sources, retaining each lane's local ranking.

    Exact duplicate text cannot consume another slot. No user-authored text is
    cut: this selects whole evidence chunks for a disclosed bounded projection.
    """
    lanes = []
    for hits in pools:
        sources = {}
        for hit in hits:
            sources.setdefault(hit.source_id, deque()).append(hit)
        lane = deque()
        active = deque(sources.values())
        while active:
            source = active.popleft()
            lane.append(source.popleft())
            if source:
                active.append(source)
        if lane:
            lanes.append(lane)
    active = deque(lanes)
    selected, seen = [], set()
    while active and len(selected) < limit:
        lane = active.popleft()
        while lane:
            hit = lane.popleft()
            identity = " ".join(hit.text.split())
            if identity not in seen:
                seen.add(identity)
                selected.append(hit)
                break
        if lane:
            active.append(lane)
    return selected
