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


def build_matrix(rows: Iterable[Tuple[str, str]], n_hint: int = 0) -> Tuple[List[str], np.ndarray]:
    """rows: iterable of (id, json_vector_text). Returns (ids, normalized float32
    matrix [N, dim]). Rows with empty/invalid/wrong-dim vectors are skipped.
    Each vector is parsed straight to float32 and stored L2-normalized — no Python
    float lists are retained.

    n_hint: optional upper-bound estimate of the number of rows (e.g. a COUNT(*)
    the caller already ran). When given, this preallocates one (n_hint, dim)
    float32 array on the first valid row and fills it in place — avoiding the
    default path's 490k-small-ndarrays-then-vstack pattern (2x peak memory at
    scale.py-file scope). If the actual valid-row count exceeds n_hint (some
    rows were skipped from the estimate, or the hint was wrong), it falls back
    to appending the overflow into a plain Python list and concatenates at the
    end — never truncates output. Output is bit-identical to the no-hint path
    regardless of hint accuracy; n_hint is purely an allocation hint."""
    ids: List[str] = []
    dim = None
    prealloc: np.ndarray = None
    n_filled = 0
    overflow_vecs: List[np.ndarray] = []
    vecs: List[np.ndarray] = []  # only used when n_hint <= 0 (no prealloc)

    def _append(arr: np.ndarray) -> None:
        nonlocal prealloc, n_filled
        if n_hint > 0:
            if prealloc is None:
                prealloc = np.empty((n_hint, arr.size), dtype=np.float32)
            if n_filled < n_hint:
                prealloc[n_filled] = arr
                n_filled += 1
            else:
                overflow_vecs.append(arr)
        else:
            vecs.append(arr)

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
        _append(arr)

    if not ids:
        return [], np.zeros((0, 0), dtype=np.float32)

    if n_hint > 0:
        mat = prealloc[:n_filled]
        if overflow_vecs:
            mat = np.vstack([mat, np.vstack(overflow_vecs)])
        return ids, mat
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
