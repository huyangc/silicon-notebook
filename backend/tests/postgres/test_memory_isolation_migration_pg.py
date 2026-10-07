"""Ruling-M1 cleanup migration ``0067_memory_kg_isolation.sql`` and the
post-readiness rebuild on real PostgreSQL (SQLite twin:
``tests/test_memory_isolation_migration.py`` / ``test_memory_isolation_rebuild.py``;
both seed ``memory_isolation_cases.py``).

Pinned: upgrade and fresh database; idempotent re-execution of the frozen SQL
(no re-queue of a finished notebook); the rebuild pass through the ordinary
maintenance slot sets the marker and purges bridge-id merge candidates;
real-planner EXPLAIN pins for the marker store's SQL and for every 0067
statement that must reach a wide table through the working tables (a
public library outside and inside F, size raised with
``MKI_PIN_BASE_OBJECTS``); the summary counts equal the SQLite twin's.
"""
from __future__ import annotations

import os
import time
from pathlib import Path

import psycopg
import pytest
from psycopg.rows import dict_row

from app.repositories.postgres.memory_isolation_store import MemoryIsolationStore
from app.repositories.postgres.migrator import PostgresMigrator
from tests import memory_isolation_cases as cases

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_memory_isolation_migration"),
]

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "app" / "repositories" / "postgres" / "migrations"
    / "0067_memory_kg_isolation.sql"
)


def _roll_back_to_66(database) -> None:
    with database.write() as db:
        db.execute("ALTER TABLE unified_kg_state DROP COLUMN memory_isolation_version")
        db.execute("DELETE FROM silicon_schema_migrations WHERE version = 67")


def _snapshot(database):
    with database.connect() as db:
        return cases.snapshot(db)


def _marker(database, notebook_id):
    with database.connect() as db:
        row = db.execute(
            "SELECT memory_isolation_version AS m FROM unified_kg_state "
            "WHERE notebook_id=%s", (notebook_id,),
        ).fetchone()
    return int(row["m"])


@pytest.fixture
def upgraded(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 67
    with postgres_database.write() as db:
        cases.seed(db, postgres=True)
    before = _snapshot(postgres_database)
    _roll_back_to_66(postgres_database)
    assert PostgresMigrator(postgres_database).migrate() == 67
    return postgres_database, before, _snapshot(postgres_database)


def test_upgrade_removes_every_pre_isolation_memory_derived_row(upgraded):
    _database, before, after = upgraded
    cases.assert_isolated(before, after)


def test_summary_counts_match_the_shared_world(postgres_database):
    """The counts 0067's summary line reports (RAISE LOG goes to the server
    log, out of a test's reach) are read from its working tables inside the
    migration's own transaction, then rolled back -- the same numbers the
    SQLite twin logs (test_migration_logs_counts_only)."""
    assert PostgresMigrator(postgres_database).migrate() == 67
    with postgres_database.write() as db:
        cases.seed(db, postgres=True)
    _roll_back_to_66(postgres_database)
    body = MIGRATION.read_text(encoding="utf-8")
    body = body[: body.index("DO $mki$")]
    with psycopg.connect(postgres_database.settings.database_url) as raw:
        raw.execute(body, prepare=False)
        counts = {
            table: raw.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
            for table in ("mki_f", "mki_g", "mki_promo", "mki_conflict_shared",
                          "mki_conflict_shared_edges", "mki_cross")
        }
        scanned = {r[0] for r in raw.execute("SELECT notebook_id FROM mki_scan")}
        raw.rollback()
    # the evidence scan covers F's non-public notebooks and its public
    # libraries whose reverse index is not attested (PUBF: flag 0; PUBR: a
    # backfill still running); the attested PUBA keeps the reverse index only
    assert scanned == set(cases.EVIDENCE_SCANNED)
    assert counts == {
        "mki_f": len(cases.F_NOTEBOOKS),
        "mki_g": len(cases.SEED_CHECKED),
        "mki_promo": len(cases.PROMOTIONS_REJECTED),
        "mki_conflict_shared": cases.CONFLICTS_APPLIED_ON_SHARED,
        "mki_conflict_shared_edges": cases.CONFLICTS_APPLIED_ON_SHARED_EDGES,
        "mki_cross": cases.CROSS_OWNER_STRIPPED,
    }


def test_upgrade_marks_only_notebooks_holding_a_memory_source(upgraded):
    database, _before, _after = upgraded
    for notebook in cases.F_NOTEBOOKS:
        assert _marker(database, notebook) == 0, notebook
    # outside F with clusters -- public libraries and the copy without a state
    # row included: marked 2 for the worker's post-readiness check
    for notebook in cases.SEED_CHECKED:
        assert _marker(database, notebook) == 2, notebook


def test_reexecuting_the_frozen_sql_changes_nothing(upgraded):
    database, _before, after = upgraded
    sql = MIGRATION.read_text(encoding="utf-8")
    with database.write() as db:
        db.execute(sql, prepare=False)
    assert _snapshot(database) == after
    assert _marker(database, cases.NB_F) == 0
    with database.write() as db:
        db.execute(
            "UPDATE unified_kg_state SET memory_isolation_version=1 "
            "WHERE notebook_id=%s", (cases.NB_F,),
        )
        # what the rebuild regenerates must survive a replay
        db.execute(
            "INSERT INTO kg_analysis_artifacts(notebook_id,kind,payload,created_at) "
            "VALUES (%s,'boards','{}'::jsonb,now())", (cases.NB_F,))
        db.execute(
            "INSERT INTO kg_rebuild_checkpoint(notebook_id,input_version,stage,"
            "item_key,payload,created_at) VALUES (%s,'v2','merge_review','k2',"
            "'{}'::jsonb,now())", (cases.NB_F,))
    settled = _snapshot(database)
    assert settled["kg_analysis_artifacts"] and settled["kg_rebuild_checkpoint"]
    with database.write() as db:
        db.execute(sql, prepare=False)
    assert _marker(database, cases.NB_F) == 1
    assert _snapshot(database) == settled


def test_fresh_database_defaults_the_marker_to_isolated(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 67
    with postgres_database.connect() as db:
        column = db.execute(
            "SELECT is_nullable, column_default FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name='unified_kg_state' "
            "AND column_name='memory_isolation_version'"
        ).fetchone()
    assert (column["is_nullable"], column["column_default"]) == ("NO", "1")


def test_rebuild_pass_sets_the_marker_through_the_ordinary_slot(
    upgraded, postgres_settings
):
    from app.repositories.postgres.repository import PostgresRepository
    from app.services.memory_isolation_rebuild import MemoryIsolationRebuild

    database, _before, _after = upgraded
    repository = PostgresRepository(postgres_settings)
    try:
        seen: list[dict] = []
        log = repository._runtime.event_log
        original = log.emit

        def recording(event, *args, **kwargs):
            seen.append(dict(event))
            return original(event, *args, **kwargs)

        log.emit = recording
        h9 = next(c for c in repository.checkup.run(cases.NB_F).checks if c.code == "H9")
        assert h9.count == 1
        service = MemoryIsolationRebuild.for_repository(repository)
        assert service.pending_notebook_ids() == sorted(cases.F_NOTEBOOKS)
        assert service.seed_check_notebook_ids() == sorted(cases.SEED_CHECKED)
        texts, mentions, merge = _gx_rows(database)
        assert any(cases.GX_SECRET in t for t in texts)
        assert cases.MERGE_CANDIDATES_STALE <= merge
        assert service.run_pass()["completed"] == len(cases.QUEUED)
        for notebook in (*cases.QUEUED, *cases.SEED_CHECKED):
            assert _marker(database, notebook) == 1, notebook
        checks = {e["notebook_id"]: e.get("signal") for e in seen
                  if e["kind"] == "memory_isolation_seed_checked"}
        assert checks == {nb: sig or "" for nb, sig in cases.SIGNALS.items()}
        # the review's GX notebook: none of the deleted Memory's text is left
        # in its published generations, its stale mention row and the merge
        # candidates naming nothing that exists are gone
        texts, mentions, merge = _gx_rows(database)
        assert not any(cases.GX_SECRET in t for t in texts), texts
        assert "ko-gx-memclaim-gone" not in mentions
        assert not (cases.MERGE_CANDIDATES_STALE & merge) and "mc-gx-keep" in merge
        assert "mc-gx-bridge" in merge  # bridge-shaped: a decision, kept
        # the curator's decisions of GK survive queueing and the rebuild, and
        # the confirmed merge holds in the published generation
        with database.connect() as db:
            decided = {r["id"]: r["status"] for r in db.execute(
                "SELECT id, status FROM concept_merge_candidates WHERE notebook_id=%s",
                (cases.NB_GK,)).fetchall()}
            members = {r["m"]: r["c"] for r in db.execute(
                "SELECT member_object_id AS m, canonical_id AS c FROM concept_clusters "
                "WHERE notebook_id=%s AND generation = (SELECT cluster_generation "
                "FROM unified_kg_state WHERE notebook_id=%s)",
                (cases.NB_GK, cases.NB_GK)).fetchall()}
        assert {k: v for k, v in decided.items()
                if k in {d[0] for d in cases.DECIDED}} == {d[0]: d[4] for d in cases.DECIDED}
        assert members["ko-gk-1"] == members["ko-gk-2"], members
        assert service.pending_count() == 0
        kinds = [e["kind"] for e in seen
                 if e["kind"].startswith("memory_isolation_")
                 and e.get("notebook_id") == cases.NB_F]
        assert kinds == ["memory_isolation_rebuild_started",
                         "memory_isolation_rebuild_completed"]
        h9 = next(c for c in repository.checkup.run(cases.NB_F).checks if c.code == "H9")
        assert h9.count == 0
        # H10: the approved generic proposal of a Memory object, read-only --
        # neither the migration nor the rebuild touched it.
        h10 = next(c for c in repository.checkup.run(cases.NB_F).checks
                   if c.code == "H10")
        assert (h10.count, h10.fix) == (1, "none")
        # the bridge-id merge candidate the SQL could not recognise is gone
        with database.connect() as db:
            left = {r["id"] for r in db.execute(
                "SELECT id FROM concept_merge_candidates WHERE notebook_id=%s",
                (cases.NB_F,)).fetchall()}
        assert not (cases.MERGE_CANDIDATES_BRIDGE & left)
    finally:
        repository.close()


def test_the_pass_leaves_shared_only_totals_even_when_the_input_looks_unchanged_pg(
    upgraded, postgres_settings
):
    """Twin of the SQLite case: the queued notebook is reclustered even when
    its stored input version matches, so ``unified_kg_status`` counts the
    shared graph only afterwards."""
    from app.repositories.postgres.repository import PostgresRepository
    from app.services.memory_isolation_rebuild import MemoryIsolationRebuild

    database, _before, _after = upgraded
    repository = PostgresRepository(postgres_settings)
    try:
        lifecycle = repository._runtime.knowledge_lifecycle
        service = MemoryIsolationRebuild.for_repository(repository)
        service._purge_bridge_candidates(cases.NB_F)
        with database.write() as db:
            db.execute(
                "UPDATE unified_kg_state SET cluster_input_version=%s, object_count=999, "
                "relation_count=999, cluster_count=GREATEST(cluster_count, 1) "
                "WHERE notebook_id=%s",
                (lifecycle._cluster_input_version(cases.NB_F), cases.NB_F),
            )
        service.run_pass()
        with database.connect() as db:
            shared_objects = db.execute(
                "SELECT COUNT(*) AS c FROM knowledge_objects ko JOIN sources s "
                "ON s.id = ko.source_id WHERE ko.notebook_id=%s "
                "AND ko.status<>'deprecated' AND s.source_type<>'memory'",
                (cases.NB_F,)).fetchone()["c"]
            shared_relations = db.execute(
                "SELECT COUNT(*) AS c FROM knowledge_relations kr JOIN sources s "
                "ON s.id = kr.source_id WHERE kr.notebook_id=%s "
                "AND s.source_type<>'memory'",
                (cases.NB_F,)).fetchone()["c"]
        status = repository.unified_kg_status(cases.NB_F)
        assert _marker(database, cases.NB_F) == 1
        assert (status["objects"], status["relations"]) == (shared_objects, shared_relations)
    finally:
        repository.close()


def _gx_rows(database):
    with database.connect() as db:
        texts = [r["t"] for r in db.execute(
            "SELECT canonical_name || ' ' || COALESCE(canonical_description, '') AS t "
            "FROM concept_clusters WHERE notebook_id = %s AND generation = "
            "(SELECT cluster_generation FROM unified_kg_state WHERE notebook_id = %s)",
            (cases.NB_GX, cases.NB_GX)).fetchall()]
        texts += [r["t"] for r in db.execute(
            "SELECT COALESCE(title, '') || ' ' || COALESCE(summary, '') AS t "
            "FROM communities WHERE notebook_id = %s AND generation = "
            "(SELECT community_generation FROM unified_kg_state WHERE notebook_id = %s)",
            (cases.NB_GX, cases.NB_GX)).fetchall()]
        mentions = {r["c"] for r in db.execute(
            "SELECT claim_object_id AS c FROM mention_edges WHERE notebook_id = %s",
            (cases.NB_GX,)).fetchall()}
        merge = {r["id"] for r in db.execute(
            "SELECT id FROM concept_merge_candidates WHERE notebook_id = %s",
            (cases.NB_GX,)).fetchall()}
    return texts, mentions, merge


def test_every_signal_is_found_on_its_own_notebook_in_pages(upgraded):
    """Same statements as the SQLite twin: each G notebook gets exactly its
    own signal at every page size -- C's BUILDING generation (a name no member
    carries) is not read, the copy's member naming a live object is not
    stale."""
    database, _before, _after = upgraded
    with database.connect() as db:
        for page in (1, 2, 5000):
            for notebook, signal in cases.SIGNALS.items():
                assert MemoryIsolationStore.seed_check_signal(
                    db, notebook, page_size=page) == signal, (notebook, page)
                assert MemoryIsolationStore.has_dangling_seed(
                    db, notebook, page_size=page) is (signal == "seed"), (notebook, page)
            for notebook, stale in ((cases.NB_GM, True), (cases.NB_GC, True),
                                    (cases.NB_GX, True), (cases.NB_C, False),
                                    (cases.NB_COPY, False), (cases.NB_GD, False)):
                assert MemoryIsolationStore.has_stale_reference(
                    db, notebook, page_size=page) is stale, (notebook, page)


def test_the_census_is_read_only_and_matches_the_check(postgres_database):
    """scripts/memory_isolation_census.py on a pre-upgrade (v66) database:
    0067's G computed live, the same signals, nothing written (the connection
    is read-only: a write attempt fails)."""
    import importlib.util

    assert PostgresMigrator(postgres_database).migrate() == 67
    with postgres_database.write() as db:
        cases.seed(db, postgres=True)
    _roll_back_to_66(postgres_database)
    before = _snapshot(postgres_database)
    path = Path(__file__).resolve().parents[3] / "scripts" / "memory_isolation_census.py"
    spec = importlib.util.spec_from_file_location("memory_isolation_census_pg", path)
    census = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(census)
    url = postgres_database.settings.database_url
    result = census.census(url)
    assert result["phase"] == "pre-upgrade"
    rows = result["notebooks"]
    assert sorted(r["notebook_id"] for r in rows if r["set"] == "F") == sorted(
        cases.F_NOTEBOOKS)
    assert {r["notebook_id"]: r["signal"] for r in rows if r["set"] == "G"} == cases.SIGNALS
    every = census.census(url, all_signals=True)
    assert {r["notebook_id"]: {k for k, v in r["signals"].items() if v}
            for r in every["notebooks"] if r["set"] == "G"}[cases.NB_GX] == {
        "dirty", "stale_reference"}
    assert census.summarise(every)["g_dirty_only"] == 2
    assert _snapshot(postgres_database) == before
    from app.repositories.postgres.memory_isolation_store import read_only_connection

    with read_only_connection(url) as db:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            db.execute("DELETE FROM notebooks")


def test_approved_memory_promotion_count_is_index_driven(postgres_database):
    """H10's read reaches promotion_candidates through
    idx_promotion_nb (notebook_id, status) and each approved row's object
    by primary key -- never a scan of either table."""
    from app.repositories.postgres import memory_isolation_store as store

    assert PostgresMigrator(postgres_database).migrate() == 67
    with postgres_database.write() as db:
        cases.seed(db, postgres=True)
        db.execute(
            "INSERT INTO promotion_candidates(id,notebook_id,object_id,object_type,"
            "status,created_at,updated_at) SELECT 'pr-pin-'||g, %s, 'ko-pin-'||g, "
            "'concept', CASE WHEN g %% 10 = 0 THEN 'approved' ELSE 'rejected' END, "
            "now(), now() FROM generate_series(1, 5000) g", (cases.NB_C,))
        # enough objects that a scan of knowledge_objects is a real choice
        db.execute("SET LOCAL statement_timeout = 0")
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,"
            "source_id,payload,evidence,created_at,updated_at) SELECT 'ko-pin-'||g, "
            "%s, 'concept', 'approved', 'src-c-doc', '{}'::jsonb, '[]'::jsonb, "
            "now(), now() FROM generate_series(1, 60000) g", (cases.NB_C,))
        db.execute("ANALYZE promotion_candidates")
        db.execute("ANALYZE knowledge_objects")
        plan = _plan(db, store._APPROVED_MEMORY_PROMOTIONS_SQL, (cases.NB_F, "memory"))
    assert "idx_promotion_nb" in plan, plan
    assert "Seq Scan" not in plan, plan
    assert "pk_knowledge_objects" in plan, plan
    with postgres_database.connect() as db:
        assert store.MemoryIsolationStore.approved_memory_promotion_count(
            db, cases.NB_F) == 1
        assert store.MemoryIsolationStore.approved_memory_promotion_count(
            db, cases.NB_C) == 0


# ------------------------------------------------------------ EXPLAIN pins
def _plan(db, sql: str, params: tuple = ()) -> str:
    rows = db.execute(f"EXPLAIN (COSTS OFF) {sql}", params).fetchall()
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


def _seed_state_rows(database, count: int) -> None:
    with database.write() as db:
        db.execute("SET LOCAL statement_timeout = 0")
        db.execute(
            "INSERT INTO notebooks(id,name,created_at,updated_at) "
            "SELECT 'nb-pin-'||g, 'p', now(), now() FROM generate_series(1,%s) g",
            (count,),
        )
        db.execute(
            "INSERT INTO unified_kg_state(notebook_id, updated_at, "
            "memory_isolation_version) "
            "SELECT 'nb-pin-'||g, now(), CASE WHEN g %% 500 = 0 THEN 0 ELSE 1 END "
            "FROM generate_series(1,%s) g",
            (count,),
        )
        db.execute("ANALYZE notebooks")
        db.execute("ANALYZE unified_kg_state")


def test_marker_reads_and_write_use_the_state_primary_key(postgres_database):
    assert PostgresMigrator(postgres_database).migrate() == 67
    _seed_state_rows(postgres_database, 30000)
    from app.repositories.postgres import memory_isolation_store as store

    with postgres_database.write() as db:
        # the real planner; the pending read is the store's own text
        # (_PENDING_FROM, NOTEBOOK_LIVE_SQL included), not a copy of it
        is_pending = _plan(
            db, "SELECT memory_isolation_version FROM unified_kg_state "
            "WHERE notebook_id = %s", ("nb-pin-50",))
        mark = _plan(
            db, "UPDATE unified_kg_state SET memory_isolation_version = 1 "
            "WHERE notebook_id = %s AND memory_isolation_version = 0", ("nb-pin-50",))
        forget = _plan(
            db, "UPDATE unified_kg_state SET cluster_input_version = '' "
            "WHERE notebook_id = %s AND memory_isolation_version = 0", ("nb-pin-50",))
        pending = _plan(
            db, f"SELECT u.notebook_id {store._PENDING_FROM} ORDER BY u.notebook_id")
    assert "pk_unified_kg_state" in is_pending, is_pending
    assert "pk_unified_kg_state" in mark, mark
    assert "pk_unified_kg_state" in forget, forget
    # one pass over the state table, notebooks reached by primary key
    assert pending.count(" on unified_kg_state") == 1, pending
    assert "pk_notebooks" in pending, pending
    with postgres_database.connect() as db:
        assert MemoryIsolationStore.pending_count(db) == 60
        assert len(MemoryIsolationStore.pending_notebook_ids(db)) == 60
        assert MemoryIsolationStore.is_pending(db, "nb-pin-500") is True
        assert MemoryIsolationStore.is_pending(db, "nb-pin-501") is False


# ---------------------------------------------- measurement + selection plans
def _seed_large(database, *, f_notebooks: int, objects_per_f: int,
                base_objects: int) -> None:
    """A deployment-shaped world: one large base-tier library (outside F
    unless ``_seed_memory_into_base`` gives it a Memory source) and
    ``f_notebooks`` personal notebooks, each with a document
    source, 20 Memory sources, ``objects_per_f`` objects (5% Memory-derived),
    one cluster row per object (clusters of 4, every 20th cluster mixed),
    mention edges, co-mentions, canonical relations, communities, merge
    candidates and chunks. Pure SQL (generate_series), so millions of rows
    are seconds."""
    per_f_mem = max(1, objects_per_f // 20)
    # a document yields tens of objects: one public-library source per 50
    # objects (at least 200), so per-source statistics look like a library's
    base_sources = max(200, base_objects // 50)
    with database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) VALUES "
            "('u-big','u@x','u','user','active',now(),now(),'b00000001','','',0)")
        db.execute(
            "INSERT INTO notebooks(id,name,tier,created_by,created_at,updated_at) "
            "SELECT 'nbf-'||f, 'f', 'personal', 'u-big', now(), now() "
            "FROM generate_series(1,%s) f", (f_notebooks,))
        db.execute(
            "INSERT INTO notebooks(id,name,tier,created_by,created_at,updated_at) "
            "VALUES ('nb-base','base','base','u-big',now(),now())")
        # the public library's reverse index is attested complete (a library
        # built after 0020); world_c_unattested clears the flag
        db.execute(
            "INSERT INTO unified_kg_state(notebook_id,updated_at,kg_mutation_seq,"
            "source_index_backfilled) SELECT id, now(), 5, "
            "CASE WHEN id = 'nb-base' THEN 1 ELSE 0 END "
            "FROM notebooks WHERE id LIKE 'nbf-%%' OR id='nb-base'")
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
            "content_md,created_at,updated_at) "
            "SELECT 'mem-'||f||'-'||m, 'nbf-'||f, 'u-big','ask_answer','confirmed','t',"
            "'c',now(),now() FROM generate_series(1,%s) f, generate_series(1,20) m",
            (f_notebooks,))
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,created_at,"
            "updated_at) SELECT 'sm-'||f||'-'||m, 'nbf-'||f, 't', 'memory', "
            "'mem-'||f||'-'||m, now(), now() "
            "FROM generate_series(1,%s) f, generate_series(1,20) m", (f_notebooks,))
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,created_at,updated_at) "
            "SELECT 'sd-'||f, 'nbf-'||f, 'd', 'upload', now(), now() "
            "FROM generate_series(1,%s) f", (f_notebooks,))
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,created_at,updated_at) "
            "SELECT 'sb-'||s, 'nb-base', 'd', 'upload', now(), now() "
            "FROM generate_series(1,%s) s", (base_sources,))
        # personal-notebook objects: every 20th is Memory-derived, and every
        # 40th shared object carries one Memory evidence item (manual merge)
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,source_id,"
            "payload,evidence,created_at,updated_at) "
            "SELECT 'ko-'||f||'-'||o, 'nbf-'||f, 'concept', 'approved', "
            "CASE WHEN o %% 20 = 0 THEN 'sm-'||f||'-'||(1 + o %% 20) ELSE 'sd-'||f END, "
            "jsonb_build_object('name','n'||o), "
            "CASE WHEN o %% 40 = 1 THEN jsonb_build_array("
            "jsonb_build_object('source_id','sd-'||f),"
            "jsonb_build_object('source_id','sm-'||f||'-1')) "
            "ELSE jsonb_build_array(jsonb_build_object('source_id',"
            "CASE WHEN o %% 20 = 0 THEN 'sm-'||f||'-'||(1 + o %% 20) ELSE 'sd-'||f END)) END, "
            "now(), now() FROM generate_series(1,%s) f, generate_series(1,%s) o",
            (f_notebooks, objects_per_f))
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,source_id,"
            "payload,evidence,created_at,updated_at) "
            "SELECT 'kb-'||o, 'nb-base', 'concept', 'approved', 'sb-'||(1 + o %% %s), "
            "jsonb_build_object('name','b'||o), "
            "jsonb_build_array(jsonb_build_object('source_id','sb-'||(1 + o %% %s))), "
            "now(), now() FROM generate_series(1,%s) o",
            (base_sources, base_sources, base_objects))
        db.execute(
            "INSERT INTO knowledge_object_sources(object_id,source_id,notebook_id) "
            "SELECT ko.id, e->>'source_id', ko.notebook_id FROM knowledge_objects ko, "
            "jsonb_array_elements(ko.evidence) e ON CONFLICT DO NOTHING")
        db.execute(
            "INSERT INTO concept_clusters(id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,object_type,created_at,generation) "
            "SELECT 'cc-'||ko.id, ko.notebook_id, "
            "'K-'||ko.notebook_id||'-'||(regexp_replace(ko.id,'^.*-','')::int / 4), ko.id, 'n', "
            "'concept', now(), 0 FROM knowledge_objects ko")
        db.execute(
            "INSERT INTO mention_edges(notebook_id,claim_object_id,concept_canonical_id) "
            "SELECT ko.notebook_id, ko.id, "
            "'K-'||ko.notebook_id||'-'||((regexp_replace(ko.id,'^.*-','')::int / 4) + 1) "
            "FROM knowledge_objects ko WHERE ko.notebook_id LIKE 'nbf-%%'")
        db.execute(
            "INSERT INTO canonical_relations(notebook_id,canonical_src,edge_type,"
            "canonical_tgt,updated_at) SELECT DISTINCT cc.notebook_id, cc.canonical_id, "
            "'related_to', cc.canonical_id||'-t', now() FROM concept_clusters cc")
        db.execute(
            "INSERT INTO concept_comentions(notebook_id,canonical_a,canonical_b) "
            "SELECT DISTINCT cc.notebook_id, cc.canonical_id, cc.canonical_id||'-t' "
            "FROM concept_clusters cc WHERE cc.notebook_id LIKE 'nbf-%%'")
        db.execute(
            "INSERT INTO communities(id,notebook_id,level,member_ids,size,title,created_at) "
            "SELECT 'cm-'||x.notebook_id||'-'||x.bucket, x.notebook_id, 0, '[]'::jsonb, 1, "
            "'t', now() FROM (SELECT DISTINCT notebook_id, "
            "(regexp_replace(member_object_id,'^.*-','')::int / 100) AS bucket "
            "FROM concept_clusters) x")
        db.execute(
            "INSERT INTO community_members(canonical_id,notebook_id,level,community_id) "
            "SELECT DISTINCT cc.canonical_id, cc.notebook_id, 0, "
            "'cm-'||cc.notebook_id||'-'||(regexp_replace(cc.member_object_id,'^.*-','')::int / 100) "
            "FROM concept_clusters cc ON CONFLICT DO NOTHING")
        db.execute(
            "INSERT INTO chunks(id,notebook_id,source_id,text,created_at) "
            "SELECT 'ch-'||f||'-'||c, 'nbf-'||f, 'sd-'||f, 't', now() "
            "FROM generate_series(1,%s) f, generate_series(1,200) c", (f_notebooks,))
        for table in ("knowledge_objects", "knowledge_object_sources", "concept_clusters",
                      "mention_edges", "canonical_relations", "concept_comentions",
                      "communities", "community_members", "chunks", "sources",
                      "memory_items", "unified_kg_state", "notebooks"):
            db.execute(f"ANALYZE {table}")


_ROW_TABLES = (
    "knowledge_objects", "knowledge_object_sources", "concept_clusters",
    "mention_edges", "canonical_relations", "concept_comentions", "communities",
    "community_members", "chunks", "sources",
)


def _counts(database) -> dict:
    with database.connect() as db:
        return {
            t: db.execute(f"SELECT COUNT(*) AS n FROM {t}").fetchone()["n"]
            for t in _ROW_TABLES
        }


def _statements() -> list[str]:
    """0067 split into its statements (comment lines dropped; the DO block
    is the one statement containing semicolons)."""
    body = "\n".join(
        line for line in MIGRATION.read_text(encoding="utf-8").splitlines()
        if not line.lstrip().startswith("--")
    )
    do_at = body.index("DO $mki$")
    tail = body[do_at:]
    do_end = tail.index("$mki$;", len("DO $mki$")) + len("$mki$;")
    head = [s.strip() for s in body[:do_at].split(";\n") if s.strip()]
    rest = [s.strip() for s in tail[do_end:].split(";\n") if s.strip()]
    return head + [tail[:do_end].rstrip(";")] + rest


#: statement prefix -> the WIDE tables it must not read with a sequential
#: scan at the default pin scale (200 000 public-library objects): there a
#: scan and the working-table path differ by orders of magnitude.
_NO_SEQ_SCAN = {
    "CREATE TEMP TABLE mki_m ": ("knowledge_objects",),
    "CREATE TEMP TABLE mki_a ": ("concept_clusters", "knowledge_objects"),
    "CREATE TEMP TABLE mki_g ": ("concept_clusters",),
    "CREATE TEMP TABLE mki_comm ": ("community_members",),
    "CREATE TEMP TABLE mki_mixed ": ("knowledge_objects",),
    "CREATE TEMP TABLE mki_cross ": ("knowledge_objects",),
    "CREATE TEMP TABLE mki_new_evidence ": ("knowledge_objects",),
    "DELETE FROM concept_clusters": ("concept_clusters",),
    "UPDATE knowledge_objects": ("knowledge_objects",),
    "DELETE FROM knowledge_object_sources": ("knowledge_objects",),
}
#: The NARROW tables (reverse index, mention bridge, canonical relations,
#: community members) are deliberately not pinned: even at 15.6M mention
#: rows / 3.9M co-mention rows with a 2M-object public library in F, the real
#: planner reads such a table once in a hash join where that is cheaper than
#: per-key probes, and each of those statements measured below 3 s (the
#: report's worlds B and C). What the pins above rule out is the costly
#: shape: a read of the WIDE tables to find the working set.


def _walk_plans(database) -> dict:
    """Run 0067 statement by statement in one transaction (then roll back),
    EXPLAINing each pinned statement right before it runs, under the REAL
    planner (no enable_* override)."""
    plans: dict[str, str] = {}
    with psycopg.connect(database.settings.database_url) as raw:
        raw.row_factory = dict_row
        for statement in _statements():
            key = next((k for k in _NO_SEQ_SCAN if statement.startswith(k)), None)
            if key is not None:
                rows = raw.execute(f"EXPLAIN (COSTS OFF) {statement}", prepare=False)
                plans[key] = "\n".join(r["QUERY PLAN"] for r in rows.fetchall())
            raw.execute(statement, prepare=False)
        raw.rollback()
    return plans


def _seed_memory_into_base(database) -> None:
    """World C: the big public library itself holds a Memory source (a
    notebook that became a public library after the source was created) with
    a few Memory objects, so the public library is IN F."""
    with database.write() as db:
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
            "content_md,created_at,updated_at) VALUES ('mem-base','nb-base','u-big',"
            "'ask_answer','confirmed','t','c',now(),now())")
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,created_at,"
            "updated_at) VALUES ('sm-base','nb-base','t','memory','mem-base',now(),now())")
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,source_id,"
            "payload,evidence,created_at,updated_at) SELECT 'kbm-'||g, 'nb-base', "
            "'concept', 'approved', 'sm-base', jsonb_build_object('name','bm'||g), "
            "jsonb_build_array(jsonb_build_object('source_id','sm-base')), now(), now() "
            "FROM generate_series(1,5) g")
        db.execute(
            "INSERT INTO concept_clusters(id,notebook_id,canonical_id,member_object_id,"
            "canonical_name,object_type,created_at,generation) SELECT 'cc-kbm-'||g, "
            "'nb-base', 'K-base-mem-'||g, 'kbm-'||g, 'n', 'concept', now(), 0 "
            "FROM generate_series(1,5) g")
        for table in ("sources", "knowledge_objects", "concept_clusters"):
            db.execute(f"ANALYZE {table}")


@pytest.mark.parametrize(
    ("base_in_f", "attested"), ((False, True), (True, True), (True, False)),
    ids=("world_b", "world_c", "world_c_unattested"))
def test_statements_are_driven_from_the_working_tables(
    postgres_database, capsys, base_in_f, attested
):
    """Under the real planner, on a world whose public library dominates
    every table (MKI_PIN_BASE_OBJECTS, default 200 000 objects), no pinned
    statement reads a big table sequentially -- with the public library
    outside F (world B), inside F with an attested reverse index (world C:
    reverse index only) and inside F unattested (its evidence is scanned
    through the notebook index). Then the real migrator runs under
    production's 30 s statement budget."""
    base_objects = int(os.environ.get("MKI_PIN_BASE_OBJECTS", "200000"))
    assert PostgresMigrator(postgres_database).migrate() == 67
    _seed_large(postgres_database, f_notebooks=5, objects_per_f=400,
                base_objects=base_objects)
    if base_in_f:
        _seed_memory_into_base(postgres_database)
    if not attested:
        with postgres_database.write() as db:
            db.execute("UPDATE unified_kg_state SET source_index_backfilled = 0 "
                       "WHERE notebook_id = 'nb-base'")
    with psycopg.connect(postgres_database.settings.database_url, autocommit=True) as raw:
        raw.execute("VACUUM ANALYZE")
    before = _counts(postgres_database)
    _roll_back_to_66(postgres_database)
    plans = _walk_plans(postgres_database)
    started = time.perf_counter()
    assert PostgresMigrator(postgres_database).migrate(
        statement_timeout_seconds=30) == 67
    elapsed = time.perf_counter() - started
    after = _counts(postgres_database)
    with capsys.disabled():
        print(f"\n[0067 pins] base_in_f={base_in_f} attested={attested} "
              f"elapsed={elapsed:.2f}s")
        for table in _ROW_TABLES:
            print(f"  {table}: {before[table]} -> {after[table]}")
    pinned = {key: list(tables) for key, tables in _NO_SEQ_SCAN.items()}
    assert set(plans) == set(_NO_SEQ_SCAN)
    with capsys.disabled():
        for key, plan in plans.items():
            print(f"  PLAN {key.splitlines()[0]}:\n    " + plan.replace("\n", "\n    "))
    for key, tables in pinned.items():
        for table in tables:
            assert f"Seq Scan on {table} " not in plans[key] + " ", (key, plans[key])
    with postgres_database.connect() as db:
        base = db.execute(
            "SELECT (SELECT COUNT(*) FROM concept_clusters WHERE notebook_id='nb-base') "
            "AS c, (SELECT memory_isolation_version FROM unified_kg_state "
            "WHERE notebook_id='nb-base') AS m").fetchone()
        pending = MemoryIsolationStore.pending_count(db)
    if base_in_f:
        # the public library's Memory clusters go, its marker is 0 and the
        # same startup worker rebuilds it
        assert base["c"] == base_objects and base["m"] == 0
        assert pending == 5 + 1
    else:
        # outside F: marked 2 like every notebook with clusters (G)
        assert base["c"] == base_objects and base["m"] == 2
        assert pending == 5
    assert after["knowledge_objects"] == before["knowledge_objects"]
    assert after["concept_clusters"] < before["concept_clusters"]


def test_the_post_readiness_check_statements_are_index_driven(postgres_database):
    """EXPLAIN pins (real planner) for every statement of the post-readiness
    check and the census, on a world whose public library holds 100 000
    objects, cluster rows, community members and mention rows: each page
    statement reaches its table through the notebook-prefixed index and each
    probe the probed table by key -- never a sequential read of a wide table
    (the cost of one check stays in proportion to its notebook, paged)."""
    from app.repositories.postgres import memory_isolation_store as store

    assert PostgresMigrator(postgres_database).migrate() == 67
    _seed_large(postgres_database, f_notebooks=5, objects_per_f=400,
                base_objects=100000)
    with postgres_database.write() as db:
        db.execute("SET LOCAL statement_timeout = 0")
        db.execute(
            "INSERT INTO mention_edges(notebook_id,claim_object_id,concept_canonical_id) "
            "SELECT 'nb-base', ko.id, 'K-nb-base-1' FROM knowledge_objects ko "
            "WHERE ko.notebook_id = 'nb-base'")
        db.execute(
            "INSERT INTO concept_merge_candidates(id,notebook_id,canonical_a,canonical_b,"
            "score,status,created_at,updated_at) SELECT 'mc-pin-'||g, 'nb-base', "
            "'K-nb-base-'||g, 'kb-'||g, 0.9, 'confirmed', now(), now() "
            "FROM generate_series(1, 2000) g")
    with psycopg.connect(postgres_database.settings.database_url, autocommit=True) as raw:
        raw.execute("VACUUM ANALYZE")
    nb = "nb-base"
    statements = {
        "canonical page": (store._CANONICAL_PAGE_SQL, (nb, 0, "", 5000),
                           ("concept_clusters",)),
        "dangling page": (store._DANGLING_PAGE_SQL, (nb, 0, "", "K-nb-base-9"),
                          ("concept_clusters", "knowledge_objects")),
        "mention page": (store._MENTION_PAGE_SQL, (nb, "", 5000), ("mention_edges",)),
        "stale mention page": (store._STALE_MENTION_PAGE_SQL, (nb, "", "kb-5"),
                               ("mention_edges", "knowledge_objects")),
        "member page": (store._MEMBER_PAGE_SQL, (nb, 0, "", 5000), ("community_members",)),
        "stale member page": (store._STALE_MEMBER_PAGE_SQL, (nb, 0, "", "K-nb-base-9"),
                              ("community_members", "concept_clusters",
                               "knowledge_objects")),
        "purge stale merge": (store._PURGE_STALE_MERGE_SQL, (nb,),
                              ("knowledge_objects",)),
        "pre-upgrade set": (store._PRE_UPGRADE_SEED_CHECK_SQL, (),
                            ("concept_clusters", "knowledge_objects", "sources")),
        "pre-upgrade F": (store._PRE_UPGRADE_F_SQL, (), ("sources",)),
        **{f"facts {name}": (sql, (nb,), ("knowledge_objects", "chunks", "sources",
                                         "kg_rebuild_checkpoint"))
           for name, sql in store._CENSUS_FACTS_SQL.items()},
    }
    with postgres_database.write() as db:
        plans = {name: _plan(db, sql, params)
                 for name, (sql, params, _tables) in statements.items()}
    for name, (_sql, _params, tables) in statements.items():
        for table in tables:
            assert f"Seq Scan on {table} " not in plans[name] + " ", (name, plans[name])
    assert "pk_mention_edges" in plans["stale mention page"], plans["stale mention page"]
    assert "idx_commmem_nb_can" in plans["stale member page"], plans["stale member page"]


def test_pre_isolation_scale_indexes_are_queued_on_postgres(
    upgraded, postgres_settings, tmp_path, monkeypatch
):
    """PostgreSQL twin of the SQLite case: a personal notebook and a public
    library, neither holding Memory, with pre-isolation published scale
    manifests and non-copyable (copy bound of one row) get a FULL build
    queued; the isolated (stamped) index and the copyable copy do not."""
    import json

    from app.core.config import Settings
    from app.repositories.postgres.repository import PostgresRepository
    from app.services.memory_isolation_rebuild import MemoryIsolationRebuild

    monkeypatch.setenv("NOTEBOOK_COPY_MAX_ROWS", "1")
    monkeypatch.setenv("VIZ_SYNC_BUILD_MAX_OBJECTS", "3")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    settings = Settings(
        database_url=postgres_settings.database_url,
        postgres_pool_min_size=1, postgres_pool_max_size=2,
        postgres_pool_acquire_timeout_seconds=1,
        postgres_statement_timeout_seconds=2,
        postgres_chunk_fts_timeout_seconds=1.0,
        postgres_lock_timeout_seconds=1,
    )
    repository = PostgresRepository(settings)
    try:
        scale = repository._runtime.scale_artifacts
        manifests = {
            cases.NB_C: {"built_at": cases.NOW},
            cases.NB_PUB: {"built_at": cases.NOW},
            cases.NB_COPY: {"built_at": cases.NOW},
            cases.NB_G: {"built_at": cases.NOW, "memory_isolation": 1},
            cases.NB_GX: {"built_at": cases.NOW},  # its KG rebuild is deferred
        }
        for notebook_id, manifest in manifests.items():
            directory = scale.artifacts.scale_dir(notebook_id)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "manifest.json").write_text(json.dumps(manifest))
        # standalone visualisations without a scale root: GK (4 objects) over
        # the synchronous budget of 3 is queued, G (3 objects) is not
        for notebook_id in (cases.NB_GK, cases.NB_G):
            directory = scale.artifacts.viz_dir(notebook_id)
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "manifest.json").write_text("{}")
        service = MemoryIsolationRebuild.for_repository(
            repository, scale_when="idle", timer=lambda delay, callback: None)
        # before the pass everything is still marked 2: nothing to build
        assert service.pre_isolation_scale_notebook_ids() == []
        held = repository.start_unified_kg_rebuild(cases.NB_GX)
        assert service.run_pass()["deferred"] == 1
        expected = sorted([cases.NB_C, cases.NB_PUB, cases.NB_GK])
        # GX's KG still awaits its rebuild (marker 0): not queued yet
        assert service.pre_isolation_scale_notebook_ids() == expected
        repository.fail_unified_kg_rebuild_submission(cases.NB_GX, held["job_id"])
        service.run_pass()
        expected = sorted(expected + [cases.NB_GX])
        assert service.pre_isolation_scale_notebook_ids() == expected
        queued = {nb: mode for nb, (mode, _ts) in scale.idle_queue.items()}
        assert {nb: queued.get(nb) for nb in expected} == {nb: "full" for nb in expected}
        assert cases.NB_COPY not in queued
        for notebook_id in expected:
            directory = (scale.artifacts.viz_dir(notebook_id) if notebook_id == cases.NB_GK
                         else scale.artifacts.scale_dir(notebook_id))
            (directory / "manifest.json").write_text(json.dumps({"memory_isolation": 1}))
        assert service.pre_isolation_scale_notebook_ids() == []
    finally:
        for notebook_id in list(repository._runtime.scale_artifacts.idle_queue):
            repository._runtime.scale_artifacts.dequeue_idle(notebook_id)
        repository.close()


def test_the_stale_purge_keeps_curator_decisions(upgraded):
    """A curator's decided pair whose losing canonical id no cluster row
    carries (GK: K-alpha beta after the confirmed merge) is a decision, not a
    stale row: the purge removes only a sentinel side whose object is gone
    (GX's mc-gx-sentinel), and keeps GX's bridge-shaped decision."""
    database, _before, _after = upgraded
    with database.write() as db:
        assert MemoryIsolationStore.purge_stale_merge_candidates(db, cases.NB_GK) == 0
        assert MemoryIsolationStore.purge_stale_merge_candidates(
            db, cases.NB_GX) == len(cases.MERGE_CANDIDATES_STALE)
        left = {r["id"] for r in db.execute(
            "SELECT id FROM concept_merge_candidates WHERE notebook_id IN (%s, %s)",
            (cases.NB_GK, cases.NB_GX)).fetchall()}
    assert {d[0] for d in cases.DECIDED} <= left
    assert {"mc-gx-bridge", "mc-gx-keep"} <= left
    assert not (cases.MERGE_CANDIDATES_STALE & left)
