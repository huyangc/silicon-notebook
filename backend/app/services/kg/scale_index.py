"""规模化 KG 检索的紧凑基底：scipy CSR 图 + 个性化 PPR + active 拼接 + 构建/加载。

设计见 docs/superpowers/specs/2026-06-29-base-kg-scale-retrieval-design.md。
本模块尽量纯函数、可单测；DB/IO 由 sqlite_repository 包装层提供数据。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np
import scipy.sparse as sp


def personalized_ppr(
    transition: "sp.csr_matrix",
    reset: "np.ndarray",
    damping: float = 0.5,
    tol: float = 1e-8,
    max_iter: int = 100,
) -> "np.ndarray":
    """个性化 PageRank 幂迭代。

    transition: 列随机转移阵 A（A[j,i] = 边 i->j 的归一化权重，按 i 的出度归一）。
    reset:      personalization 向量（非负；内部归一为和=1 作为 teleport 分布）。
    返回稳态分布 x（和=1）；全零 reset → 全零向量（调用方据此回退 dense）。
    """
    s = float(reset.sum())
    if s <= 0:
        return np.zeros(transition.shape[0], dtype=np.float64)
    p = (reset.astype(np.float64) / s)
    x = p.copy()
    d = float(damping)
    for _ in range(max_iter):
        x_new = (1.0 - d) * p + d * transition.dot(x)
        x_new += (1.0 - x_new.sum()) * p
        if np.abs(x_new - x).sum() < tol:
            x = x_new
            break
        x = x_new
    total = x.sum()
    return x / total if total > 0 else x


def build_transition(
    node_ids: List[str],
    edges: List[Tuple[str, str, float]],
) -> Tuple["sp.csr_matrix", Dict[str, int]]:
    """边列表 -> 列随机转移阵 A（A[j,i]=i->j 归一化权重）。

    端点不在 node_ids 的边丢弃（防悬空）。out-degree 加权归一。返回 (A_csr, index)。
    调用方负责把无向边拆成正反两条。
    """
    index = {nid: i for i, nid in enumerate(node_ids)}
    n = len(node_ids)
    rows, cols, data = [], [], []
    for s, t, w in edges:
        si, ti = index.get(s), index.get(t)
        if si is None or ti is None:
            continue
        rows.append(ti)
        cols.append(si)
        data.append(float(w))
    if not data:
        return sp.csr_matrix((n, n), dtype=np.float64), index
    M = sp.csr_matrix((data, (rows, cols)), shape=(n, n), dtype=np.float64)
    colsum = np.asarray(M.sum(axis=0)).ravel()
    colsum[colsum == 0] = 1.0
    D = sp.diags(1.0 / colsum)
    return (M @ D).tocsr(), index


def save_scale_index(
    out_dir: str,
    *,
    node_ids: List[str],
    transition: "sp.csr_matrix",
    idf: List[float],
    chunk_index: List[int],
    ann_vectors,
    ann_labels: List[str],
    manifest: dict,
) -> dict:
    """把构建好的数组落盘到 out_dir。

    ann_vectors: (m, dim) float32 numpy array（只含有 embeddings 的 KG 节点）。
    ann_labels: 对应 kg 节点 object_id 列表（与 ann_vectors 行对齐）。
    写出 7 个文件：graph.npz, node_ids.npy, idf.npy, chunk_index.npy,
    ann.bin, ann_labels.npy, manifest.json。返回传入的 manifest dict。
    """
    import hnswlib

    os.makedirs(out_dir, exist_ok=True)

    sp.save_npz(os.path.join(out_dir, "graph.npz"), transition)
    np.save(os.path.join(out_dir, "node_ids.npy"), np.asarray(node_ids, dtype=object))
    np.save(os.path.join(out_dir, "idf.npy"), np.asarray(idf, dtype=np.float32))
    np.save(os.path.join(out_dir, "chunk_index.npy"), np.asarray(chunk_index, dtype=np.int32))
    np.save(os.path.join(out_dir, "ann_labels.npy"), np.asarray(ann_labels, dtype=object))

    ann_vecs = np.asarray(ann_vectors, dtype=np.float32) if len(ann_vectors) else np.empty((0, 1), dtype=np.float32)
    dim = int(ann_vecs.shape[1]) if ann_vecs.shape[0] > 0 else int(manifest.get("dim", 1))
    if dim < 1:
        dim = 1

    idx = hnswlib.Index(space="cosine", dim=dim)
    idx.init_index(max_elements=max(1, ann_vecs.shape[0]), ef_construction=200, M=16, random_seed=42)
    if ann_vecs.shape[0] > 0:
        idx.add_items(ann_vecs, np.arange(ann_vecs.shape[0]))
    idx.save_index(os.path.join(out_dir, "ann.bin"))

    with open(os.path.join(out_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh)

    return manifest
