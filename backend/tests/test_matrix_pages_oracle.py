"""Oracle: ``matrix_pages`` concatenated ≡ ``build_matrix`` (batch-3 W4 T-W4-3.3).

``matrix_pages`` exists so the offline build can feed hnswlib one page at a
time instead of allocating one whole-notebook matrix. That is only safe if the
paged stream is the SAME stream: ``build_matrix``'s five semantics
(runtime-dim truncation before the dim decision, first-valid-row fixes the
dim, wrong-dim rows skipped, per-row L2 normalization, ids row-aligned) are
carried ACROSS pages, not re-derived per page.

The interesting failure this pins is the one a naive "call build_matrix per
page" implementation has: a page whose FIRST row is a wrong-dim outlier would
redefine the width for that page and silently admit rows the whole-matrix load
would have dropped. ``_wrong_dim_at_page_head`` puts exactly that row at a page
boundary for every page size it can reach.
"""
from __future__ import annotations

import json

import numpy as np
import pytest

from app.domain.vector_index import build_matrix, encode_vector, matrix_pages


def _rows(vectors):
    return [(f"id{i}", encode_vector(v)) for i, v in enumerate(vectors)]


def _concatenated(rows, page_rows, runtime_dim=0):
    ids: list[str] = []
    blocks: list[np.ndarray] = []
    widths: set[int] = set()
    for page_ids, page_matrix in matrix_pages(
        iter(rows), page_rows, runtime_dim=runtime_dim
    ):
        assert len(page_ids) == page_matrix.shape[0], "ids must stay row-aligned"
        assert len(page_ids) <= page_rows, "a page must never exceed its cap"
        widths.add(int(page_matrix.shape[1]))
        ids.extend(page_ids)
        blocks.append(page_matrix)
    assert len(widths) <= 1, f"every page must share one width, got {widths}"
    if not ids:
        return [], np.zeros((0, 0), dtype=np.float32)
    return ids, np.vstack(blocks)


@pytest.mark.parametrize("page_rows", [1, 2, 3, 7, 64])
def test_paged_stream_is_element_identical_to_the_whole_matrix(page_rows):
    rng = np.random.default_rng(20260904)
    rows = _rows(rng.normal(size=(23, 8)).astype(np.float32))

    want_ids, want_matrix = build_matrix(iter(rows), runtime_dim=0)
    got_ids, got_matrix = _concatenated(rows, page_rows)

    assert got_ids == want_ids
    assert got_matrix.dtype == want_matrix.dtype
    np.testing.assert_array_equal(got_matrix, want_matrix)


@pytest.mark.parametrize("page_rows", [1, 2, 3, 5, 8])
def test_a_wrong_dim_row_at_a_page_head_is_still_skipped(page_rows):
    """The dim is fixed by the first valid row of the WHOLE stream. A per-page
    ``build_matrix`` would let each page's own first row redefine it."""
    good = np.ones((9, 4), dtype=np.float32)
    rows = _rows(good)
    # A 6-wide row is inserted at several positions. MEASURED coverage, not
    # assumed: the per-page-``build_matrix`` mutation only dies at page_rows 1
    # and 2 — at 3, 5 and 8 the wrong-dim rows never land on a page head that
    # would let a per-page call redefine the width. Those larger sizes are
    # regression coverage for the page-boundary arithmetic, not anchors for
    # this particular mutation.
    for position in (1, 2, 3, 5, 8):
        rows.insert(position, (f"odd{position}", encode_vector(np.ones(6, dtype=np.float32))))

    want_ids, want_matrix = build_matrix(iter(rows), runtime_dim=0)
    got_ids, got_matrix = _concatenated(rows, page_rows)

    assert not any(i.startswith("odd") for i in want_ids)
    assert got_ids == want_ids
    np.testing.assert_array_equal(got_matrix, want_matrix)


@pytest.mark.parametrize("page_rows", [1, 4, 100])
def test_runtime_dim_truncation_matches_the_whole_matrix_path(page_rows):
    rng = np.random.default_rng(7)
    rows = _rows(rng.normal(size=(11, 16)).astype(np.float32))

    want_ids, want_matrix = build_matrix(iter(rows), runtime_dim=4)
    got_ids, got_matrix = _concatenated(rows, page_rows, runtime_dim=4)

    assert want_matrix.shape[1] == 4
    assert got_ids == want_ids
    np.testing.assert_array_equal(got_matrix, want_matrix)


@pytest.mark.parametrize("page_rows", [1, 2, 5])
def test_unusable_rows_never_occupy_a_page_slot(page_rows):
    """Empty / unparseable / None rows are skipped by both paths, and a skipped
    row must not consume a page slot — otherwise pages would silently shrink."""
    rows = [
        ("empty", b""),
        ("good0", encode_vector(np.ones(3, dtype=np.float32))),
        ("none", None),
        ("bad-json", "not json at all"),
        ("good1", encode_vector(np.full(3, 2.0, dtype=np.float32))),
        ("legacy", json.dumps([1.0, 0.0, 0.0])),
        ("good2", encode_vector(np.full(3, 3.0, dtype=np.float32))),
    ]

    want_ids, want_matrix = build_matrix(iter(rows), runtime_dim=0)
    got_ids, got_matrix = _concatenated(rows, page_rows)

    assert want_ids == ["good0", "good1", "legacy", "good2"]
    assert got_ids == want_ids
    np.testing.assert_array_equal(got_matrix, want_matrix)

    pages = list(matrix_pages(iter(rows), page_rows, runtime_dim=0))
    full_pages = pages[:-1]
    assert all(len(page_ids) == page_rows for page_ids, _ in full_pages)


def test_a_stream_with_no_usable_row_yields_no_page_at_all():
    rows = [("empty", b""), ("none", None)]
    assert list(matrix_pages(iter(rows), 4, runtime_dim=0)) == []
    assert build_matrix(iter(rows), runtime_dim=0)[0] == []
