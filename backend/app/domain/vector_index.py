"""Per-notebook vector matrices for fast, low-memory retrieval.

Sunk to app.domain in B3 (pure codec/matrix helpers, zero app.services/
app.repositories dependency). ``app.services.vector_index`` re-exports every
name here unchanged for existing importers.

Vectors live in SQLite either as legacy JSON text or (new writes / backfilled
rows) as raw float32 BLOBs (`encode_vector`/`decode_vector` below). BLOB rows
skip json.loads entirely — `np.frombuffer` reinterprets the bytes in place,
which is the dominant win at scale (490k x 1024-dim rows: JSON parsing is the
serial bottleneck of a matrix load). This module streams whichever format a
row has into ONE L2-normalized float32 numpy matrix (~4 bytes/float), so
per-query cosine similarity is a single matmul with low, bounded memory.
"""
from __future__ import annotations

import json
from typing import Dict, Iterable, List, Optional, Tuple, Union

import numpy as np


def encode_vector(vec) -> bytes:
    """Encode a vector (list/tuple/ndarray of floats) as a raw float32 BLOB.
    This is the canonical write-path format — every embeddings INSERT/UPDATE
    should store `encode_vector(vec)` instead of `json.dumps(vec)`."""
    return np.asarray(vec, dtype=np.float32).tobytes()


def decode_vector(raw: Union[bytes, bytearray, memoryview, str, None]) -> Optional[np.ndarray]:
    """Decode a `vector` column value regardless of stored format:
      - bytes/bytearray/memoryview (new BLOB rows) -> np.frombuffer(dtype=float32),
        copied so the array can outlive the source buffer/row.
      - str (legacy JSON rows) -> json.loads then np.asarray(dtype=float32).
      - None/empty -> None.
    Raises nothing on malformed input — callers that need skip-on-error
    semantics should catch around this call (mirrors build_matrix's existing
    try/except skip behavior)."""
    if raw is None or (isinstance(raw, (str, bytes, bytearray, memoryview)) and len(raw) == 0):
        return None
    if isinstance(raw, (bytes, bytearray, memoryview)):
        return np.frombuffer(raw, dtype=np.float32).copy()
    return np.asarray(json.loads(raw), dtype=np.float32)


def resolve_runtime_dim(settings=None) -> int:
    """生效的运行时截断维;0 = 关闭(不截断)。settings 缺省读全局 get_settings()。
    语义:EMBED_DIM 恒为存储/原生维,EMBED_RUNTIME_DIM 是相似度空间维
    (MRL 前缀截断,见 docs/superpowers/specs/2026-07-03-runtime-dim-1024-plan.md)。"""
    if settings is None:
        from app.core.config import get_settings
        settings = get_settings()
    return int(getattr(settings, "embed_runtime_dim", 0) or 0)


def truncate_vec(arr: np.ndarray, runtime_dim: int) -> np.ndarray:
    """MRL 运行时截断:取前 runtime_dim 维 + L2 re-normalize。全仓唯一截断语义
    (截后归一,原子操作),与 app.eval.mrl_truncation.truncated_sims 的口径由
    测试锁死等价。runtime_dim<=0 或向量本就不长于 runtime_dim → 原样返回
    (短于运行时维的异维残留留给下游 dim 检查按既有语义跳过)。"""
    if runtime_dim <= 0 or arr.size <= runtime_dim:
        return arr
    out = arr[:runtime_dim]
    norm = float(np.linalg.norm(out))
    if norm > 0:
        out = out / norm
    return out.astype(np.float32, copy=False)


def build_matrix(rows: Iterable[Tuple[str, str]], n_hint: int = 0,
                 runtime_dim: "Optional[int]" = None) -> Tuple[List[str], np.ndarray]:
    """rows: iterable of (id, vector_raw) where vector_raw is either legacy JSON
    text or a raw float32 BLOB (bytes/memoryview) — see `decode_vector`. Returns
    (ids, normalized float32 matrix [N, dim]). Rows with empty/invalid/wrong-dim
    vectors are skipped. Each vector is parsed straight to float32 and stored
    L2-normalized — no Python float lists are retained.

    n_hint: optional upper-bound estimate of the number of rows (e.g. a COUNT(*)
    the caller already ran). When given, this preallocates one (n_hint, dim)
    float32 array on the first valid row and fills it in place — avoiding the
    default path's 490k-small-ndarrays-then-vstack pattern (2x peak memory at
    scale.py-file scope). If the actual valid-row count exceeds n_hint (some
    rows were skipped from the estimate, or the hint was wrong), it falls back
    to appending the overflow into a plain Python list and concatenates at the
    end — never truncates output. Output is bit-identical to the no-hint path
    regardless of hint accuracy; n_hint is purely an allocation hint.

    runtime_dim: MRL 运行时截断维 — None(默认)= 读 settings 的 EMBED_RUNTIME_DIM;
    显式 0 = 不截断(真相源工具/测试用)。截断发生在逐行 decode 之后、首行定维
    判定**之前**(否则原生维首行会把已截断的后续行整批当异维丢),且在预分配
    矩阵之前生效 —— 峰值内存按截断维计,这是大库 fold/build 峰值 ÷4 的前提。"""
    rd = resolve_runtime_dim() if runtime_dim is None else int(runtime_dim)
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
            arr = decode_vector(raw)
        except Exception:  # noqa: BLE001 — skip unparseable rows
            continue
        if arr is None:
            continue
        if arr.ndim != 1 or arr.size == 0:
            continue
        if rd > 0:
            arr = truncate_vec(arr, rd)     # 必须先于定维判定(见 docstring)
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


def matrix_pages(rows: Iterable[Tuple[str, str]], page_rows: int,
                 runtime_dim: "Optional[int]" = None):
    """``build_matrix``, yielded one bounded page at a time.

    Generator of ``(ids, matrix)`` pairs holding at most ``page_rows`` VALID
    rows each (invalid/skipped rows never occupy a page slot), for the offline
    build's ANN legs: hnswlib copies each page into its own graph, so the
    caller can drop a page the moment ``add_items`` returns instead of keeping
    one whole-notebook matrix resident (the 8M-row relation leg is ~33GB).

    All five ``build_matrix`` semantics are carried ACROSS pages, not
    re-derived per page — a per-page ``build_matrix`` call would break the
    middle two:
      1. ``runtime_dim`` truncation happens per row, before the dim decision;
      2. the FIRST valid row of the FIRST non-empty page fixes ``dim`` for the
         whole scan (per-page re-derivation would let a page whose first row
         is a wrong-dim outlier redefine the width mid-stream);
      3. rows whose (post-truncation) width differs from that ``dim`` are
         skipped, wherever they fall;
      4. each retained row is L2-normalized;
      5. ``ids`` stay row-aligned with the matrix, in scan order.
    Concatenating every page therefore reproduces ``build_matrix(rows)``
    element for element — the property the oracle test pins.

    ``page_rows`` must be >= 1. An input yielding no valid row yields no page
    at all (the caller's "no vectors" branch), mirroring ``build_matrix``'s
    empty return rather than emitting a zero-row matrix.
    """
    rd = resolve_runtime_dim() if runtime_dim is None else int(runtime_dim)
    cap = max(1, int(page_rows))
    dim = None
    ids: List[str] = []
    vecs: List[np.ndarray] = []
    for vid, raw in rows:
        if not raw:
            continue
        try:
            arr = decode_vector(raw)
        except Exception:  # noqa: BLE001 — skip unparseable rows (build_matrix parity)
            continue
        if arr is None:
            continue
        if arr.ndim != 1 or arr.size == 0:
            continue
        if rd > 0:
            arr = truncate_vec(arr, rd)     # 必须先于定维判定(见 build_matrix)
        if dim is None:
            dim = int(arr.size)
        elif arr.size != dim:
            continue
        norm = float(np.linalg.norm(arr))
        if norm > 0:
            arr = arr / norm
        ids.append(vid)
        vecs.append(arr)
        if len(ids) >= cap:
            yield ids, np.vstack(vecs)
            ids, vecs = [], []
    if ids:
        yield ids, np.vstack(vecs)


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


def top_k_sims(query_vector, ids: List[str], matrix: np.ndarray, k: int) -> List[Tuple[str, float]]:
    """Top-`k` (id, cosine_sim) pairs, sorted desc, WITHOUT materializing a full
    {id: sim} dict over all N rows — at N in the millions that dict itself is a
    GB-scale allocation (millions of (str, float) pairs). Stays numpy end to
    end: matrix @ q -> np.argpartition selects the top-k *indices* in O(N)
    without a full sort, then only those k rows are turned into (id, float)
    tuples. Same cosine-similarity semantics/values as query_sims — this is
    purely a materialization-avoiding variant for the top-k case.

    Degenerate inputs (empty query_vector/ids/matrix, dim mismatch, k<=0)
    return []. A zero-norm query_vector returns the first k ids at sim=0.0
    (mirrors query_sims' zero-norm fallback, arbitrary order tie-break)."""
    if not query_vector or not ids or matrix.size == 0 or k <= 0:
        return []
    q = np.asarray(query_vector, dtype=np.float32)
    if q.ndim != 1 or q.shape[0] != matrix.shape[1]:
        return []
    n = len(ids)
    kk = min(k, n)
    qn = float(np.linalg.norm(q))
    if qn == 0:
        return [(i, 0.0) for i in ids[:kk]]
    q = q / qn
    sims = matrix @ q
    if kk >= n:
        top_idx = np.argsort(-sims)
    else:
        part = np.argpartition(-sims, kk - 1)[:kk]
        top_idx = part[np.argsort(-sims[part])]
    return [(ids[int(i)], float(sims[int(i)])) for i in top_idx]
