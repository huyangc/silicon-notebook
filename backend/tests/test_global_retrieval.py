"""Global fan-out cannot populate shared full-library caches or overrun SQL."""
from contextlib import nullcontext
from types import SimpleNamespace
import sqlite3
import threading
import time

import numpy as np
import pytest

from app.core.config import Settings
from app.repositories.read_budget import read_budget, ReadBudget, ReadBudgetExceeded
from app.repositories.sqlite.database import _BudgetedReadConnection, _Conn, SqliteDatabase
from app.repositories.postgres.database import _BudgetedReadConnection as PgBudgetConnection
from app.services.global_retrieval import retrieve_global_candidates, GlobalRetrievalSkipped
from app.services.retrieval_run import retrieval_run
from app.services.scale_artifact_catalog import ScaleArtifactCatalog
from app.services.source_scope import source_scope_context
from app.services.vector_cache import LRUProcessCache
from app.repositories.ports import ChunkLexicalSearchTimeout


def _forbidden(*args, **kwargs):
    pytest.fail("global retrieval entered a cold/unbounded producer")


def _rig(warm=False):
    counts = {"fts": 0, "hydrate": 0, "ann": 0}
    cache = LRUProcessCache(max_entries=2)
    def knn(vector, *, k):
        counts["ann"] += 1
        return np.array([[0]]), np.array([[0.]])
    index = SimpleNamespace(
        manifest={"dim": 2}, chunk_ann_handle=SimpleNamespace(knn_query=knn),
        chunk_ann_labels=["warm-chunk"], chunk_ann_source_names=["source"],
    )
    if warm:
        cache["warm"] = index
    catalog = ScaleArtifactCatalog(
        artifacts=SimpleNamespace(load_scale=_forbidden), settings=Settings(),
        version=_forbidden, scale_cache=lambda: cache, load_lock=threading.Lock,
        load_locks=lambda: {}, note_model_error=_forbidden,
    )
    def fts(db, nb, query, *, k, allowed_source_ids):
        counts["fts"] += 1
        assert k == 200
        assert allowed_source_ids == ("source",)
        return [{"chunk_id": nb + "-chunk"}]
    def evidence_rows(db, ids):
        counts["hydrate"] += 1
        assert len(ids) <= 400
        return {cid: dict(id=cid, source_id="source", text="battery power",
                         section_path="", source_title="Battery", element_ids=[cid+"-element"],
                         element_fingerprints={cid+"-element": ("source", "fingerprint")})
                for cid in ids}
    candidates = SimpleNamespace(
        settings=Settings(), scale_runtime=SimpleNamespace(catalog=catalog),
        _embed_query=_forbidden, _connect=lambda: nullcontext(None),
        _chunk_fts_hits=fts, hydrate_chunk_candidates=_forbidden,
        _in_batches=lambda ids: [ids],
        sources=SimpleNamespace(global_candidate_evidence=evidence_rows),
        embeddings=SimpleNamespace(rows_by_ids=lambda db, table, column, ids: [
            {"vid": cid, "vector": "[1.0, 0.0]"} for cid in ids
        ], global_small_chunk_vector_page=lambda *args, **kwargs: (False, [])),
        _mask_vector_matrix=lambda ids, matrix, allowed: (ids, matrix),
        _vector_matrix=_forbidden, _gather_chunks=_forbidden,
    )
    return candidates, counts, cache, index


def test_24_cold_notebooks_never_load_scale_ann_or_full_vector_cache():
    candidates, counts, cache, index = _rig(warm=True)
    with retrieval_run(run_kind="ask_global") as run:
        run.memoized_embedding("battery", lambda: [1., 0.])
        for number in range(24):
            nb = f"cold-{number}"
            with source_scope_context(nb, {"mode": "include", "source_ids": ["source"]}):
                result = retrieve_global_candidates(candidates, nb, "battery")
            assert result.degraded
            assert result.chunks[0].chunk_id == nb + "-chunk"
    assert counts == {"fts": 24, "hydrate": 24, "ann": 0}
    assert cache.get("warm") is index


def test_warm_ann_borrows_handle_without_delta_or_lazy_artifact_loading():
    candidates, counts, cache, index = _rig(warm=True)
    with retrieval_run(run_kind="ask_global") as run:
        run.memoized_embedding("battery", lambda: [1., 0.])
        with source_scope_context("warm", {"mode": "include", "source_ids": ["source"]}):
            result = retrieve_global_candidates(candidates, "warm", "battery")
    assert not result.degraded
    assert counts == {"fts": 1, "hydrate": 1, "ann": 1}
    assert cache.get("warm") is index
    assert result.evidence_fingerprints == {"warm-chunk-element": ("source", "fingerprint")}


def test_restricted_warm_ann_uses_source_first_lexical_candidates():
    candidates, counts, _, index = _rig(warm=True)
    index.chunk_ann_source_names.append("private-source")
    with retrieval_run(run_kind="ask_global") as run:
        run.memoized_embedding("battery", lambda: [1., 0.])
        with source_scope_context("warm", {"mode": "include", "source_ids": ["source"]}):
            result = retrieve_global_candidates(candidates, "warm", "battery")
    assert result.degraded
    assert counts["ann"] == 0


def test_warm_ann_survives_optional_fts_timeout_within_notebook_budget():
    candidates, _, _, _ = _rig(warm=True)
    def fts(*args, **kwargs):
        raise ChunkLexicalSearchTimeout()
    candidates._chunk_fts_hits = fts
    with retrieval_run(run_kind="ask_global") as run:
        run.memoized_embedding("battery", lambda: [1., 0.])
        with source_scope_context("warm", {"mode": "include", "source_ids": ["source"]}):
            result = retrieve_global_candidates(candidates, "warm", "battery")
    assert [chunk.chunk_id for chunk in result.chunks] == ["warm-chunk"]


@pytest.mark.parametrize("defect", ["missing", "wrong_source", "no_elements"])
def test_unbound_chunks_cannot_reach_synthesis(defect):
    candidates, _, _, _ = _rig()
    original = candidates.sources.global_candidate_evidence
    def evidence(db, ids):
        rows = original(db, ids)
        for row in rows.values():
            if defect == "missing":
                row["element_fingerprints"] = {}
            elif defect == "wrong_source":
                row["element_fingerprints"] = {row["element_ids"][0]: ("foreign", "hash")}
            else:
                row["element_ids"] = []
        return rows
    candidates.sources.global_candidate_evidence = evidence
    with source_scope_context("cold", {"mode": "include", "source_ids": ["source"]}):
        result = retrieve_global_candidates(candidates, "cold", "battery")
    assert result.chunks == []
    assert result.evidence_fingerprints == {}


def test_sqlite_budget_interrupts_one_expensive_statement_and_restores_connection():
    connection = sqlite3.connect(":memory:", factory=_Conn)
    connection.execute("PRAGMA busy_timeout = 7000")
    budget = ReadBudget(time.monotonic() + .02)
    with pytest.raises(sqlite3.OperationalError, match="interrupted"):
        with _BudgetedReadConnection(connection, budget) as db:
            db.execute("WITH RECURSIVE numbers(n) AS (VALUES(1) UNION ALL "
                       "SELECT n+1 FROM numbers WHERE n<100000000) SELECT sum(n) FROM numbers").fetchone()
    assert connection.execute("SELECT 1").fetchone()[0] == 1
    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 7000
    connection.close()


def test_nested_sqlite_read_restores_outer_deadline_and_busy_timeout(tmp_path):
    database = SqliteDatabase(Settings(sqlite_path="nested-budget.db"), tmp_path)
    connection = database.connect()
    connection.execute("PRAGMA busy_timeout = 7000")
    try:
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            with read_budget(time.monotonic() + .1):
                with database.connect() as outer:
                    outer_busy_timeout = connection.execute("PRAGMA busy_timeout").fetchone()[0]
                    with read_budget(time.monotonic() + .02):
                        with database.connect() as inner:
                            assert inner.connection is outer.connection
                            assert inner.execute("SELECT 1").fetchone()[0] == 1
                            assert connection.execute("PRAGMA busy_timeout").fetchone()[0] <= 20
                    assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == outer_busy_timeout
                    # The inner exit must not remove the outer VM interrupt.
                    # A check only before execute cannot stop this statement.
                    outer.execute("WITH RECURSIVE numbers(n) AS (VALUES(1) UNION ALL "
                                  "SELECT n+1 FROM numbers WHERE n<100000000) SELECT sum(n) FROM numbers").fetchone()
        assert connection.execute("SELECT 1").fetchone()[0] == 1
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 7000
    finally:
        database.close_local()


def test_postgres_each_statement_gets_remaining_budget_and_cleanup_is_not_blocked():
    calls = []
    connection = SimpleNamespace(execute=lambda *args, **kwargs: calls.append(args))
    budget = ReadBudget(time.monotonic() + 5)
    proxy = PgBudgetConnection(connection, budget)
    proxy.execute("SELECT 1")
    proxy.execute("SELECT 2")
    assert [call[0] for call in calls] == [
        "SELECT set_config('statement_timeout', %s, true)", "SELECT 1",
        "SELECT set_config('statement_timeout', %s, true)", "SELECT 2",
    ]
    assert 0 < int(calls[2][1][0][:-2]) <= int(calls[0][1][0][:-2]) <= 5000
    expired = PgBudgetConnection(connection, ReadBudget(time.monotonic() - 1))
    with pytest.raises(ReadBudgetExceeded):
        expired.execute("SELECT 3")
    expired.execute("ROLLBACK TO SAVEPOINT chunk_fts_budget")
    assert calls[-1][0].startswith("ROLLBACK")


def test_expired_notebook_never_starts_another_query():
    candidates, counts, _, _ = _rig()
    with pytest.raises(GlobalRetrievalSkipped, match="timeout"):
        retrieve_global_candidates(candidates, "cold", "battery", deadline=time.monotonic()-1)
    assert counts == {"fts": 0, "hydrate": 0, "ann": 0}
