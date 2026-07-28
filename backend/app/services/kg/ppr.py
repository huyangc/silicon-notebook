"""rustworkx-backed in-memory graph for HippoRAG-style Personalized PageRank
cross-document retrieval. Pure functions, zero I/O — unit-testable.

Graph is a PyDiGraph with RECIPROCAL edges: rx.pagerank only accepts PyDiGraph
(it rejects PyGraph); adding both directions per edge makes PPR flow
symmetrically — equivalent to HippoRAG's igraph directed=False. Three node kinds
share one index:
  - KG entity      key = object_id
  - passage(chunk) key = f"chunk:{chunk_id}"
  - cluster router key = f"cluster:{canonical_id}"  (synthetic synonym hub)
Synonym bridges are a star: every member of a concept cluster links to the
cluster's router node, so PPR mass flows between same-concept nodes living in
different documents (N edges per cluster, not N^2).
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple

import rustworkx as rx

from app.services.kg.edge_schema import is_queryable_edge_pair


def build_ppr_graph(
    kg_nodes: Dict[str, dict],
    chunk_ids: List[str],
    relations: List[dict],
    memberships: List[Tuple[str, str]],
    cluster_groups: Dict[str, List[str]],
    extra_edges: Optional[List[Tuple[str, str, float]]] = None,
) -> Tuple[rx.PyDiGraph, Dict[str, int], Dict[int, str]]:
    """Build the reciprocal-edge PPR digraph.

    kg_nodes       — {object_id: {"type": str, "name": str}}
    chunk_ids      — list of chunk_id strings (passage nodes)
    relations      — [{"source_object_id","target_object_id", ...}, ...] (KG↔KG)
    memberships    — [(object_id, chunk_id), ...] (KG↔chunk)
    cluster_groups — {canonical_id: [object_id, ...]} (synonym bridges)

    Returns (G, key_to_idx, chunk_idx_to_id):
      key_to_idx      — {node_key: vertex_idx} (object_id / chunk:* / cluster:*)
      chunk_idx_to_id — {vertex_idx: chunk_id} for passage nodes only
    """
    G: rx.PyDiGraph = rx.PyDiGraph()
    key_to_idx: Dict[str, int] = {}
    chunk_idx_to_id: Dict[int, str] = {}

    def _add(key: str, payload: dict) -> int:
        idx = key_to_idx.get(key)
        if idx is None:
            idx = G.add_node(payload)
            key_to_idx[key] = idx
        return idx

    for oid, meta in kg_nodes.items():
        _add(oid, {"kind": "entity", "object_id": oid,
                   "object_type": meta.get("type", ""), "name": meta.get("name", "")})

    for cid in chunk_ids:
        idx = _add(f"chunk:{cid}", {"kind": "chunk", "chunk_id": cid})
        chunk_idx_to_id[idx] = cid

    seen_pairs: set = set()

    def _edge(a: int, b: int, weight: float) -> None:
        # Add BOTH directions (rx.pagerank needs a PyDiGraph; reciprocal edges
        # make traversal symmetric). Dedup on the unordered pair so one logical
        # undirected edge yields exactly two directed edges.
        if a == b:
            return
        k = (a, b) if a < b else (b, a)
        if k in seen_pairs:
            return
        seen_pairs.add(k)
        G.add_edge(a, b, {"weight": weight})
        G.add_edge(b, a, {"weight": weight})

    for rel in relations:
        if (rel["source_object_id"] not in kg_nodes
                or rel["target_object_id"] not in kg_nodes):
            continue
        a = key_to_idx.get(rel["source_object_id"])
        b = key_to_idx.get(rel["target_object_id"])
        if a is None or b is None:
            continue  # dangling
        if not is_queryable_edge_pair(
            rel.get("edge_type"),
            kg_nodes[rel["source_object_id"]].get("type"),
            kg_nodes[rel["target_object_id"]].get("type"),
        ):
            continue
        _edge(a, b, 1.0)

    for oid, cid in memberships:
        a = key_to_idx.get(oid)
        b = key_to_idx.get(f"chunk:{cid}")
        if a is None or b is None:
            continue
        _edge(a, b, 1.0)

    for canonical_id, members in cluster_groups.items():
        present = [key_to_idx[o] for o in members if o in key_to_idx]
        if not present:
            continue
        router = _add(f"cluster:{canonical_id}", {"kind": "cluster",
                                                  "canonical_id": canonical_id})
        for m in present:
            _edge(router, m, 1.0)

    for a_oid, b_oid, weight in (extra_edges or []):
        a = key_to_idx.get(a_oid)
        b = key_to_idx.get(b_oid)
        if a is not None and b is not None:
            _edge(a, b, float(weight))

    return G, key_to_idx, chunk_idx_to_id


_VARIANT_TOKEN = re.compile(r'[\s\-_]*\b(v?\d+(?:\.\d+)*|\d+\.?\d*\s*[bBmM])\b', re.IGNORECASE)


def _variant_base(name: str) -> Optional[str]:
    """Strip version (v3, 2.5) and size (7B, 70B) tokens → base model name.
    Returns None if no such token was present (so plain concepts are excluded)."""
    stripped = _VARIANT_TOKEN.sub(' ', name)
    base = re.sub(r'[\s\-_]+', ' ', stripped).strip().lower()
    if base == re.sub(r'[\s\-_]+', ' ', name).strip().lower():
        return None  # nothing stripped → not a versioned/sized entity
    return base if len(base) >= 3 else None


def variant_edge_pairs(kg_nodes: Dict[str, dict], weight: float) -> List[Tuple[str, str, float]]:
    """Group entities by version/size-stripped base name; connect distinct members
    pairwise with `weight`. Only entities that HAD a version/size token participate."""
    groups: Dict[str, list] = {}
    for oid, meta in kg_nodes.items():
        base = _variant_base(str(meta.get("name", "")))
        if base:
            groups.setdefault(base, []).append(oid)
    out: List[Tuple[str, str, float]] = []
    for members in groups.values():
        uniq = sorted(set(members))
        if len(uniq) < 2:
            continue
        rep = uniq[0]
        for m in uniq[1:]:
            out.append((rep, m, float(weight)))   # 星型:O(k),连通性经 rep 保持
    return out


def emb_synonym_edges(ids, matrix, threshold: float = 0.8, top_k: int = 20,
                      max_entities: int = 50000, prebuilt_index=None,
                      ef_construction: int = 200):
    """hnswlib ANN KNN over entity embeddings → synonym edges (id_a,id_b,cosine).
    每节点取 top_k 邻居、cosine ≥ threshold。规模化:超 max_entities 不再返 []
    而是照常走 ANN(hnswlib 支持百万级);max_entities 仅作签名兼容。`matrix` 是
    (n, d) float 数组(行对齐 `ids`)。

    不再显式归一化整矩阵拷贝:hnswlib space="cosine" 在 add_items/knn_query 时
    内部已对向量归一化,`M / norms` 是纯浪费的 2GB 级拷贝(490k 节点规模下)。
    输入矩阵不被修改(纯函数)。

    prebuilt_index: 调用方已建好的 hnswlib.Index(add_items 完毕、行序与 ids 对齐、
    标签 0..n-1)。传入时跳过 init_index/add_items,直接复用其构建结果做 KNN——
    build_scale_index 用它把「同义边 KNN」和「持久化 ann.bin」共享同一次 hnsw
    构建(hnsw 是流水线里最贵的计算,49 万节点规模下建两遍是真机事故的主因之一)。
    None(默认)时保持现行为:内部自建一次性索引(ef_construction 可配,默认 200,
    与旧硬编码值一致)。本函数是纯函数,ef_construction 由调用方传入而非在此处
    读取 settings。

    fail-open:hnswlib 异常返 []。"""
    import numpy as np
    import hnswlib
    n = len(ids)
    if n < 2 or matrix is None:
        return []
    M = np.asarray(matrix, dtype=np.float32)
    if M.ndim != 2 or M.shape[0] != n:
        return []
    dim = int(M.shape[1])
    try:
        if prebuilt_index is not None:
            idx = prebuilt_index
        else:
            idx = hnswlib.Index(space="cosine", dim=dim)
            idx.init_index(max_elements=n, ef_construction=ef_construction, M=16, random_seed=42)
            idx.add_items(M, np.arange(n))
        idx.set_ef(max(top_k + 1, 64))
        k = min(top_k + 1, n)                       # +1 因含自身
        labels, distances = idx.knn_query(M, k=k)
    except Exception:
        return []                                   # fail-open:同义边为空,不崩 build

    # 向量化后处理(替代原逐行 Python 循环 + 元组 set 去重,千万级边规模下 set
    # 本身就是 ~5GB 级分配)。labels[i] 按 knn_query 返回的距离升序排列(近邻优先),
    # 这里按行主序(i 从 0 升序、行内保持 knn_query 原序)展平后用 np.unique 的
    # "首次出现即代表" 特性做去重——与旧实现完全一致的选择规则:同一无向对
    # (a,b) 若出现多次(i 的行里找到 j、j 的行里也找到 i),保留"行主序最先遇到"
    # 的那个 sim(而非取 max)。np.unique(..., return_index=True) 对已排序的 keys
    # 数组返回的是"该值第一次出现的位置"，只要保证输入按行主序排列即可复现。
    labels_arr = np.asarray(labels)
    dist_arr = np.asarray(distances)
    row_idx = np.repeat(np.arange(n), labels_arr.shape[1])
    col_idx = labels_arr.reshape(-1).astype(np.int64)
    sims = 1.0 - dist_arr.reshape(-1).astype(np.float64)

    mask = (col_idx != row_idx) & (sims >= threshold)
    if not np.any(mask):
        return []
    row_idx = row_idx[mask]
    col_idx = col_idx[mask]
    sims = sims[mask]

    a = np.minimum(row_idx, col_idx)
    b = np.maximum(row_idx, col_idx)
    keys = a * n + b   # unordered-pair key, unique per (a,b) with a<b

    # keys are already in row-major encounter order (row_idx ascending, ties
    # broken by original per-row knn order) because we built them via a
    # stable reshape of the row-major labels/distances arrays and only
    # filtered with a boolean mask (mask preserves relative order). A stable
    # "first occurrence" dedup on keys therefore reproduces the old
    # first-seen-wins semantics exactly.
    _, first_idx = np.unique(keys, return_index=True)
    first_idx = np.sort(first_idx)   # np.unique sorts by key, not by first_idx

    out = [(ids[int(a[i])], ids[int(b[i])], float(sims[i])) for i in first_idx]
    return out


def run_ppr(
    G: rx.PyDiGraph,
    chunk_idx_to_id: Dict[int, str],
    reset: Dict[int, float],
    damping: float = 0.5,
) -> List[Tuple[str, float]]:
    """Run Personalized PageRank and return chunk rankings.

    reset   — {vertex_idx: weight} personalization vector (>=1 non-zero entry).
    Returns [(chunk_id, normalized_score), ...] sorted desc; scores min-max
    normalized into [0,1] so they satisfy the relevance/tau invariant. Empty
    reset (or no non-zero weight) → [] (caller falls back to dense retrieval).
    """
    if not reset or not any(w > 0 for w in reset.values()) or G.num_nodes() == 0:
        return []
    scores = rx.pagerank(
        G,
        alpha=damping,
        personalization={int(k): float(v) for k, v in reset.items() if v > 0},
        weight_fn=lambda payload: float(payload.get("weight", 1.0)),
    )
    raw = [(cid, float(scores[idx])) for idx, cid in chunk_idx_to_id.items()]
    if not raw:
        return []
    vals = [s for _, s in raw]
    lo, hi = min(vals), max(vals)
    span = hi - lo
    norm = [(cid, (s - lo) / span if span > 0 else 0.0) for cid, s in raw]
    norm.sort(key=lambda x: x[1], reverse=True)
    return norm
