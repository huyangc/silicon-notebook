"""Offline query-plan diagnostics understand the current expression-index schema."""
from __future__ import annotations

import importlib.util
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import pytest

from app.domain.instant_order import utc_microsecond_key
from app.repositories.sqlite_functions import register_sqlite_functions

SCRIPTS = Path(__file__).resolve().parents[2] / "scripts"


def _diagnostic(monkeypatch):
    monkeypatch.syspath_prepend(str(SCRIPTS))
    spec = importlib.util.spec_from_file_location("diag_db_schema_test", SCRIPTS / "diag_db.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _database(path):
    with sqlite3.connect(path) as connection:
        register_sqlite_functions(connection)
        connection.executescript("""
            CREATE TABLE notebooks(id TEXT PRIMARY KEY);
            CREATE TABLE ask_jobs(id TEXT PRIMARY KEY,
                notebook_id TEXT REFERENCES notebooks(id) ON DELETE CASCADE,
                created_at TEXT);
            CREATE INDEX ask_exact_time ON ask_jobs(notebook_id, sn_utc_us(created_at), id);
        """)


def test_readonly_diagnostic_keeps_notebook_cascade_plan_with_expression_indexes(tmp_path, monkeypatch):
    path = tmp_path / "snapshot.db"
    _database(path)
    before = path.read_bytes()
    diagnostic = _diagnostic(monkeypatch)
    deadline = time.monotonic() + 10
    connection = diagnostic._open_read_only(path, deadline, immutable_snapshot=True)
    try:
        evidence = diagnostic._empty_evidence(None)
        diagnostic._collect_plans(connection, evidence, None, (), {}, deadline)
        assert any(row["probe"] == "notebook_delete" for row in evidence["delete_plan"])
        assert not any(row["probe"] == "plan.notebook_delete" for row in evidence["degraded"])
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA query_only").fetchone()[0] == 1
        with pytest.raises(sqlite3.OperationalError, match="readonly"):
            connection.execute("DELETE FROM notebooks")
    finally:
        connection.close()
    assert path.read_bytes() == before


@pytest.mark.parametrize("stamp", [None, "", "invalid", "2026-10-01T08:00:00.000201",
    "2026-10-01T09:00:00.000201+01:00", "1969-12-31T23:59:59.999999Z"])
def test_diagnostic_schema_function_matches_runtime_time_keys(tmp_path, monkeypatch, stamp):
    path = tmp_path / "snapshot.db"
    _database(path)
    connection = _diagnostic(monkeypatch)._open_read_only(path, time.monotonic() + 10)
    try:
        assert connection.execute("SELECT sn_utc_us(?)", (stamp,)).fetchone()[0] == utc_microsecond_key(stamp)
    finally:
        connection.close()


@pytest.mark.process_contract
def test_diagnostic_schema_registration_remains_app_free(tmp_path):
    path = tmp_path / "snapshot.db"
    _database(path)
    code = """
import sys, time
sys.path.insert(0, sys.argv[1])
import diag_db
connection = diag_db._open_read_only(__import__('pathlib').Path(sys.argv[2]), time.monotonic() + 10)
try:
    assert connection.execute('EXPLAIN QUERY PLAN DELETE FROM notebooks WHERE id=?', ('missing',)).fetchall()
finally:
    connection.close()
assert not any(name == 'app' or name.startswith('app.') for name in sys.modules)
"""
    result = subprocess.run([sys.executable, "-c", code, str(SCRIPTS), str(path)],
        cwd=tmp_path, capture_output=True, text=True, timeout=15)
    assert result.returncode == 0, result.stderr
