"""One caller-local deadline for the optional KG lexical supplement."""
from __future__ import annotations

import math
from time import monotonic

from psycopg import errors


class KnowledgeLexicalBudgetExpired(RuntimeError):
    """A bounded lexical phase stopped; earlier completed rows remain usable."""


class KnowledgeLexicalBudget:
    """A read-only connection view sharing one deadline across physical SQL.

    Each SELECT lives in a private savepoint. Rolling that savepoint back on
    success as well as failure restores the original statement timeout without
    issuing another SET under a nearly exhausted timeout. Client-side cursors
    retain their received result rows. Caller-owned writes before the savepoint
    and caller transaction isolation are unaffected.
    """

    def __init__(self, connection, seconds: float):
        if not math.isfinite(seconds) or seconds <= 0:
            raise ValueError("KG lexical budget must be positive and finite")
        self.connection = connection
        self._started = monotonic()
        self.deadline = self._started + seconds
        self.exhausted = False
        self._timeout_ms = None

    def __enter__(self):
        row = self.connection.execute(
            "SELECT setting::bigint AS timeout_ms FROM pg_settings "
            "WHERE name='statement_timeout'"
        ).fetchone()
        self._timeout_ms = int(row["timeout_ms"])
        if self._timeout_ms:
            self.deadline = min(self.deadline, self._started + self._timeout_ms / 1000)
        return self

    def __exit__(self, *_exc):
        if monotonic() >= self.deadline:
            self.exhausted = True

    def execute(self, statement, params=None, **options):
        # KNN availability detection owns a nested savepoint. Its control
        # statements must remain usable after an expired read budget.
        if isinstance(statement, str) and statement.startswith((
            "SAVEPOINT ", "ROLLBACK TO SAVEPOINT ", "RELEASE SAVEPOINT ",
        )):
            return self.connection.execute(statement, params, **options)
        remaining = self.deadline - monotonic()
        if self.exhausted or remaining <= 0:
            self.exhausted = True
            raise KnowledgeLexicalBudgetExpired("KG lexical budget exhausted")
        timeout_ms = max(1, math.ceil(remaining * 1000))
        if self._timeout_ms:
            timeout_ms = min(timeout_ms, self._timeout_ms)
        self.connection.execute("SAVEPOINT kg_lexical_statement")
        try:
            self.connection.execute(
                "SELECT set_config('statement_timeout', %s, true)",
                (f"{timeout_ms}ms",),
            )
            return self.connection.execute(statement, params, **options)
        except errors.QueryCanceled:
            self.exhausted = True
            raise KnowledgeLexicalBudgetExpired("KG lexical budget exhausted") from None
        finally:
            self.connection.execute("ROLLBACK TO SAVEPOINT kg_lexical_statement")
            self.connection.execute("RELEASE SAVEPOINT kg_lexical_statement")

    def __getattr__(self, name):
        return getattr(self.connection, name)
