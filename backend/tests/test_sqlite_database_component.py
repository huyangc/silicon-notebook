import sqlite3
import pytest

from app.repositories.sqlite.database import SqliteDatabase
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def settings(tmp_path):
    return Settings(sqlite_path=str(tmp_path / "db.sqlite"), storage_dir=str(tmp_path / "storage"))


@pytest.fixture
def repo(settings):
    return SQLiteRepository(settings)


def test_runtime_components_share_exact_write_lock_object(repo):
    assert repo._write_lock is repo._runtime.database.write_lock


def test_two_repository_instances_keep_independent_locks_for_same_db(settings):
    first = SQLiteRepository(settings)
    second = SQLiteRepository(settings)
    assert first._write_lock is not second._write_lock


def test_connection_pragmas(settings, tmp_path):
    db = SqliteDatabase(settings, tmp_path)
    with db.connect() as conn:
        assert conn.row_factory is sqlite3.Row
        assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
        assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == settings.db_busy_timeout_ms
        assert conn.execute("PRAGMA synchronous").fetchone()[0] == 1
        assert conn.execute("PRAGMA cache_size").fetchone()[0] == -16384
        assert conn.execute("PRAGMA temp_store").fetchone()[0] == 2
        assert conn.execute("PRAGMA mmap_size").fetchone()[0] == 268435456


def test_write_rolls_back_on_error(settings, tmp_path):
    db = SqliteDatabase(settings, tmp_path)
    with db.write() as conn:
        conn.execute("CREATE TABLE t (x INTEGER)")
    try:
        with db.write() as conn:
            conn.execute("INSERT INTO t VALUES (1)")
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    with db.connect() as conn:
        assert conn.execute("SELECT count(*) FROM t").fetchone()[0] == 0
