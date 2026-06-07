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
from collections import Counter
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


def _norm_statement(s: str) -> str:
    """Claim/step text normalizer: lowercase, drop punctuation, collapse whitespace."""
    cleaned = re.sub(r"[^\w\s]+", " ", (s or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def _norm_formula(s: str) -> str:
    """Formula normalizer: drop ALL whitespace, lowercase (expression identity)."""
    return re.sub(r"\s+", "", (s or "").lower())


def _steps_signature(payload: dict) -> str:
    steps = (payload or {}).get("steps")
    if not isinstance(steps, list):
        return ""
    names = sorted(_norm_statement(st.get("name", ""))
                   for st in steps if isinstance(st, dict) and st.get("name"))
    return "|".join(n for n in names if n)


def seed_concept(obj) -> str:
    """Seed function for concepts. obj may be a dict (production) or str (test shorthand)."""
    if isinstance(obj, str):
        return _norm(obj)
    return _norm(obj.get("name", ""))


def seed_claim(obj) -> str:
    """Seed function for claims. obj may be a dict (production) or str (test shorthand)."""
    if isinstance(obj, str):
        return _norm_statement(obj)
    return _norm_statement(obj.get("name", ""))


def seed_formula(obj) -> str:
    """Seed function for formulas. obj may be a dict (production) or str (test shorthand)."""
    if isinstance(obj, str):
        return _norm_formula(obj)
    return _norm_formula(obj.get("name", ""))


def seed_procedure(obj) -> str:
    """Seed function for procedures. obj may be a dict (production) or str (test shorthand)."""
    if isinstance(obj, str):
        return _norm_statement(obj)
    nm = _norm_statement(obj.get("name", ""))
    sig = _steps_signature(obj.get("payload") or {})
    return f"{nm}#{sig}" if sig else nm


_CONTRAST_GROUPS = [
    {"single", "double"}, {"low", "high"}, {"n", "p"}, {"nmos", "pmos"},
    {"series", "shunt"}, {"voltage", "current"}, {"positive", "negative"},
    {"input", "output"}, {"forward", "reverse"},
    {"drain", "source", "gate", "bulk", "body"},
    {"first", "second", "third", "fourth"}, {"upper", "lower"},
    {"even", "odd"}, {"internal", "external"}, {"inverting", "noninverting"},
]


def _discriminative_conflict(name_a: str, name_b: str) -> bool:
    """两个规范名仅各差一个 token 且该对差异 token 属同一对立组 → 视为不同变体, 禁止合并。"""
    ta, tb = _norm(name_a).split(), _norm(name_b).split()
    only_a = list((Counter(ta) - Counter(tb)).elements())
    only_b = list((Counter(tb) - Counter(ta)).elements())
    if len(only_a) == 1 and len(only_b) == 1 and only_a[0] != only_b[0]:
        for g in _CONTRAST_GROUPS:
            if only_a[0] in g and only_b[0] in g:
                return True
    return False


def _ann_candidates(seeds: List[str], reps: Dict[str, "np.ndarray"],
                    k: int = 5, lo: float = 0.82) -> List[tuple]:
    """hnswlib 余弦 top-k 近邻候选(sim≥lo), 去重无序对。O(N log N)。
    reps: seed -> 代表向量(未归一化亦可, cosine 空间内部归一)。"""
    import hnswlib
    idx_seeds = [s for s in seeds if s in reps]
    n = len(idx_seeds)
    if n < 2:
        return []
    M = np.asarray([reps[s] for s in idx_seeds], dtype=np.float32)
    dim = int(M.shape[1])
    index = hnswlib.Index(space="cosine", dim=dim)
    index.init_index(max_elements=n, ef_construction=200, M=16, random_seed=42)
    index.set_num_threads(1)
    index.add_items(M, np.arange(n))
    index.set_ef(max(64, k + 32))
    kk = min(k + 1, n)
    labels, distances = index.knn_query(M, k=kk)
    out: List[tuple] = []
    seen: set = set()
    for i in range(n):
        for lab, dist in zip(labels[i], distances[i]):
            j = int(lab)
            if j == i:
                continue
            sim = 1.0 - float(dist)
            if sim < lo:
                continue
            a, b = (i, j) if i < j else (j, i)
            if (a, b) in seen:
                continue
            seen.add((a, b))
            out.append((idx_seeds[a], idx_seeds[b], sim))
    return out


def _star_groups(seeds: List[str], members: Dict[str, List[str]],
                 edges: List[tuple], hi: float) -> Dict[str, str]:
    """贪心星型: 按成员数降序, 未分配 seed 作锚点, 认领其 ≥hi 直接邻居中未分配者。
    只允许"锚点—直接邻居", 不允许锚点间再链 → 簇直径有界, 无链式大簇。
    返回 seed -> anchor。O(N log N + N·k)。"""
    adj: Dict[str, List[tuple]] = {}
    for a, b, sim in edges:
        if sim >= hi:
            adj.setdefault(a, []).append((b, sim))
            adj.setdefault(b, []).append((a, sim))
    order = sorted(seeds, key=lambda s: (-len(members.get(s, [])), s))
    assigned: Dict[str, str] = {}
    for s in order:
        if s in assigned:
            continue
        assigned[s] = s
        for nb, _sim in adj.get(s, []):
            if nb not in assigned:
                assigned[nb] = s
    return assigned


class _UF:
    def __init__(self, items): self.p = {x: x for x in items}
    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]; x = self.p[x]
        return x
    def union(self, a, b): self.p[self.find(a)] = self.find(b)


def cluster_objects(
    objects: List[dict],
    vectors: Dict[str, List[float]],
    confirmed: Set[FrozenSet[str]],
    rejected: Set[FrozenSet[str]],
    *,
    seed_fn,
    conflict_fn=None,
    id_prefix: str = "K-",
    hi: float = 0.94,
    lo: float = 0.82,
    top_k: int = 5,
    max_pending: int = 1000,
) -> dict:
    """Generic cross-document clustering (concept/claim/formula/procedure).
    seed_fn(obj)->str 决定精确合并 key;conflict_fn(name_a,name_b)->bool 为护栏(None=不拦);
    id_prefix 决定 canonical_id 前缀(各类型隔离)。其余算法(ANN→护栏→星型→三档分流→
    confirmed/rejected force-union/block)与原 cluster_concepts 完全一致。"""
    seed_of = {c["object_id"]: seed_fn(c) for c in objects}
    seeds = sorted(set(seed_of.values()))
    uf = _UF(seeds)
    # confirmed/rejected 存储的是已经过 seed_fn 处理的 seed 字符串(caller 负责对齐)
    for pair in confirmed:
        if len(pair) != 2:
            continue   # both names normalize equal (size-1 fold) or malformed: harmless
        a, b = tuple(pair)
        if a in uf.p and b in uf.p:
            uf.union(a, b)
    rej = {frozenset(p) for p in rejected}

    seed_first_name: Dict[str, str] = {}
    for c in objects:
        s = seed_of[c["object_id"]]
        if s not in seed_first_name:
            seed_first_name[s] = c.get("name", "")

    members: Dict[str, List[str]] = {}
    for c in objects:
        members.setdefault(seed_of[c["object_id"]], []).append(c["object_id"])

    reps: Dict[str, np.ndarray] = {}
    for s in seeds:
        vs = [vectors[o] for o in members[s] if o in vectors]
        if vs:
            reps[s] = np.mean(np.asarray(vs, dtype=np.float32), axis=0)

    raw = _ann_candidates(seeds, reps, k=top_k, lo=lo)
    cand = []
    for a, b, sim in raw:
        if rej and frozenset((a, b)) in rej:
            continue
        if conflict_fn and conflict_fn(seed_first_name.get(a, a), seed_first_name.get(b, b)):
            continue
        cand.append((a, b, sim))

    star = _star_groups(seeds, members, cand, hi)
    auto_set = {frozenset((nb, anc)) for nb, anc in star.items() if nb != anc}

    groups: Dict[str, List[str]] = {}
    for s in seeds:
        groups.setdefault(uf.find(s), []).append(s)
    canon_id, canon_name = {}, {}
    for root, grp in groups.items():
        best = max(grp, key=lambda s: len(members[s]))
        cid = id_prefix + min(grp)
        for s in grp:
            canon_id[s] = cid
        canon_name[cid] = seed_first_name[best]
    cluster_map = {c["object_id"]: canon_id[seed_of[c["object_id"]]] for c in objects}
    names = {c["object_id"]: canon_name[cluster_map[c["object_id"]]] for c in objects}

    auto_candidates = [(canon_id[a], canon_id[b], sim) for a, b, sim in cand
                       if sim >= hi and frozenset((a, b)) in auto_set and canon_id[a] != canon_id[b]]
    pending = [(canon_id[a], canon_id[b], sim) for a, b, sim in cand
               if sim < hi and canon_id[a] != canon_id[b]]
    pending.sort(key=lambda t: t[2], reverse=True)
    was_capped = len(pending) > max_pending
    pending = pending[:max_pending]
    return {"cluster_map": cluster_map, "canonical_names": names,
            "auto_candidates": auto_candidates, "pending": pending, "capped": was_capped}


def cluster_concepts(
    concepts: List[dict],
    vectors: Dict[str, List[float]],
    confirmed: Set[FrozenSet[str]],
    rejected: Set[FrozenSet[str]],
    hi: float = 0.94,
    lo: float = 0.82,
    top_k: int = 5,
    max_pending: int = 1000,
) -> dict:
    """精确同名 + 已确认对 force-union; 向量候选经 ANN→护栏→星型, 但**不自动 union**:
    ≥hi 进 auto_candidates(LLM 兜底), [lo,hi) 进 pending(人工)。全程 sub-quadratic。
    薄包装: 委托 cluster_objects,使用 _norm seed + _discriminative_conflict 护栏 + K- 前缀。
    confirmed/rejected 中的名称先用 _norm 标准化,与 cluster_objects 期待的 seed 格式对齐。"""
    norm_confirmed = {frozenset(_norm(n) for n in p) for p in confirmed}
    norm_rejected = {frozenset(_norm(n) for n in p) for p in rejected}
    return cluster_objects(
        concepts, vectors, norm_confirmed, norm_rejected,
        seed_fn=lambda c: _norm(c.get("name", "")),
        conflict_fn=_discriminative_conflict,
        id_prefix="K-",
        hi=hi, lo=lo, top_k=top_k, max_pending=max_pending,
    )


def derive_unified_graph(nodes: List[dict], edges: List[dict], cluster_map: Dict[str, str]) -> dict:
    """Rewire member-Concept endpoints to canonical ids; dedup edges. O(V+E).

    Only CONCEPT members are folded to canonical ids. cluster_map may also carry
    non-concept (claim/formula/procedure) entries (used for answer-context dedup),
    but the unified graph view folds concepts only — folding non-concept node ids
    on edges while keeping their nodes raw would create dangling edges."""
    concept_ids = {n["id"] for n in nodes if n["object_type"] == "concept"}
    def canon(oid): return cluster_map.get(oid, oid) if oid in concept_ids else oid
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
