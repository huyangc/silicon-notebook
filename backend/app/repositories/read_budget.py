"""Execution-local deadlines for bounded read-only repository work."""
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
import time


class ReadBudgetExceeded(TimeoutError):
    """The caller's read budget expired; contains no database diagnostics."""


@dataclass(frozen=True)
class ReadBudget:
    deadline: float
    cancel_event: object = None

    def remaining_seconds(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0 or (
            self.cancel_event is not None and self.cancel_event.is_set()
        ):
            raise ReadBudgetExceeded("read budget exhausted")
        return remaining

    def check(self) -> None:
        self.remaining_seconds()


def classify_read_failure(exc: BaseException) -> "str | None":
    """Name what a driver failure says about a bounded read, or ``None``.

    A read budget is enforced in several places at once and each reports in its
    own vocabulary, so the same underlying fact arrives as a different class
    depending on which layer noticed first:

    * ``"timeout"`` -- the budget itself expired (``ReadBudgetExceeded``), the
      PostgreSQL server cancelled a statement whose ``statement_timeout`` we
      derived from the budget (``psycopg.errors.QueryCanceled``), or SQLite's
      progress handler interrupted a running statement. Narrowing the work is
      the useful response.
    * ``"saturated"`` -- no connection could be leased in the remaining budget
      (``psycopg_pool.PoolTimeout``, which the PostgreSQL adapter re-raises as
      a credential-safe subclass). This one is deliberately NOT ``"timeout"``:
      the query never ran, the pool was full, and telling the user to select
      fewer notebooks would be a guess about someone else's load.
    * ``None`` -- not a budget/lease failure at all.

    Callers in ``app/services`` must not import a database driver to tell those
    apart -- the layering forbids it and the set is repository knowledge -- so
    the mapping lives here, beside the budget that causes them. Driver imports
    are lazy and fail-soft so a deployment running only one backend never pays
    for, or breaks on, the other's absence.
    """
    if isinstance(exc, ReadBudgetExceeded):
        return "timeout"
    import sqlite3

    if isinstance(exc, sqlite3.OperationalError):
        # sqlite3 has no dedicated class for an interrupted statement; the
        # message is the only signal the driver gives.
        return "timeout" if "interrupted" in str(exc).lower() else None
    from importlib import import_module

    for module_name, attribute, reason in (
        ("psycopg_pool", "PoolTimeout", "saturated"),
        ("psycopg.errors", "QueryCanceled", "timeout"),
    ):
        try:
            candidate = getattr(import_module(module_name), attribute, None)
        except Exception:  # noqa: BLE001 — backend not installed
            candidate = None
        if candidate is not None and isinstance(exc, candidate):
            return reason
    return None


# SQLite primary result codes (sqlite3.h); ``sqlite3.Error.sqlite_errorcode``
# carries the extended code, whose low byte is the primary one.
_SQLITE_INTERRUPT = 9
_SQLITE_TOOBIG = 18
# PostgreSQL SQLSTATE class 54 "program limit exceeded" (54000, 54001
# statement_too_complex, 54011 too_many_columns, 54023 too_many_arguments).
_PG_LIMIT_CLASS = "54"
_PG_QUERY_CANCELED = "57014"


def classify_statement_failure(exc: BaseException) -> str:
    """Content-free reason for a swallowed statement failure.

    One of a fixed set, decided by exception type, SQLSTATE or SQLite result
    code -- never by message text (which can quote data):

    * ``"statement_timeout"`` -- the statement was cancelled by a deadline: the
      read budget (``ReadBudgetExceeded``), a store's private lexical budget
      (``ChunkLexicalSearchTimeout``), PostgreSQL ``statement_timeout`` /
      cancellation (SQLSTATE 57014), or SQLite's progress-handler interrupt
      (``SQLITE_INTERRUPT``).
    * ``"variable_limit"`` -- the statement exceeded a size limit the driver or
      server reports with a code: PostgreSQL SQLSTATE class 54 (program limit
      exceeded, e.g. 54023 too many arguments) and SQLite ``SQLITE_TOOBIG``
      (a bound value or the statement too large).  The per-statement bound
      parameter caps themselves carry no code -- SQLite reports "too many SQL
      variables" as a generic ``SQLITE_ERROR`` and libpq refuses more than
      65,535 parameters client-side without a SQLSTATE -- so what keeps those
      from recurring is ``tests/test_id_list_binding_guard.py``, not this code.
    * ``"other"`` -- anything else.
    """
    if isinstance(exc, ReadBudgetExceeded):
        return "statement_timeout"
    from app.repositories.ports import ChunkLexicalSearchTimeout

    if isinstance(exc, ChunkLexicalSearchTimeout):
        return "statement_timeout"
    import sqlite3

    if isinstance(exc, sqlite3.Error):
        code = getattr(exc, "sqlite_errorcode", None)
        primary = code & 0xFF if isinstance(code, int) else None
        if primary == _SQLITE_INTERRUPT:
            return "statement_timeout"
        if primary == _SQLITE_TOOBIG:
            return "variable_limit"
        return "other"
    sqlstate = getattr(exc, "sqlstate", None)
    if isinstance(sqlstate, str):
        if sqlstate == _PG_QUERY_CANCELED:
            return "statement_timeout"
        if sqlstate.startswith(_PG_LIMIT_CLASS):
            return "variable_limit"
    return "other"


_CURRENT = ContextVar("repository_read_budget", default=None)


def current_read_budget():
    return _CURRENT.get()


@contextmanager
def read_budget(deadline: float, cancel_event=None):
    previous = current_read_budget()
    budget = ReadBudget(
        min(deadline, previous.deadline) if previous else deadline,
        cancel_event if cancel_event is not None else (
            previous.cancel_event if previous else None
        ),
    )
    token = _CURRENT.set(budget)
    try:
        budget.check()
        yield budget
    finally:
        _CURRENT.reset(token)
