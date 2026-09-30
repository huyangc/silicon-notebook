"""PostgreSQL twin of ``tests/test_ppr_source_ceiling.py``'s store half, and
the plan pins of ``ChunkStore.graph_hydrate_rows(allowed_source_ids=...)``.

The PPR slot filter (audit B-2) hydrates one window of ranked chunk ids with
the run's per-library source ceilings: ``{notebook_id: frozen source ids}``.
Pinned here on a live server:

* behaviour -- a listed library keeps only its listed sources, an empty list
  denies it, each list admits only its own library's sources, and with
  nothing listed the statement is the historical one, byte for byte, sent the
  historical way (the plan cache keeps it);
* plan -- the <= ``_IN_CHUNK`` candidate primary keys drive the read
  (``pk_chunks``); the ceiling travels as ONE ``id_binding`` parameter that a
  custom plan folds into a constant array and only filters with; a forced
  generic plan of the same statement is the control that keeps it opaque;
* the listed statement never enters ``pg_prepared_statements``, however often
  it runs on one connection.
"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest
from psycopg import sql as pg_sql

from app.repositories.postgres.chunk_store import ChunkStore
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.id_binding import execute_ids
from app.repositories.postgres.migrator import PostgresMigrator
from app.services.source_scope import CeilingSet

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_ppr_hydration_pins"),
]

ACTIVE = "nb-active"
BASE = "nb-base"
USER = "u-pin"
NOW = "2026-09-30T00:00:00+00:00"
BASE_SOURCES = 6_000
# The frozen ceiling of the mounted library: every visible source it had when
# the run froze (all but the late upload), padded to the 49k-id width of a
# large library with ids no row carries.
CEILING = CeilingSet(
    [f"b-{i:05d}" for i in range(BASE_SOURCES)]
    + [f"b-filler-{i:05d}" for i in range(43_000)]
)
ALL = ["c-a-0", "c-a-1", "c-b-00000-0", "c-b-00001-1", "c-kh-0", "c-kh-1", "c-late-0"]
EXECUTIONS = 15


@pytest.fixture
def pin_database(postgres_settings):
    settings = postgres_settings.model_copy(update={
        "postgres_pool_min_size": 1,
        "postgres_pool_max_size": 1,
        "postgres_pool_acquire_timeout_seconds": 10,
        "postgres_statement_timeout_seconds": 20,
    })
    database = PostgresDatabase(settings, Path(__file__).resolve().parents[3])
    try:
        PostgresMigrator(database).migrate()
        _seed(database)
        yield database
    finally:
        database.close()


def _seed(database: PostgresDatabase) -> None:
    """Active library (two sources); mounted library with 6 000 visible
    sources of two chunks each (skewed: every tenth source has ten more, so
    ``chunks.source_id`` has most-common values), a Knowhow projection with
    two chunks and a source uploaded after the freeze."""
    with database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO users(id,email,display_name,role,created_at,updated_at) "
            "VALUES (%s,'pin@example.test','Pin','admin',%s,%s)", (USER, NOW, NOW))
        for nb in (ACTIVE, BASE):
            db.execute(
                "INSERT INTO notebooks(id,name,created_by,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s)", (nb, nb, USER, NOW, NOW))
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,"
            "created_at,updated_at) VALUES "
            "('a-0',%s,'A0','file','ready','ready',%s,%s),"
            "('a-1',%s,'A1','file','ready','ready',%s,%s),"
            "('kh',%s,'KH','knowhow','ready','ready',%s,%s),"
            "('late',%s,'Late','file','ready','ready',%s,%s)",
            (ACTIVE, NOW, NOW, ACTIVE, NOW, NOW, BASE, NOW, NOW, BASE, NOW, NOW))
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,"
            "created_at,updated_at) SELECT 'b-'||lpad(g::text,5,'0'), %s, 'B '||g, "
            "'file', 'ready', 'ready', %s, %s FROM generate_series(0, %s) g",
            (BASE, NOW, NOW, BASE_SOURCES - 1))
        db.execute(
            "INSERT INTO chunks(id,notebook_id,source_id,text,element_ids,created_at) "
            "SELECT 'c-b-'||lpad(g::text,5,'0')||'-'||k, %s, 'b-'||lpad(g::text,5,'0'), "
            "'t', '[]', %s FROM generate_series(0, %s) g, "
            "generate_series(0, 11) k WHERE k < 2 OR g %% 10 = 0",
            (BASE, NOW, BASE_SOURCES - 1))
        for chunk_id, nb, source in (
            ("c-a-0", ACTIVE, "a-0"), ("c-a-1", ACTIVE, "a-1"),
            ("c-kh-0", BASE, "kh"), ("c-kh-1", BASE, "kh"), ("c-late-0", BASE, "late"),
        ):
            db.execute(
                "INSERT INTO chunks(id,notebook_id,source_id,text,element_ids,created_at) "
                "VALUES (%s,%s,%s,'t','[]',%s)", (chunk_id, nb, source, NOW))
    with database.connect() as db:
        db.execute("ANALYZE chunks")
        db.execute("ANALYZE sources")


def _hydrate(database, ids, **kwargs) -> set[str]:
    with database.connect() as db:
        return {row["id"] for row in ChunkStore.graph_hydrate_rows(db, ids, **kwargs)}


def test_listed_library_keeps_only_its_frozen_sources(pin_database):
    assert _hydrate(pin_database, ALL, allowed_source_ids={BASE: CEILING}) == {
        "c-a-0", "c-a-1", "c-b-00000-0", "c-b-00001-1"}


def test_empty_list_denies_and_none_lists_nothing(pin_database):
    assert _hydrate(pin_database, ALL, allowed_source_ids={BASE: frozenset()}) == {
        "c-a-0", "c-a-1"}
    assert _hydrate(pin_database, ALL, allowed_source_ids={BASE: None}) == set(ALL)
    assert _hydrate(pin_database, ALL, allowed_source_ids={}) == set(ALL)
    assert _hydrate(pin_database, ALL) == set(ALL)


def test_each_list_admits_only_its_own_library(pin_database):
    got = _hydrate(pin_database, ALL, allowed_source_ids={
        ACTIVE: frozenset({"a-0", "b-00000"}), BASE: frozenset()})
    assert got == {"c-a-0"}


def test_rows_keep_document_order(pin_database):
    with pin_database.connect() as db:
        rows = ChunkStore.graph_hydrate_rows(
            db, list(reversed(ALL)), allowed_source_ids={BASE: CEILING})
    assert [row["id"] for row in rows] == [
        "c-b-00000-0", "c-b-00001-1", "c-a-0", "c-a-1"]


def _recording(captured):
    original = psycopg.Connection.execute

    def recording_execute(self, query, params=None, **options):
        if isinstance(query, str) and "chunk_notebook_id" in query:
            captured.append((query, params, options))
        return original(self, query, params, **options)

    return original, recording_execute


def test_nothing_listed_sends_the_historical_statement(pin_database, monkeypatch):
    captured: list = []
    _original, recording = _recording(captured)
    monkeypatch.setattr(psycopg.Connection, "execute", recording)
    _hydrate(pin_database, ["c-a-0", "c-a-1"])
    _hydrate(pin_database, ["c-a-0", "c-a-1"], allowed_source_ids={})
    historical = (
        "SELECT c.id,c.source_id,c.text,c.section_path,c.element_ids,"
        "c.notebook_id AS chunk_notebook_id,s.title AS source_title "
        "FROM chunks c JOIN sources s ON s.id=c.source_id "
        "WHERE c.id IN (%s,%s) ORDER BY c.ordinal"
    )
    assert [(q, list(p), o) for q, p, o in captured] == [
        (historical, ["c-a-0", "c-a-1"], {})] * 2


def _listed_statement(database, monkeypatch) -> tuple[str, list]:
    captured: list = []
    _original, recording = _recording(captured)
    monkeypatch.setattr(psycopg.Connection, "execute", recording)
    _hydrate(database, ALL, allowed_source_ids={BASE: CEILING})
    monkeypatch.undo()
    assert len(captured) == 1
    sql, params, options = captured[0]
    assert options == {"prepare": False}
    return sql, list(params)


def _custom_plan(database, sql, params) -> str:
    with database.connect() as db:
        rows = execute_ids(db, f"EXPLAIN (COSTS OFF, VERBOSE) {sql}", params).fetchall()
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


def _generic_plan(database, sql, params) -> str:
    with database.connect() as db:
        db.execute(sql, params, prepare=True).fetchall()
        name = db.execute(
            "SELECT name FROM pg_prepared_statements "
            "WHERE strpos(statement, 'chunk_notebook_id') > 0"
        ).fetchone()["name"]
        db.execute("SET LOCAL plan_cache_mode = force_generic_plan")
        cursor = psycopg.ClientCursor(db)
        marks = ",".join("%s" for _ in params)
        cursor.execute(
            pg_sql.SQL("EXPLAIN (COSTS OFF, VERBOSE) EXECUTE {}(").format(
                pg_sql.Identifier(name)).as_string(db) + marks + ")",
            params,
        )
        rows = cursor.fetchall()
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


def test_listed_plan_is_driven_by_candidate_keys(pin_database, monkeypatch):
    sql, params = _listed_statement(pin_database, monkeypatch)
    # One parameter per listed library ceiling plus the listed-notebook list
    # and each library id: the 49k ceiling is ONE value.
    assert len(params) == len(ALL) + 3
    plan = _custom_plan(pin_database, sql, params)
    assert "pk_chunks" in plan, plan
    assert "Index Cond: (c.id = ANY" in plan, plan
    # The ceiling is a folded constant that only filters the candidate rows,
    # tested on an expression without statistics (no per-element MCV pass).
    assert "((c.source_id || ''::text) = ANY ('{b-00000," in plan, plan
    assert "string_to_array" not in plan, plan
    assert "Index Cond: (c.source_id" not in plan, plan
    assert "idx_chunks_nb" not in plan and "idx_chunks_source" not in plan, plan
    generic = _generic_plan(pin_database, sql, params)
    # Control: generically planned, the ceiling stays an opaque parameter --
    # the shape execute_ids exists to avoid -- and the keys still drive.
    assert "string_to_array($" in generic, generic
    assert "pk_chunks" in generic, generic


@pytest.fixture
def postgres_repository(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


def test_pg_mounted_knowhow_and_late_uploads_never_take_a_slot(
    postgres_repository, monkeypatch,
):
    """The acceptance case end to end on PostgreSQL (twin of
    ``test_ppr_source_ceiling.test_mounted_knowhow_and_late_uploads_never_
    take_a_slot``): the best-ranked candidates are the mounted library's
    Knowhow rows and its post-freeze upload; all three slots still go to
    in-ceiling passages."""
    from app.services.source_scope import source_scope_context

    repository = postgres_repository
    active, base = _seed_mounted(repository)
    _rank(repository, monkeypatch,
          ["c-kh-0", "c-kh-1", "c-late", "c-b1", "c-a1", "c-b2", "c-a2"])
    with source_scope_context(
        active,
        {"mode": "include", "source_ids": ["a1", "a2"], "narrowed": False,
         "owner_id": "user-local"},
        None,
        {base: ["b1", "b2"]},
    ):
        out = repository._ppr_retrieve(active, "q")
    assert [c.chunk_id for c in out] == ["c-b1", "c-a1", "c-b2"]


def test_pg_rows_of_a_library_listed_mid_window_are_judged_row_by_row(
    postgres_repository, monkeypatch,
):
    """Twin of ``test_ppr_source_ceiling.test_rows_of_a_library_listed_mid_
    window_are_judged_row_by_row`` on PostgreSQL, read on the UNFILTERED
    ``graph._ppr_retrieve``: the mounted library is excluded and the ranking
    carries no chunk map, so c-b1 lists it and c-b2, in the same window,
    must be refused too."""
    from app.services.source_scope import source_scope_context

    repository = postgres_repository
    active, _base = _seed_mounted(repository)
    _rank(repository, monkeypatch, ["c-b1", "c-b2", "c-a1", "c-a2", "c-a3"])
    with source_scope_context(active, None, {"mode": "include", "notebook_ids": []}):
        out = repository.retrieval.graph._ppr_retrieve(active, "q")
    assert [c.chunk_id for c in out] == ["c-a1", "c-a2", "c-a3"]


def _seed_mounted(repository) -> tuple[str, str]:
    """Active library (a1-a3) with a mounted library: b1, b2, a Knowhow
    projection of two chunks and a post-freeze upload."""
    from app.models.schemas import NotebookCreate

    base = repository.create_notebook(NotebookCreate(name="base")).id
    repository.mark_notebook_base(base)
    active = repository.create_notebook(NotebookCreate(name="active")).id
    repository.replace_notebook_bases(active, [base], "user-local")
    rows = (
        (active, "a1", "file", "c-a1"), (active, "a2", "file", "c-a2"),
        (active, "a3", "file", "c-a3"),
        (base, "b1", "file", "c-b1"), (base, "b2", "file", "c-b2"),
        (base, "kh", "knowhow", "c-kh-0"), (base, "kh", "knowhow", "c-kh-1"),
        (base, "late", "file", "c-late"),
    )
    with repository._runtime.database.write() as db:
        for notebook_id, source_id, source_type, chunk_id in rows:
            db.execute(
                "INSERT INTO sources(id,notebook_id,title,source_type,status,"
                "parse_status,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,'ready','ready',%s,%s) ON CONFLICT DO NOTHING",
                (source_id, notebook_id, source_id, source_type, NOW, NOW))
            db.execute(
                "INSERT INTO chunks(id,notebook_id,source_id,text,element_ids,created_at) "
                "VALUES (%s,%s,%s,'t','[]',%s)", (chunk_id, notebook_id, source_id, NOW))
    return active, base


def _rank(repository, monkeypatch, ranking) -> None:
    """``scale_ppr`` replaced by a fixed ranking (a plain list: complete, no
    chunk map), ``ppr_top_chunks`` = 3."""
    graph = repository.retrieval.graph
    monkeypatch.setattr(graph.settings, "ppr_top_chunks", 3)
    monkeypatch.setattr(
        graph, "scale_ppr",
        lambda nb, q, max_results=None: [
            (cid, 1.0 - i / 10) for i, cid in enumerate(ranking)
        ][:max_results],
    )


def test_listed_statement_never_reaches_the_plan_cache(pin_database, monkeypatch):
    sql, params = _listed_statement(pin_database, monkeypatch)
    for _ in range(EXECUTIONS):
        _hydrate(pin_database, ALL, allowed_source_ids={BASE: CEILING})
    with pin_database.connect() as db:
        prepared = [
            str(row["statement"]) for row in db.execute(
                "SELECT statement FROM pg_prepared_statements").fetchall()
            if "chunk_notebook_id" in str(row["statement"])
        ]
        assert prepared == []
        # Control: the same statement run the default way IS prepared.
        for _ in range(EXECUTIONS):
            db.execute(sql, params).fetchall()
        prepared = [
            str(row["statement"]) for row in db.execute(
                "SELECT statement FROM pg_prepared_statements").fetchall()
            if "chunk_notebook_id" in str(row["statement"])
        ]
    assert len(prepared) == 1
