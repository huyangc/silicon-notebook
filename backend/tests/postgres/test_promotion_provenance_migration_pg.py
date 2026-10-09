"""PR-E8 promotion-provenance migration ``0068_promotion_provenance.sql`` on
real PostgreSQL (SQLite twin: ``tests/test_promotion_provenance_migration.py``;
both seed ``promotion_provenance_migration_cases.py`` and assert the same
``EXPECTED`` rows, which is the cross-backend equality).

Pinned: the upgrade; idempotent re-execution of the frozen SQL; the summary
counts the SQLite twin logs (read from 0068's working tables inside its own
transaction, then rolled back); EXPLAIN pins for the statements that reach a
wide table (the candidate read through ``idx_kos_notebook`` + ``pk_sources``,
the evidence expansion through ``pk_knowledge_objects``).
"""
from __future__ import annotations

from pathlib import Path

import psycopg
import pytest

from app.repositories.postgres.migrator import PostgresMigrator
from tests import promotion_provenance_migration_cases as cases

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_promotion_provenance_migration"),
]

MIGRATION = (
    Path(__file__).resolve().parents[2]
    / "app" / "repositories" / "postgres" / "migrations"
    / "0068_promotion_provenance.sql"
)


def _seed_at_67(database) -> None:
    assert PostgresMigrator(database).migrate(target_version=67) == 67
    with database.write() as db:
        cases.seed(db, postgres=True)


def _snapshot(database) -> dict:
    with database.connect() as db:
        return cases.snapshot(db)


def test_pg_upgrade_rewrites_foreign_entries_to_the_librarys_own_provenance(
    postgres_database,
):
    _seed_at_67(postgres_database)
    assert PostgresMigrator(postgres_database).migrate() == 71
    cases.assert_migrated(_snapshot(postgres_database))


def test_pg_reexecuting_the_frozen_sql_changes_nothing(postgres_database):
    _seed_at_67(postgres_database)
    assert PostgresMigrator(postgres_database).migrate() == 71
    after = _snapshot(postgres_database)
    with postgres_database.write() as db:
        db.execute(MIGRATION.read_text(encoding="utf-8"), prepare=False)
    assert _snapshot(postgres_database) == after


def test_pg_summary_counts_match_the_sqlite_log(postgres_database):
    _seed_at_67(postgres_database)
    body = MIGRATION.read_text(encoding="utf-8")
    body = body[: body.index("DO $pp$")]
    with psycopg.connect(postgres_database.settings.database_url) as raw:
        raw.execute(body, prepare=False)
        counts = raw.execute(
            "SELECT (SELECT count(DISTINCT base_id) FROM pp_new_evidence),"
            "(SELECT count(*) FROM pp_new_evidence),"
            "(SELECT count(*) FROM pp_rewrite WHERE body <> ''),"
            "(SELECT count(*) FROM pp_rewrite WHERE body = ''),"
            "(SELECT count(*) FROM pp_new_evidence WHERE NOT EXISTS ("
            "SELECT 1 FROM jsonb_array_elements(evidence) AS ev(item) "
            "WHERE jsonb_typeof(ev.item) = 'object'))"
        ).fetchone()
        raw.rollback()
    # = test_migration_logs_counts_only on SQLite
    assert counts == (2, 8, 6, 2, 2)


def test_pg_candidate_and_expansion_statements_keep_index_paths(postgres_database):
    """Under the DEFAULT planner settings (nothing switched off), with a
    20,000-object personal notebook beside a small attested public library:
    the candidate read drives the library's reverse index through
    idx_kos_notebook, its evidence scan reads the library's objects through a
    notebook index (never the whole table), and the foreign-entry filter of
    step 3 reads the candidates by primary key."""
    _seed_at_67(postgres_database)
    with postgres_database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier) VALUES ('nb-pp-filler','F','','','ready',%s,"
            "now(),now(),'personal')", (cases.USER,))
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,source_id,"
            "payload,evidence,created_at,updated_at) SELECT 'kf-'||g,'nb-pp-filler',"
            "'claim','approved','s-p','{}'::jsonb,'[]'::jsonb,now(),now() "
            "FROM generate_series(0,19999) g")
        db.execute(
            "INSERT INTO knowledge_object_sources(object_id,source_id,notebook_id) "
            "SELECT 'kf-'||g,'s-p','nb-pp-filler' FROM generate_series(0,19999) g")
    with psycopg.connect(postgres_database.settings.database_url, autocommit=True) as raw:
        for table in ("knowledge_objects", "knowledge_object_sources", "sources",
                      "promotion_candidates", "memory_items"):
            raw.execute(f"VACUUM (ANALYZE) {table}")
    body = MIGRATION.read_text(encoding="utf-8")
    prefix = body[: body.index("-- 2. Candidate objects")]
    candidate = body[body.index("CREATE TEMP TABLE pp_cand"):body.index("ANALYZE pp_cand;")]
    candidate_select = candidate[candidate.index("SELECT"):].rstrip().rstrip(";")
    objects = body[body.index("CREATE TEMP TABLE pp_obj"):body.index("ANALYZE pp_obj;")]
    objects_select = objects[objects.index("SELECT"):].rstrip().rstrip(";")
    with psycopg.connect(postgres_database.settings.database_url) as raw:
        raw.execute(prefix, prepare=False)
        cand_plan = "\n".join(
            row[0] for row in raw.execute(f"EXPLAIN (COSTS OFF) {candidate_select}"))
        raw.execute(candidate, prepare=False)
        raw.execute("ANALYZE pp_cand")
        obj_plan = "\n".join(
            row[0] for row in raw.execute(f"EXPLAIN (COSTS OFF) {objects_select}"))
        raw.rollback()
    assert "idx_kos_notebook" in cand_plan, cand_plan
    assert "Seq Scan on knowledge_object_sources" not in cand_plan, cand_plan
    assert "Seq Scan on knowledge_objects" not in cand_plan, cand_plan
    assert "idx_knowledge_objects_nb_" in cand_plan, cand_plan
    assert "pk_knowledge_objects" in obj_plan, obj_plan
    assert "Seq Scan on knowledge_objects" not in obj_plan, obj_plan
