"""Pure PPR-graph edge-synthesis helpers (sunk from app.services.kg.ppr in B3).

variant_edge_pairs / emb_synonym_edges are the two synonym-bridge builders
consumed directly by app.repositories (index_projection_store, both
backends) when it assembles extra_edges for build_ppr_graph. They are pure
functions (variant_edge_pairs is regex-only; emb_synonym_edges lazily
imports numpy/hnswlib inside the function body) with zero app.services/
app.repositories dependency, so they live here rather than in
app.services.kg.ppr (which stays put — build_ppr_graph, run_ppr and the rest
of the PPR-graph module are not part of this move).
``app.services.kg.ppr`` re-exports both names unchanged for existing
importers.
"""
from __future__ import annotations

import re
from typing import Dict, List, Optional, Tuple


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
