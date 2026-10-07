"""PR-E2·E2-1 on real PostgreSQL: the weak-support store statements
(``weak_support_relation_rows`` / ``relation_endpoint_name_rows`` with
``allowed_source_ids`` or ``viewer_id``) and the overlay's
``object_support_source_rows`` -- same answers as the SQLite twin
(``tests/test_retrieval_leg_ceilings.py``) and EXPLAIN pins.

Pinned plans, on a corpus where OTHER notebooks hold Memory sources and
Memory-derived KG rows (so a plan that loses the notebook predicate reads
them), asserted positively:

* the canonical probe seeks ``pk_canonical_relations`` by (notebook_id,
  canonical_src) and the target's cluster members through a (notebook_id,
  canonical_id) index -- ``kc``'s Index Cond names both columns;
* the target's objects are probed by primary key (``ko.id = ANY(...)``) and,
  in the list form, the reverse index by object id
  (``Index Cond: (object_id = ko.id)``) -- never ``idx_kos_source`` (a seek
  per ceiling id) nor ``idx_kos_notebook`` (a walk of the notebook);
* the list form carries the ceiling as ONE folded constant array in the
  custom plan, and an opaque ``string_to_array($n)`` only in the generic
  control; the viewer form binds no list at all;
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
USER_A, USER_B = "u-e21-a", "u-e21-b"


def _seed(database) -> None:
    with database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        for user in (USER_A, USER_B):
            db.execute(
                "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
                "username,password_hash,password_salt,password_iterations) "
                "VALUES (%s,%s,%s,'user','active',%s,%s,%s,'','',0)",
                (user, f"{user}@example.test", user, NOW, NOW, user))
        for nb in [NB] + [f"nb-noise-{i}" for i in range(NOISE)]:
            db.execute(
                "INSERT INTO notebooks(id,name,created_at,updated_at) VALUES (%s,%s,%s,%s)",
                (nb, nb, NOW, NOW))
            db.execute(
                "INSERT INTO unified_kg_state(notebook_id,source_index_backfilled,"
                "updated_at) VALUES (%s,1,%s)", (nb, NOW))
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
            "content_md,created_at,updated_at) VALUES "
            "('mem-b',%s,%s,'ask_answer','confirmed','t','x',%s,%s)", (NB, USER_B, NOW, NOW))
        for nb in [NB] + [f"nb-noise-{i}" for i in range(NOISE)]:
            db.execute(
                "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,created_at,"
                "updated_at) VALUES (%s,%s,'v','markdown',NULL,%s,%s),"
                "(%s,%s,'m','memory',%s,%s,%s)",
                (f"{nb}-vis" if nb != NB else "src-vis", nb, NOW, NOW,
                 f"{nb}-mem" if nb != NB else "src-mb", nb,
                 "mem-b" if nb == NB else None, NOW, NOW))
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
            "concept_clusters, knowledge_relations, canonical_relations, sources, "
            "memory_items")


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


def _targets(db, **kwargs):
    return sorted(r["canonical_tgt"] for r in UnifiedKgStore.weak_support_relation_rows(
        db, NB, ["ko-s"], 2, 24, **kwargs))


@pytest.mark.parametrize("backfilled", [1, 0])
def test_pg_weak_support_matches_the_sqlite_twin(seeded, backfilled):
    _set_backfilled(seeded, backfilled)
    with seeded.connect() as db:
        unbounded = _targets(db)
        listed = _targets(db, allowed_source_ids=["src-vis"])
        as_a = _targets(db, viewer_id=USER_A)
        as_b = _targets(db, viewer_id=USER_B)
        as_nobody = _targets(db, viewer_id="")
        denied = _targets(db, allowed_source_ids=[])
        row = UnifiedKgStore.weak_support_relation_rows(
            db, NB, ["ko-s"], 2, 24, viewer_id=USER_A)[0]
    assert unbounded == ["K-good", "K-mem", "ko-t1", "ko-t2", "ko-t5"]
    assert listed == as_a == ["K-good", "ko-t1", "ko-t5"]
    assert as_b == unbounded
    assert as_nobody == as_a
    assert denied == []
    assert isinstance(row["sample_relation_ids"], str)
    assert set(row.keys()) == {"canonical_src", "edge_type", "canonical_tgt",
                               "source_count", "sample_relation_ids"}


@pytest.mark.parametrize("form", ["list", "viewer"])
def test_pg_weak_support_gate_sits_before_the_limit(seeded, form):
    """``K-good`` removed, as in the SQLite twin: the first row in sort order
    is ``K-mem``, outside the ceiling, so a gate applied after ``LIMIT 1``
    would return nothing."""
    with seeded.write() as db:
        db.execute(
            "DELETE FROM canonical_relations WHERE notebook_id=%s AND canonical_tgt='K-good'",
            (NB,))
    kwargs = {"allowed_source_ids": ["src-vis"]} if form == "list" else {"viewer_id": USER_A}
    with seeded.connect() as db:
        rows = UnifiedKgStore.weak_support_relation_rows(db, NB, ["ko-s"], 2, 1, **kwargs)
    assert [r["canonical_tgt"] for r in rows] == ["ko-t1"]


def test_pg_endpoint_names_carry_the_sample_source_and_honour_the_ceiling(seeded):
    with seeded.connect() as db:
        plain = UnifiedKgStore.relation_endpoint_name_rows(db, NB, ["kr-1", "kr-5"])
        sourced = UnifiedKgStore.relation_endpoint_name_rows(
            db, NB, ["kr-1", "kr-5"], with_source_id=True)
        listed = UnifiedKgStore.relation_endpoint_name_rows(
            db, NB, ["kr-1", "kr-5"], allowed_source_ids=["src-vis"])
        as_a = UnifiedKgStore.relation_endpoint_name_rows(
            db, NB, ["kr-1", "kr-5"], viewer_id=USER_A)
        as_b = UnifiedKgStore.relation_endpoint_name_rows(
            db, NB, ["kr-1", "kr-5"], viewer_id=USER_B)
        denied = UnifiedKgStore.relation_endpoint_name_rows(
            db, NB, ["kr-1"], allowed_source_ids=[])
    assert set(plain[0].keys()) == {"rid", "src_name", "tgt_name"}
    assert {(r["rid"], r["source_id"]) for r in sourced} == {
        ("kr-1", "src-vis"), ("kr-5", "src-mb")}
    assert [r["rid"] for r in listed] == ["kr-1"]
    assert [r["rid"] for r in as_a] == ["kr-1"]
    assert sorted(r["rid"] for r in as_b) == ["kr-1", "kr-5"]
    assert denied == []


@pytest.mark.parametrize("backfilled", [1, 0])
def test_pg_object_support_sources(seeded, backfilled):
    _set_backfilled(seeded, backfilled)
    with seeded.connect() as db:
        rows = UnifiedKgStore.object_support_source_rows(db, NB, ["ko-t1", "ko-t2", "ko-x"])
    assert sorted((r["object_id"], r["source_id"]) for r in rows) == [
        ("ko-t1", "src-vis"), ("ko-t2", "src-mb")]


def _recorded(database, call, *, unprepared: bool) -> tuple[str, object]:
    """The one statement of interest ``call`` sent (unprepared through
    ``execute_ids`` for a list binding, a plain execute otherwise)."""
    captured: list[tuple[str, object]] = []
    original = psycopg.Connection.execute

    def recording(self, query, params=None, **options):
        text = str(query)
        if (options.get("prepare") is False) == unprepared and (
            "canonical_relations" in text or "kr.id AS rid" in text
            or "AS object_id" in text or "kos.object_id, kos.source_id" in text
        ):
            captured.append((text, params))
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


def _assert_key_driven(plan: str) -> None:
    for table in ("canonical_relations", "concept_clusters", "knowledge_object_sources",
                  "knowledge_objects", "knowledge_relations"):
        assert f"Seq Scan on {table}" not in plan, plan
    assert "idx_kos_source" not in plan, plan
    assert "idx_kos_notebook" not in plan, plan


def _assert_probe_shape(plan: str) -> None:
    _assert_key_driven(plan)
    assert "pk_canonical_relations" in plan, plan
    # Cluster members: both columns of the (notebook_id, canonical_id) key.
    # (The OFFSET 0 subquery's columns print under the table's own name.)
    kc = [line for line in plan.split("\n") if "Index Cond" in line and "kc." in line]
    assert any("kc.notebook_id = " in line
               and "kc.canonical_id = canonical_relations.canonical_tgt" in line
               for line in kc), plan
    # The target's objects: primary key only.
    assert "Index Cond: (ko.id = ANY (" in plan, plan
    assert "Index Cond: (ko.notebook_id" not in plan, plan


@pytest.mark.parametrize("backfilled", [1, 0])
def test_pg_weak_support_list_probe_plan(seeded, backfilled):
    _set_backfilled(seeded, backfilled)
    sql, params = _recorded(seeded, lambda db: UnifiedKgStore.weak_support_relation_rows(
        db, NB, ["ko-s"], 2, 24, allowed_source_ids=CEILING), unprepared=True)
    plan = _custom_plan(seeded, sql, params)
    generic = _generic_plan(seeded, sql, params)
    _assert_probe_shape(plan)
    if backfilled:
        assert "Index Cond: (kos.object_id = ko.id)" in plan, plan
    else:
        assert "jsonb_array_elements" in plan, plan
    # Custom plan: the ceiling is one folded constant array.  Generic control:
    # an opaque parameter.
    assert "= ANY ('{src-pad-00000," in plan and "string_to_array" not in plan, plan
    assert plan.count("= ANY ('{src-pad-00000,") == 1, plan
    assert "string_to_array($" in generic, generic


def test_pg_weak_support_viewer_probe_plan(seeded):
    sql, params = _recorded(seeded, lambda db: UnifiedKgStore.weak_support_relation_rows(
        db, NB, ["ko-s"], 2, 24, viewer_id=USER_A), unprepared=False)
    assert "string_to_array" not in sql
    plan = _custom_plan(seeded, sql, params)
    _assert_probe_shape(plan)
    # ``sources`` / ``memory_items`` are a few rows here, so they are scanned;
    # what must exist is an index path to them (the memory_sql pins' method).
    with seeded.connect() as db:
        db.execute("SET LOCAL enable_seqscan = off")
        rows = db.execute(f"EXPLAIN (COSTS OFF) {sql}", params).fetchall()
    forced = "\n".join(str(row["QUERY PLAN"]) for row in rows)
    assert "Seq Scan" not in forced, forced
    assert "pk_sources" in forced and "pk_memory_items" in forced, forced


@pytest.mark.parametrize("form", ["list", "viewer"])
def test_pg_endpoint_name_plan(seeded, form):
    kwargs = {"allowed_source_ids": CEILING} if form == "list" else {"viewer_id": USER_A}
    sql, params = _recorded(seeded, lambda db: UnifiedKgStore.relation_endpoint_name_rows(
        db, NB, ["kr-1", "kr-5"], **kwargs), unprepared=form == "list")
    plan = _custom_plan(seeded, sql, params)
    _assert_key_driven(plan)
    # The sample relations' primary keys drive; the gate only filters.
    assert "(kr.id = ANY ('{kr-1,kr-5}'::text[]))" in plan, plan
    assert "Index Cond: (kr.source_id" not in plan, plan
    if form == "list":
        assert "= ANY ('{src-pad-00000," in plan and "string_to_array" not in plan, plan
        assert "string_to_array($" in _generic_plan(seeded, sql, params)


@pytest.mark.parametrize("backfilled", [1, 0])
def test_pg_object_support_sources_plan(seeded, backfilled):
    _set_backfilled(seeded, backfilled)
    sql, params = _recorded(seeded, lambda db: UnifiedKgStore.object_support_source_rows(
        db, NB, ["ko-t1", "ko-t2"]), unprepared=True)
    plan = _custom_plan(seeded, sql, params)
    _assert_key_driven(plan)
    if backfilled:
        assert "Index Cond: (kos.object_id = ANY (" in plan, plan
    else:
        assert "Index Cond: (ko.id = ANY (" in plan, plan


def _pg_seam(postgres_settings, tmp_path, *, own_memory: bool):
    """The SQLite twin's seam scenario on a PostgreSQL repository: written
    through ``store_kg`` (the reverse index is filled by the real writer)."""
    from app.core.request_context import reset_request_user, set_request_user
    from app.models.schemas import NotebookCreate
    from app.repositories.postgres.repository import PostgresRepository
    from app.services.embedding import FakeEmbedder
    from app.services.source_scope import source_scope_context
    from tests import test_retrieval_leg_ceilings as twin
    from tests.model_testkit import bind_all_embedding_clients

    postgres_settings.storage_dir = str(tmp_path / "pg-storage")
    postgres_settings.event_log_enabled = False
    postgres_settings.llm_log_enabled = False
    repository = PostgresRepository(postgres_settings)
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    repository.settings.graph_ppr_enabled = False
    try:
        a = repository.create_user("a00000001", "pw123456")
        b = repository.create_user("b00000002", "pw123456")
        token = set_request_user(a)
        try:
            nb = repository.create_notebook(NotebookCreate(name="seam")).id
            repository.add_member(nb, b.id)
        finally:
            reset_request_user(token)
        with repository._write() as db:
            db.execute(
                "INSERT INTO sources(id,notebook_id,title,source_type,created_at,updated_at) "
                "VALUES ('src-vis',%s,'v','markdown',%s,%s)", (nb, NOW, NOW))
            if own_memory:
                db.execute(
                    "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
                    "content_md,created_at,updated_at) VALUES "
                    "('mem-b',%s,%s,'ask_answer','confirmed','t','x',%s,%s)",
                    (nb, b.id, NOW, NOW))
                db.execute(
                    "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,"
                    "created_at,updated_at) VALUES ('src-mb',%s,'m','memory','mem-b',%s,%s)",
                    (nb, NOW, NOW))
        _store_seam(repository, nb, own_memory, twin)
        scope = (twin._freeze(b.id, hidden=["src-mb"]) if own_memory else twin._freeze(a.id))
        with source_scope_context(nb, scope):
            _c, block, id_map, _h, _p = repository.retrieval.mixed_chunk_candidates(
                nb, twin.SEAM_QUERY, twin.SEAM_QUERY, [twin.SEAM_QUERY])
        return block, id_map
    finally:
        repository.close()


def _store_seam(repository, nb, own_memory, twin):
    def concept(local, name, sid, evidence=True):
        return {"local_id": local, "object_type": "concept",
                "payload": {"name": name, "section_path": "1"},
                "evidence": [twin._ev(sid, f"{name} in {sid}")] if evidence else []}

    repository.store_kg(nb, "src-vis", [
        concept("A", "Mixture-of-Experts (MoE)", "src-vis"),
        concept("B", "Router balance", "src-vis"),
        concept("C", "Capacity factor", "src-vis", evidence=False),
    ], [twin._edge("A", "B", "src-vis"), twin._edge("A", "C", "src-vis")])
    if own_memory:
        repository.store_kg(nb, "src-mb", [
            concept("A", "Mixture-of-Experts (MoE)", "src-mb"),
            concept("Z", "ZEBRAQUARTZ plan", "src-mb"),
        ], [twin._edge("A", "Z", "src-mb")])
    with repository._connect() as db:
        assert db.execute(
            "SELECT count(*) AS c FROM knowledge_object_sources WHERE notebook_id=%s", (nb,)
        ).fetchone()["c"] > 0


@pytest.mark.parametrize("own_memory", [False, True])
def test_pg_mix_seam_is_byte_identical_to_master(postgres_settings, tmp_path, own_memory):
    """No one else's Memory (none at all, or only the asker's own): the
    all-selected overlay through ``mixed_chunk_candidates`` is master's block,
    evidence-less ``Capacity factor`` and its chain line included."""
    from tests import test_retrieval_leg_ceilings as twin

    block, id_map = _pg_seam(postgres_settings, tmp_path, own_memory=own_memory)
    assert block == (twin._MASTER_OWN if own_memory else twin._MASTER_PLAIN)
    assert any(v["name"] == "Capacity factor" for v in id_map.values())


def test_pg_unbound_statements_bind_no_list(seeded):
    """Without the keywords neither statement goes through ``execute_ids``,
    and the viewer form binds no list either."""
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
            UnifiedKgStore.weak_support_relation_rows(db, NB, ["ko-s"], 2, 24, viewer_id=USER_A)
            UnifiedKgStore.relation_endpoint_name_rows(db, NB, ["kr-1"], viewer_id=USER_A)
    finally:
        psycopg.Connection.execute = original
    assert captured == []
