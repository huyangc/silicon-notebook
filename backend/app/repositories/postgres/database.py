"""Bounded PostgreSQL connection pool and transaction boundary."""
from __future__ import annotations

import logging
import math
import sys
import threading
from contextlib import contextmanager
from contextvars import ContextVar
from pathlib import Path
from typing import Any, Callable, Iterable, Iterator

import psycopg
from psycopg import IsolationLevel
from psycopg.pq import TransactionStatus
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool, PoolTimeout

from app.core.config import Settings
from app.core.database_url import database_identity, redact_database_url
from app.repositories.postgres.rows import PostgresRow
from app.repositories.scale_build_lock import (
    SCALE_BUILD_LOCK_UNAVAILABLE,
    ScaleBuildLockAttempt,
    advisory_lock_key,
    advisory_lock_oid,
)


class PostgresDatabaseError(RuntimeError):
    """A safe PostgreSQL pool/lifecycle failure with no connection secrets."""


class PostgresDatabaseClosedError(PostgresDatabaseError):
    """Raised when a closed database pool is used."""


class NestedPostgresWriteError(PostgresDatabaseError):
    """Raised before acquisition when one execution context nests write()."""


class PostgresPoolTimeout(PostgresDatabaseError, PoolTimeout):
    """A credential-safe pool timeout that preserves PoolTimeout semantics."""


_WRITE_ACTIVE: ContextVar[bool] = ContextVar("postgres_write_active", default=False)
_CONNECTION_DEPTH: ContextVar[int] = ContextVar(
    "postgres_connection_depth", default=0
)
_ISOLATION_LEVELS = {
    "read committed": IsolationLevel.READ_COMMITTED,
    "repeatable read": IsolationLevel.REPEATABLE_READ,
    "serializable": IsolationLevel.SERIALIZABLE,
}


_CONNECTION_LOG_STATE = threading.local()
_KNOWHOW_PROJECTION_LOCK_NAMESPACE = 0x534E4B48  # "SNKH"
_SCALE_BUILD_LOCK_NAMESPACE = 0x53434C42  # "SCLB"
# A lock session sits idle for the whole build (hours on a 9M-object library).
# TCP keepalives make a dead peer surface as a broken session -- which releases
# the advisory lock -- instead of a half-open socket that looks alive forever.
_LOCK_SESSION_KEEPALIVES = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 5,
}


class PostgresScaleBuildLock:
    """One held per-notebook advisory lock on its own dedicated session.

    Ownership crosses threads: the admitting thread acquires, the build worker
    verifies and releases.  Only one of them touches the session at a time, and
    the mutex below makes that literal rather than merely intended.
    """

    supported = True

    def __init__(
        self,
        connection: psycopg.Connection[PostgresRow],
        namespace: int,
        key: int,
        on_release: Callable[[], None],
    ) -> None:
        self._connection = connection
        self._namespace = namespace
        self._key = key
        self._on_release = on_release
        self._released = False
        self._mutex = threading.Lock()

    def verify_held(self) -> bool:
        """Re-read the lock from ``pg_locks`` on the owning session.

        This is the guard in front of the only destructive step (the artifact
        swap).  A session that was terminated -- managed-PostgreSQL idle
        reaper, failover, operator ``pg_terminate_backend`` -- released the
        advisory lock silently, so a heartbeat that merely proves the object
        still exists in this process proves nothing about the database.
        """
        with self._mutex:
            if self._released:
                return False
            try:
                row = self._connection.execute(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM pg_locks "
                    "WHERE locktype = 'advisory' AND granted "
                    "AND pid = pg_backend_pid() "
                    "AND classid = %s::oid AND objid = %s::oid"
                    ") AS held",
                    (
                        advisory_lock_oid(self._namespace),
                        advisory_lock_oid(self._key),
                    ),
                ).fetchone()
                self._connection.commit()
            except Exception:  # noqa: BLE001 - an unusable session is "not held"
                try:
                    self._connection.rollback()
                except Exception:
                    pass
                return False
            return bool(row is not None and row["held"])

    def release(self) -> None:
        """Unlock and close. Closing the session is the authoritative release."""
        with self._mutex:
            if self._released:
                return
            self._released = True
            try:
                self._connection.execute(
                    "SELECT pg_advisory_unlock(%s, %s)",
                    (self._namespace, self._key),
                )
                self._connection.commit()
            except Exception:  # noqa: BLE001 - the close below is the no-leak net
                pass
            finally:
                try:
                    self._connection.close()
                except Exception:  # noqa: BLE001 - nothing left to salvage
                    pass
                self._on_release()


class _ConnectingThreadLogFilter(logging.Filter):
    """Suppress raw libpq diagnostics on the thread parsing a secret conninfo."""

    def filter(self, record: logging.LogRecord) -> bool:
        if getattr(_CONNECTION_LOG_STATE, "suppress", False):
            # Invalid percent encodings and query-option values are rendered
            # verbatim by Psycopg before its exception reaches the pool. Replace
            # the complete diagnostic instead of trying to enumerate secrets.
            record.msg = "PostgreSQL connection diagnostic suppressed"
            record.args = ()
            record.exc_info = None
            record.exc_text = None
        return True


_PSYCOPG_LOG_FILTER = _ConnectingThreadLogFilter()
logging.getLogger("psycopg").addFilter(_PSYCOPG_LOG_FILTER)


class _SafeDiagnosticConnection(psycopg.Connection[PostgresRow]):
    """Keep raw conninfo failures out of both psycopg and pool loggers."""

    @classmethod
    def connect(cls, conninfo: str = "", **kwargs):
        previous = getattr(_CONNECTION_LOG_STATE, "suppress", False)
        _CONNECTION_LOG_STATE.suppress = True
        try:
            return super().connect(conninfo, **kwargs)
        except Exception:
            # psycopg_pool logs the exception after connect() returns. Give it
            # a stable generic error with no original cause or raw conninfo.
            raise psycopg.OperationalError("PostgreSQL connection failed") from None
        finally:
            _CONNECTION_LOG_STATE.suppress = previous


class PostgresDatabase:
    """Own a lazy bounded Psycopg pool; never serialize writes in Python."""

    def __init__(self, settings: Settings, root_dir: Path) -> None:
        if database_identity(settings.database_url).scheme != "postgresql":
            raise ValueError("PostgresDatabase requires a PostgreSQL DATABASE_URL")
        if not (
            1 <= settings.postgres_pool_min_size <= settings.postgres_pool_max_size
        ):
            raise ValueError("PostgreSQL pool sizes must satisfy 1 <= min <= max")
        if settings.postgres_pool_acquire_timeout_seconds <= 0:
            raise ValueError("PostgreSQL pool acquisition timeout must be positive")
        if settings.postgres_statement_timeout_seconds <= 0:
            raise ValueError("PostgreSQL statement timeout must be positive")
        if settings.postgres_lock_timeout_seconds <= 0:
            raise ValueError("PostgreSQL lock timeout must be positive")

        self.settings = settings
        self.root_dir = Path(root_dir)
        self._database_url = settings.database_url
        self._diagnostic_url = redact_database_url(settings.database_url)
        self._acquire_timeout = float(
            settings.postgres_pool_acquire_timeout_seconds
        )
        self._projection_connect_timeout_seconds = max(
            1, min(30, math.ceil(self._acquire_timeout))
        )
        self._statement_timeout_ms = int(
            settings.postgres_statement_timeout_seconds * 1000
        )
        self._lock_timeout_ms = int(settings.postgres_lock_timeout_seconds * 1000)
        self._lifecycle_lock = threading.Lock()
        self._projection_lock_capacity = max(
            1, min(4, settings.postgres_pool_max_size)
        )
        self._projection_lock_slots = threading.BoundedSemaphore(
            self._projection_lock_capacity
        )
        # Scale-build locks get their own budget rather than sharing the
        # projection lock's: a build holds its session for hours, and a shared
        # semaphore would let concurrent builds starve every knowhow projection
        # on this process. Sized one above the build ceiling so an admission
        # probe can always run while every executing build holds a session.
        self._scale_build_lock_capacity = max(
            1, int(getattr(settings, "scale_build_concurrency", 2)) + 1
        )
        self._scale_build_lock_slots = threading.BoundedSemaphore(
            self._scale_build_lock_capacity
        )
        self._opened = False
        self._closed = False
        self._pool: ConnectionPool[psycopg.Connection[PostgresRow]] = ConnectionPool(
            conninfo=self._database_url,
            connection_class=_SafeDiagnosticConnection,
            min_size=settings.postgres_pool_min_size,
            max_size=settings.postgres_pool_max_size,
            timeout=self._acquire_timeout,
            kwargs={
                "autocommit": False,
                "application_name": "silicon-notebook",
                "row_factory": dict_row,
            },
            configure=self._configure_connection,
            check=ConnectionPool.check_connection,
            reset=self._reset_connection,
            open=False,
            name="silicon-notebook-postgres",
        )

    def __repr__(self) -> str:
        return (
            f"{type(self).__name__}(database_url={self._diagnostic_url!r}, "
            f"closed={self._closed})"
        )

    def resolve_path(self, value: str | Path) -> Path:
        path = Path(value)
        return path if path.is_absolute() else self.root_dir / path

    def _configure_connection(self, conn: psycopg.Connection[PostgresRow]) -> None:
        self._restore_session_defaults(conn)

    @staticmethod
    def _restore_client_defaults(conn: psycopg.Connection[PostgresRow]) -> None:
        # Psycopg transaction settings and row_factory are client-side state:
        # RESET ALL cannot repair them. Roll back first so setters are legal,
        # then establish the exact contract every borrower receives.
        if conn.info.transaction_status != TransactionStatus.IDLE:
            conn.rollback()
        conn.autocommit = False
        conn.isolation_level = IsolationLevel.READ_COMMITTED
        conn.read_only = False
        conn.deferrable = False
        conn.row_factory = dict_row

    def _restore_session_defaults(
        self, conn: psycopg.Connection[PostgresRow]
    ) -> None:
        # Pool clients may change session-scoped values. Reassert the adapter's
        # contract both for new connections and before a returned connection is
        # reused; the callback must leave the connection idle.
        self._restore_client_defaults(conn)
        # RESET ALL is transactional and restores libpq startup options too,
        # including the fixture/tenant search_path. It also prevents arbitrary
        # client SET values (work_mem, role-adjacent GUCs, etc.) leaking to the
        # next borrower.
        conn.execute("RESET ALL")
        conn.execute(
            "SELECT "
            "set_config('statement_timeout', %s, false), "
            "set_config('lock_timeout', %s, false), "
            "set_config('TimeZone', 'UTC', false), "
            "set_config('application_name', 'silicon-notebook', false)",
            (f"{self._statement_timeout_ms}ms", f"{self._lock_timeout_ms}ms"),
        )
        conn.commit()
        if conn.info.transaction_status != TransactionStatus.IDLE:
            raise psycopg.ProgrammingError(
                "PostgreSQL pool reset did not leave the connection idle"
            )

    def _reset_connection(self, conn: psycopg.Connection[PostgresRow]) -> None:
        self._restore_session_defaults(conn)

    def _safe_error(self, operation: str) -> PostgresDatabaseError:
        return PostgresDatabaseError(
            f"PostgreSQL {operation} failed for {self._diagnostic_url}"
        )

    def _ensure_open(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                raise PostgresDatabaseClosedError(
                    f"PostgreSQL pool is closed for {self._diagnostic_url}"
                )
            if self._opened:
                return
            try:
                self._pool.open(wait=True, timeout=self._acquire_timeout)
            except Exception:
                # The underlying error may embed a full conninfo. Do not retain
                # it as __cause__/__context__ in startup diagnostics.
                raise self._safe_error("pool startup") from None
            self._opened = True

    def _ensure_projection_lock_open(self) -> None:
        with self._lifecycle_lock:
            if self._closed:
                raise PostgresDatabaseClosedError(
                    f"PostgreSQL pool is closed for {self._diagnostic_url}"
                )

    def _open_projection_lock_connection(self):
        """Open a dedicated lock session at one lifecycle linearization point."""
        with self._lifecycle_lock:
            if self._closed:
                raise PostgresDatabaseClosedError(
                    f"PostgreSQL pool is closed for {self._diagnostic_url}"
                )
            return _SafeDiagnosticConnection.connect(
                self._database_url,
                autocommit=False,
                row_factory=dict_row,
                application_name="silicon-notebook-projection-lock",
                connect_timeout=self._projection_connect_timeout_seconds,
            )

    @contextmanager
    def offline_maintenance_session(
        self,
    ) -> Iterator[psycopg.Connection[PostgresRow]]:
        """Open a non-pooled session for the process-wide maintenance lock.

        The command itself still needs normal pooled read/write connections.
        Keeping its session-level advisory lock in the pool would deadlock a
        valid ``max_size=1`` deployment, so this lifecycle seam is deliberately
        separate from :meth:`connect` and always closes the session.
        """
        self._ensure_projection_lock_open()
        connection = None
        try:
            connection = self._open_projection_lock_connection()
            self._restore_session_defaults(connection)
            connection.execute(
                "SET application_name = 'silicon-notebook-offline-maintenance'"
            )
            connection.commit()
        except PostgresDatabaseClosedError:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            raise
        except Exception:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass
            raise self._safe_error("offline maintenance session") from None

        assert connection is not None
        try:
            yield connection
        finally:
            try:
                connection.close()
            except Exception:
                pass

    @contextmanager
    def _acquire(self) -> Iterator[psycopg.Connection[PostgresRow]]:
        self._ensure_open()
        manager = self._pool.connection(timeout=self._acquire_timeout)
        try:
            conn = manager.__enter__()
        except PoolTimeout:
            raise PostgresPoolTimeout(
                f"PostgreSQL pool acquisition timed out for {self._diagnostic_url}"
            ) from None
        except Exception:
            raise self._safe_error("pool acquisition") from None

        try:
            self._restore_client_defaults(conn)
        except Exception:
            manager.__exit__(*sys.exc_info())
            raise self._safe_error("client-state reset") from None

        token = _CONNECTION_DEPTH.set(_CONNECTION_DEPTH.get() + 1)
        try:
            try:
                yield conn
            except BaseException:
                manager.__exit__(*sys.exc_info())
                raise
            else:
                manager.__exit__(None, None, None)
        finally:
            _CONNECTION_DEPTH.reset(token)

    @contextmanager
    def connect(self) -> Iterator[psycopg.Connection[PostgresRow]]:
        """Acquire one healthy dict-row connection and return it transactionally."""
        with self._acquire() as conn:
            yield conn

    def is_connection_held(self) -> bool:
        """Whether this execution currently owns any pooled connection."""
        return _CONNECTION_DEPTH.get() > 0

    @property
    def in_write_transaction(self) -> bool:
        """Whether this execution context is currently inside ``write()``.

        Mirrors ``SqliteDatabase.in_write_transaction`` so a read path that must
        never run inside a write transaction can enforce that contract at
        runtime on *both* backends rather than only in a comment. PostgreSQL
        hands out a different pooled connection per ``connect()``, so a read
        issued from inside ``write()`` silently observes the pre-commit
        database — the same silent-staleness failure SQLite has, minus the
        process-wide write lock.
        """
        return _WRITE_ACTIVE.get()

    @contextmanager
    def write(
        self, *, isolation_level: str = "read committed"
    ) -> Iterator[psycopg.Connection[PostgresRow]]:
        """Open one write transaction without a process-wide Python lock."""
        normalized = " ".join(isolation_level.strip().lower().split())
        level = _ISOLATION_LEVELS.get(normalized)
        if level is None:
            supported = ", ".join(_ISOLATION_LEVELS)
            raise ValueError(
                f"unsupported PostgreSQL isolation level; expected one of: {supported}"
            )
        if _WRITE_ACTIVE.get():
            raise NestedPostgresWriteError(
                "nested PostgreSQL write() transactions are not supported"
            )

        token = _WRITE_ACTIVE.set(True)
        try:
            with self._acquire() as conn:
                conn.isolation_level = level
                yield conn
        finally:
            _WRITE_ACTIVE.reset(token)

    def bulk_write(
        self,
        batches: Iterable[list[Any]],
        apply: Callable[[psycopg.Connection[PostgresRow], list[Any]], None],
    ) -> int:
        """Apply each batch in its own transaction and retain prior commits.

        PostgreSQL coordinates concurrent writers in the database, so this is
        the backend-neutral counterpart of SQLite's short-transaction helper;
        it deliberately adds no process-wide lock or inter-batch sleep.
        """
        count = 0
        for batch in batches:
            with self.write() as connection:
                apply(connection, batch)
            count += 1
        return count

    @staticmethod
    def begin_guarded_write(conn: psycopg.Connection[PostgresRow]) -> None:
        """Backend-neutral guarded-write seam.

        ``write()`` already owns a PostgreSQL transaction. The stores invoked
        after this seam acquire the concrete parent-row locks that protect the
        terminal write predicate, so no additional statement is required here.
        """
        del conn

    @contextmanager
    def table_projection_lock(self, table_id: str) -> Iterator[None]:
        """Serialize one table's complete projection across PG processes.

        A dedicated, non-pooled session is intentional. A waiter must not
        occupy a pool slot while the lock holder needs pooled connections for
        the projection itself (a two-slot pool would otherwise deadlock).
        Closing the dedicated session is the final safety net that releases
        the session advisory lock even if explicit unlock fails.
        """
        lock_key = advisory_lock_key(table_id)
        self._ensure_projection_lock_open()
        self._projection_lock_slots.acquire()
        try:
            connection = None
            try:
                connection = self._open_projection_lock_connection()
                self._restore_session_defaults(connection)
                # A projection can legitimately outlive the normal query timeout;
                # a queued pass must wait rather than fail and leave stale state.
                connection.execute("SET statement_timeout = 0")
                connection.execute("SET lock_timeout = 0")
                connection.commit()
                connection.execute(
                    "SELECT pg_advisory_lock(%s, %s)",
                    (_KNOWHOW_PROJECTION_LOCK_NAMESPACE, lock_key),
                )
                connection.commit()
            except PostgresDatabaseClosedError:
                raise
            except Exception:
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass
                raise self._safe_error("projection lock acquisition") from None

            try:
                yield
            finally:
                try:
                    connection.execute(
                        "SELECT pg_advisory_unlock(%s, %s)",
                        (_KNOWHOW_PROJECTION_LOCK_NAMESPACE, lock_key),
                    )
                    connection.commit()
                except Exception:
                    # Session close below is the authoritative no-leak cleanup.
                    pass
                finally:
                    connection.close()
        finally:
            self._projection_lock_slots.release()

    def _open_scale_build_lock_connection(self):
        """Open the dedicated, keepalive-armed session one build lock rides on."""
        with self._lifecycle_lock:
            if self._closed:
                raise PostgresDatabaseClosedError(
                    f"PostgreSQL pool is closed for {self._diagnostic_url}"
                )
            return _SafeDiagnosticConnection.connect(
                self._database_url,
                autocommit=False,
                row_factory=dict_row,
                application_name="silicon-notebook-scale-build-lock",
                connect_timeout=self._projection_connect_timeout_seconds,
                **_LOCK_SESSION_KEEPALIVES,
            )

    @staticmethod
    def _disable_idle_session_timeout(
        conn: psycopg.Connection[PostgresRow],
    ) -> None:
        """Stop a managed instance from reaping this idle lock session.

        ``idle_session_timeout`` is commonly non-zero on hosted PostgreSQL and
        would terminate the holder mid-build — which releases the advisory lock
        *silently*, leaving two writers on one artifact tree. The GUC is USERSET
        so this always works where it exists; it does not exist before
        PostgreSQL 14, where there is no such reaper to disable.
        """
        try:
            conn.execute("SET idle_session_timeout = 0")
            conn.commit()
        except psycopg.Error:
            try:
                conn.rollback()
            except Exception:  # noqa: BLE001 - reported by the next statement
                pass

    def try_scale_build_lock(self, notebook_id: str) -> ScaleBuildLockAttempt:
        """Claim one notebook's scale-index build across processes, or fail fast.

        Three outcomes, per ``scale_build_lock``'s ``ScaleBuildLockAttempt``:

        * a held :class:`PostgresScaleBuildLock`;
        * ``None`` — ``pg_try_advisory_lock`` said somebody else owns this
          notebook (another thread, another service process, the offline CLI);
        * ``SCALE_BUILD_LOCK_UNAVAILABLE`` — this process has spent its budget
          of dedicated lock sessions, so the claim was never even asked about.

        The last two used to share ``None``. They are opposite facts: "held
        elsewhere" is a true statement about the NOTEBOOK, while an exhausted
        session budget is a fact about THIS PROCESS, and reporting it as
        "already building" both lies and (with no queue entry behind it) loses
        the request (codex W-CLI R1 P1-1).

        Non-blocking on purpose: admission runs on request-serving threads and
        an offline build legitimately runs for tens of minutes. Unlike
        ``table_projection_lock`` this session keeps the pool's normal
        ``statement_timeout`` — it never waits on a lock, and an unbounded
        timeout would let a wedged re-verification stall a swap forever.
        """
        lock_key = advisory_lock_key(notebook_id)
        self._ensure_projection_lock_open()
        if not self._scale_build_lock_slots.acquire(blocking=False):
            return SCALE_BUILD_LOCK_UNAVAILABLE
        connection = None
        try:
            connection = self._open_scale_build_lock_connection()
            self._restore_session_defaults(connection)
            # Any SET has to follow the RESET ALL inside the call above, or it
            # is wiped before it ever takes effect (see table_projection_lock).
            connection.execute(
                "SET application_name = 'silicon-notebook-scale-build-lock'"
            )
            connection.commit()
            self._disable_idle_session_timeout(connection)
            row = connection.execute(
                "SELECT pg_try_advisory_lock(%s, %s) AS acquired",
                (_SCALE_BUILD_LOCK_NAMESPACE, lock_key),
            ).fetchone()
            connection.commit()
            acquired = bool(row is not None and row["acquired"])
        except PostgresDatabaseClosedError:
            self._close_and_release_lock_session(connection)
            raise
        except Exception:
            self._close_and_release_lock_session(connection)
            raise self._safe_error("scale build lock acquisition") from None
        if not acquired:
            self._close_and_release_lock_session(connection)
            return None
        return PostgresScaleBuildLock(
            connection,
            _SCALE_BUILD_LOCK_NAMESPACE,
            lock_key,
            self._scale_build_lock_slots.release,
        )

    def _close_and_release_lock_session(self, connection) -> None:
        """Give the session budget back on every path that keeps no handle."""
        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - nothing left to salvage
                pass
        self._scale_build_lock_slots.release()

    def close(self) -> None:
        """Close the pool once. Closing an unopened/already-closed pool is safe."""
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            self._pool.close()
