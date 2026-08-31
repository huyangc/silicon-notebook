"""codex PR#643 R6 P1 — ``PostgresScaleBuildLock.verify_held``'s freeze cap.

The fold swap now hands ``swap_fold_directory`` a LIVE verifier instead of a
frozen snapshot (see ``scale_index_builder.py``'s ``fold``), and that live
call runs from inside the process-global ``building_lock``. A ``pg_locks``
round trip that rode the pool's normal 30s ``statement_timeout`` from inside
that lock would freeze every notebook's status poll and admission, not just
the one being verified — so ``verify_held`` now caps its own query to a short
per-call timeout instead.

Pure Python + fake connection, mirroring ``test_postgres_recover_interrupted_jobs.py``'s
fake-connection shape: no real PostgreSQL server needed, so this lives at the
main test root rather than ``tests/postgres/`` (which the whole directory
skips without a live server).
"""
from __future__ import annotations

from typing import Callable

import psycopg
import pytest

from app.repositories.postgres import database as database_module
from app.repositories.postgres.database import PostgresScaleBuildLock


class _FakeCursor:
    def __init__(self, row: dict | None = None):
        self._row = row

    def fetchone(self):
        return self._row


class _FakeLockConnection:
    """Records every statement so tests can assert ordering, not just outcome."""

    def __init__(
        self,
        *,
        held: bool = True,
        on_locks_query: Callable[[], None] | None = None,
    ) -> None:
        self.statements: list[tuple[str, tuple]] = []
        self._held = held
        self._on_locks_query = on_locks_query
        self.commits = 0
        self.rollbacks = 0
        self.closed = False

    def execute(self, sql: str, params: tuple = ()):
        self.statements.append((sql, params))
        if "pg_locks" in sql:
            if self._on_locks_query is not None:
                self._on_locks_query()
            return _FakeCursor({"held": self._held})
        return _FakeCursor(None)

    def commit(self) -> None:
        self.commits += 1

    def rollback(self) -> None:
        self.rollbacks += 1

    def close(self) -> None:
        self.closed = True


def _lock(conn: _FakeLockConnection) -> PostgresScaleBuildLock:
    return PostgresScaleBuildLock(
        conn, namespace=1, key=2, on_release=lambda: None
    )


def test_verify_held_caps_its_query_with_a_short_local_timeout():
    """Mutation anchor: drop the ``set_config`` call, or flip its ``is_local``
    (third) argument from ``true`` to ``false``, and this goes red — either
    change lets the query ride the session's persistent 30s default instead
    of a per-call cap that reverts on commit."""
    conn = _FakeLockConnection(held=True)

    assert _lock(conn).verify_held() is True

    timeout_calls = [
        (sql, params)
        for sql, params in conn.statements
        if "set_config" in sql and "statement_timeout" in sql
    ]
    assert len(timeout_calls) == 1
    sql, params = timeout_calls[0]
    assert params[0] == f"{database_module._VERIFY_HELD_STATEMENT_TIMEOUT_MS}ms"
    # The third ``set_config`` argument is literal ``true`` (SET LOCAL
    # semantics) so it cannot leak past this call's transaction onto the
    # lock session's later release()/re-verify calls.
    assert "true" in sql.lower()

    locks_index = next(
        i for i, (sql, _) in enumerate(conn.statements) if "pg_locks" in sql
    )
    timeout_index = conn.statements.index(timeout_calls[0])
    assert timeout_index < locks_index, (
        "the timeout cap must be set BEFORE the query it is meant to bound"
    )
    assert conn.commits == 1
    assert conn.rollbacks == 0


def test_verify_held_treats_a_cancelled_query_as_lock_lost():
    """A real ``statement_timeout`` expiry surfaces as ``QueryCanceled``. The
    existing ``except Exception`` net already covers this, but this pins the
    specific failure mode the new cap exists for: verify_held must fall
    through to its established never-raises -> False contract, not propagate
    the cancellation. False only makes the caller's swap refuse more
    eagerly — the safe direction when a claim cannot be proven."""

    def blow_up() -> None:
        raise psycopg.errors.QueryCanceled(
            "canceling statement due to statement timeout"
        )

    conn = _FakeLockConnection(held=True, on_locks_query=blow_up)

    assert _lock(conn).verify_held() is False
    assert conn.rollbacks == 1
    assert conn.commits == 0
