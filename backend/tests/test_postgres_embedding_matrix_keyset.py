"""Unit tests for the PostgreSQL `embedding_matrix` whole-notebook keyset
pagination (index_projection_store.embedding_matrix's object_ids=None path,
via embedding_store.EmbeddingStore.vector_pages).

No live PostgreSQL connection is used or required — these are G1-tier unit
tests, so this file lives at the main test root, NOT under
``backend/tests/postgres/`` (that directory is ignored by the G1/G2 lanes and
its G3 lane selects by the ``postgres_integration`` marker; a markerless fake-db
file there runs in zero gates — same placement rule as
``test_postgres_knowledge_counts_cache.py``). A fake connection records the
rendered SQL text + params of every
`execute()` call and serves pages against an in-memory, id-sorted dataset, so
the pagination SHAPE (not just the eventual (ids, matrix) values) is under
test: an unbounded single-SELECT regression or a non-advancing keyset would
produce byte-identical *results* on small test data while violating the
per-page-bounded-memory contract that is the entire point of this change —
see `docs/superpowers/specs` design note "PG embedding_matrix 全库路径 keyset
分页" and `app/repositories/postgres/embedding_store.py::vector_pages`'s
docstring for the full argument.
"""
from __future__ import annotations

import types

import numpy as np

from app.repositories.postgres.embedding_store import (
    MATRIX_FETCH_BATCH,
    EmbeddingStore,
)
from app.repositories.postgres.index_projection_store import IndexProjectionStore


def _vec_bytes(n: float) -> bytes:
    return np.array([n, n + 1.0], dtype=np.float32).tobytes()


def _make_dataset(count: int) -> list[tuple[str, bytes]]:
    """`count` (vid, vector_bytes) rows, already sorted by vid ascending —
    matches the `ORDER BY {id}` the real query issues."""
    width = max(3, len(str(max(count - 1, 0))))
    return [(f"obj-{i:0{width}d}", _vec_bytes(float(i))) for i in range(count)]


class _FakeCursor:
    def __init__(self, rows: list[dict]):
        self._rows = rows

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _FakeConnection:
    """Serves `vector_pages`' keyset pages (and `version_row`'s COUNT probe)
    against an in-memory, vid-sorted dataset, and records every execute()
    call's rendered SQL text + params — no real database involved.

    A page-count safety cap converts a non-advancing (buggy) keyset scan
    into a fast, clean test failure instead of the real hazard a broken
    `last_vid` update would cause against a live database: an actual
    infinite loop. The cap lives here, in the test double, NOT in the
    production code under test (`vector_pages` itself has no artificial page
    limit — see its docstring)."""

    def __init__(self, dataset: list[tuple[str, bytes]], *, max_calls: int):
        self._dataset = list(dataset)
        self.calls: list[tuple[str, tuple]] = []
        self._max_calls = max_calls

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params):
        sql_text = (
            statement.as_string(None) if hasattr(statement, "as_string") else str(statement)
        )
        params = tuple(params)
        self.calls.append((sql_text, params))
        if len(self.calls) > self._max_calls:
            raise AssertionError(
                f"too many execute() calls ({len(self.calls)} > {self._max_calls}) — "
                "keyset pagination looks non-advancing (last_vid regression), which "
                "would infinite-loop against a real database"
            )
        if "COUNT(*)" in sql_text:
            return _FakeCursor([{"c": len(self._dataset), "ts": None}])
        if len(params) == 2:
            _notebook_id, limit = params
            rows = self._dataset[:limit]
        elif len(params) == 3:
            _notebook_id, last_vid, limit = params
            rows = [row for row in self._dataset if row[0] > last_vid][:limit]
        else:
            raise AssertionError(f"unexpected execute() param shape: {params!r}")
        return _FakeCursor([{"vid": vid, "vector": vector} for vid, vector in rows])


# ─────────────────────────────────────────── EmbeddingStore.vector_pages ──

def test_vector_pages_shape_and_dedup_across_three_pages():
    """25 rows, batch=10 -> 3 pages (10, 10, 5). Every statement carries a
    LIMIT (MUT-1/MUT-3 guard); only page 2+ carries the `> %s` keyset
    predicate (MUT-2 guard, together with the id-set dedup assertion below)."""
    dataset = _make_dataset(25)
    conn = _FakeConnection(dataset, max_calls=6)

    out = list(
        EmbeddingStore.vector_pages(
            conn, "nb-1", "knowledge_embeddings", "object_id", batch=10
        )
    )

    assert len(conn.calls) == 3
    for i, (sql_text, params) in enumerate(conn.calls):
        assert "LIMIT" in sql_text, f"call {i} missing LIMIT: {sql_text}"
        if i == 0:
            assert "> %s" not in sql_text
            assert len(params) == 2
        else:
            assert "> %s" in sql_text
            assert len(params) == 3

    expected = dataset
    assert [vid for vid, _ in out] == [vid for vid, _ in expected]
    assert [vec for _, vec in out] == [vec for _, vec in expected]
    got_ids = [vid for vid, _ in out]
    assert len(set(got_ids)) == len(got_ids)  # no id revisited across pages


def test_vector_pages_exact_page_boundary_issues_trailing_empty_page():
    """20 rows, batch=10: page1 and page2 each land exactly on `batch`, so
    `len(page) < batch` cannot fire until a trailing, empty page confirms
    nothing is left — pins this implementation's choice of 3 calls (not 2)
    for the exact-boundary case, per spec T3#2."""
    dataset = _make_dataset(20)
    conn = _FakeConnection(dataset, max_calls=6)

    out = list(
        EmbeddingStore.vector_pages(
            conn, "nb-1", "knowledge_embeddings", "object_id", batch=10
        )
    )

    assert len(conn.calls) == 3
    assert conn.calls[0][1] == ("nb-1", 10)
    assert "> %s" in conn.calls[-1][0]  # trailing page is still a keyset call
    assert [vid for vid, _ in out] == [vid for vid, _ in dataset]


def test_vector_pages_empty_notebook_issues_one_page():
    conn = _FakeConnection([], max_calls=3)

    out = list(
        EmbeddingStore.vector_pages(
            conn, "nb-empty", "knowledge_embeddings", "object_id", batch=10
        )
    )

    assert out == []
    assert len(conn.calls) == 1
    assert "> %s" not in conn.calls[0][0]


def test_matrix_fetch_batch_constant_matches_sqlite_side():
    """Parity pin: the two `MATRIX_FETCH_BATCH` constants are independently
    defined (adapters do not cross-import; a TEST may import both sides) and
    this assertion is what keeps them numerically equal — pinning only one
    side's literal would let the other drift silently."""
    from app.repositories.sqlite.index_projection_store import (
        MATRIX_FETCH_BATCH as sqlite_batch,
    )
    assert MATRIX_FETCH_BATCH == sqlite_batch == 10_000


# ───────────────────────────────── IndexProjectionStore.embedding_matrix ──

def _store(connect, in_batches=None) -> IndexProjectionStore:
    return IndexProjectionStore(
        types.SimpleNamespace(embed_runtime_dim=0),  # no MRL truncation in these tests
        connect=connect,
        in_batches=in_batches or (lambda ids: [list(ids)]),
        ent_chunk_map=None,
        mention_extra_edges=None,
        vector_matrix=None,
    )


def test_embedding_matrix_object_ids_none_paginates_and_uses_n_hint():
    """Whole-notebook load (object_ids=None) must go through
    `EmbeddingStore.vector_pages` at its production default batch size (this
    call site passes no explicit `batch`, so `embedding_matrix` must be
    wired to `MATRIX_FETCH_BATCH`) plus the pre-existing COUNT(*) n_hint —
    25 rows all fit in one page at that batch size: 1 COUNT(*) + 1 page."""
    dataset = _make_dataset(25)
    conn = _FakeConnection(dataset, max_calls=6)
    store = _store(connect=lambda: conn)

    ids, matrix = store.embedding_matrix("nb-1", "knowledge_embeddings", "object_id")

    assert len(conn.calls) == 2  # 1 COUNT(*) n_hint + 1 keyset page (25 < default batch)
    page_call = conn.calls[1]
    assert page_call[1] == ("nb-1", MATRIX_FETCH_BATCH)
    assert ids == [vid for vid, _ in dataset]
    assert matrix.shape[0] == 25


class _FakeIdLookupConnection:
    """Serves the fold path's pre-existing (untouched by this change)
    `WHERE notebook_id=%s AND {id}=ANY(%s)` query shape
    (EmbeddingStore.vector_rows_for_ids) — used only to prove
    `embedding_matrix`'s object_ids=list branch is unaffected by the
    object_ids=None branch's pagination rewrite."""

    def __init__(self, by_id: dict):
        self._by_id = by_id
        self.calls: list[tuple] = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, statement, params):
        self.calls.append(tuple(params))
        _notebook_id, ids = params
        rows = [{"vid": vid, "vector": self._by_id[vid]} for vid in ids if vid in self._by_id]
        return _FakeCursor(rows)


def test_embedding_matrix_fold_path_object_ids_list_unaffected():
    """object_ids=[...] must still use the bounded ANY() lookup (never the
    keyset pager) — MUT-4 guard: this task's rewrite of the object_ids=None
    branch must not touch this branch at all."""
    by_id = dict(_make_dataset(5))
    conn = _FakeIdLookupConnection(by_id)
    store = _store(connect=lambda: conn)

    ids, matrix = store.embedding_matrix(
        "nb-1", "knowledge_embeddings", "object_id",
        object_ids=["obj-002", "obj-000"],
    )

    assert len(conn.calls) == 1  # one bounded ANY() lookup, no pagination
    assert ids == ["obj-002", "obj-000"]
    assert matrix.shape[0] == 2


def _unreachable_connect():
    raise AssertionError("must not connect — empty object_ids must short-circuit")


def test_embedding_matrix_fold_path_empty_object_ids_short_circuits():
    """object_ids=[] returns ([], []) without issuing any query at all —
    frozen short-circuit, untouched by this change."""
    store = _store(connect=_unreachable_connect)

    ids, matrix = store.embedding_matrix(
        "nb-1", "knowledge_embeddings", "object_id", object_ids=[]
    )

    assert ids == []
    assert matrix == []
