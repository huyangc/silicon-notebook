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


@dataclass
class ScaleIndex:
    node_ids: list
    node_index: dict
    transition: "sp.csr_matrix"
    idf: "np.ndarray"
    chunk_index: "np.ndarray"
    ann_labels: list
    ann_path: str
    manifest: dict


def load_scale_index(out_dir: str):
    """从 out_dir 加载持久化的 ScaleIndex。manifest 不存在或目录不存在时返回 None。
    ANN 索引不预加载（延迟由调用方按需用 ann_path 打开）。"""
    mpath = os.path.join(out_dir, "manifest.json")
    if not os.path.exists(mpath):
        return None
    with open(mpath) as fh:
        manifest = json.load(fh)
    transition = sp.load_npz(os.path.join(out_dir, "graph.npz"))
    node_ids = list(np.load(os.path.join(out_dir, "node_ids.npy"), allow_pickle=True))
    idf = np.load(os.path.join(out_dir, "idf.npy"))
    chunk_index = np.load(os.path.join(out_dir, "chunk_index.npy"))
    ann_labels = list(np.load(os.path.join(out_dir, "ann_labels.npy"), allow_pickle=True))
    return ScaleIndex(
        node_ids=node_ids,
        node_index={n: i for i, n in enumerate(node_ids)},
        transition=transition,
        idf=idf,
        chunk_index=chunk_index,
        ann_labels=ann_labels,
        ann_path=os.path.join(out_dir, "ann.bin"),
        manifest=manifest,
    )


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


def splice_active(
    base_ids: List[str],
    base_transition: "sp.csr_matrix",
    active_ids: List[str],
    active_edges: List[Tuple[str, str, float]],
) -> Tuple[List[str], "sp.csr_matrix"]:
    """把 active 的节点/边并入 base，按 id 合一（共享 canonical_id 自然合并）。
    返回 (combined_ids, combined_transition)。base 边从 base_transition 的稀疏结构还原
    （转移阵已列归一，权重信息有损，v1 用结构 + 权重 1.0 重算；等价测试证 top-k 稳健）。"""
    base_coo = base_transition.tocoo()
    # build_transition 建阵时: A[target_row, source_col] = i->j 归一化权重
    # COO 中 row=target(j), col=source(i) → 边方向 source->target = base_ids[col]->base_ids[row]
    base_edges_reconstructed = [
        (base_ids[i], base_ids[j], 1.0)
        for i, j in zip(base_coo.col, base_coo.row)
    ]
    base_set = set(base_ids)
    combined_ids = list(base_ids) + [a for a in active_ids if a not in base_set]
    combined_edges = base_edges_reconstructed + list(active_edges)
    A, _ = build_transition(combined_ids, combined_edges)
    return combined_ids, A
