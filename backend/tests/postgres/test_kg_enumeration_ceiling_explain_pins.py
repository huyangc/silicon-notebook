"""PR-B·B1 EXPLAIN pins (real PostgreSQL): the source-ceiling predicate of
``knowledge_object_page_rows`` / ``count_knowledge`` keeps its access paths
under a realistically large ceiling (thousands of ids, ONE array parameter).

What is pinned is the exact statement the store issues (captured through a
recording connection, not re-typed here):

* the page walks ``idx_knowledge_objects_nb_type_created`` in keyset order
  (no Sort: the predicate sits below the LIMIT without breaking the ordered
  scan);
* the support probe reaches ``knowledge_object_sources`` through
  ``idx_kos_object`` (object_id) — a few rows per object — never a Seq Scan
  of the evidence table and never ``idx_kos_source_object``, which would seek
  once per CEILING id for every candidate row;
* the ceiling is one hashed array (``= ANY``), bound once.
"""
from __future__ import annotations

import json
import threading

import psycopg
import pytest

from app.repositories.postgres.knowledge_store import KnowledgeStore
from app.repositories.postgres.migrator import PostgresMigrator
from app.services.knowledge_contracts import USABLE_STATUSES
from app.services.repository_runtime import RepositoryCompatibilitySeams
from tests.kg_ceiling_fixture import RecordingConnection

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_kg_enumeration_ceiling_explain"),
]

NOTEBOOK = "nb-ceil-explain"
OBJECTS = 20_000
SOURCES = 8_000
CEILING = [f"src-{index}" for index in range(0, SOURCES, 2)]     # 4 000 ids


def _seams() -> RepositoryCompatibilitySeams:
    lock = threading.Lock()
    counter: dict[str, int] = {}

    def new_id(prefix: str) -> str:
        with lock:
            counter[prefix] = counter.get(prefix, 0) + 1
            return f"{prefix}-{counter[prefix]:04d}"

    return RepositoryCompatibilitySeams(
        new_id=new_id, now=lambda: "2026-09-01T00:00:00+00:00",
        copy_chunk_size=lambda: 100, remap_json_ids=lambda value, _m: value,
        in_chunk_size=lambda: 100,
    )


def _seed(postgres_database, *, backfilled: bool) -> None:
    now = "2026-09-01T00:00:00+00:00"
    with postgres_database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        for notebook_id in (NOTEBOOK, NOTEBOOK + "-noise"):
            db.execute(
                "INSERT INTO notebooks(id,name,created_at,updated_at) VALUES (%s,'e',%s,%s)",
                (notebook_id, now, now),
            )
            db.execute(
                "INSERT INTO unified_kg_state (notebook_id,source_index_backfilled,"
                "updated_at) VALUES (%s,%s,%s)",
                (notebook_id, 1 if backfilled else 0, now),
            )
        # Two notebooks so the evidence table is not "this notebook only";
        # each object has two evidence sources.
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,payload,"
            "evidence,source_id,created_at,updated_at) "
            "SELECT nb||'-ko-'||g, nb, 'concept', 'approved', '{}'::jsonb, "
            "jsonb_build_array(jsonb_build_object('source_id','src-'||(g %% %s)), "
            "jsonb_build_object('source_id','src-'||((g+1) %% %s))), "
            "'src-'||(g %% %s), %s::timestamptz + g * interval '1 second', %s "
            "FROM generate_series(0, %s) g, unnest(%s::text[]) nb",
            (SOURCES, SOURCES, SOURCES, now, now, OBJECTS - 1,
             [NOTEBOOK, NOTEBOOK + "-noise"]),
        )
        db.execute(
            "INSERT INTO knowledge_object_sources (object_id,source_id,notebook_id) "
            "SELECT ko.id, ev->>'source_id', ko.notebook_id FROM knowledge_objects ko "
            "CROSS JOIN LATERAL jsonb_array_elements(ko.evidence) ev"
        )
    with psycopg.connect(postgres_database.settings.database_url, autocommit=True) as raw:
        raw.execute("VACUUM (ANALYZE) knowledge_objects")
        raw.execute("VACUUM (ANALYZE) knowledge_object_sources")


def _captured(call) -> tuple[str, tuple]:
    """Run ``call(recorder)`` and return the one knowledge_objects statement."""
    statements = []

    def run(connection):
        recorder = RecordingConnection(connection)
        call(recorder)
        statements.extend(
            (sql, params) for sql, params in recorder.calls
            if "FROM knowledge_objects" in sql
        )

    return run, statements


def _plan(connection, sql: str, params: tuple) -> str:
    rows = connection.execute(f"EXPLAIN (COSTS OFF) {sql}", params).fetchall()
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


def _statement(postgres_database, call) -> tuple[str, str]:
    run, statements = _captured(call)
    with postgres_database.connect() as connection:
        run(connection)
        assert len(statements) == 1, statements
        sql, params = statements[0]
        # One array parameter carries the whole ceiling.
        assert sum(1 for p in params if isinstance(p, list) and len(p) == len(CEILING)) == 1
        assert len(params) <= 8, len(params)
        return sql, _plan(connection, sql, params)


def _page(store, after=None):
    return lambda db: store.knowledge_object_page_rows(
        db, NOTEBOOK, "concept", after, 25, allowed_source_ids=CEILING,
    )


def test_page_with_reverse_index_keeps_keyset_scan_and_object_probe(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 65
    _seed(postgres_database, backfilled=True)
    store = KnowledgeStore(postgres_database, _seams())
    after = ("2026-09-01T01:00:00+00:00", f"{NOTEBOOK}-ko-3600")
    for call in (_page(store), _page(store, after)):
        _sql, plan = _statement(postgres_database, call)
        assert "idx_knowledge_objects_nb_type_created" in plan, plan
        assert "Sort" not in plan, plan
        assert "idx_kos_object" in plan, plan
        assert "Seq Scan on knowledge_object_sources" not in plan, plan
        assert "idx_kos_source_object" not in plan, plan
        assert "Seq Scan on knowledge_objects" not in plan, plan


def test_count_with_reverse_index_probes_by_object(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 65
    _seed(postgres_database, backfilled=True)
    store = KnowledgeStore(postgres_database, _seams())
    _sql, plan = _statement(
        postgres_database,
        lambda db: store.count_knowledge(
            db, NOTEBOOK, "concept", USABLE_STATUSES,
            supported_by_source_ids=CEILING,
            excluding_owner_source_ids=("src-1", "src-3"),
        ),
    )
    assert "Seq Scan on knowledge_object_sources" not in plan, plan
    assert "idx_kos_source_object" not in plan, plan
    assert "Seq Scan on knowledge_objects" not in plan, plan


def test_uncertified_index_reads_evidence_json_not_the_reverse_index(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 65
    _seed(postgres_database, backfilled=False)
    store = KnowledgeStore(postgres_database, _seams())
    _sql, plan = _statement(postgres_database, _page(store))
    assert "idx_knowledge_objects_nb_type_created" in plan, plan
    assert "Sort" not in plan, plan
    assert "jsonb_array_elements" in plan, plan
    assert "knowledge_object_sources" not in plan, plan
    assert "Seq Scan on knowledge_objects" not in plan, plan


def test_ceiling_statements_never_become_server_side_prepared(postgres_database):
    """psycopg prepares a statement on its fifth execution per connection;
    PostgreSQL may then plan it GENERICALLY, where the ceiling array is an
    opaque ``$n`` (assumed ~10 elements, ``= ANY`` unhashed) — the page then
    drives from ``idx_kos_source`` and sorts the notebook's whole supported
    set before the LIMIT (see ``_execute_id_list_statement``).  Pin that the
    store's ceiling statements are sent unprepared however often they run, and
    that the check itself can see a prepared statement (control)."""
    assert PostgresMigrator(postgres_database).migrate() == 65
    _seed(postgres_database, backfilled=True)
    store = KnowledgeStore(postgres_database, _seams())
    prepared_sql = (
        "SELECT count(*) AS c FROM pg_prepared_statements "
        "WHERE statement LIKE %s"
    )
    with postgres_database.connect() as connection:
        recorder = RecordingConnection(connection)
        for _ in range(8):
            store.knowledge_object_page_rows(
                recorder, NOTEBOOK, "concept", None, 25, allowed_source_ids=CEILING,
            )
            store.count_knowledge(
                recorder, NOTEBOOK, "concept", USABLE_STATUSES,
                supported_by_source_ids=CEILING,
            )
        listed = [
            options for (sql, _params), options in zip(recorder.calls, recorder.options)
            if "FROM knowledge_objects" in sql
        ]
        assert len(listed) == 16 and all(o == {"prepare": False} for o in listed), listed
        assert connection.execute(
            prepared_sql, ("%knowledge_object_sources%",)).fetchone()["c"] == 0
        # Control: the same statement through psycopg's default path IS
        # prepared by now, so a zero above is not a blind check.
        sql, params = next(
            call for call in reversed(recorder.calls) if "FROM knowledge_objects" in call[0]
        )
        for _ in range(8):
            connection.execute(sql, params).fetchall()
        assert connection.execute(
            prepared_sql, ("%knowledge_object_sources%",)).fetchone()["c"] >= 1
