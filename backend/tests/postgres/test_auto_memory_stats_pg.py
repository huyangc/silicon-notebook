"""``scripts/auto_memory_stats.py`` on PostgreSQL (0087). SQLite twin:
``tests/test_auto_memory_stats.py``; shared cases:
``tests/auto_memory_stats_cases.py``."""
from __future__ import annotations

import importlib.util
import pathlib
import sys

import pytest
from psycopg.types.json import Jsonb

from tests import auto_memory_stats_cases as cases
from tests.memory_suggest_store_cases import Env

pytestmark = pytest.mark.postgres_integration

SCRIPT = pathlib.Path(__file__).resolve().parents[3] / "scripts" / "auto_memory_stats.py"
SPEC = importlib.util.spec_from_file_location("auto_memory_stats_pg", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["auto_memory_stats_pg"] = module
SPEC.loader.exec_module(module)


@pytest.fixture
def pg_stats(request, postgres_settings):
    database = request.getfixturevalue("postgres_database")
    from app.repositories.postgres.migrator import PostgresMigrator

    PostgresMigrator(database).migrate()

    def run(sql: str, params: tuple = ()) -> None:
        with database.write() as db:
            db.execute(sql.replace("?", "%s"), params)

    def rows(sql: str, params: tuple = ()) -> list[dict]:
        with database.connect() as db:
            return [dict(row) for row in db.execute(sql.replace("?", "%s"), params).fetchall()]

    env = Env(store=None, run=run, rows=rows, json=Jsonb)
    connection = module._open_postgres(postgres_settings.database_url)

    def collect(**kwargs):
        return module.collect(connection, "postgresql", **kwargs)

    try:
        yield cases.Stats(env=env, collect=collect)
    finally:
        connection.rollback()
        connection.close()


def test_counts_by_signal_and_status(pg_stats):
    cases.assert_counts_by_signal_and_status(pg_stats)


def test_funnel(pg_stats):
    cases.assert_funnel(pg_stats)


def test_corrected_real_rows_are_counted(pg_stats):
    cases.assert_corrected_real_rows_are_counted(pg_stats)


def test_deprecation_window(pg_stats):
    cases.assert_deprecation_window(pg_stats)


def test_job_outcomes(pg_stats):
    cases.assert_job_outcomes(pg_stats)


def test_citation_counts(pg_stats):
    cases.assert_citation_counts(pg_stats)


def test_filters(pg_stats):
    cases.assert_filters(pg_stats)


def test_report_is_content_free(pg_stats):
    cases.assert_report_is_content_free(pg_stats)


def test_connection_is_read_only(pg_stats, postgres_settings):
    import psycopg

    cases.seed_scenario(pg_stats.env)
    connection = module._open_postgres(postgres_settings.database_url)
    try:
        with pytest.raises(psycopg.errors.ReadOnlySqlTransaction):
            connection.execute("DELETE FROM memory_items")
    finally:
        connection.rollback()
        connection.close()
