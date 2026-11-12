"""Healthy bounded reads can be reused; real driver interruptions cannot."""
import sqlite3

import pytest

from app.core.config import Settings
from app.repositories import read_budget as budgets
from app.repositories.read_budget import ReadBudgetExceeded, read_budget
from app.repositories.sqlite.database import SqliteDatabase


def test_budget_expiry_before_sql_preserves_connection(tmp_path, monkeypatch):
    database = SqliteDatabase(Settings(database_url=f"sqlite:///{tmp_path / 'reads.db'}"), tmp_path)
    now = [100.0]
    monkeypatch.setattr(budgets.time, "monotonic", lambda: now[0])
    try:
        with pytest.raises(ReadBudgetExceeded), read_budget(101.0):
            with database.connect_read() as db:
                connection = db.connection
                db.execute("CREATE TABLE probe(value INTEGER)")
                db.execute("INSERT INTO probe VALUES (1)")
                now[0] = 102.0
                db.execute("SELECT 1")
        with database.connect_read() as reused:
            assert reused is connection
            assert reused.execute("SELECT COUNT(*) FROM probe").fetchone()[0] == 0
            assert reused.execute("SELECT 1").fetchone()[0] == 1
    finally:
        database.close()


def test_driver_interruption_discards_connection(tmp_path):
    database = SqliteDatabase(Settings(database_url=f"sqlite:///{tmp_path / 'reads.db'}"), tmp_path)
    try:
        with pytest.raises(sqlite3.OperationalError, match="interrupted"):
            with database.connect_read() as interrupted:
                interrupted.set_progress_handler(lambda: 1, 1)
                interrupted.execute("SELECT 1")
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            interrupted.execute("SELECT 1")
        with database.connect_read() as fresh:
            assert fresh is not interrupted
            assert fresh.execute("SELECT 1").fetchone()[0] == 1
    finally:
        database.close()
