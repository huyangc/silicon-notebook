"""Global fan-out cannot populate shared full-library caches or overrun SQL."""
from contextlib import nullcontext
from types import SimpleNamespace
import json
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
    counts = {"fts": 0, "hydrate": 0, "ann": 0, "probe": 0}
    ef = []
    cache = LRUProcessCache(max_entries=2)
    def knn(vector, *, k):
        counts["ann"] += 1
        assert ef, "ANN recall depends on ef; hnswlib defaults to 10"
        return np.array([[0]]), np.array([[0.]])
    index = SimpleNamespace(
        manifest={"dim": 2},
        chunk_ann_handle=SimpleNamespace(knn_query=knn, set_ef=ef.append),
        chunk_ann_labels=["warm-chunk"], chunk_ann_source_names=["source"],
    )
    # Every ``set_ef`` this rig observes, so tests can pin the search width the
    # global lane asks for without reaching into hnswlib.
    index.recorded_ef = ef
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
        return {cid: dict(id=cid, source_id=chunk_sources.get(cid, "source"),
                         # Distinct per chunk: ``rank_source_chunks`` collapses
                         # normalized duplicates within one source, which would
                         # silently reduce every multi-hit assertion to one row.
                         text=f"battery power {cid}",
                         section_path="", source_title="Battery", element_ids=[cid+"-element"],
                         element_fingerprints={cid+"-element": (
                             chunk_sources.get(cid, "source"), "fingerprint")})
                for cid in ids}
    # chunk_id -> source_id for the ceiling probe; anything unlisted belongs to
    # the one admitted source. Tests override entries to place chunks outside
    # the frozen ceiling (Memory/Knowhow projections in production).
    chunk_sources = {}
    def chunk_source_ids(db, ids):
        counts["probe"] += 1
        return {cid: chunk_sources.get(cid, "source") for cid in ids}
    candidates = SimpleNamespace(
        settings=Settings(), scale_runtime=SimpleNamespace(catalog=catalog),
        _embed_query=_forbidden, _connect=lambda: nullcontext(None),
        _chunk_fts_hits=fts, hydrate_chunk_candidates=_forbidden,
        _in_batches=lambda ids: [ids],
        sources=SimpleNamespace(global_candidate_evidence=evidence_rows,
                                global_chunk_source_ids=chunk_source_ids),
        embeddings=SimpleNamespace(rows_by_ids=lambda db, table, column, ids: [
            {"vid": cid, "vector": "[1.0, 0.0]"} for cid in ids
        ], global_small_chunk_vector_page=lambda *args, **kwargs: (False, [])),
        _mask_vector_matrix=lambda ids, matrix, allowed: (ids, matrix),
        _vector_matrix=_forbidden, _gather_chunks=_forbidden,
    )
    candidates.chunk_sources = chunk_sources
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
    assert counts == {"fts": 24, "hydrate": 24, "ann": 0, "probe": 0}
    assert cache.get("warm") is index


def test_warm_ann_borrows_handle_without_delta_or_lazy_artifact_loading():
    candidates, counts, cache, index = _rig(warm=True)
    with retrieval_run(run_kind="ask_global") as run:
        run.memoized_embedding("battery", lambda: [1., 0.])
        with source_scope_context("warm", {"mode": "include", "source_ids": ["source"]}):
            result = retrieve_global_candidates(candidates, "warm", "battery")
    assert not result.degraded
    assert counts == {"fts": 1, "hydrate": 1, "ann": 1, "probe": 0}
    assert cache.get("warm") is index
    # k is capped by the label count (1 here), so ef falls back to the floor the
    # single-notebook chunk lane uses.
    assert index.recorded_ef == [64]
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


def _legacy_ann(candidates, index, labels, *, projections=()):
    """A pre-sidecar index over ``labels``; ``projections`` sit outside the ceiling.

    Production shape: ``scale_index_builder`` pages ALL of ``chunk_embeddings``
    into the chunk ANN, so Memory/Knowhow projection chunks are indexed even
    though ``visible_source_ids_by_notebook`` excludes their source type from
    every global ceiling.
    """
    index.chunk_ann_source_names = None
    index.chunk_ann_labels = list(labels)
    candidates.chunk_sources.update({label: "projection" for label in projections})
    candidates._chunk_fts_hits = lambda *args, **kwargs: []
    widths = []

    def knn(vector, *, k):
        widths.append(k)
        taken = min(k, len(labels))
        return (np.array([list(range(taken))]),
                np.array([[0.] * taken]))

    index.chunk_ann_handle.knn_query = knn
    return widths


def _ask(candidates, notebook="warm"):
    with retrieval_run(run_kind="ask_global") as run:
        run.memoized_embedding("battery", lambda: [1., 0.])
        with source_scope_context(notebook, {"mode": "include", "source_ids": ["source"]}):
            return retrieve_global_candidates(candidates, notebook, "battery")


def test_legacy_index_without_source_sidecar_uses_ann_and_verifies_once():
    """A pre-sidecar index plus the always-present frozen ceiling took the
    paged brute-force lane in production and timed out. It may use native ANN,
    but only after ONE ceiling probe says the neighbourhood survives; when
    nothing was dropped there is no second query."""
    candidates, counts, _, index = _rig(warm=True)
    widths = _legacy_ann(candidates, index, [f"chunk-{n}" for n in range(500)])
    result = _ask(candidates)
    assert not result.degraded
    assert widths == [200] and counts["probe"] == 1
    assert index.recorded_ef == [201]
    assert len(result.chunks) == 200


def test_ceiling_excluded_projections_trigger_bounded_over_fetch():
    """The whole Top-K belongs to excluded projection chunks. Re-query wider
    instead of publishing an empty semantic lane as a healthy one."""
    candidates, counts, _, index = _rig(warm=True)
    labels = [f"projection-{n}" for n in range(200)] + [f"chunk-{n}" for n in range(600)]
    widths = _legacy_ann(candidates, index, labels,
                         projections=[f"projection-{n}" for n in range(200)])
    result = _ask(candidates)
    # First round: 200 hits, all dropped. Second round: 800 hits, 600 survive.
    assert widths == [200, 800]
    assert counts["probe"] == 2 and not result.degraded
    assert len(result.chunks) == 200
    assert all(chunk.chunk_id.startswith("chunk-") for chunk in result.chunks)


def test_exhausted_over_fetch_falls_back_to_the_exact_sql_lane():
    """Still starved after the bounded rounds: the paged lane applies the
    ceiling inside SQL, so it cannot be starved the same way."""
    candidates, counts, _, index = _rig(warm=True)
    labels = [f"projection-{n}" for n in range(9000)]
    widths = _legacy_ann(candidates, index, labels, projections=labels)
    pages = []

    def page(db, nb, *, allowed_source_ids, max_chunks, after, page_size, size_gate=True):
        pages.append(after)
        if after:
            return True, []
        return True, [{"vid": "exact-chunk", "vector": "[1.0, 0.0]"}]

    candidates.embeddings.global_small_chunk_vector_page = page
    result = _ask(candidates)
    assert len(widths) == 3  # initial + two bounded over-fetch rounds
    assert pages and [chunk.chunk_id for chunk in result.chunks] == ["exact-chunk"]
    assert not result.degraded


def test_starved_library_without_an_exact_lane_is_disclosed_as_degraded():
    """Over the paged lane's own size rail: keep what survived, but say the
    neighbourhood is partial and leave a content-free receipt."""
    candidates, counts, _, index = _rig(warm=True)
    # ``chunk-0`` sits at label 3100, inside the widest over-fetch (3200) but
    # far past the first two rounds, and 8101 labels keep the index unexhausted.
    projections = [f"projection-{n}" for n in range(8100)]
    labels = projections[:3100] + ["chunk-0"] + projections[3100:]
    widths = _legacy_ann(candidates, index, labels, projections=projections)
    events = []
    candidates.event_log = SimpleNamespace(emit=events.append)
    result = _ask(candidates)
    assert len(widths) == 3
    assert result.degraded
    assert [chunk.chunk_id for chunk in result.chunks] == ["chunk-0"]
    assert [event["kind"] for event in events] == ["global_retrieval_ann_starved"]
    assert events[0]["notebook_id"] == "warm" and events[0]["survivors"] == 1
    assert events[0]["dropped"] == 3199 and events[0]["k"] == 3200
    assert "battery" not in json.dumps(events[0])


def test_reading_the_whole_index_is_not_starvation():
    """Fewer admitted chunks than ``chunk_recall`` is a small library, not a
    starved one: every label was examined, so the survivors are exhaustive."""
    candidates, counts, _, index = _rig(warm=True)
    labels = ["projection-0", "chunk-0", "chunk-1"]
    widths = _legacy_ann(candidates, index, labels, projections=["projection-0"])
    result = _ask(candidates)
    assert len(widths) == 1 and not result.degraded
    assert sorted(chunk.chunk_id for chunk in result.chunks) == ["chunk-0", "chunk-1"]


def test_legacy_index_ann_hits_are_still_bound_by_the_frozen_ceiling():
    """The relaxed lane choice must not relax authority: a hydrated row whose
    source left the frozen ceiling is dropped before scoring."""
    candidates, counts, _, index = _rig(warm=True)
    index.chunk_ann_source_names = None
    original = candidates.sources.global_candidate_evidence
    def evidence(db, ids):
        rows = original(db, ids)
        for row in rows.values():
            row["source_id"] = "revoked"
            row["element_fingerprints"] = {
                row["element_ids"][0]: ("revoked", "fingerprint")
            }
        return rows
    candidates.sources.global_candidate_evidence = evidence
    with retrieval_run(run_kind="ask_global") as run:
        run.memoized_embedding("battery", lambda: [1., 0.])
        with source_scope_context("warm", {"mode": "include", "source_ids": ["source"]}):
            result = retrieve_global_candidates(candidates, "warm", "battery")
    assert counts["ann"] == 1
    assert result.chunks == [] and result.evidence_fingerprints == {}


def test_server_cancelled_statement_is_a_timeout_not_an_index_defect():
    from psycopg.errors import QueryCanceled

    candidates, _, _, _ = _rig()
    events = []
    candidates.event_log = SimpleNamespace(emit=events.append)
    candidates._chunk_fts_hits = lambda *args, **kwargs: (_ for _ in ()).throw(
        QueryCanceled("canceling statement due to statement timeout")
    )
    with source_scope_context("cold", {"mode": "include", "source_ids": ["source"]}):
        with pytest.raises(GlobalRetrievalSkipped, match="timeout"):
            # A generous deadline: only the driver exception may classify this.
            retrieve_global_candidates(candidates, "cold", "battery",
                                       deadline=time.monotonic() + 600)
    assert [event["reason"] for event in events] == ["timeout"]
    assert events[0]["error_type"] == "QueryCanceled"


def test_exhausted_connection_pool_is_saturation_not_a_scope_problem():
    """A lease the pool could not grant means the query never ran. Telling the
    user to select fewer notebooks would blame them for someone else's load."""
    from psycopg_pool import PoolTimeout

    candidates, _, _, _ = _rig()
    events = []
    candidates.event_log = SimpleNamespace(emit=events.append)
    candidates._chunk_fts_hits = lambda *args, **kwargs: (_ for _ in ()).throw(
        PoolTimeout("pool acquisition timed out")
    )
    with source_scope_context("cold", {"mode": "include", "source_ids": ["source"]}):
        with pytest.raises(GlobalRetrievalSkipped, match="saturated"):
            retrieve_global_candidates(candidates, "cold", "battery",
                                       deadline=time.monotonic() + 600)
    assert [event["reason"] for event in events] == ["saturated"]
    assert events[0]["error_type"] == "PoolTimeout"


def test_bounded_lexical_timeout_is_reported_like_every_other_skip():
    """The most common production failure -- PostgreSQL cancelling the chunk
    FTS probe with no semantic candidates to fall back on -- used to raise
    straight past the emitter and leave operators with no receipt at all."""
    candidates, _, _, _ = _rig()
    events = []
    candidates.event_log = SimpleNamespace(emit=events.append)
    candidates._chunk_fts_hits = lambda *args, **kwargs: (_ for _ in ()).throw(
        ChunkLexicalSearchTimeout("chunk lexical search exceeded its bounded deadline")
    )
    with source_scope_context("cold", {"mode": "include", "source_ids": ["source"]}):
        with pytest.raises(GlobalRetrievalSkipped, match="timeout"):
            retrieve_global_candidates(candidates, "cold", "battery",
                                       deadline=time.monotonic() + 600)
    assert len(events) == 1
    assert events[0]["kind"] == "global_retrieval_skipped"
    assert events[0]["reason"] == "timeout"
    assert events[0]["error_type"] == "ChunkLexicalSearchTimeout"
    assert "bounded deadline" not in json.dumps(events[0])


def test_unexpected_failure_emits_a_content_free_receipt():
    candidates, _, _, _ = _rig()
    events = []
    candidates.event_log = SimpleNamespace(emit=events.append)
    candidates._chunk_fts_hits = lambda *args, **kwargs: (_ for _ in ()).throw(
        RuntimeError("relation chunks does not exist for battery question")
    )
    with source_scope_context("cold", {"mode": "include", "source_ids": ["source"]}):
        with pytest.raises(GlobalRetrievalSkipped, match="unavailable"):
            retrieve_global_candidates(candidates, "cold", "battery",
                                       deadline=time.monotonic() + 600)
    assert len(events) == 1
    event = events[0]
    assert event["kind"] == "global_retrieval_skipped"
    assert event["notebook_id"] == "cold" and event["reason"] == "unavailable"
    assert event["error_type"] == "RuntimeError" and event["latency_ms"] >= 0
    assert "battery" not in json.dumps(event) and "chunks does not exist" not in json.dumps(event)


def test_event_sink_failure_cannot_change_the_skip_reason():
    candidates, _, _, _ = _rig()
    def explode(event):
        raise RuntimeError("event sink down")
    candidates.event_log = SimpleNamespace(emit=explode)
    candidates._chunk_fts_hits = lambda *args, **kwargs: (_ for _ in ()).throw(ValueError("boom"))
    with source_scope_context("cold", {"mode": "include", "source_ids": ["source"]}):
        with pytest.raises(GlobalRetrievalSkipped, match="unavailable"):
            retrieve_global_candidates(candidates, "cold", "battery",
                                       deadline=time.monotonic() + 600)


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
    assert counts == {"fts": 0, "hydrate": 0, "ann": 0, "probe": 0}
