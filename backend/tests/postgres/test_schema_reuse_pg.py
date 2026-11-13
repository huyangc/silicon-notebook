"""Per-worker schema reuse in ``tests/postgres/conftest.py``.

A reused schema must be indistinguishable from a freshly migrated one at the
start of every test, and anything a test did that a data reset cannot undo
(DDL, GRANTs, planner statistics, a rewritten migration ledger, a lock that
blocks the reset) must make the fixture drop the schema instead of handing it
to the next test.
"""
from __future__ import annotations

import os
import threading

import psycopg
import pytest

from tests.postgres import conftest as pg

pytestmark = [pytest.mark.postgres_integration]


@pytest.fixture
def base_url():
    url = os.environ.get("TEST_POSTGRES_URL")
    if not url:
        pytest.skip("TEST_POSTGRES_URL is not configured")
    return url


@pytest.fixture(autouse=True)
def isolated_pool(monkeypatch):
    """Run every case against this module's own pool and outcome counts."""
    monkeypatch.setattr(pg, "_POOLED_SCHEMAS", {})
    monkeypatch.setattr(pg, "_REUSE_OUTCOMES", {})
    yield
    url = os.environ.get("TEST_POSTGRES_URL")
    if url:
        with psycopg.connect(url, autocommit=True) as conn:
            for schema in pg._POOLED_SCHEMAS.values():
                pg._drop_schema(conn, schema)


def _parked(base_url):
    """The schema the last teardown parked; names the refusal reason if none."""
    assert base_url in pg._POOLED_SCHEMAS, f"teardown did not reuse: {pg._REUSE_OUTCOMES}"
    return pg._POOLED_SCHEMAS[base_url]


def _test_schemas(base_url):
    with psycopg.connect(base_url) as conn:
        return {row[0] for row in conn.execute(
            "SELECT nspname FROM pg_namespace WHERE nspname LIKE 'sn\\_test\\_%'"
        )}


def _write_rows(url, count=1):
    with psycopg.connect(url, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO notebooks (id, name, created_at, updated_at) "
            "VALUES ('nb-reuse', 'reuse', now(), now())"
        )
        for i in range(count):
            conn.execute(
                "INSERT INTO knowledge_objects (id, notebook_id, object_type, created_at, updated_at) "
                "VALUES (%s, 'nb-reuse', 'concept', now(), now())",
                (f"o-reuse-{i}",),
            )


def _fresh_state(url):
    """Everything a freshly migrated schema must show for the reset to be faithful."""
    with psycopg.connect(url) as conn:
        conn.execute("SELECT pg_stat_clear_snapshot()")
        return {
            "rows": conn.execute(
                "SELECT count(*) FROM notebooks").fetchone()[0]
            + conn.execute("SELECT count(*) FROM knowledge_objects").fetchone()[0],
            "sequences": sorted(conn.execute(
                "SELECT sequencename, last_value FROM pg_sequences "
                "WHERE schemaname = current_schema()").fetchall()),
            "counters": conn.execute(
                "SELECT relname, reltuples, relpages, relallvisible FROM pg_class "
                "WHERE relnamespace = current_schema()::regnamespace "
                "AND relname IN ('notebooks', 'knowledge_objects', 'user_profiles') "
                "ORDER BY relname").fetchall(),
            # No table may carry enough modifications for autovacuum to ANALYZE
            # it during the next test.
            "near_autoanalyze": conn.execute(
                "SELECT count(*) FROM pg_stat_user_tables WHERE schemaname = current_schema() "
                "AND n_mod_since_analyze * 2 >= "
                "current_setting('autovacuum_analyze_threshold')::bigint").fetchone()[0],
        }


def test_a_reused_schema_is_renamed_and_matches_a_freshly_migrated_one(base_url):
    with pg._reused_postgres_scope(base_url) as fresh:
        expected = _fresh_state(fresh.url)
        _write_rows(fresh.url, count=200)
        with psycopg.connect(fresh.url, autocommit=True) as conn:
            first_ordinal = conn.execute(
                "SELECT min(ordinal) FROM knowledge_objects").fetchone()[0]
            conn.execute("VACUUM user_profiles")  # never written, only vacuumed
            conn.execute(
                "SELECT setval(pg_get_serial_sequence('knowledge_objects', 'ordinal'), 1000, false)")
            conn.execute("SELECT pg_stat_force_next_flush()")
    _parked(base_url)
    with pg._reused_postgres_scope(base_url) as reused:
        assert reused.schema != fresh.schema
        assert not {fresh.schema} & _test_schemas(base_url)
        assert _fresh_state(reused.url) == expected
        _write_rows(reused.url)
        with psycopg.connect(reused.url) as conn:
            assert conn.execute(
                "SELECT ordinal FROM knowledge_objects").fetchone()[0] == first_ordinal


def test_a_late_writer_from_the_previous_test_cannot_reach_the_next_one(base_url):
    with pg._reused_postgres_scope(base_url) as first:
        stale_url = first.url
    with pg._reused_postgres_scope(base_url) as second:
        with psycopg.connect(stale_url, autocommit=True) as conn:
            with pytest.raises(psycopg.errors.UndefinedTable):
                conn.execute(
                    "INSERT INTO notebooks (id, name, created_at, updated_at) "
                    "VALUES ('nb-late', 'late', now(), now())"
                )
        with psycopg.connect(second.url) as conn:
            assert conn.execute("SELECT count(*) FROM notebooks").fetchone()[0] == 0


@pytest.mark.parametrize("change", [
    "ALTER TABLE notebooks ADD COLUMN reuse_probe text",
    "ALTER TABLE notebooks DROP COLUMN purpose; "
    "ALTER TABLE notebooks ADD COLUMN purpose text COLLATE \"C\" NOT NULL DEFAULT ''",
    "CREATE INDEX reuse_probe_idx ON notebooks (name)",
    "CREATE TABLE reuse_probe (id int)",
    # A table owner (the least-privilege CI role) may disable user triggers;
    # foreign-key triggers need a superuser, so the ordinary lane cannot touch them.
    "ALTER TABLE chunks DISABLE TRIGGER USER",
    "CREATE RULE reuse_probe AS ON INSERT TO notebooks DO INSTEAD NOTHING",
    "ALTER SEQUENCE knowledge_objects_ordinal_seq INCREMENT BY 5",
    "ALTER TABLE notebooks ALTER COLUMN name SET STATISTICS 5",
    # A column option set by DDL still counts; only internal pg_ options
    # (pg_attver, which differs per migration) are ignored.
    "ALTER TABLE notebooks ALTER COLUMN name SET (n_distinct = 5)",
    "REVOKE ALL ON notebooks FROM PUBLIC; GRANT SELECT ON notebooks TO PUBLIC",
    "ALTER TABLE notebooks SET (autovacuum_enabled = false)",
    "DELETE FROM silicon_schema_migrations WHERE version = "
    "(SELECT max(version) FROM silicon_schema_migrations)",
    "ANALYZE notebooks",
])
def test_anything_a_data_reset_cannot_undo_drops_the_schema(base_url, change):
    with pg._reused_postgres_scope(base_url):
        pass  # migrate once so the case below runs on a reused schema
    parked = _parked(base_url)
    before = _test_schemas(base_url)
    with pg._reused_postgres_scope(base_url) as scoped:
        _write_rows(scoped.url)
        with psycopg.connect(scoped.url, autocommit=True) as conn:
            conn.execute(change)
    assert pg._POOLED_SCHEMAS == {}
    # The reused schema (taken from the parked one) is gone, under every name.
    assert _test_schemas(base_url) == before - {parked}


def test_a_reset_blocked_by_a_lock_drops_the_schema(base_url, monkeypatch):
    monkeypatch.setattr(pg, "_RESET_LOCK_TIMEOUT", "200ms")
    before = _test_schemas(base_url)
    holder = psycopg.connect(base_url)
    try:
        with pg._reused_postgres_scope(base_url) as scoped:
            holder.execute(f'SELECT 1 FROM "{scoped.schema}".notebooks LIMIT 1')
            # The holder keeps its lock past the reset's timeout, then lets the
            # fallback drop proceed.
            threading.Timer(1.5, holder.rollback).start()
    finally:
        holder.close()
    assert pg._POOLED_SCHEMAS == {}
    assert _test_schemas(base_url) == before


def test_an_untouched_schema_is_reused_without_migrating_again(base_url, monkeypatch):
    with pg._reused_postgres_scope(base_url):
        pass
    calls = []
    monkeypatch.setattr(pg, "_migrate_scope", lambda url: calls.append(url))
    with pg._reused_postgres_scope(base_url) as scoped:
        with psycopg.connect(scoped.url) as conn:
            assert conn.execute("SELECT to_regclass('notebooks')").fetchone()[0] is not None
    assert calls == []


def test_a_failed_setup_leaves_no_schema_behind(base_url, monkeypatch):
    before = _test_schemas(base_url)

    def broken(url):
        raise RuntimeError("migration failed")

    monkeypatch.setattr(pg, "_migrate_scope", broken)
    with pytest.raises(RuntimeError, match="migration failed"):
        with pg._reused_postgres_scope(base_url):
            pass
    assert _test_schemas(base_url) == before


def test_a_seeding_migration_is_refused_rather_than_erased(base_url, monkeypatch):
    with pg._reused_postgres_scope(base_url) as scoped:
        _write_rows(scoped.url)
        with psycopg.connect(base_url, autocommit=True) as conn:
            monkeypatch.setattr(pg, "_BASELINE_FINGERPRINTS", {})
            with pytest.raises(RuntimeError, match="not empty"):
                pg._capture_baseline(conn, base_url, scoped.schema)


def test_the_fixture_reuses_a_migrated_schema_for_unmarked_tests(postgres_scope):
    with psycopg.connect(postgres_scope.url) as conn:
        assert conn.execute("SELECT to_regclass('notebooks')").fetchone()[0] is not None
        assert conn.execute("SELECT count(*) FROM silicon_schema_migrations").fetchone()[0] > 0


@pytest.mark.postgres_fresh_schema
def test_the_fresh_schema_marker_keeps_an_empty_unmigrated_schema(postgres_scope):
    with psycopg.connect(postgres_scope.url) as conn:
        assert conn.execute(
            "SELECT count(*) FROM pg_tables WHERE schemaname = current_schema()"
        ).fetchone()[0] == 0
