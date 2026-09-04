"""Bounded PostgreSQL connection pool and transaction boundary."""
from __future__ import annotations

import logging
import math
import secrets
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
# 批 3·W1 PR-3 §4.3: this session/``application_name`` is no longer
# scale-build-only — notebook delete's phase 4 (§T-3b) claims the SAME
# per-notebook namespace+key before sweeping the notebook's disk artifacts,
# so a session sitting here in ``pg_locks``/``pg_stat_activity`` may be
# either a build or a delete. The literal name is kept generic on purpose
# (``try_scale_build_lock`` the METHOD keeps its name — every call site and
# test already spells it, and design doc §4.3 only asks for the
# application_name/docs wording to generalize, not a method rename) so an
# operator reading `pg_stat_activity` does not go looking for a "build" that
# does not exist. See ``docs/operations.md``'s `pg_locks` troubleshooting
# section for the operator-facing side of this.
_NOTEBOOK_EXCLUSIVE_LOCK_APPLICATION_NAME = "silicon-notebook-notebook-exclusive-lock"
# A lock session sits idle for the whole build (hours on a 9M-object library).
# TCP keepalives make a dead peer surface as a broken session -- which releases
# the advisory lock -- instead of a half-open socket that looks alive forever.
_LOCK_SESSION_KEEPALIVES = {
    "keepalives": 1,
    "keepalives_idle": 30,
    "keepalives_interval": 10,
    "keepalives_count": 5,
}
# ``verify_held`` is now called from inside the process-global
# ``building_lock`` (codex PR#643 R6 P1: the fold swap's claim check must be
# live, not a pre-lock snapshot). That lock is shared by every notebook's
# status poll and admission, so a network stall on this session's ``pg_locks``
# round trip must not ride the pool's normal 30s ``statement_timeout`` — that
# would freeze every notebook, not just the one being verified. ``SET LOCAL``
# (via ``set_config(..., true)``) scopes this to the single verify query's
# transaction and reverts to the session default on the next commit.
_VERIFY_HELD_STATEMENT_TIMEOUT_MS = 5000
# codex PR#643 R19 P2-a: ``statement_timeout`` above only bounds SERVER-side
# execution of the query it wraps — not the SET LOCAL call that installs it,
# and not a stalled TCP send/recv on either statement. If PostgreSQL or the
# network wedges mid-round-trip, both are unbounded at the client, and
# ``verify_held`` runs from inside the process-global ``building_lock``, so
# every notebook's status/admission would stay blocked until the OS notices
# the dead peer via keepalives (``_LOCK_SESSION_KEEPALIVES`` above — roughly
# 30 + 10*5 = 80s, more on some platforms). These two connection-level
# parameters cap the CLIENT side of that same round trip so it fails no later
# than the server-side cap does:
#
# * ``connect_timeout`` (seconds) bounds the initial TCP handshake/startup —
#   the same knob ``table_projection_lock``'s dedicated session uses, but
#   fixed at the ``verify_held`` tier rather than derived from the pool's
#   (potentially much larger) acquire timeout.
# * ``tcp_user_timeout`` (milliseconds) bounds how long the kernel will wait
#   for an ACKed send before giving up on the socket, which is what actually
#   caps a stalled ``set_config``/query send or read once the connection is
#   already established. libpq only forwards it where the OS TCP stack
#   supports ``TCP_USER_TIMEOUT`` (Linux); on platforms without it — notably
#   macOS, so every local dev run — libpq silently ignores the parameter and
#   the session falls back to the keepalive-only bound above. Production runs
#   on Linux, where this is the parameter that actually fires; a developer
#   seeing the old ~80s keepalive bound on macOS is that expected fallback,
#   not a regression.
_LOCK_SESSION_CONNECT_TIMEOUT_SECONDS = 5
_LOCK_SESSION_TCP_USER_TIMEOUT_MS = 6000


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
        # Minted once per acquisition (not per notebook): a claim-unique
        # staging-path suffix (codex PR#643 R1 P1). Random hex rather than
        # ``pg_backend_pid()`` — an extra round trip this constructor would
        # otherwise need, and OS pids can be recycled across a later session
        # on the same notebook, which a purely random token cannot collide
        # with by construction.
        self.claim_token = secrets.token_hex(8)

    def verify_held(self) -> bool:
        """Re-read the lock from ``pg_locks`` on the owning session.

        This is the guard in front of the only destructive step (the artifact
        swap).  A session that was terminated -- managed-PostgreSQL idle
        reaper, failover, operator ``pg_terminate_backend`` -- released the
        advisory lock silently, so a heartbeat that merely proves the object
        still exists in this process proves nothing about the database.

        A caller (the fold swap) may hold the process-global ``building_lock``
        for the duration of this call, so the ``pg_locks`` query is capped to
        ``_VERIFY_HELD_STATEMENT_TIMEOUT_MS`` -- an upper bound on how long
        that freezes every other notebook's status/admission, well under the
        pool's normal statement_timeout. A timeout or any other failure here
        falls through to the existing "never raises -> False" contract: an
        unusable/slow session is conservatively treated as lock-lost, which
        only makes the swap refuse more eagerly, never less.
        """
        with self._mutex:
            if self._released:
                return False
            try:
                self._connection.execute(
                    "SELECT set_config('statement_timeout', %s, true)",
                    (f"{_VERIFY_HELD_STATEMENT_TIMEOUT_MS}ms",),
                )
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
        # on this process. Sized one above the ceiling so an admission probe
        # can always run while every executing build/delete holds a session.
        #
        # 批 3·W1 PR-3 §4.3: this is the SAME namespace/key notebook delete's
        # phase 4 claims (design doc §T-3b — one per-notebook independent
        # lock covers both "scale build" and "notebook delete", not two
        # separate locks), so a long-running delete now also occupies one of
        # these dedicated sessions for its whole phase-4 sweep. The "+1"
        # comment above ("an admission probe can always run") stops being
        # true if the ceiling only counts builds — a delete's session is
        # invisible to that headroom and `_admit_scale_op`'s probe starts
        # seeing `SCALE_BUILD_LOCK_UNAVAILABLE` ("undecidable") instead of a
        # real answer whenever every build AND every delete slot is full.
        # Cross-checked against §1.2's connection-budget accounting and
        # §T-4's `NOTEBOOK_DELETE_CONCURRENCY` (default 1, max 2) — the same
        # config value all three call sites (here, background_jobs's delete
        # pool, and this comment) must share.
        self._scale_build_lock_capacity = max(
            1,
            int(getattr(settings, "scale_build_concurrency", 2))
            + int(getattr(settings, "notebook_delete_concurrency", 1))
            + 1,
        )
        self._scale_build_lock_slots = threading.BoundedSemaphore(
            self._scale_build_lock_capacity
        )
        # Active dedicated lock sessions (P2, codex PR#643 R33): the pool
        # only tracks pooled connections, so without this registry a held
        # scale-build claim outlives ``close()`` — the advisory lock stays
        # granted, other instances keep seeing the notebook busy, and a
        # detached worker's ``verify_held`` still answers True after
        # repository shutdown.
        self._scale_build_lock_registry_lock = threading.Lock()
        self._active_scale_build_locks: set = set()
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
        """Open the dedicated, keepalive-armed session one build lock rides on.

        ``connect_timeout``/``tcp_user_timeout`` are this session's own client
        transport bound (codex PR#643 R19 P2-a) — deliberately NOT
        ``self._projection_connect_timeout_seconds`` (derived from the pool's
        acquire timeout, and shared with ``table_projection_lock``'s dedicated
        session): a wedged ``verify_held`` round trip runs inside the
        process-global ``building_lock`` and must fail no later than that
        call's own ``_VERIFY_HELD_STATEMENT_TIMEOUT_MS`` server-side cap, so
        this session gets a fixed bound at the same tier instead of whatever
        the pool happens to be configured with. See the constants' docstring
        above for the platform caveat: ``tcp_user_timeout`` is a no-op where
        the OS does not support it (macOS/local dev falls back to the
        keepalive bound; production Linux is where it actually fires).
        """
        with self._lifecycle_lock:
            if self._closed:
                raise PostgresDatabaseClosedError(
                    f"PostgreSQL pool is closed for {self._diagnostic_url}"
                )
            return _SafeDiagnosticConnection.connect(
                self._database_url,
                autocommit=False,
                row_factory=dict_row,
                application_name=_NOTEBOOK_EXCLUSIVE_LOCK_APPLICATION_NAME,
                connect_timeout=_LOCK_SESSION_CONNECT_TIMEOUT_SECONDS,
                tcp_user_timeout=_LOCK_SESSION_TCP_USER_TIMEOUT_MS,
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

    def scale_build_claim_held_anywhere(self, notebook_id: str) -> bool:
        """READ-ONLY probe: is this notebook's exclusive claim granted to ANY
        session (this process, another service process, the offline CLI — or
        notebook delete's phase 4, which shares the namespace)?

        codex #676 R6 P2: the serving process's in-memory ``building`` set
        cannot see the supported "offline CLI beside the live service"
        topology, and reporting a terminal ``viz_unavailable`` while that
        build runs strands an open canvas (the frontend only polls on
        ``viz_building``). Unlike ``try_scale_build_lock`` this NEVER takes
        the lock — a try-acquire probe would momentarily hold the claim and
        could spuriously refuse a real claimer racing it. A plain ``pg_locks``
        read on a pooled connection cannot race anything. Fail-open False:
        an unanswerable probe must not invent a builder that would keep a
        canvas polling forever.
        """
        lock_key = advisory_lock_key(notebook_id)
        try:
            with self.connect() as db:
                row = db.execute(
                    "SELECT EXISTS ("
                    "SELECT 1 FROM pg_locks "
                    "WHERE locktype = 'advisory' AND granted "
                    "AND classid = %s::oid AND objid = %s::oid"
                    ") AS held",
                    (
                        advisory_lock_oid(_SCALE_BUILD_LOCK_NAMESPACE),
                        advisory_lock_oid(lock_key),
                    ),
                ).fetchone()
            return bool(row is not None and row["held"])
        except Exception:  # noqa: BLE001 - unanswerable == no builder observed
            return False

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
                f"SET application_name = '{_NOTEBOOK_EXCLUSIVE_LOCK_APPLICATION_NAME}'"
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
        handle = PostgresScaleBuildLock(
            connection,
            _SCALE_BUILD_LOCK_NAMESPACE,
            lock_key,
            lambda: self._retire_scale_build_lock(handle),
        )
        # Registration is atomic with the closed-state check (P2, codex
        # PR#643 R34): ``close()`` sets ``_closed`` BEFORE it snapshots this
        # registry (both under this lock's happens-before), so either this
        # branch sees ``_closed`` and releases the just-acquired session
        # itself, or the snapshot includes this handle and ``close()``
        # releases it — an acquisition can no longer thread the gap and
        # leave an untracked advisory lock alive after shutdown.
        with self._scale_build_lock_registry_lock:
            closed = self._closed
            if not closed:
                self._active_scale_build_locks.add(handle)
        if closed:
            handle.release()
            raise PostgresDatabaseClosedError(
                "the database closed while the scale build lock was being "
                "acquired; the lock was released and nothing is held"
            )
        return handle

    def _retire_scale_build_lock(self, handle) -> None:
        """The handle's ``on_release``: give the session budget back and drop
        it from the active registry (P2, codex PR#643 R33)."""
        with self._scale_build_lock_registry_lock:
            self._active_scale_build_locks.discard(handle)
        self._scale_build_lock_slots.release()

    def _close_and_release_lock_session(self, connection) -> None:
        """Give the session budget back on every path that keeps no handle."""
        if connection is not None:
            try:
                connection.close()
            except Exception:  # noqa: BLE001 - nothing left to salvage
                pass
        self._scale_build_lock_slots.release()

    def close(self) -> None:
        """Close the pool once. Closing an unopened/already-closed pool is safe.

        Also releases every still-held scale-build lock session (P2, codex
        PR#643 R33): those dedicated connections live outside the pool, so
        closing only the pool would leave their advisory locks granted —
        other instances keep seeing the notebook busy, and a detached build
        worker's ``verify_held`` keeps answering True after this repository
        shut down. ``release()`` is idempotent and thread-safe, so racing a
        worker's own release is harmless; after this, that worker's next
        ``verify_held`` answers False and its publish refuses."""
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            self._pool.close()
        with self._scale_build_lock_registry_lock:
            active = list(self._active_scale_build_locks)
        for handle in active:
            handle.release()
