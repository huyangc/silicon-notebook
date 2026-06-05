"""Per-notebook vector matrices for fast, low-memory retrieval.

Vectors live in SQLite as JSON text. Loading thousands of them as Python float
lists blows up memory (each float is a ~24-byte Python object). This module
streams them into ONE L2-normalized float32 numpy matrix (~4 bytes/float), so
per-query cosine similarity is a single matmul with low, bounded memory.
"""
from __future__ import annotations

import json
from typing import Dict, Iterable, List, Tuple

import numpy as np


def build_matrix(rows: Iterable[Tuple[str, str]]) -> Tuple[List[str], np.ndarray]:
    """rows: iterable of (id, json_vector_text). Returns (ids, normalized float32
    matrix [N, dim]). Rows with empty/invalid/wrong-dim vectors are skipped.
    Each vector is parsed straight to float32 and stored L2-normalized — no Python
    float lists are retained."""
    ids: List[str] = []
    vecs: List[np.ndarray] = []
    dim = None
    for vid, raw in rows:
        if not raw:
            continue
        try:
            arr = np.asarray(json.loads(raw), dtype=np.float32)
        except Exception:  # noqa: BLE001 — skip unparseable rows
            continue
        if arr.ndim != 1 or arr.size == 0:
            continue
        if dim is None:
            dim = int(arr.size)
        elif arr.size != dim:
            continue
        norm = float(np.linalg.norm(arr))
        if norm > 0:
            arr = arr / norm
        ids.append(vid)
        vecs.append(arr)
    if not vecs:
        return [], np.zeros((0, 0), dtype=np.float32)
    return ids, np.vstack(vecs)


def query_sims(query_vector, ids: List[str], matrix: np.ndarray) -> Dict[str, float]:
    """Cosine similarity of a query against the pre-normalized matrix. {id: sim}."""
    if not query_vector or not ids or matrix.size == 0:
        return {}
    q = np.asarray(query_vector, dtype=np.float32)
    if q.ndim != 1 or q.shape[0] != matrix.shape[1]:
        return {}
    qn = float(np.linalg.norm(q))
    if qn == 0:
        return {i: 0.0 for i in ids}
    q = q / qn
    sims = matrix @ q
    return {i: float(s) for i, s in zip(ids, sims)}
