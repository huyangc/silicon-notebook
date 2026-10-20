"""The rebuild's paged cluster-map write on SQLite (twin:
``tests/postgres/test_cluster_map_write_paging_pg.py``; shared cases and
expectations: ``tests/cluster_map_write_cases.py``), plus the SQLite plan pins
for the two statements a page runs.

The plan pins are why the SQLite store needs neither PostgreSQL's ``CROSS JOIN
LATERAL ... OFFSET 0`` nor its migration 0079 (see the store docstring): in
every statistics state SQLite can be in, the canonical side is a three-key
seek and the scratch side is one ordered index range over
``(notebook_id, run_id, rowid)``. Run them on SQLite 3.45 (CI) as well as the
local version: ``DYLD_LIBRARY_PATH=<libsqlite 3.45 lib> pytest ...``.
"""
from __future__ import annotations

import sqlite3

import pytest

from app.core.config import Settings
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests import cluster_map_write_cases as cases
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    repository = SQLiteRepository(Settings())
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    try:
        yield repository
    finally:
        repository.close_local()


def test_paged_write_matches_one_shot_row_for_row(repo):
    cases.check_paged_write_matches_one_shot(repo, postgres=False)


def test_pass_b_reads_vectors_in_scratch_insertion_order(repo):
    cases.check_pass_b_reads_vectors_in_scratch_insertion_order(repo, postgres=False)


def test_rebuild_pages_and_a_lost_claim_stops_between_pages(repo, monkeypatch):
    cases.check_rebuild_pages_and_a_lost_claim_stops_between_pages(
        repo, postgres=False, monkeypatch=monkeypatch)


def _fill_run(rt, run: str, rows: int) -> None:
    with rt.database.write() as db:
        db.executemany(
            "INSERT INTO kg_cluster_scratch (notebook_id, run_id, object_id, seed) "
            "VALUES ('nb-plan', ?, ?, ?)",
            [(run, f"ko-{i:06d}", f"seed {i % (rows * 9 // 10)}") for i in range(rows)])
        db.executemany(
            "INSERT INTO kg_canonical_scratch (notebook_id, run_id, seed, "
            "canonical_id, canonical_name) VALUES ('nb-plan', ?, ?, ?, ?)",
            [(run, f"seed {g}", f"KL-{g}", f"name {g}")
             for g in range(rows * 9 // 10)])


@pytest.mark.parametrize("statistics", ["none", "stale", "taken_empty", "fresh"])
def test_page_statements_seek_by_key_in_every_statistics_state(
    repo, monkeypatch, statistics,
):
    """Both page statements, captured from the real store method:

    * the bound read is one covering seek into
      ``idx_kg_cluster_scratch_nb_run (notebook_id, run_id, rowid>?)``;
    * the INSERT drives the scratch side by the same index with the page's
      rowid range and probes ``kg_canonical_scratch`` by all three keys --
      never a scan of either table, never a temp B-tree for the ORDER BY.

    States: no ``sqlite_stat1`` (production never runs ANALYZE); statistics
    taken while the scratch held another, larger run (stale); statistics taken
    on an empty scratch; statistics of the run itself (fresh). 3.45 picks
    ``SCAN s`` for the old unpaged statement once any statistics exist --
    linear, but a full scan per page would be quadratic over the pages."""
    from app.repositories.sqlite import database as sqlite_database

    rt = repo._runtime
    with rt.database.write() as db:
        db.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) "
            "VALUES ('nb-plan','p','2026-01-01','2026-01-01')")
    if statistics == "stale":
        _fill_run(rt, "run-old", 3000)
        with rt.database.write() as db:
            db.execute("ANALYZE")
            db.execute("DELETE FROM kg_cluster_scratch")
            db.execute("DELETE FROM kg_canonical_scratch")
    elif statistics == "taken_empty":
        with rt.database.write() as db:
            db.execute("ANALYZE")
    _fill_run(rt, "run-new", 400)
    _fill_run(rt, "run-other", 400)
    if statistics == "fresh":
        with rt.database.write() as db:
            db.execute("ANALYZE")
    rt.database.close_local()

    captured: list[tuple[str, tuple]] = []
    original = sqlite_database._Conn.execute

    def recording(self, sql, parameters=(), /):
        if isinstance(sql, str) and "FROM kg_cluster_scratch AS s" in sql:
            captured.append((sql, tuple(parameters)))
        return original(self, sql, parameters)

    monkeypatch.setattr(sqlite_database._Conn, "execute", recording)
    with rt.database.write() as db:
        assert rt.unified_kg.write_cluster_map_generation(
            db, "nb-plan", "claim", "run-new", "2026-01-01T00:00:00", 5,
            start=None, page_rows=100) is not None
    monkeypatch.setattr(sqlite_database._Conn, "execute", original)
    assert len(captured) == 2, [s[:60] for s, _p in captured]
    # Statement shape: the page asks for rowid order itself (the plans below
    # deliver it anyway, so only this line notices the ORDER BY going missing).
    assert " ".join(captured[1][0].split()).endswith("ORDER BY s.rowid")
    db = rt.database.connect()
    bound, insert = (
        [row[3] for row in sqlite3.Connection.execute(db, "EXPLAIN QUERY PLAN " + sql, params)]
        for sql, params in captured
    )
    # Both forms read forward from the page start and stop after ``page_rows``
    # of the run's rows; 3.45 with stale statistics takes the table's rowid
    # range instead of the run's index entries.
    assert bound in (
        ["SEARCH s USING COVERING INDEX idx_kg_cluster_scratch_nb_run "
         "(notebook_id=? AND run_id=? AND rowid>?)"],
        ["SEARCH s USING INTEGER PRIMARY KEY (rowid>?)"],
    ), bound
    # The scratch side is a seek bounded by the page's rowid range either way:
    # through the run's index entries, or -- the stale-statistics choice --
    # straight through the table's rowid range (which may also hold other
    # runs' rows written concurrently with this one; still one pass over the
    # run's span across all pages, never a rescan per page).
    assert insert[0] in (
        "SEARCH s USING INDEX idx_kg_cluster_scratch_nb_run "
        "(notebook_id=? AND run_id=? AND rowid>? AND rowid<?)",
        "SEARCH s USING INTEGER PRIMARY KEY (rowid>? AND rowid<?)",
    ), insert
    assert insert[1:] == [
        "SEARCH c USING INDEX idx_kg_canonical_scratch_nb_run_seed "
        "(notebook_id=? AND run_id=? AND seed=?)",
    ], insert


@pytest.mark.parametrize("statistics", ["none", "stale", "taken_empty", "fresh"])
def test_scratch_readers_drive_by_the_scratch_and_probe_by_key(
    repo, monkeypatch, statistics,
):
    """Pass B's vector read and the description stage's evidence read, captured
    from the real store methods: the scratch side drives (by its index, or a
    plain scan once statistics exist -- linear either way) and the joined
    table is a primary-key probe per scratch row. Never the other way round:
    with ``knowledge_embeddings`` / ``knowledge_objects`` outside, the scratch
    side would have no ``object_id`` key to probe and every outer row would
    rescan the run (the PostgreSQL twin of this check is
    ``tests/postgres/test_cluster_map_write_plan_pins.py``)."""
    from app.repositories.sqlite import database as sqlite_database

    rt = repo._runtime
    with rt.database.write() as db:
        db.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) "
            "VALUES ('nb-plan','p','2026-01-01','2026-01-01')")
        db.executemany(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,payload,evidence,"
            "created_at,updated_at) VALUES (?,'nb-plan','claim','{}','[]','t','t')",
            [(f"ko-{i:06d}",) for i in range(400)])
        db.executemany(
            "INSERT INTO knowledge_embeddings(object_id,notebook_id,vector,created_at) "
            "VALUES (?,'nb-plan',?,'t')", [(f"ko-{i:06d}", b"\0" * 4) for i in range(400)])
    if statistics == "stale":
        _fill_run(rt, "run-old", 3000)
        with rt.database.write() as db:
            db.execute("ANALYZE")
            db.execute("DELETE FROM kg_cluster_scratch")
            db.execute("DELETE FROM kg_canonical_scratch")
    elif statistics == "taken_empty":
        with rt.database.write() as db:
            db.execute("ANALYZE")
    _fill_run(rt, "run-new", 400)
    if statistics == "fresh":
        with rt.database.write() as db:
            db.execute("ANALYZE")
    rt.database.close_local()

    captured: list[tuple[str, tuple]] = []
    original = sqlite_database._Conn.execute

    def recording(self, sql, parameters=(), /):
        if isinstance(sql, str) and "JOIN kg_cluster_scratch s" in sql:
            captured.append((sql, tuple(parameters)))
        return original(self, sql, parameters)

    monkeypatch.setattr(sqlite_database._Conn, "execute", recording)
    with rt.database.connect() as db:
        list(rt.unified_kg.scratch_vector_rows(db, "nb-plan", "run-new"))
        rt.unified_kg.cluster_evidence_rows(
            db, "nb-plan", "run-new", ["seed 1", "seed 2", "seed 3"])
    monkeypatch.setattr(sqlite_database._Conn, "execute", original)
    assert len(captured) == 2, [s[:60] for s, _p in captured]
    db = rt.database.connect()
    # A "BLOOM FILTER ON e" step (SQLite's pre-probe filter, chosen with
    # statistics) only skips probes; it is not a join order.
    vectors, evidence = (
        [row[3] for row in sqlite3.Connection.execute(db, "EXPLAIN QUERY PLAN " + sql, params)
         if not row[3].startswith("BLOOM FILTER ON ")]
        for sql, params in captured
    )
    driver =("SEARCH s USING INDEX idx_kg_cluster_scratch_nb_run "
              "(notebook_id=? AND run_id=?)", "SCAN s")
    assert vectors[0] in driver and vectors[1:] == [
        "SEARCH e USING INDEX sqlite_autoindex_knowledge_embeddings_1 (object_id=?)",
    ], vectors
    assert evidence[0] in driver and evidence[1:] == [
        "SEARCH k USING INDEX sqlite_autoindex_knowledge_objects_1 (id=?)",
    ], evidence
