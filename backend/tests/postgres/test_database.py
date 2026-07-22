from __future__ import annotations

import threading
import time
from datetime import datetime, timezone

import pytest


pytestmark = pytest.mark.postgres_integration


def test_write_commits_and_rows_are_dicts(postgres_database):
    with postgres_database.write() as conn:
        conn.execute("CREATE TABLE commit_probe (id integer PRIMARY KEY, value text)")
        conn.execute("INSERT INTO commit_probe VALUES (1, 'saved')")

    with postgres_database.connect() as conn:
        row = conn.execute("SELECT id, value FROM commit_probe").fetchone()

    assert type(row) is dict
    assert row == {"id": 1, "value": "saved"}


def test_write_rolls_back_on_exception(postgres_database):
    with postgres_database.write() as conn:
        conn.execute("CREATE TABLE rollback_probe (id integer PRIMARY KEY)")

    with pytest.raises(RuntimeError, match="abort"):
        with postgres_database.write() as conn:
            conn.execute("INSERT INTO rollback_probe VALUES (1)")
            raise RuntimeError("abort")

    with postgres_database.connect() as conn:
        count = conn.execute("SELECT count(*) AS count FROM rollback_probe").fetchone()
    assert count == {"count": 0}


def test_nested_write_is_rejected_before_pool_acquisition(postgres_database):
    from app.repositories.postgres.database import NestedPostgresWriteError

    with postgres_database.write() as conn:
        conn.execute("CREATE TABLE nested_probe (id integer PRIMARY KEY)")
        with pytest.raises(NestedPostgresWriteError):
            with postgres_database.write():
                raise AssertionError("unreachable")
        conn.execute("INSERT INTO nested_probe VALUES (1)")

    with postgres_database.connect() as conn:
        assert conn.execute("SELECT count(*) AS n FROM nested_probe").fetchone()["n"] == 1


def test_invalid_isolation_level_fails_before_pool_acquisition(postgres_database, monkeypatch):
    monkeypatch.setattr(
        postgres_database,
        "_acquire",
        lambda: (_ for _ in ()).throw(AssertionError("pool was acquired")),
    )
    with pytest.raises(ValueError, match="isolation"):
        with postgres_database.write(isolation_level="read uncommitted"):
            pass


@pytest.mark.parametrize(
    ("requested", "expected"),
    [
        ("read committed", "read committed"),
        ("repeatable read", "repeatable read"),
        ("serializable", "serializable"),
    ],
)
def test_write_applies_supported_isolation_levels(postgres_database, requested, expected):
    with postgres_database.write(isolation_level=requested) as conn:
        row = conn.execute("SHOW transaction_isolation").fetchone()
    assert row["transaction_isolation"] == expected


def test_timestamptz_values_are_timezone_aware_utc(postgres_database):
    with postgres_database.connect() as conn:
        row = conn.execute(
            "SELECT TIMESTAMPTZ '2026-07-22 09:10:11+08' AS happened_at"
        ).fetchone()

    happened_at = row["happened_at"]
    assert isinstance(happened_at, datetime)
    assert happened_at.tzinfo is not None
    assert happened_at.utcoffset().total_seconds() == 0
    assert happened_at == datetime(2026, 7, 22, 1, 10, 11, tzinfo=timezone.utc)


def test_server_side_statement_and_lock_timeouts_are_configured(postgres_database):
    with postgres_database.connect() as conn:
        row = conn.execute(
            "SELECT current_setting('statement_timeout') AS statement_timeout, "
            "current_setting('lock_timeout') AS lock_timeout, "
            "current_setting('application_name') AS application_name, "
            "current_setting('TimeZone') AS timezone"
        ).fetchone()
    assert row == {
        "statement_timeout": "2s",
        "lock_timeout": "1s",
        "application_name": "silicon-notebook",
        "timezone": "UTC",
    }


def test_pool_reset_restores_server_side_session_defaults(
    postgres_database, postgres_scope
):
    with postgres_database.connect() as conn:
        original_work_mem = conn.execute(
            "SELECT current_setting('work_mem') AS work_mem"
        ).fetchone()["work_mem"]
        conn.execute("SET statement_timeout = 0")
        conn.execute("SET lock_timeout = 0")
        conn.execute("SET TIME ZONE 'Asia/Shanghai'")
        conn.execute("SET application_name = 'mutated-by-client'")
        conn.execute("SET search_path = public")
        conn.execute("SET work_mem = '64MB'")

    with postgres_database.connect() as conn:
        row = conn.execute(
            "SELECT current_setting('statement_timeout') AS statement_timeout, "
            "current_setting('lock_timeout') AS lock_timeout, "
            "current_setting('TimeZone') AS timezone, "
            "current_setting('application_name') AS application_name, "
            "current_setting('search_path') AS search_path, "
            "current_setting('work_mem') AS work_mem, "
            "current_schema() AS schema"
        ).fetchone()
    assert row == {
        "statement_timeout": "2s",
        "lock_timeout": "1s",
        "timezone": "UTC",
        "application_name": "silicon-notebook",
        "search_path": postgres_scope.schema,
        "work_mem": original_work_mem,
        "schema": postgres_scope.schema,
    }


def test_statement_timeout_cancels_long_query(postgres_database):
    import psycopg

    started = time.monotonic()
    with pytest.raises(psycopg.errors.QueryCanceled):
        with postgres_database.connect() as conn:
            conn.execute("SELECT pg_sleep(3)")
    assert time.monotonic() - started < 2.5


def test_lock_timeout_cancels_wait_for_locked_row(postgres_database):
    import psycopg

    with postgres_database.write() as conn:
        conn.execute("CREATE TABLE lock_probe (id integer PRIMARY KEY, value integer)")
        conn.execute("INSERT INTO lock_probe VALUES (1, 0)")

    with postgres_database.connect() as holder:
        holder.execute("UPDATE lock_probe SET value = 1 WHERE id = 1")
        started = time.monotonic()
        with pytest.raises(psycopg.errors.LockNotAvailable):
            with postgres_database.connect() as waiter:
                waiter.execute("UPDATE lock_probe SET value = 2 WHERE id = 1")
        assert time.monotonic() - started < 2.5


def test_pool_acquisition_timeout_is_bounded(postgres_database):
    from psycopg_pool import PoolTimeout

    postgres_database._pool.resize(1, 1)
    with postgres_database.connect():
        started = time.monotonic()
        with pytest.raises(PoolTimeout):
            with postgres_database.connect():
                pass
        assert time.monotonic() - started < 2.5


def test_unrelated_row_can_update_while_another_row_is_locked(postgres_database):
    with postgres_database.write() as conn:
        conn.execute("CREATE TABLE concurrency_probe (id integer PRIMARY KEY, value integer)")
        conn.execute("INSERT INTO concurrency_probe VALUES (1, 0), (2, 0)")

    updated = threading.Event()
    failures: list[BaseException] = []

    def update_other_row() -> None:
        try:
            with postgres_database.write() as conn:
                conn.execute("UPDATE concurrency_probe SET value = 1 WHERE id = 2")
            updated.set()
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    with postgres_database.connect() as holder:
        holder.execute("UPDATE concurrency_probe SET value = 1 WHERE id = 1")
        worker = threading.Thread(target=update_other_row)
        worker.start()
        assert updated.wait(1.5), "unrelated update was serialized behind a Python lock"
    worker.join(timeout=2)

    assert not failures
    with postgres_database.connect() as conn:
        rows = conn.execute(
            "SELECT id, value FROM concurrency_probe ORDER BY id"
        ).fetchall()
    assert rows == [{"id": 1, "value": 1}, {"id": 2, "value": 1}]


def test_close_is_idempotent(postgres_database):
    from app.repositories.postgres.database import PostgresDatabaseClosedError

    postgres_database.close()
    postgres_database.close()
    with pytest.raises(PostgresDatabaseClosedError):
        with postgres_database.connect():
            pass


def test_connection_failure_and_repr_redact_credentials_and_query_options(
    postgres_settings, tmp_path
):
    from app.repositories.postgres.database import PostgresDatabase, PostgresDatabaseError

    secret_url = (
        "postgresql://secret-user:secret-password@127.0.0.1:1/"
        "silicon_notebook_task4_test?sslmode=disable&access_token=secret-token"
    )
    settings = postgres_settings.model_copy(
        update={
            "database_url": secret_url,
            "postgres_pool_acquire_timeout_seconds": 0.1,
            "postgres_pool_min_size": 1,
            "postgres_pool_max_size": 1,
        }
    )
    database = PostgresDatabase(settings, tmp_path)
    try:
        diagnostic = repr(database)
        with pytest.raises(PostgresDatabaseError) as caught:
            with database.connect():
                pass
        diagnostic += str(caught.value) + repr(caught.value)
    finally:
        database.close()

    for secret in ("secret-user", "secret-password", "secret-token", "sslmode"):
        assert secret not in diagnostic
    assert "postgresql://127.0.0.1:1/silicon_notebook_task4_test" in diagnostic


def test_resolve_path_preserves_repository_path_contract(postgres_database, tmp_path):
    assert postgres_database.resolve_path("relative/file.md") == (
        postgres_database.root_dir / "relative/file.md"
    )
    assert postgres_database.resolve_path(tmp_path / "file.md") == tmp_path / "file.md"
