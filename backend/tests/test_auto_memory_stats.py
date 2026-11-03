"""``scripts/auto_memory_stats.py`` on SQLite (auto user-memory plan T12).
PostgreSQL twin: ``tests/postgres/test_auto_memory_stats_pg.py``; shared
cases: ``tests/auto_memory_stats_cases.py``."""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sqlite3
import sys
from pathlib import Path

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from tests import auto_memory_stats_cases as cases
from tests.memory_suggest_store_cases import Env

SCRIPT = pathlib.Path(__file__).resolve().parents[2] / "scripts" / "auto_memory_stats.py"
SPEC = importlib.util.spec_from_file_location("auto_memory_stats", SCRIPT)
module = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules["auto_memory_stats"] = module
SPEC.loader.exec_module(module)


@pytest.fixture
def sqlite_stats(tmp_path: Path):
    db_path = tmp_path / "stats.db"
    repo = SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{db_path}",
            storage_dir=str(tmp_path / "storage"),
            _env_file=None,
            event_log_enabled=False,
            llm_log_enabled=False,
        )
    )
    database = repo._runtime.database

    def run(sql: str, params: tuple = ()) -> None:
        with database.write(operation="test.seed") as db:
            db.execute(sql, params)

    def rows(sql: str, params: tuple = ()) -> list[dict]:
        with database.connect() as db:
            return [dict(row) for row in db.execute(sql, params).fetchall()]

    env = Env(
        store=None, run=run, rows=rows,
        json=lambda value: json.dumps(value, ensure_ascii=False),
    )
    connection = module._open_sqlite(str(db_path))

    def collect(**kwargs):
        return module.collect(connection, "sqlite", **kwargs)

    try:
        yield cases.Stats(env=env, collect=collect), db_path
    finally:
        connection.close()
        repo.close()


def test_counts_by_signal_and_status(sqlite_stats):
    cases.assert_counts_by_signal_and_status(sqlite_stats[0])


def test_funnel(sqlite_stats):
    cases.assert_funnel(sqlite_stats[0])


def test_corrected_real_rows_are_counted(sqlite_stats):
    cases.assert_corrected_real_rows_are_counted(sqlite_stats[0])


def test_deprecation_window(sqlite_stats):
    cases.assert_deprecation_window(sqlite_stats[0])


def test_job_outcomes(sqlite_stats):
    cases.assert_job_outcomes(sqlite_stats[0])


def test_citation_counts(sqlite_stats):
    cases.assert_citation_counts(sqlite_stats[0])


def test_filters(sqlite_stats):
    cases.assert_filters(sqlite_stats[0])


def test_report_is_content_free(sqlite_stats):
    cases.assert_report_is_content_free(sqlite_stats[0])


def test_main_prints_one_content_free_json_document_and_writes_nothing(
    sqlite_stats, capsys
):
    stats, db_path = sqlite_stats
    cases.seed_scenario(stats.env)
    before = db_path.read_bytes()

    assert module.main(["--db", str(db_path)]) == 0

    out = capsys.readouterr().out
    report = json.loads(out)
    assert report["read_only"] is True and report["dialect"] == "sqlite"
    assert report["suggestions"]["funnel"]["all"]["proposed"] == 9
    for sentinel in (*cases.SENTINELS, *cases.MEMORY_IDS):
        assert sentinel not in out
    assert db_path.read_bytes() == before


def test_connection_is_read_only(sqlite_stats):
    _stats, db_path = sqlite_stats
    connection = module._open_sqlite(str(db_path))
    try:
        with pytest.raises(sqlite3.OperationalError):
            connection.execute("DELETE FROM memory_items")
    finally:
        connection.close()


def test_since_must_be_a_date(capsys):
    with pytest.raises(SystemExit):
        module.main(["--db", "unused.db", "--since", "not-a-date"])


def test_missing_database_is_refused_without_creating_it(tmp_path):
    missing = tmp_path / "missing.db"
    with pytest.raises(SystemExit):
        module.main(["--db", str(missing)])
    assert not missing.exists()


def test_collect_signature_default_now_is_current_time(sqlite_stats):
    stats, _db_path = sqlite_stats
    cases.seed_scenario(stats.env)
    # without ``now`` the real clock is used: everything seeded in 2026 has matured
    window = stats.collect()["suggestions"]["deprecation_30d"]["all"]
    assert window["adopted"] == 5
