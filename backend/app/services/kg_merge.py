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

_MAX_REPS = 4000  # above this, skip the vector tier (name-seed only)
_log = logging.getLogger(__name__)


def _norm(name: str) -> str:
    return re.sub(r"[\s\-_]+", " ", (name or "").strip().lower())

class _UF:
    def __init__(self, items): self.p = {x: x for x in items}
    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]; x = self.p[x]
        return x
    def union(self, a, b): self.p[self.find(a)] = self.find(b)

def cluster_concepts(concepts: List[dict], vectors: Dict[str, List[float]],
                     confirmed: Set[FrozenSet[str]], rejected: Set[FrozenSet[str]],
                     hi: float = 0.90, lo: float = 0.82) -> dict:
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
    pending: List[tuple] = []
    if len(seeds) > _MAX_REPS:
        _log.warning(
            "kg_merge: %d seeds exceeds _MAX_REPS=%d; vector tier skipped",
            len(seeds), _MAX_REPS,
        )
    else:
        reps = []
        for s in seeds:
            vs = [vectors[o] for o in members[s] if o in vectors]
            reps.append(np.mean(np.asarray(vs, dtype=np.float32), axis=0) if vs else None)
        idx = [i for i, r in enumerate(reps) if r is not None]
        if idx:
            M = np.asarray([reps[i] for i in idx], dtype=np.float64)
            M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-8)
            sims = M @ M.T
            for a in range(len(idx)):
                for b in range(a + 1, len(idx)):
                    sa, sb = seeds[idx[a]], seeds[idx[b]]
                    if rej and frozenset((sa, sb)) in rej:
                        continue
                    sim = float(sims[a, b])
                    if sim >= hi:
                        uf.union(sa, sb)
                    elif sim >= lo:
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
    return {"cluster_map": cluster_map, "canonical_names": names, "pending": pend_out,
            "capped": len(seeds) > _MAX_REPS}


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
