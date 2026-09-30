"""PostgreSQL conformance and EXPLAIN pins for the E2-4 store reads (PR-E2;
ledger B-6, B-9, B-11, N-4).

* **Conformance** -- the ``store_evidence_cases`` contract, the same one the
  SQLite store answers in ``tests/test_store_evidence_ceiling_plans.py``.
* **Byte identity** -- without a ceiling each statement is the historical one,
  sent the historical way (a plain ``execute``: it keeps the plan cache).
* **Plan cache** -- with a ceiling the statement is sent unprepared
  (``id_binding.execute_ids``) and never reaches ``pg_prepared_statements``
  after 15 executions on one connection (psycopg prepares on the 5th; a
  generic plan from about the 11th turns the ceiling into an opaque
  parameter).
* **Plans** -- the ceiling is a folded constant that filters; the endpoints
  (relations) or the trigram text match (exact probe) drive; the owner-library
  evidence read goes element -> source -> notebook by primary key.

A bulk of 12 000 sources / objects / chunks and 36 000 relations sits next to
the contract rows so the planner costs a realistic notebook
(``VACUUM ANALYZE``).
"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from app.domain.repository import RepositoryCompatibilitySeams
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.postgres.id_binding import execute_ids
from app.repositories.postgres.knowledge_store import KnowledgeStore
from app.repositories.postgres.migrator import PostgresMigrator
from tests import store_evidence_cases as cases

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_store_evidence_pins"),
]

BULK = 12_000
EXECUTIONS = 15
WIDE = frozenset(
    cases.SOURCES + ["s-priv"]
    + [f"sb-{n:05d}" for n in range(BULK)]
    + [f"pad-{n:05d}" for n in range(20_000)]
)


@pytest.fixture
def database(postgres_settings):
    """One pooled connection: every call below runs on the backend whose
    ``pg_prepared_statements`` the cache pin reads."""
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
    with database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        cases.seed(db.execute, "%s")
        for ordinal, (chunk_id, source, text) in enumerate(cases.exact_chunks()):
            db.execute(
                "INSERT INTO chunks(id,notebook_id,source_id,text,ordinal,element_ids,"
                "created_at) VALUES (%s,%s,%s,%s,%s,'[]',%s)",
                (chunk_id, cases.NB, source, text, ordinal + 1, cases.NOW),
            )
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,"
            "created_at,updated_at) SELECT 'sb-'||lpad(g::text,5,'0'), %s, 'Bulk '||g, "
            "'file','ready','ready', %s, %s FROM generate_series(0, %s) g",
            (cases.NB, cases.NOW, cases.NOW, BULK - 1),
        )
        db.execute(
            "INSERT INTO source_elements(id,source_id,element_type,location_label,text,"
            "created_at) SELECT 'elb-'||g, 'sb-'||lpad(g::text,5,'0'), 'paragraph', "
            "'p1', 'bulk element '||g, %s FROM generate_series(0, %s) g",
            (cases.NOW, BULK - 1),
        )
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
            "evidence,source_id,created_at,updated_at) "
            "SELECT 'kob-'||lpad(g::text,5,'0'), %s, 'concept', 'approved', "
            "jsonb_build_object('name','bulk '||g), '[]'::jsonb, "
            "'sb-'||lpad(g::text,5,'0'), %s, %s FROM generate_series(0, %s) g",
            (cases.NB, cases.NOW, cases.NOW, BULK - 1),
        )
        db.execute(
            "INSERT INTO knowledge_relations(id,notebook_id,source_id,source_object_id,"
            "target_object_id,edge_type,review_status,evidence,created_at) "
            "SELECT 'krb-'||g||'-'||k, %s, 'sb-'||lpad(((g + k) %% %s)::text,5,'0'), "
            "'kob-'||lpad(g::text,5,'0'), 'kob-'||lpad(((g + 1) %% %s)::text,5,'0'), "
            "'related_to', 'pending', '[]'::jsonb, %s "
            "FROM generate_series(0, %s) g, generate_series(0, 2) k",
            (cases.NB, BULK, BULK, cases.NOW, BULK - 1),
        )
        db.execute(
            "INSERT INTO chunks(id,notebook_id,source_id,text,ordinal,element_ids,created_at) "
            "SELECT 'cb-'||g, %s, 'sb-'||lpad(g::text,5,'0'), "
            "'bulk text w'||(g %% 997)||' w'||((g * 7) %% 991), 1000 + g, '[]', %s "
            "FROM generate_series(0, %s) g",
            (cases.NB, cases.NOW, BULK - 1),
        )
    with psycopg.connect(database.settings.database_url, autocommit=True) as raw:
        raw.execute(
            "VACUUM (ANALYZE) sources, source_elements, knowledge_objects, "
            "knowledge_relations, chunks, notebooks"
        )


def _seams() -> RepositoryCompatibilitySeams:
    return RepositoryCompatibilitySeams(
        new_id=lambda prefix: f"{prefix}-e24", now=lambda: cases.NOW,
        copy_chunk_size=lambda: 100, remap_json_ids=lambda value, _map: value,
        in_chunk_size=lambda: 900,
    )


def _on_connection(database, fn):
    def call(*args, **kwargs):
        with database.connect() as db:
            return fn(db, *args, **kwargs)
    return call


class _Recorder:
    """Every ``execute`` the stores send: (sql, params, options)."""

    def __init__(self, monkeypatch):
        self.calls: list[tuple[str, object, dict]] = []
        original = psycopg.Connection.execute

        def recording(conn, query, params=None, **options):
            if isinstance(query, str):
                self.calls.append((query, params, dict(options)))
            return original(conn, query, params, **options)

        monkeypatch.setattr(psycopg.Connection, "execute", recording)

    def only(self, marker: str) -> tuple[str, object, dict]:
        matching = [call for call in self.calls if marker in call[0]]
        assert len(matching) == 1, [sql[:100] for sql, _p, _o in self.calls]
        return matching[0]


def _plan(database, sql: str, params) -> str:
    with database.connect() as db:
        rows = execute_ids(db, f"EXPLAIN (COSTS OFF, VERBOSE) {sql}", params).fetchall()
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


# --------------------------------------------------------------- conformance
def test_in_network_relation_rows_contract(database):
    store = KnowledgeStore(database, _seams())
    cases.check_in_network_relations(_on_connection(
        database,
        lambda db, ids, **kwargs: store.in_network_relation_rows(db, cases.NB, ids, **kwargs),
    ))


def test_chunk_exact_search_contract(database):
    store = KnowledgeStore(database, _seams())
    cases.check_chunk_exact(_on_connection(
        database,
        lambda db, needle, k, **kwargs: store.chunk_exact_search(
            db, cases.NB, needle, k, **kwargs),
    ))


def test_node_context_owner_library_contract(database):
    store = KnowledgeStore(database, _seams())
    cases.check_node_context_owner(
        lambda object_id: store.node_context(cases.NB, object_id, check_access=False)
    )


def test_follow_relation_evidence_contract(database):
    store = KnowledgeStore(database, _seams())
    cases.check_follow_relation_evidence(_on_connection(
        database,
        lambda db, ids, **kwargs: store.follow_relation_evidence_rows(db, ids, **kwargs),
    ))


# ------------------------------------------------------------ byte identity
def test_without_a_ceiling_the_statements_are_the_historical_ones(database, monkeypatch):
    store = KnowledgeStore(database, _seams())
    ids = ["ko-a", "ko-b", "ko-c"]
    recorder = _Recorder(monkeypatch)
    with database.connect() as db:
        store.in_network_relation_rows(db, cases.NB, ids)
        store.chunk_exact_search(db, cases.NB, cases.NEEDLE, 5)
    relation_sql, relation_params, relation_options = recorder.only("DISTINCT r.source_object_id")
    assert relation_sql == (
        "SELECT DISTINCT r.source_object_id, r.target_object_id, r.edge_type, "
        "src.object_type AS source_type, tgt.object_type AS target_type "
        "FROM knowledge_relations AS r "
        "JOIN knowledge_objects AS src ON src.id=r.source_object_id "
        "JOIN knowledge_objects AS tgt ON tgt.id=r.target_object_id "
        "WHERE r.notebook_id=%s AND r.review_status!='rejected' "
        "AND r.source_object_id IN (%s,%s,%s) "
        "AND r.target_object_id IN (%s,%s,%s) "
        "ORDER BY r.source_object_id, r.edge_type, r.target_object_id"
    )
    assert list(relation_params) == [cases.NB, *ids, *ids]
    assert relation_options == {}
    exact_sql, exact_params, exact_options = recorder.only("ILIKE")
    assert "ANY(" not in exact_sql and "string_to_array" not in exact_sql
    assert exact_sql.endswith(' ILIKE %s ORDER BY candidate_similarity DESC,id COLLATE "C" LIMIT %s')
    assert len(exact_params) == 4 and exact_options == {}


# ------------------------------------------------------------ plan cache
def test_ceiling_statements_stay_out_of_the_plan_cache(database, monkeypatch):
    store = KnowledgeStore(database, _seams())
    recorder = _Recorder(monkeypatch)
    calls = {
        "relations": lambda db: store.in_network_relation_rows(
            db, cases.NB, ["ko-a", "ko-b", "ko-c"], allowed_source_ids=WIDE),
        "exact": lambda db: store.chunk_exact_search(
            db, cases.NB, cases.NEEDLE, 5, allowed_source_ids=sorted(WIDE)),
    }
    for name, call in calls.items():
        recorder.calls.clear()
        for _ in range(EXECUTIONS):
            with database.connect() as db:
                call(db)
        bound = [c for c in recorder.calls if "string_to_array" in c[0]]
        assert len(bound) == EXECUTIONS, name
        assert all(options == {"prepare": False} for _s, _p, options in bound), name
        with database.connect() as db:
            leaked = [
                row["statement"] for row in db.execute(
                    "SELECT statement FROM pg_prepared_statements").fetchall()
                if "string_to_array" in str(row["statement"])
            ]
        assert leaked == [], name


# ------------------------------------------------------------------ plans
def test_relation_ceiling_is_a_constant_filter_and_the_endpoints_drive(database, monkeypatch):
    store = KnowledgeStore(database, _seams())
    recorder = _Recorder(monkeypatch)
    with database.connect() as db:
        rows = store.in_network_relation_rows(
            db, cases.NB, ["ko-a", "ko-b", "ko-c", "kob-00001", "kob-00002"],
            allowed_source_ids=WIDE,
        )
    assert cases.edges(rows)[("ko-a", "supports", "ko-b")] == 3
    sql, params, _options = recorder.only("COUNT(DISTINCT r.source_id)")
    plan = _plan(database, sql, params)
    assert "idx_knowledge_relations_nb_source" in plan or "idx_knowledge_relations_nb_target" in plan, plan
    assert "r.source_id = ANY ('{" in plan, plan
    assert "string_to_array" not in plan, plan
    assert "idx_knowledge_relations_source " not in plan, plan
    assert "Index Cond: (r.source_id" not in plan, plan
    assert not any(
        "Seq Scan on" in line and ".knowledge_relations" in line
        for line in plan.splitlines()
    ), plan


def test_exact_probe_ceiling_is_a_constant_filter_on_the_trigram_match(database, monkeypatch):
    store = KnowledgeStore(database, _seams())
    recorder = _Recorder(monkeypatch)
    with database.connect() as db:
        store.chunk_exact_search(
            db, cases.NB, cases.NEEDLE, 5, allowed_source_ids=sorted(WIDE),
        )
    sql, params, _options = recorder.only("ILIKE")
    plan = _plan(database, sql, params)
    assert "idx_chunks_text_trgm" in plan, plan
    assert "chunks.source_id = ANY ('{" in plan, plan
    assert "Index Cond: (chunks.source_id" not in plan, plan
    assert "idx_chunks_source" not in plan, plan


def test_owner_library_evidence_read_goes_by_primary_keys(database, monkeypatch):
    store = KnowledgeStore(database, _seams())
    recorder = _Recorder(monkeypatch)
    element_ids = ["el-own", "el-priv"] + [f"elb-{n}" for n in range(0, BULK, 400)]
    with database.connect() as db:
        enriched = store._enrich_evidence(
            db, [{"element_id": element} for element in element_ids],
            owner_notebook_id=cases.NB,
        )
        store._element_texts(db, element_ids, owner_notebook_id=cases.NB)
    by_element = {row["element_id"]: row["element_text"] for row in enriched}
    assert by_element["el-own"] == cases.OWN_TEXT
    assert by_element["el-priv"] == ""  # not read: no stored span in this probe
    for marker in ("se.element_type", "SELECT se.id, se.text"):
        sql, params, _options = recorder.only(marker)
        plan = _plan(database, sql, params)
        assert "pk_source_elements" in plan, plan
        assert "Index Scan using pk_sources" in plan, plan
        # The two-row notebooks table may be scanned; the element and source
        # tables never are, and no notebook-wide source index is walked.
        for line in plan.splitlines():
            if "Seq Scan on" in line:
                assert line.rstrip().endswith(" onb"), plan
        assert "idx_sources_" not in plan, plan
