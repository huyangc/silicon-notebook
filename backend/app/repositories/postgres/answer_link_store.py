"""PostgreSQL persistence for MCP answer links (0074): expiring anonymous links
to one answered ``ask`` job, each issued by one Agent token."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.core.capability_tokens import ANSWER_LINK_PREFIX, new_capability_token
from app.repositories.postgres._store_utils import (
    TimestampInput,
    normalized_clock,
)
from app.repositories.postgres.database import PostgresDatabase

_SWEEP_INTERVAL_SECONDS = 3600


def _iso(value: datetime) -> str:
    # The same fixed-width UTC spelling the SQLite store keeps.
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")
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
            last = self._last_sweep_monotonic
            return last is None or self._monotonic() - last >= _SWEEP_INTERVAL_SECONDS

    def _mark_swept(self) -> None:
        # Only after the write committed: a failed transaction rolls its sweep
        # back, and must not cost the next hour of sweeping.
        with self._sweep_lock:
            self._last_sweep_monotonic = self._monotonic()

    @staticmethod
    def _read_key(db: Any, job_kind: str, job_id: str, agent_token_id: str) -> Any:
        return db.execute(
            "SELECT token, expires_at FROM answer_links WHERE job_kind = %s "
            "AND job_id = %s AND agent_token_id = %s",
            (job_kind, job_id, agent_token_id),
        ).fetchone()

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
            row = None
            # A fresh statement under READ COMMITTED: a concurrent winner whose
            # insert made ours a no-op has committed by now, so it is visible.
            # If that winner was swept away meanwhile, insert once more.
            for _attempt in range(2):
                db.execute(
                    "INSERT INTO answer_links (" + _COLUMNS + ") "
                    "VALUES (%s,%s,%s,%s,%s,%s,%s) "
                    "ON CONFLICT (job_kind, job_id, agent_token_id) DO NOTHING",
                    (
                        candidate, job_kind, job_id, owner_id, agent_token_id,
                        now, now + timedelta(seconds=ttl_seconds),
                    ),
                )
                row = self._read_key(db, job_kind, job_id, agent_token_id)
                if row is not None:
                    break
            if row is None:
                raise RuntimeError("answer link vanished between insert and read-back")
        if sweep:
            self._mark_swept()
        return {
            "token": str(row["token"]),
            "expires_at": _iso(row["expires_at"]),
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
            "created_at": _iso(row["created_at"]),
            "expires_at": _iso(row["expires_at"]),
            "expired": row["expires_at"] <= self.now(),
        }

    # 测试观察点:显式清扫入口,生产在签发时顺带清扫(同一 _sweep)
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
