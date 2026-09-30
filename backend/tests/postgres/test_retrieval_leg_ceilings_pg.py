"""PR-E2·E2-1 on real PostgreSQL: the weak-support store statements
(``weak_support_relation_rows`` / ``relation_endpoint_name_rows`` with
``allowed_source_ids``) -- same answers as the SQLite twin
(``tests/test_retrieval_leg_ceilings.py``) and EXPLAIN pins.

Pinned plans, on a corpus where OTHER notebooks hold Memory sources and
Memory-derived KG rows (so a plan that loses the notebook predicate reads
them), for the custom plan psycopg gets (``execute_ids``, unprepared) and, as
the control, the generic plan the same statement would get from the plan
cache:

* the canonical probe seeks ``canonical_relations``' primary key by
  (notebook_id, canonical_src) -- never a Seq Scan;
* target support reaches ``concept_clusters`` through a (notebook_id,
  canonical_id) index and ``knowledge_object_sources`` through the object id
  (``idx_kos_object`` or the (object_id, source_id) key) -- never by ceiling id
  (``idx_kos_source``), never a Seq Scan;
* the ceiling is ONE folded constant array in the custom plan (hashed, real
  length), and an opaque ``string_to_array($n)`` only in the generic control;
* the name read is driven by the sample relations' primary keys.
"""
from __future__ import annotations

import json

import psycopg
import pytest
from psycopg import sql as pg_sql

from app.repositories.postgres.id_binding import execute_ids
from app.repositories.postgres.migrator import PostgresMigrator
from app.repositories.postgres.unified_kg_store import UnifiedKgStore

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_retrieval_leg_ceilings"),
]

NB = "nb-e21"
NOW = "2026-09-01T00:00:00+00:00"
NOISE = 20
CEILING = ["src-vis"] + [f"src-pad-{i:05d}" for i in range(4999)]


def _seed(database) -> None:
    with database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        for nb in [NB] + [f"nb-noise-{i}" for i in range(NOISE)]:
            db.execute(
                "INSERT INTO notebooks(id,name,created_at,updated_at) VALUES (%s,%s,%s,%s)",
                (nb, nb, NOW, NOW))
            db.execute(
                "INSERT INTO unified_kg_state(notebook_id,source_index_backfilled,"
                "updated_at) VALUES (%s,1,%s)", (nb, NOW))
            db.execute(
                "INSERT INTO sources(id,notebook_id,title,source_type,created_at,"
                "updated_at) VALUES (%s,%s,'v','markdown',%s,%s),"
                "(%s,%s,'m','memory',%s,%s)",
                (f"{nb}-vis" if nb != NB else "src-vis", nb, NOW, NOW,
                 f"{nb}-mem" if nb != NB else "src-mb", nb, NOW, NOW))
        # The notebook under test: the same hand-built canonical layer as the
        # SQLite twin (``_raw_kg``).
        objects = [("ko-s", "src-vis", ["src-vis"]), ("ko-t1", "src-vis", ["src-vis"]),
                   ("ko-t2", "src-mb", ["src-mb"]), ("ko-g", "src-vis", ["src-vis"]),
                   ("ko-m", "src-mb", ["src-mb"]), ("ko-t5", "src-vis", ["src-vis"])]
        for oid, sid, evidence in objects:
            db.execute(
                "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
                "evidence,source_id,created_at,updated_at) "
                "VALUES (%s,%s,'concept','approved',%s::jsonb,%s::jsonb,%s,%s,%s)",
                (oid, NB, json.dumps({"name": f"name {oid}"}),
                 json.dumps([{"source_id": s, "quote": "q"} for s in evidence]),
                 sid, NOW, NOW))
            for s in evidence:
                db.execute(
                    "INSERT INTO knowledge_object_sources(object_id,source_id,notebook_id) "
                    "VALUES (%s,%s,%s)", (oid, s, NB))
        db.execute(
            "INSERT INTO concept_clusters(id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,object_type,created_at) VALUES "
            "('cc-g',%s,'K-good','ko-g','Good cluster','concept',%s),"
            "('cc-m',%s,'K-mem','ko-m','Memory cluster','concept',%s)",
            (NB, NOW, NB, NOW))
        for rid, target, sid, canonical in (
            ("kr-1", "ko-t1", "src-vis", "ko-t1"), ("kr-2", "ko-t2", "src-vis", "ko-t2"),
            ("kr-3", "ko-g", "src-vis", "K-good"), ("kr-4", "ko-m", "src-mb", "K-mem"),
            ("kr-5", "ko-t5", "src-mb", "ko-t5"),
        ):
            db.execute(
                "INSERT INTO knowledge_relations(id,notebook_id,source_id,source_object_id,"
                "target_object_id,edge_type,evidence,created_at) "
                "VALUES (%s,%s,%s,'ko-s',%s,'kind_of','[]'::jsonb,%s)",
                (rid, NB, sid, target, NOW))
            db.execute(
                "INSERT INTO canonical_relations(notebook_id,canonical_src,edge_type,"
                "canonical_tgt,support_count,source_count,sample_relation_ids,updated_at) "
                "VALUES (%s,'ko-s','kind_of',%s,1,1,%s::jsonb,%s)",
                (NB, canonical, json.dumps([rid]), NOW))
        # Noise: every other notebook holds Memory-derived objects, clusters,
        # relations and canonical edges (1 500 per notebook).
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
            "evidence,source_id,created_at,updated_at) "
            "SELECT nb||'-ko-'||g, nb, 'concept', 'approved', '{}'::jsonb, "
            "jsonb_build_array(jsonb_build_object('source_id', nb||'-mem')), nb||'-mem', "
            "%s, %s FROM generate_series(0, 1499) g, "
            "(SELECT 'nb-noise-'||i AS nb FROM generate_series(0, %s) i) n",
            (NOW, NOW, NOISE - 1))
        db.execute(
            "INSERT INTO knowledge_object_sources(object_id,source_id,notebook_id) "
            "SELECT id, source_id, notebook_id FROM knowledge_objects "
            "WHERE notebook_id LIKE 'nb-noise-%%'")
        db.execute(
            "INSERT INTO concept_clusters(id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,object_type,created_at) "
            "SELECT 'cc-'||id, notebook_id, 'K-'||notebook_id||'-'||(split_part(id,'-ko-',2)"
            "::int / 3), id, 'n', 'concept', %s FROM knowledge_objects "
            "WHERE notebook_id LIKE 'nb-noise-%%'", (NOW,))
        db.execute(
            "INSERT INTO knowledge_relations(id,notebook_id,source_id,source_object_id,"
            "target_object_id,edge_type,evidence,created_at) "
            "SELECT 'kr-'||id, notebook_id, source_id, id, id, 'kind_of', '[]'::jsonb, %s "
            "FROM knowledge_objects WHERE notebook_id LIKE 'nb-noise-%%'", (NOW,))
        db.execute(
            "INSERT INTO canonical_relations(notebook_id,canonical_src,edge_type,"
            "canonical_tgt,support_count,source_count,sample_relation_ids,updated_at) "
            "SELECT notebook_id, 'ko-s', 'kind_of', 'K-'||id, 1, 1, "
            "jsonb_build_array('kr-'||id), %s FROM knowledge_objects "
            "WHERE notebook_id LIKE 'nb-noise-%%'", (NOW,))
    with psycopg.connect(database.settings.database_url, autocommit=True) as raw:
        raw.execute(
            "VACUUM (ANALYZE) knowledge_objects, knowledge_object_sources, "
            "concept_clusters, knowledge_relations, canonical_relations, sources")


@pytest.fixture
def seeded(postgres_database):
    assert PostgresMigrator(postgres_database).migrate()
    _seed(postgres_database)
    return postgres_database


def _set_backfilled(database, flag: int) -> None:
    with database.write() as db:
        db.execute(
            "UPDATE unified_kg_state SET source_index_backfilled=%s WHERE notebook_id=%s",
            (flag, NB))


@pytest.mark.parametrize("backfilled", [1, 0])
def test_pg_weak_support_matches_the_sqlite_twin(seeded, backfilled):
    _set_backfilled(seeded, backfilled)
    with seeded.connect() as db:
        unbounded = UnifiedKgStore.weak_support_relation_rows(db, NB, ["ko-s"], 2, 24)
        bound = UnifiedKgStore.weak_support_relation_rows(
            db, NB, ["ko-s"], 2, 24, allowed_source_ids=["src-vis"])
        denied = UnifiedKgStore.weak_support_relation_rows(
            db, NB, ["ko-s"], 2, 24, allowed_source_ids=[])
        limited = UnifiedKgStore.weak_support_relation_rows(
            db, NB, ["ko-s"], 2, 1, allowed_source_ids=["src-vis"])
    assert sorted(r["canonical_tgt"] for r in unbounded) == [
        "K-good", "K-mem", "ko-t1", "ko-t2", "ko-t5"]
    assert sorted(r["canonical_tgt"] for r in bound) == ["K-good", "ko-t1", "ko-t5"]
    assert denied == []
    # The gate is below the LIMIT: the first in-ceiling row, not an empty page.
    assert [r["canonical_tgt"] for r in limited] == ["K-good"]
    assert isinstance(bound[0]["sample_relation_ids"], str)


def test_pg_endpoint_names_carry_the_sample_source_and_honour_the_ceiling(seeded):
    with seeded.connect() as db:
        unbounded = UnifiedKgStore.relation_endpoint_name_rows(db, NB, ["kr-1", "kr-5"])
        bound = UnifiedKgStore.relation_endpoint_name_rows(
            db, NB, ["kr-1", "kr-5"], allowed_source_ids=["src-vis"])
        denied = UnifiedKgStore.relation_endpoint_name_rows(
            db, NB, ["kr-1"], allowed_source_ids=[])
    assert {(r["rid"], r["source_id"]) for r in unbounded} == {
        ("kr-1", "src-vis"), ("kr-5", "src-mb")}
    assert [r["rid"] for r in bound] == ["kr-1"]
    assert denied == []


def _recorded(database, call) -> tuple[str, object]:
    captured: list[tuple[str, object]] = []
    original = psycopg.Connection.execute

    def recording(self, query, params=None, **options):
        if options.get("prepare") is False and isinstance(query, str):
            captured.append((query, params))
        return original(self, query, params, **options)

    psycopg.Connection.execute = recording
    try:
        with database.connect() as db:
            call(db)
    finally:
        psycopg.Connection.execute = original
    assert len(captured) == 1, [sql[:100] for sql, _ in captured]
    return captured[0]


def _custom_plan(database, sql, params) -> str:
    with database.connect() as db:
        rows = execute_ids(db, f"EXPLAIN (COSTS OFF, VERBOSE) {sql}", params).fetchall()
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


def _generic_plan(database, sql, params) -> str:
    with database.connect() as db:
        db.execute(sql, params, prepare=True).fetchall()
        name = db.execute(
            "SELECT name FROM pg_prepared_statements "
            "WHERE strpos(statement, 'string_to_array') > 0"
        ).fetchone()["name"]
        db.execute("SET LOCAL plan_cache_mode = force_generic_plan")
        cursor = psycopg.ClientCursor(db)
        placeholders = ",".join("%s" for _ in params)
        cursor.execute(
            pg_sql.SQL("EXPLAIN (COSTS OFF, VERBOSE) EXECUTE {}(").format(
                pg_sql.Identifier(name)).as_string(db) + placeholders + ")",
            params,
        )
        rows = cursor.fetchall()
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


def _assert_no_scan(plan: str) -> None:
    for table in ("canonical_relations", "concept_clusters", "knowledge_object_sources",
                  "knowledge_objects", "knowledge_relations"):
        assert f"Seq Scan on {table}" not in plan, plan
    # Neither ``idx_kos_source`` nor ``idx_kos_source_object``: both seek once
    # per CEILING id instead of probing the few rows of one object.
    assert "idx_kos_source" not in plan, plan


@pytest.mark.parametrize("backfilled", [1, 0])
def test_pg_weak_support_probe_plan(seeded, backfilled):
    _set_backfilled(seeded, backfilled)
    sql, params = _recorded(seeded, lambda db: UnifiedKgStore.weak_support_relation_rows(
        db, NB, ["ko-s"], 2, 24, allowed_source_ids=CEILING))
    plan = _custom_plan(seeded, sql, params)
    generic = _generic_plan(seeded, sql, params)
    _assert_no_scan(plan)
    assert "pk_canonical_relations" in plan, plan
    # Custom plan: the ceiling is one folded constant array.  Generic control:
    # an opaque parameter, and -- the cliff ``execute_ids`` exists to avoid --
    # the plan then drives the support probe by ceiling id (``idx_kos_source``)
    # over a Seq Scan of every notebook's clusters.
    assert "= ANY ('{src-vis," in plan and "string_to_array" not in plan, plan
    assert "string_to_array($" in generic, generic
    if backfilled:
        assert "knowledge_object_sources" in plan, plan
    else:
        assert "jsonb_array_elements" in plan, plan


def test_pg_endpoint_name_plan(seeded):
    sql, params = _recorded(seeded, lambda db: UnifiedKgStore.relation_endpoint_name_rows(
        db, NB, ["kr-1", "kr-5"], allowed_source_ids=CEILING))
    plan = _custom_plan(seeded, sql, params)
    generic = _generic_plan(seeded, sql, params)
    _assert_no_scan(plan)
    # The sample relations' primary keys drive; the ceiling only filters them.
    assert "(kr.id = ANY ('{kr-1,kr-5}'::text[]))" in plan, plan
    assert "Index Cond: (kr.source_id" not in plan, plan
    assert "= ANY ('{src-vis," in plan and "string_to_array" not in plan, plan
    assert "string_to_array($" in generic, generic


def test_pg_unbound_statements_bind_no_list(seeded):
    """Without the keyword neither statement goes through ``execute_ids``."""
    captured: list[str] = []
    original = psycopg.Connection.execute

    def recording(self, query, params=None, **options):
        if options.get("prepare") is False:
            captured.append(str(query))
        return original(self, query, params, **options)

    psycopg.Connection.execute = recording
    try:
        with seeded.connect() as db:
            UnifiedKgStore.weak_support_relation_rows(db, NB, ["ko-s"], 2, 24)
            UnifiedKgStore.relation_endpoint_name_rows(db, NB, ["kr-1"])
    finally:
        psycopg.Connection.execute = original
    assert captured == []
