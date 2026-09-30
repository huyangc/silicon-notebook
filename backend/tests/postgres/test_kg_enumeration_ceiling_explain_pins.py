"""PR-B·B1 EXPLAIN pins (real PostgreSQL): the source-ceiling statements of
``knowledge_object_page_rows`` / ``count_knowledge`` keep their plan shape
under realistically large ceilings (4 000 and 49 000 ids, ONE parameter), on
the first AND the fifteenth execution on one connection.

What is pinned is the exact statement the store issues (captured through a
recording connection, not re-typed here), explained through
``source_ceiling.execute_with_ceiling`` — the same unprepared, statement-local
settings the store runs it under:

* the page walks ``idx_knowledge_objects_nb_type_created`` in keyset order
  (no Sort: the predicate sits below the LIMIT without breaking the ordered
  scan);
* the support probe reaches ``knowledge_object_sources`` through
  ``idx_kos_object`` (object_id) — a few rows per object — never a Seq Scan
  of the evidence table and never ``idx_kos_source_object``, which would seek
  once per CEILING id for every candidate row;
* the ceiling is one ``\x1f``-joined text parameter (``id_binding.bind_ids``,
  ``string_to_array(%s,E'\x1f')``) folded into a hashed constant array
  (``= ANY ('{...}'::text[])``);
* no Gather / Gather Merge: without the statement-local
  ``max_parallel_workers_per_gather = 0`` the 49k page plans Gather Merge and
  ships the whole constant to every worker (the control test shows it).
"""
from __future__ import annotations

import threading

import psycopg
import pytest

from app.repositories.postgres import source_ceiling
from app.repositories.postgres.id_binding import ID_SEPARATOR
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
OBJECTS = 30_000
SOURCES = 30_000
# Every object has sources g and g+1, so the even ids support all of them.
CEILING_4K = frozenset(f"src-{index}" for index in range(0, 8_000, 2))
CEILING_49K = frozenset(
    [f"src-{index}" for index in range(0, SOURCES, 2)]
    + [f"absent-{index}" for index in range(34_000)]
)
CEILINGS = {"4k": CEILING_4K, "49k": CEILING_49K}


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


def _statement(connection, call) -> tuple[str, tuple]:
    """Run ``call(recorder)`` once and return its one knowledge_objects
    statement (the certificate read is the other statement it issues)."""
    recorder = RecordingConnection(connection)
    call(recorder)
    statements = [
        (sql, params) for sql, params in recorder.calls if "FROM knowledge_objects" in sql
    ]
    assert len(statements) == 1, statements
    sql, params = statements[0]
    # Text parameters only: the ceiling travels as one joined string, never
    # as a Python list adapted element by element.
    longest = max((p for p in params if isinstance(p, str)), key=len)
    assert longest.count(ID_SEPARATOR) >= 3_999, len(longest)
    assert not any(isinstance(p, (list, tuple, frozenset)) for p in params), params
    return sql, params


def _plan(connection, sql: str, params: tuple, *, with_settings: bool = True) -> str:
    explain = "EXPLAIN (COSTS OFF) " + sql
    if with_settings:
        rows = source_ceiling.execute_with_ceiling(connection, explain, params).fetchall()
    else:
        rows = connection.execute(explain, params, prepare=False).fetchall()
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


def _plans_first_and_fifteenth(postgres_database, call) -> list[str]:
    """The plan on the first execution and after fourteen more on the same
    connection (a prepared statement would have gone generic by then)."""
    with postgres_database.connect() as connection:
        sql, params = _statement(connection, call)
        first = _plan(connection, sql, params)
        for _ in range(13):
            call(connection)
        sql_again, params_again = _statement(connection, call)
        assert (sql_again, params_again) == (sql, params)
        return [first, _plan(connection, sql, params)]


def _page(store, ceiling, after=None):
    return lambda db: store.knowledge_object_page_rows(
        db, NOTEBOOK, "concept", after, 25, allowed_source_ids=ceiling,
    )


def _count(store, ceiling):
    return lambda db: store.count_knowledge(
        db, NOTEBOOK, "concept", USABLE_STATUSES,
        supported_by_source_ids=ceiling,
        excluding_owner_source_ids=("src-1", "src-3"),
    )


def _assert_common(plan: str) -> None:
    assert "Gather" not in plan, plan
    assert "Seq Scan on knowledge_objects" not in plan, plan
    assert "'::text[]" in plan and "string_to_array" not in plan, plan


def test_page_with_reverse_index_keeps_keyset_scan_and_object_probe(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 66
    _seed(postgres_database, backfilled=True)
    store = KnowledgeStore(postgres_database, _seams())
    after = ("2026-09-01T01:00:00+00:00", f"{NOTEBOOK}-ko-3600")
    for name, ceiling in CEILINGS.items():
        for call in (_page(store, ceiling), _page(store, ceiling, after)):
            for plan in _plans_first_and_fifteenth(postgres_database, call):
                _assert_common(plan)
                assert "idx_knowledge_objects_nb_type_created" in plan, (name, plan)
                assert "Sort" not in plan, (name, plan)
                assert "idx_kos_object" in plan, (name, plan)
                assert "Seq Scan on knowledge_object_sources" not in plan, (name, plan)
                assert "idx_kos_source_object" not in plan, (name, plan)


def test_count_with_reverse_index_never_seeks_per_ceiling_id(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 66
    _seed(postgres_database, backfilled=True)
    store = KnowledgeStore(postgres_database, _seams())
    for name, ceiling in CEILINGS.items():
        for plan in _plans_first_and_fifteenth(postgres_database, _count(store, ceiling)):
            _assert_common(plan)
            assert "Seq Scan on knowledge_object_sources" not in plan, (name, plan)
            assert "idx_kos_source_object" not in plan, (name, plan)


def test_uncertified_index_reads_evidence_json_not_the_reverse_index(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 66
    _seed(postgres_database, backfilled=False)
    store = KnowledgeStore(postgres_database, _seams())
    for name, ceiling in CEILINGS.items():
        for plan in _plans_first_and_fifteenth(postgres_database, _page(store, ceiling)):
            _assert_common(plan)
            assert "idx_knowledge_objects_nb_type_created" in plan, (name, plan)
            assert "Sort" not in plan, (name, plan)
            assert "jsonb_array_elements" in plan, (name, plan)
            assert "knowledge_object_sources" not in plan, (name, plan)
        for plan in _plans_first_and_fifteenth(postgres_database, _count(store, ceiling)):
            _assert_common(plan)
            assert "knowledge_object_sources" not in plan, (name, plan)


def test_control_without_statement_settings_the_49k_page_goes_parallel(postgres_database):
    """Control for the "no Gather" pins: the same custom plan WITHOUT
    ``execute_with_ceiling``'s settings is parallel here, so their absence
    above is the settings' doing, not the fixture's size."""
    assert PostgresMigrator(postgres_database).migrate() == 66
    _seed(postgres_database, backfilled=True)
    store = KnowledgeStore(postgres_database, _seams())
    with postgres_database.connect() as connection:
        sql, params = _statement(connection, _page(store, CEILING_49K))
        plan = _plan(connection, sql, params, with_settings=False)
    assert "Gather" in plan, plan


def test_ceiling_statements_never_become_server_side_prepared(postgres_database):
    """psycopg prepares a statement on its fifth execution per connection;
    PostgreSQL may then plan it GENERICALLY, where the ceiling is an opaque
    ``$n`` (assumed ~10 elements, ``= ANY`` unhashed) — the uncertified count
    went from ~40 ms to ~3.6 s at its sixth execution (see the
    ``source_ceiling`` module docstring).  Pin that the store's ceiling
    statements are sent unprepared however often they run, and that the check
    itself can see a prepared statement (control)."""
    assert PostgresMigrator(postgres_database).migrate() == 66
    _seed(postgres_database, backfilled=True)
    store = KnowledgeStore(postgres_database, _seams())
    prepared_sql = (
        "SELECT count(*) AS c FROM pg_prepared_statements "
        "WHERE statement LIKE %s"
    )
    with postgres_database.connect() as connection:
        recorder = RecordingConnection(connection)
        for _ in range(8):
            _page(store, CEILING_4K)(recorder)
            store.count_knowledge(
                recorder, NOTEBOOK, "concept", USABLE_STATUSES,
                supported_by_source_ids=CEILING_4K,
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
