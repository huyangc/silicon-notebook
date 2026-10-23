"""SQLite persistence for MCP answer links (v94): expiring anonymous links to
one answered ``ask`` job, each issued by one Agent token."""
from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.core.capability_tokens import ANSWER_LINK_PREFIX, new_capability_token
from app.repositories.sqlite.database import SqliteDatabase

_SWEEP_INTERVAL_SECONDS = 3600


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    # One fixed-width UTC spelling, so the stored text orders as the instant.
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
        database: SqliteDatabase,
        *,
        now: Callable[[], str],
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self.database = database
        self.now = now
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
            "SELECT token, expires_at FROM answer_links WHERE job_kind = ? "
            "AND job_id = ? AND agent_token_id = ?",
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
        now = _instant(self.now())
        candidate = new_capability_token(ANSWER_LINK_PREFIX)
        sweep = self._sweep_due()
        with self.database.write(operation="answer_links.issue") as db:
            if sweep:
                self._sweep(db, now, sweep_grace_seconds)
            db.execute(
                "DELETE FROM answer_links WHERE job_kind = ? AND job_id = ? "
                "AND agent_token_id = ? AND expires_at <= ?",
                (job_kind, job_id, agent_token_id, _iso(now)),
            )
            row = None
            for _attempt in range(2):
                db.execute(
                    "INSERT INTO answer_links (" + _COLUMNS + ") VALUES (?,?,?,?,?,?,?) "
                    "ON CONFLICT (job_kind, job_id, agent_token_id) DO NOTHING",
                    (
                        candidate, job_kind, job_id, owner_id, agent_token_id,
                        _iso(now), _iso(now + timedelta(seconds=ttl_seconds)),
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
            "token": row["token"],
            "expires_at": row["expires_at"],
            "reused": row["token"] != candidate,
        }

    def answer_link(self, token: str) -> dict[str, Any] | None:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT " + _COLUMNS + " FROM answer_links WHERE token = ?", (token,)
            ).fetchone()
        if row is None:
            return None
        return {
            "token": row["token"],
            "job_kind": row["job_kind"],
            "job_id": row["job_id"],
            "owner_id": row["owner_id"],
            "agent_token_id": row["agent_token_id"],
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
            "expired": _instant(row["expires_at"]) <= _instant(self.now()),
        }

    # 测试观察点:显式清扫入口,生产在签发时顺带清扫(同一 _sweep)
    def sweep_answer_links(self, *, grace_seconds: int) -> int:
        now = _instant(self.now())
        with self.database.write(operation="answer_links.sweep") as db:
            return self._sweep(db, now, grace_seconds)

    @staticmethod
    def _sweep(db: Any, now: datetime, grace_seconds: int) -> int:
        cutoff = _iso(now - timedelta(seconds=grace_seconds))
        removed = 0
        for where, params in (
            ("expires_at < ?", (cutoff,)),
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
