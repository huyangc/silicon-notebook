import json
import sqlite3
import threading
import time
from contextlib import contextmanager

import pytest

from app.core import diagnostics_runtime as diagnostics
from app.models.schemas import NotebookCreate
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


def _diagnostics_runtime(tmp_path):
    return diagnostics.DiagnosticsRuntime(
        tmp_path / "diagnostics",
        readiness_provider=lambda: {},
        concurrency_provider=lambda: {},
        enable_signal=False,
    )


def _wait_until(predicate, *, timeout=2.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        value = predicate()
        if value:
            return value
        time.sleep(0.005)
    raise AssertionError("timed out waiting for diagnostic state")


def test_write_reports_holder_waiter_and_reentry_depth(settings, tmp_path):
    db = SqliteDatabase(settings, tmp_path)
    runtime = _diagnostics_runtime(tmp_path)
    holder_entered = threading.Event()
    release_holder = threading.Event()
    waiter_acquired = threading.Event()
    failures = []

    def hold_write_lock():
        try:
            with db.write(operation="holder-op"):
                holder_entered.set()
                release_holder.wait(timeout=2)
        except BaseException as exc:  # preserve worker failures for the assertion thread
            failures.append(exc)

    def wait_for_write_lock():
        try:
            with db.write(operation="waiter-op"):
                waiter_acquired.set()
        except BaseException as exc:  # preserve worker failures for the assertion thread
            failures.append(exc)

    with diagnostics.install_runtime(runtime):
        holder = threading.Thread(target=hold_write_lock)
        waiter = threading.Thread(target=wait_for_write_lock)
        holder.start()
        assert holder_entered.wait(timeout=2)
        waiter.start()
        try:
            lock_state = _wait_until(
                lambda: (
                    snapshot
                    if len(snapshot["waiters"]) == 1
                    and snapshot["holder"] is not None
                    and snapshot["waiters"][0]["duration_ms"] > 0
                    and snapshot["holder"]["duration_ms"] > 0
                    else None
                )
                if (snapshot := runtime.snapshot()["write_lock"])
                else None
            )
            assert lock_state["holder"]["operation"] == "holder-op"
            assert lock_state["holder"]["depth"] == 1
            assert lock_state["waiters"][0]["operation"] == "waiter-op"
            assert not waiter_acquired.is_set()
        finally:
            release_holder.set()
            holder.join(timeout=2)
            waiter.join(timeout=2)

        assert not holder.is_alive()
        assert not waiter.is_alive()
        assert failures == []
        assert waiter_acquired.is_set()
        assert runtime.snapshot()["write_lock"] == {"holder": None, "waiters": []}

        with db.write(operation="outer-op"):
            outer = runtime.snapshot()["write_lock"]
            assert outer["holder"]["depth"] == 1
            assert outer["waiters"] == []
            with db.write(operation="inner-op"):
                nested = runtime.snapshot()["write_lock"]
                assert nested["holder"]["depth"] == 2
                assert nested["holder"]["operation"] == "outer-op"
                assert nested["waiters"] == []
            assert runtime.snapshot()["write_lock"]["holder"]["depth"] == 1
        assert runtime.snapshot()["write_lock"]["holder"] is None


def test_write_diagnostics_clean_up_after_body_exception(settings, tmp_path):
    db = SqliteDatabase(settings, tmp_path)
    runtime = _diagnostics_runtime(tmp_path)

    with diagnostics.install_runtime(runtime):
        with pytest.raises(RuntimeError, match="boom"):
            with db.write(operation="failing-op"):
                assert runtime.snapshot()["write_lock"]["holder"]["depth"] == 1
                raise RuntimeError("boom")

        assert runtime.snapshot()["write_lock"] == {"holder": None, "waiters": []}
        assert db.write_lock.acquire(timeout=0.1)
        db.write_lock.release()


def test_active_sql_is_sanitized_while_sqlite_execute_blocks(settings, tmp_path):
    db = SqliteDatabase(settings, tmp_path)
    with db.write() as conn:
        conn.execute("CREATE TABLE notebooks (id TEXT)")
        conn.execute("INSERT INTO notebooks VALUES ('one')")

    runtime = _diagnostics_runtime(tmp_path)
    sql_started = threading.Event()
    release_sql = threading.Event()
    failures = []

    def run_query():
        try:
            conn = db.connect()

            def diag_block(value):
                sql_started.set()
                release_sql.wait(timeout=2)
                return value

            conn.create_function("diag_block", 1, diag_block)
            conn.execute(
                "SELECT diag_block(?) FROM notebooks",
                ("PRIVATE-SQL-PARAMETER",),
            ).fetchall()
        except BaseException as exc:  # preserve worker failures for the assertion thread
            failures.append(exc)
        finally:
            db.close_local()

    with diagnostics.install_runtime(runtime):
        worker = threading.Thread(target=run_query)
        worker.start()
        assert sql_started.wait(timeout=2)
        try:
            active = runtime.snapshot()["active_sql"]
            assert len(active) == 1
            assert active[0]["verb"] == "SELECT"
            assert active[0]["table"] == "notebooks"
            assert len(active[0]["fingerprint"]) == 12
            encoded = json.dumps(active)
            assert "SELECT diag_block" not in encoded
            assert "PRIVATE-SQL-PARAMETER" not in encoded
        finally:
            release_sql.set()
            worker.join(timeout=2)

    assert not worker.is_alive()
    assert failures == []
    assert runtime.snapshot()["active_sql"] == []


def test_notebook_delete_names_its_write_operation(repo, monkeypatch):
    notebook = repo.create_notebook(NotebookCreate(name="doomed"))
    database = repo._runtime.database
    original_write = database.write
    operations = []

    @contextmanager
    def observed_write(*, operation="sqlite.write"):
        operations.append(operation)
        with original_write(operation=operation) as conn:
            yield conn

    monkeypatch.setattr(database, "write", observed_write)
    repo._runtime.notebook_store.delete_row_and_orphan_embeddings(notebook.id)

    assert operations == ["notebook.delete"]
