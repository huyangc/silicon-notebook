import json
import math

import numpy as np

from app.services.vector_index import build_matrix, query_sims, top_k_sims, encode_vector, decode_vector
from app.services.retrieval import cosine


def test_build_matrix_normalizes_and_skips_bad():
    rows = [
        ("a", json.dumps([3.0, 4.0])),       # norm 5 -> normalized
        ("b", json.dumps([1.0, 0.0])),
        ("c", ""),                            # empty -> skipped
        ("d", "not-json"),                    # invalid -> skipped
        ("e", json.dumps([1.0, 2.0, 3.0])),  # wrong dim -> skipped
    ]
    ids, mat = build_matrix(rows)
    assert ids == ["a", "b"]
    assert mat.dtype == np.float32
    assert mat.shape == (2, 2)
    # each row L2-normalized
    for row in mat:
        assert math.isclose(float(np.linalg.norm(row)), 1.0, abs_tol=1e-5)


def test_query_sims_matches_cosine():
    rows = [
        ("a", json.dumps([0.1, 0.2, 0.3, 0.4])),
        ("b", json.dumps([0.4, 0.3, 0.2, 0.1])),
        ("c", json.dumps([-0.1, -0.2, -0.3, -0.4])),
    ]
    raw = {"a": [0.1, 0.2, 0.3, 0.4], "b": [0.4, 0.3, 0.2, 0.1], "c": [-0.1, -0.2, -0.3, -0.4]}
    ids, mat = build_matrix(rows)
    q = [0.1, 0.2, 0.3, 0.4]
    sims = query_sims(q, ids, mat)
    for k in ids:
        assert math.isclose(sims[k], cosine(q, raw[k]), abs_tol=1e-5)
    assert math.isclose(sims["a"], 1.0, abs_tol=1e-5)
    assert math.isclose(sims["c"], -1.0, abs_tol=1e-5)


def test_query_sims_edge_cases():
    ids, mat = build_matrix([("a", json.dumps([1.0, 0.0]))])
    assert query_sims([], ids, mat) == {}              # no query
    assert query_sims([1.0, 0.0], [], np.zeros((0, 0), dtype=np.float32)) == {}  # empty index
    assert query_sims([1.0, 2.0, 3.0], ids, mat) == {} # dim mismatch
    assert query_sims([0.0, 0.0], ids, mat)["a"] == 0.0  # zero query


def test_build_matrix_empty():
    ids, mat = build_matrix([])
    assert ids == [] and mat.size == 0


def _rows(n):
    rng = np.random.default_rng(3)
    return [(f"id{i}", json.dumps(rng.normal(size=4).tolist())) for i in range(n)]


def test_build_matrix_n_hint_exact_matches_no_hint():
    rows = _rows(10)
    ids0, mat0 = build_matrix(rows)
    ids1, mat1 = build_matrix(rows, n_hint=10)
    assert ids0 == ids1
    assert mat0.dtype == mat1.dtype == np.float32
    assert np.array_equal(mat0, mat1)


def test_build_matrix_n_hint_oversized_trims():
    rows = _rows(5)
    ids0, mat0 = build_matrix(rows)
    ids1, mat1 = build_matrix(rows, n_hint=50)
    assert ids0 == ids1
    assert mat1.shape == mat0.shape
    assert np.array_equal(mat0, mat1)


def test_build_matrix_n_hint_undersized_falls_back():
    rows = _rows(8)
    ids0, mat0 = build_matrix(rows)
    ids1, mat1 = build_matrix(rows, n_hint=3)  # too small — must not truncate output
    assert ids0 == ids1
    assert mat1.shape == mat0.shape
    assert np.array_equal(mat0, mat1)


def test_build_matrix_n_hint_with_skipped_rows():
    rows = [
        ("a", json.dumps([3.0, 4.0])),
        ("bad", ""),                 # skipped — n_hint counts raw rows, not valid ones
        ("b", json.dumps([1.0, 0.0])),
    ]
    ids0, mat0 = build_matrix(rows)
    ids1, mat1 = build_matrix(rows, n_hint=3)
    assert ids0 == ids1 == ["a", "b"]
    assert np.array_equal(mat0, mat1)


def test_build_matrix_n_hint_zero_or_none_like_no_hint():
    rows = _rows(4)
    ids0, mat0 = build_matrix(rows)
    ids1, mat1 = build_matrix(rows, n_hint=0)
    assert ids0 == ids1
    assert np.array_equal(mat0, mat1)


# --- BLOB storage (encode_vector/decode_vector) ---------------------------

def test_encode_decode_roundtrip():
    vec = [0.1, -2.5, 3.0, 0.0]
    blob = encode_vector(vec)
    assert isinstance(blob, bytes)
    out = decode_vector(blob)
    assert out.dtype == np.float32
    assert np.allclose(out, np.asarray(vec, dtype=np.float32))


def test_decode_vector_none_and_empty():
    assert decode_vector(None) is None
    assert decode_vector("") is None
    assert decode_vector(b"") is None


def test_decode_vector_json_str_path_unchanged():
    vec = [1.0, 2.0, 3.0]
    out = decode_vector(json.dumps(vec))
    assert np.allclose(out, np.asarray(vec, dtype=np.float32))


def test_decode_vector_memoryview():
    vec = [1.0, 2.0, 3.0]
    blob = encode_vector(vec)
    out = decode_vector(memoryview(blob))
    assert np.allclose(out, np.asarray(vec, dtype=np.float32))


def test_build_matrix_all_blob_matches_all_json_oracle():
    rng = np.random.default_rng(7)
    vecs = [rng.normal(size=8).astype(np.float32) for _ in range(12)]
    json_rows = [(f"id{i}", json.dumps(v.tolist())) for i, v in enumerate(vecs)]
    blob_rows = [(f"id{i}", encode_vector(v)) for i, v in enumerate(vecs)]
    ids_j, mat_j = build_matrix(json_rows)
    ids_b, mat_b = build_matrix(blob_rows)
    assert ids_j == ids_b
    assert mat_j.dtype == mat_b.dtype == np.float32
    assert np.allclose(mat_j, mat_b, atol=1e-6)


def test_build_matrix_mixed_json_and_blob_rows_equivalent_to_all_json():
    rng = np.random.default_rng(11)
    vecs = [rng.normal(size=6).astype(np.float32) for _ in range(10)]
    # Oracle: every row stored as legacy JSON text.
    oracle_rows = [(f"id{i}", json.dumps(v.tolist())) for i, v in enumerate(vecs)]
    # Mixed: even-indexed rows are BLOB (as if backfilled), odd stay JSON.
    mixed_rows = [
        (f"id{i}", encode_vector(v) if i % 2 == 0 else json.dumps(v.tolist()))
        for i, v in enumerate(vecs)
    ]
    ids_o, mat_o = build_matrix(oracle_rows)
    ids_m, mat_m = build_matrix(mixed_rows)
    assert ids_o == ids_m
    assert np.allclose(mat_o, mat_m, atol=1e-6)


def test_build_matrix_mixed_with_n_hint_equivalent():
    rng = np.random.default_rng(13)
    vecs = [rng.normal(size=5).astype(np.float32) for _ in range(9)]
    mixed_rows = [
        (f"id{i}", encode_vector(v) if i % 3 == 0 else json.dumps(v.tolist()))
        for i, v in enumerate(vecs)
    ]
    ids0, mat0 = build_matrix(mixed_rows)
    ids1, mat1 = build_matrix(mixed_rows, n_hint=9)
    assert ids0 == ids1
    assert np.allclose(mat0, mat1, atol=1e-6)


def test_build_matrix_skips_wrong_length_blob_like_wrong_dim_json():
    good = np.array([1.0, 2.0, 3.0, 4.0], dtype=np.float32)
    rows = [
        ("a", encode_vector(good)),
        # 3-byte blob: not a multiple of 4 -> np.frombuffer raises -> skipped,
        # same disposition as an unparseable/invalid JSON row.
        ("bad_len", b"\x01\x02\x03"),
        # Right byte-multiple but wrong dim (2 floats vs established dim 4).
        ("wrong_dim", encode_vector([1.0, 2.0])),
        ("b", encode_vector(good * 2)),
    ]
    ids, mat = build_matrix(rows)
    assert ids == ["a", "b"]
    assert mat.shape == (2, 4)


def test_query_sims_matches_cosine_blob_rows():
    rows = [
        ("a", encode_vector([0.1, 0.2, 0.3, 0.4])),
        ("b", encode_vector([0.4, 0.3, 0.2, 0.1])),
        ("c", encode_vector([-0.1, -0.2, -0.3, -0.4])),
    ]
    raw = {"a": [0.1, 0.2, 0.3, 0.4], "b": [0.4, 0.3, 0.2, 0.1], "c": [-0.1, -0.2, -0.3, -0.4]}
    ids, mat = build_matrix(rows)
    q = [0.1, 0.2, 0.3, 0.4]
    sims = query_sims(q, ids, mat)
    for k in ids:
        assert math.isclose(sims[k], cosine(q, raw[k]), abs_tol=1e-5)


# ── top_k_sims: numpy argpartition top-K, no full {id: float} dict ──────────
# (P0-1/2: at millions-of-relations scale, query_sims' full {id: float} dict is
# itself a GB-scale allocation; top_k_sims stays numpy end-to-end and only
# materializes the K winners.)

def _rand_matrix_rows(n, dim, seed=0):
    rng = np.random.default_rng(seed)
    return [(f"id{i}", encode_vector(rng.normal(size=dim).astype(np.float32))) for i in range(n)]


def test_top_k_sims_matches_query_sims_top_slice():
    rows = _rand_matrix_rows(50, 8, seed=1)
    ids, mat = build_matrix(rows)
    q = [0.3, -0.1, 0.2, 0.5, -0.4, 0.1, 0.05, -0.2]
    k = 7
    full = query_sims(q, ids, mat)
    want = sorted(full.items(), key=lambda kv: kv[1], reverse=True)[:k]
    got = top_k_sims(q, ids, mat, k)
    assert [g[0] for g in got] == [w[0] for w in want]
    for (gid, gs), (wid, ws) in zip(got, want):
        assert gid == wid
        assert math.isclose(gs, ws, abs_tol=1e-5)


def test_top_k_sims_k_exceeds_n_returns_all_sorted():
    rows = _rand_matrix_rows(5, 4, seed=2)
    ids, mat = build_matrix(rows)
    q = [1.0, 0.0, 0.0, 0.0]
    got = top_k_sims(q, ids, mat, k=100)
    assert len(got) == 5
    scores = [s for _, s in got]
    assert scores == sorted(scores, reverse=True)
    assert {i for i, _ in got} == set(ids)


def test_top_k_sims_degenerate_inputs_return_empty():
    rows = _rand_matrix_rows(3, 4, seed=3)
    ids, mat = build_matrix(rows)
    q = [1.0, 0.0, 0.0, 0.0]
    assert top_k_sims(None, ids, mat, 2) == []
    assert top_k_sims(q, [], mat, 2) == []
    assert top_k_sims(q, ids, np.zeros((0, 0), dtype=np.float32), 2) == []
    assert top_k_sims(q, ids, mat, 0) == []
    assert top_k_sims([1.0, 0.0], ids, mat, 2) == []   # dim mismatch


def test_top_k_sims_zero_norm_query_returns_first_k_at_zero():
    rows = _rand_matrix_rows(4, 4, seed=4)
    ids, mat = build_matrix(rows)
    got = top_k_sims([0.0, 0.0, 0.0, 0.0], ids, mat, 2)
    assert got == [(ids[0], 0.0), (ids[1], 0.0)]
