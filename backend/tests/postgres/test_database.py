from __future__ import annotations

import threading
import time
import logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from app.domain.extensions import (
    RetrievalContributionCallContext,
    RetrievalEvidenceProposal,
)
from app.extensions import default_extension_runtime


pytestmark = pytest.mark.postgres_integration


class _Cancellation:
    def is_set(self) -> bool:
        return False

    def raise_if_cancelled(self) -> None:
        return None


class _PoolReadingProposalSource:
    def __init__(self, database) -> None:
        self.database = database
        self.calls = 0
        self.proposal = RetrievalEvidenceProposal(
            identity="graph",
            notebook_id="notebook",
            source_id="source",
            provenance_kind="ppr",
            provenance_reference="graph",
            value=type("Chunk", (), {"chunk_id": "graph"})(),
            token_cost=0,
        )

    def propose(self):
        self.calls += 1
        with self.database.connect() as connection:
            connection.execute("SELECT 1").fetchone()
        return (self.proposal,)

    def read(self, identities):
        return (self.proposal,) if identities == ("graph",) else ()


def _retrieval_call(source, database):
    return RetrievalContributionCallContext(
        actor_id="actor",
        notebook_id="notebook",
        scope_id="scope",
        scope_narrowed=True,
        run_id="run",
        run_kind="report_generation",
        cancellation=_Cancellation(),
        max_items=1,
        max_tokens=1,
        max_proposals=1,
        proposal_source=source,
        connection_probe=database,
    )


def test_retrieval_host_releases_pool_size_one_before_contributor_fanout(
    postgres_database,
):
    postgres_database._pool.resize(1, 1)
    source = _PoolReadingProposalSource(postgres_database)
    context = _retrieval_call(source, postgres_database)
    host = default_extension_runtime().retrieval_contributors
    baseline = [type("Chunk", (), {"chunk_id": "base"})()]

    with postgres_database.connect():
        blocked = host.run(
            baseline,
            invocation="selected_evidence",
            call_context=context,
            baseline_identity=lambda chunk: chunk.chunk_id,
            cancellation=context.cancellation,
        )
    assert blocked is baseline
    assert source.calls == 0

    accepted = host.run(
        baseline,
        invocation="selected_evidence",
        call_context=context,
        baseline_identity=lambda chunk: chunk.chunk_id,
        cancellation=context.cancellation,
    )
    assert [chunk.chunk_id for chunk in accepted] == ["base", "graph"]
    assert source.calls == 1


def test_connection_probe_depth_resets_after_nested_and_exceptional_leases(
    postgres_database,
):
    assert postgres_database.is_connection_held() is False
    with postgres_database.connect():
        assert postgres_database.is_connection_held() is True
        with postgres_database.connect():
            assert postgres_database.is_connection_held() is True
        assert postgres_database.is_connection_held() is True
    assert postgres_database.is_connection_held() is False

    with pytest.raises(RuntimeError, match="lease failure"):
        with postgres_database.connect():
            assert postgres_database.is_connection_held() is True
            raise RuntimeError("lease failure")
    assert postgres_database.is_connection_held() is False


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


def test_pool_reset_restores_client_transaction_state_and_write_rollback(
    postgres_database,
):
    from psycopg import IsolationLevel
    from psycopg.pq import TransactionStatus
    from psycopg.rows import dict_row, tuple_row

    # Force reuse of the exact polluted connection instead of allowing a fresh
    # second pool member to make the test pass accidentally.
    postgres_database._pool.resize(1, 1)
    with postgres_database.write() as conn:
        conn.execute(
            "CREATE TABLE client_state_probe (id integer PRIMARY KEY, value text)"
        )

    with postgres_database.connect() as conn:
        assert conn.info.transaction_status == TransactionStatus.IDLE
        conn.autocommit = True
        conn.isolation_level = IsolationLevel.SERIALIZABLE
        conn.read_only = True
        conn.deferrable = True
        conn.row_factory = tuple_row

    with pytest.raises(RuntimeError, match="rollback probe"):
        with postgres_database.write() as conn:
            assert conn.info.transaction_status == TransactionStatus.IDLE
            assert conn.autocommit is False
            assert conn.isolation_level == IsolationLevel.READ_COMMITTED
            assert conn.read_only is False
            assert conn.deferrable is False
            assert conn.row_factory is dict_row
            conn.execute("INSERT INTO client_state_probe VALUES (1, 'must rollback')")
            raise RuntimeError("rollback probe")

    with postgres_database.connect() as conn:
        assert conn.autocommit is False
        assert conn.isolation_level == IsolationLevel.READ_COMMITTED
        assert conn.read_only is False
        assert conn.deferrable is False
        assert conn.row_factory is dict_row
        row = conn.execute("SELECT count(*) AS count FROM client_state_probe").fetchone()
    assert row == {"count": 0}


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


def test_projection_lock_waits_past_normal_lock_timeout(postgres_database):
    waiter_started = threading.Event()
    waiter_acquired = threading.Event()
    failures: list[BaseException] = []

    def wait_for_same_table() -> None:
        waiter_started.set()
        try:
            with postgres_database.table_projection_lock("kh-lock-timeout"):
                waiter_acquired.set()
        except BaseException as exc:  # pragma: no cover - asserted below
            failures.append(exc)

    with postgres_database.table_projection_lock("kh-lock-timeout"):
        worker = threading.Thread(target=wait_for_same_table)
        worker.start()
        assert waiter_started.wait(timeout=1)
        # Fixture lock_timeout is 1s. Holding longer proves the dedicated
        # advisory-lock session disabled it rather than failing the waiter.
        assert not waiter_acquired.wait(timeout=1.2)
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert failures == []
    assert waiter_acquired.is_set()


def test_projection_lock_sessions_are_bounded(postgres_database):
    capacity = postgres_database._projection_lock_capacity
    release = threading.Event()
    reached_capacity = threading.Event()
    counter_lock = threading.Lock()
    active = 0
    max_active = 0

    def hold_distinct_table(index: int) -> int:
        nonlocal active, max_active
        with postgres_database.table_projection_lock(f"kh-capacity-{index}"):
            with counter_lock:
                active += 1
                max_active = max(max_active, active)
                if active == capacity:
                    reached_capacity.set()
            assert release.wait(timeout=5)
            with counter_lock:
                active -= 1
        return index

    worker_count = capacity + 4
    with ThreadPoolExecutor(max_workers=worker_count) as pool:
        futures = [pool.submit(hold_distinct_table, i) for i in range(worker_count)]
        assert reached_capacity.wait(timeout=3)
        with counter_lock:
            assert active == capacity
            assert max_active == capacity
        release.set()
        assert sorted(future.result(timeout=5) for future in futures) == list(
            range(worker_count)
        )
    assert active == 0


def test_projection_lock_after_close_never_connects(postgres_database, monkeypatch):
    from app.repositories.postgres import database as database_module
    from app.repositories.postgres.database import PostgresDatabaseClosedError

    calls = []

    def forbidden_connect(cls, *_args, **_kwargs):
        calls.append(cls)
        raise AssertionError("dedicated connection opened after close")

    monkeypatch.setattr(
        database_module._SafeDiagnosticConnection,
        "connect",
        classmethod(forbidden_connect),
    )
    postgres_database.close()
    with pytest.raises(PostgresDatabaseClosedError):
        with postgres_database.table_projection_lock("kh-after-close"):
            pass
    assert calls == []


def test_projection_lock_waiter_rechecks_close_after_slot(
    postgres_database, monkeypatch
):
    from app.repositories.postgres import database as database_module
    from app.repositories.postgres.database import PostgresDatabaseClosedError

    semaphore = threading.BoundedSemaphore(1)
    assert semaphore.acquire(blocking=False)
    postgres_database._projection_lock_slots = semaphore
    waiter_started = threading.Event()
    calls = []
    failures = []

    def forbidden_connect(cls, *_args, **_kwargs):
        calls.append(cls)
        raise AssertionError("waiter connected after close")

    monkeypatch.setattr(
        database_module._SafeDiagnosticConnection,
        "connect",
        classmethod(forbidden_connect),
    )

    def wait_for_slot():
        waiter_started.set()
        try:
            with postgres_database.table_projection_lock("kh-wait-close"):
                raise AssertionError("closed waiter entered")
        except BaseException as exc:
            failures.append(exc)

    worker = threading.Thread(target=wait_for_slot)
    worker.start()
    assert waiter_started.wait(timeout=2)
    postgres_database.close()
    semaphore.release()
    worker.join(timeout=3)
    assert not worker.is_alive()
    assert len(failures) == 1
    assert isinstance(failures[0], PostgresDatabaseClosedError)
    assert calls == []


def test_projection_lock_connect_and_close_failures_release_slot(
    postgres_database, monkeypatch
):
    from app.repositories.postgres import database as database_module
    from app.repositories.postgres.database import PostgresDatabaseError

    semaphore = threading.BoundedSemaphore(1)
    postgres_database._projection_lock_slots = semaphore

    def failing_connect(cls, *_args, **_kwargs):
        raise RuntimeError("connect failed")

    monkeypatch.setattr(
        database_module._SafeDiagnosticConnection,
        "connect",
        classmethod(failing_connect),
    )
    with pytest.raises(PostgresDatabaseError):
        with postgres_database.table_projection_lock("kh-connect-failure"):
            pass
    assert semaphore.acquire(blocking=False)
    semaphore.release()

    class CloseFailureConnection:
        def execute(self, *_args, **_kwargs):
            return self

        def commit(self):
            return None

        def close(self):
            raise RuntimeError("close failed")

    fake = CloseFailureConnection()
    monkeypatch.setattr(postgres_database, "_restore_session_defaults", lambda _c: None)
    monkeypatch.setattr(
        database_module._SafeDiagnosticConnection,
        "connect",
        classmethod(lambda cls, *_args, **_kwargs: fake),
    )
    with pytest.raises(RuntimeError, match="close failed"):
        with postgres_database.table_projection_lock("kh-close-failure"):
            pass
    assert semaphore.acquire(blocking=False)
    semaphore.release()


def test_projection_lock_uses_bounded_connect_timeout(
    postgres_settings, tmp_path, monkeypatch
):
    from app.repositories.postgres import database as database_module
    from app.repositories.postgres.database import PostgresDatabase, PostgresDatabaseError

    settings = postgres_settings.model_copy(
        update={"postgres_pool_acquire_timeout_seconds": 999}
    )
    database = PostgresDatabase(settings, tmp_path)
    captured = {}

    def capture_connect(cls, *_args, **kwargs):
        captured.update(kwargs)
        raise RuntimeError("stop after capture")

    monkeypatch.setattr(
        database_module._SafeDiagnosticConnection,
        "connect",
        classmethod(capture_connect),
    )
    try:
        with pytest.raises(PostgresDatabaseError):
            with database.table_projection_lock("kh-connect-timeout"):
                pass
    finally:
        database.close()
    assert captured["connect_timeout"] == 30


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


@pytest.mark.parametrize(
    ("secret_url", "specific_secrets"),
    [
        (
            "postgresql://secret-user:secret-password@127.0.0.1:1/"
            "silicon_notebook_adapter_test?sslmode=secret-token&"
            "application_name=query-secret&target_session_attrs=prefer-standby",
            (
                "secret-token",
                "query-secret",
                "prefer-standby",
                "sslmode",
                "application_name",
                "target_session_attrs",
            ),
        ),
        (
            "postgresql://secret-user:secret-password%ZZ@127.0.0.1:1/"
            "silicon_notebook_adapter_test",
            ("secret-password%ZZ", "%ZZ"),
        ),
    ],
)
def test_connection_failure_and_repr_redact_credentials_and_query_options(
    postgres_settings, tmp_path, caplog, secret_url, specific_secrets
):
    from app.repositories.postgres.database import PostgresDatabase, PostgresDatabaseError

    settings = postgres_settings.model_copy(
        update={
            "database_url": secret_url,
            "postgres_pool_acquire_timeout_seconds": 0.1,
            "postgres_pool_min_size": 1,
            "postgres_pool_max_size": 1,
        }
    )
    caplog.set_level(logging.DEBUG, logger="psycopg")
    caplog.set_level(logging.DEBUG, logger="psycopg.pool")
    database = PostgresDatabase(settings, tmp_path)
    try:
        diagnostic = repr(database)
        with pytest.raises(PostgresDatabaseError) as caught:
            with database.connect():
                pass
        diagnostic += str(caught.value) + repr(caught.value)
    finally:
        database.close()
    diagnostic += "\n".join(record.getMessage() for record in caplog.records)

    for secret in ("secret-user", "secret-password", *specific_secrets):
        assert secret not in diagnostic
    assert "postgresql://127.0.0.1:1/silicon_notebook_adapter_test" in diagnostic


def test_resolve_path_preserves_repository_path_contract(postgres_database, tmp_path):
    assert postgres_database.resolve_path("relative/file.md") == (
        postgres_database.root_dir / "relative/file.md"
    )
    assert postgres_database.resolve_path(tmp_path / "file.md") == tmp_path / "file.md"
