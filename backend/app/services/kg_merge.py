"""Pure cross-document Concept clustering. No DB/IO. Vectorized cosine over
one representative vector per name-seed cluster (well under O(N^2) of members).
confirmed pairs force-union; rejected pairs block.

Note — transitive-reject limitation (v1):
  ``rejected`` suppresses the DIRECT vector edge between two seeds, but does
  NOT guarantee they never co-cluster.  A transitive chain (A–C and B–C both
  ≥ hi) can still merge a rejected A–B pair.  This is an accepted v1
  limitation.
"""
from __future__ import annotations
import logging
import re
from typing import Dict, List, Set, FrozenSet

import numpy as np

_MAX_REPS = 4000  # sanity constant; no longer causes skipping the vector tier
_log = logging.getLogger(__name__)

_ALIASES = {
    "vco": "voltage controlled oscillator",
    "pll": "phase locked loop",
    "lna": "low noise amplifier",
    "mos": "mos transistor",
    "mosfet": "mos transistor",
    "bjt": "bipolar junction transistor",
    "opamp": "op amp",
    "op amp": "op amp",
}


def _norm(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9+/ ]+", " ", (name or "").strip().lower())
    cleaned = re.sub(r"[\s\-_]+", " ", cleaned).strip()
    return _ALIASES.get(cleaned, cleaned)


class _UF:
    def __init__(self, items): self.p = {x: x for x in items}
    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]; x = self.p[x]
        return x
    def union(self, a, b): self.p[self.find(a)] = self.find(b)


def cluster_concepts(
    concepts: List[dict],
    vectors: Dict[str, List[float]],
    confirmed: Set[FrozenSet[str]],
    rejected: Set[FrozenSet[str]],
    hi: float = 0.90,
    lo: float = 0.82,
    top_k: int = 5,
    max_pending: int = 1000,
) -> dict:
    seed_of = {c["object_id"]: _norm(c["name"]) for c in concepts}
    seeds = sorted(set(seed_of.values()))
    uf = _UF(seeds)
    for pair in confirmed:
        a, b = (_norm(n) for n in tuple(pair))
        if a in uf.p and b in uf.p:
            uf.union(a, b)
    rej = {frozenset(_norm(n) for n in p) for p in rejected}

    # O(N) pre-pass: first name seen for each seed (used for canonical name lookup)
    seed_first_name: Dict[str, str] = {}
    for c in concepts:
        s = seed_of[c["object_id"]]
        if s not in seed_first_name:
            seed_first_name[s] = c["name"]

    members: Dict[str, List[str]] = {}
    for c in concepts:
        members.setdefault(seed_of[c["object_id"]], []).append(c["object_id"])

    # Build representative vectors (mean of member vectors) for each seed
    reps = []
    for s in seeds:
        vs = [vectors[o] for o in members[s] if o in vectors]
        reps.append(np.mean(np.asarray(vs, dtype=np.float32), axis=0) if vs else None)
    idx = [i for i, r in enumerate(reps) if r is not None]

    raw_candidates: List[tuple] = []
    if idx:
        if len(seeds) > _MAX_REPS:
            _log.info(
                "kg_merge: %d seeds exceeds _MAX_REPS=%d; using bounded top-k vector candidates",
                len(seeds), _MAX_REPS,
            )
        M = np.asarray([reps[i] for i in idx], dtype=np.float32)
        M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-8)
        block = 512
        seen_pairs: set = set()
        for start in range(0, len(idx), block):
            end = min(start + block, len(idx))
            sims = M[start:end] @ M.T
            for local_i, row in enumerate(sims):
                global_i = start + local_i
                row[global_i] = -1.0
                k = min(top_k, len(row) - 1)
                if k <= 0:
                    continue
                top = np.argpartition(row, -k)[-k:]
                for global_j in top:
                    global_j = int(global_j)
                    if global_j <= global_i:
                        continue
                    pair_key = (global_i, global_j)
                    if pair_key in seen_pairs:
                        continue
                    seen_pairs.add(pair_key)
                    sa, sb = seeds[idx[global_i]], seeds[idx[global_j]]
                    if rej and frozenset((sa, sb)) in rej:
                        continue
                    sim = float(row[global_j])
                    if sim >= lo:
                        raw_candidates.append((sa, sb, sim))

    # Sort all candidates by score descending; process hi first for auto-union
    raw_candidates.sort(key=lambda t: t[2], reverse=True)

    pending_set: set = set()
    pending: List[tuple] = []
    for sa, sb, sim in raw_candidates:
        if sim >= hi:
            uf.union(sa, sb)
        else:
            pair_key = frozenset((sa, sb))
            if pair_key not in pending_set:
                pending_set.add(pair_key)
                pending.append((sa, sb, sim))

    groups: Dict[str, List[str]] = {}
    for s in seeds:
        groups.setdefault(uf.find(s), []).append(s)
    canon_id, canon_name = {}, {}
    for root, grp in groups.items():
        best = max(grp, key=lambda s: len(members[s]))
        cid = "K-" + min(grp)
        for s in grp:
            canon_id[s] = cid
        canon_name[cid] = seed_first_name[best]
    cluster_map = {c["object_id"]: canon_id[seed_of[c["object_id"]]] for c in concepts}
    names = {c["object_id"]: canon_name[cluster_map[c["object_id"]]] for c in concepts}
    pend_out = [(canon_id[a], canon_id[b], sim) for a, b, sim in pending if canon_id[a] != canon_id[b]]
    # Cap pending at max_pending (already sorted desc by score)
    was_capped = len(pend_out) > max_pending
    pend_out = pend_out[:max_pending]
    return {"cluster_map": cluster_map, "canonical_names": names, "pending": pend_out,
            "capped": was_capped}


def derive_unified_graph(nodes: List[dict], edges: List[dict], cluster_map: Dict[str, str]) -> dict:
    """Rewire member-Concept endpoints to canonical ids; dedup edges. O(V+E)."""
    def canon(oid): return cluster_map.get(oid, oid)
    seen_concept, out_nodes = set(), []
    for n in nodes:
        if n["object_type"] == "concept":
            cid = canon(n["id"])
            if cid in seen_concept:
                continue
            seen_concept.add(cid)
            out_nodes.append({**n, "id": cid})
        else:
            out_nodes.append(n)
    seen_edge, out_edges = set(), []
    for e in edges:
        s, t = canon(e["source_object_id"]), canon(e["target_object_id"])
        if s == t:
            continue
        key = (s, t, e["edge_type"])
        if key in seen_edge:
            continue
        seen_edge.add(key)
        out_edges.append({"source_object_id": s, "target_object_id": t, "edge_type": e["edge_type"]})
    return {"nodes": out_nodes, "edges": out_edges}
