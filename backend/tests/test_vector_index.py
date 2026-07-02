import json
import math

import numpy as np

from app.services.vector_index import build_matrix, query_sims
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
