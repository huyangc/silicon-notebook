"""PostgreSQL persistence for MCP answer links (0074): expiring anonymous links
to one answered ``ask`` job, each issued by one Agent token."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta
from typing import Any, Callable

from app.core.capability_tokens import ANSWER_LINK_PREFIX, new_capability_token
from app.repositories.postgres._store_utils import (
    TimestampInput,
    iso_timestamp,
    normalized_clock,
)
from app.repositories.postgres.database import PostgresDatabase

_SWEEP_INTERVAL_SECONDS = 3600
_COLUMNS = "token, job_kind, job_id, owner_id, agent_token_id, created_at, expires_at"


class AnswerLinkStore:
    """Rows are never trusted past their ``expires_at``: ``answer_link`` reports
    ``expired`` from this store's clock and callers decide what to show.

    ``issue_answer_link`` is the only write path and also the only place stale
    rows are purged, at most once per interval per store instance (the throttle
    lives on the instance, not in module state)."""

    def __init__(
        self,
        database: PostgresDatabase,
        *,
        now: Callable[[], TimestampInput],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.database = database
        self.now = normalized_clock(now)
        self._monotonic = monotonic
        self._sweep_lock = threading.Lock()
        self._last_sweep_monotonic: float | None = None

    def _sweep_due(self) -> bool:
        with self._sweep_lock:
            current = self._monotonic()
            last = self._last_sweep_monotonic
            if last is not None and current - last < _SWEEP_INTERVAL_SECONDS:
                return False
            self._last_sweep_monotonic = current
            return True

    def issue_answer_link(
        self,
        *,
        job_kind: str,
        job_id: str,
        owner_id: str,
        agent_token_id: str,
        ttl_seconds: int,
        sweep_grace_seconds: int,
    ) -> dict[str, Any]:
        now = self.now()
        candidate = new_capability_token(ANSWER_LINK_PREFIX)
        sweep = self._sweep_due()
        with self.database.write() as db:
            if sweep:
                self._sweep(db, now, sweep_grace_seconds)
            db.execute(
                "DELETE FROM answer_links WHERE job_kind = %s AND job_id = %s "
                "AND agent_token_id = %s AND expires_at <= %s",
                (job_kind, job_id, agent_token_id, now),
            )
            db.execute(
                "INSERT INTO answer_links (" + _COLUMNS + ") "
                "VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (job_kind, job_id, agent_token_id) DO NOTHING",
                (
                    candidate, job_kind, job_id, owner_id, agent_token_id,
                    now, now + timedelta(seconds=ttl_seconds),
                ),
            )
            # A fresh statement under READ COMMITTED: a concurrent winner whose
            # insert made ours a no-op has committed by now, so it is visible.
            row = db.execute(
                "SELECT token, expires_at FROM answer_links WHERE job_kind = %s "
                "AND job_id = %s AND agent_token_id = %s",
                (job_kind, job_id, agent_token_id),
            ).fetchone()
        return {
            "token": str(row["token"]),
            "expires_at": iso_timestamp(row["expires_at"]),
            "reused": str(row["token"]) != candidate,
        }

    def answer_link(self, token: str) -> dict[str, Any] | None:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT " + _COLUMNS + " FROM answer_links WHERE token = %s", (token,)
            ).fetchone()
        if row is None:
            return None
        return {
            "token": str(row["token"]),
            "job_kind": str(row["job_kind"]),
            "job_id": str(row["job_id"]),
            "owner_id": str(row["owner_id"]),
            "agent_token_id": str(row["agent_token_id"]),
            "created_at": iso_timestamp(row["created_at"]),
            "expires_at": iso_timestamp(row["expires_at"]),
            "expired": row["expires_at"] <= self.now(),
        }

    def sweep_answer_links(self, *, grace_seconds: int) -> int:
        now = self.now()
        with self.database.write() as db:
            return self._sweep(db, now, grace_seconds)

    @staticmethod
    def _sweep(db: Any, now: datetime, grace_seconds: int) -> int:
        cutoff = now - timedelta(seconds=grace_seconds)
        removed = 0
        for where, params in (
            ("expires_at < %s", (cutoff,)),
            (
                "NOT EXISTS (SELECT 1 FROM agent_access_tokens t WHERE t.id = "
                "answer_links.agent_token_id AND t.revoked_at IS NULL)",
                (),
            ),
            (
                "job_kind = 'notebook' AND NOT EXISTS (SELECT 1 FROM ask_jobs j "
                "WHERE j.id = answer_links.job_id)",
                (),
            ),
            (
                "job_kind = 'global' AND NOT EXISTS (SELECT 1 FROM global_ask_jobs j "
                "WHERE j.id = answer_links.job_id)",
                (),
            ),
        ):
            removed += db.execute("DELETE FROM answer_links WHERE " + where, params).rowcount
        return removed
