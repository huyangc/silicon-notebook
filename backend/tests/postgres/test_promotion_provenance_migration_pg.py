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
    assert PostgresMigrator(postgres_database).migrate() == 68
    cases.assert_migrated(_snapshot(postgres_database))


def test_pg_reexecuting_the_frozen_sql_changes_nothing(postgres_database):
    _seed_at_67(postgres_database)
    assert PostgresMigrator(postgres_database).migrate() == 68
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
            "SELECT (SELECT count(DISTINCT base_id) FROM pp_rewrite),"
            "(SELECT count(*) FROM pp_new_evidence),"
            "(SELECT count(*) FROM pp_rewrite WHERE body <> ''),"
            "(SELECT count(*) FROM pp_rewrite WHERE body = ''),"
            "(SELECT count(*) FROM pp_new_evidence WHERE evidence = '[]'::jsonb)"
        ).fetchone()
        raw.rollback()
    # = test_migration_logs_counts_only on SQLite
    assert counts == (2, 6, 5, 1, 1)


def test_pg_candidate_and_expansion_statements_keep_index_paths(postgres_database):
    """A public library with 20,000 objects whose reverse index is attested:
    the candidate read is driven by idx_kos_notebook and anti-joins the
    library's own sources (read by notebook, never the whole table), and the
    expansion reads the candidates by primary key."""
    _seed_at_67(postgres_database)
    with postgres_database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,source_id,"
            "payload,evidence,created_at,updated_at) SELECT 'kb-'||g,%s,'claim',"
            "'approved','s-b','{}'::jsonb,'[]'::jsonb,now(),now() "
            "FROM generate_series(0,19999) g", (cases.BASE,))
        db.execute(
            "INSERT INTO knowledge_object_sources(object_id,source_id,notebook_id) "
            "SELECT 'kb-'||g,'s-b',%s FROM generate_series(0,19999) g", (cases.BASE,))
    with psycopg.connect(postgres_database.settings.database_url, autocommit=True) as raw:
        for table in ("knowledge_objects", "knowledge_object_sources", "sources"):
            raw.execute(f"VACUUM (ANALYZE) {table}")
    body = MIGRATION.read_text(encoding="utf-8")
    prefix = body[: body.index("-- 2. Candidate objects.")]
    candidate = body[body.index("CREATE TEMP TABLE pp_cand"):body.index("ANALYZE pp_cand;")]
    candidate_select = candidate[candidate.index("SELECT"):].rstrip().rstrip(";")
    expansion = body[body.index("CREATE TEMP TABLE pp_item"):body.index("ANALYZE pp_item;")]
    expansion_select = expansion[expansion.index("SELECT"):].rstrip().rstrip(";")
    with psycopg.connect(postgres_database.settings.database_url) as raw:
        raw.execute(prefix, prepare=False)
        raw.execute("SET LOCAL enable_seqscan=off")
        raw.execute("SET LOCAL enable_bitmapscan=off")
        cand_plan = "\n".join(
            row[0] for row in raw.execute(f"EXPLAIN (COSTS OFF) {candidate_select}"))
        raw.execute(candidate, prepare=False)
        raw.execute("ANALYZE pp_cand")
        item_plan = "\n".join(
            row[0] for row in raw.execute(f"EXPLAIN (COSTS OFF) {expansion_select}"))
        raw.rollback()
    assert "idx_kos_notebook" in cand_plan, cand_plan
    assert "Anti Join" in cand_plan, cand_plan
    assert "Seq Scan on knowledge_object_sources" not in cand_plan, cand_plan
    assert "Seq Scan on sources" not in cand_plan, cand_plan
    assert "Seq Scan on knowledge_objects" not in cand_plan, cand_plan
    assert "pk_knowledge_objects" in item_plan, item_plan
    assert "Seq Scan on knowledge_objects" not in item_plan, item_plan
