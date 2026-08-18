"""PostgreSQL row persistence for Agentic Memory P1's per-notebook
"understanding".

A byte-for-byte behavioural mirror of
``app/repositories/sqlite/agent_profile_store.py``: same method names, same
bounds, same return shapes (``evidence``/``history`` decoded to Python lists,
timestamps normalised to ISO strings). The genuine differences are the ones
the backends disagree about — ``jsonb`` instead of JSON text, ``timestamptz``
instead of ISO strings (with the historical empty-string sentinel restored on
read for ``started_at``/``finished_at``, which are nullable here and
default-``''`` on SQLite), ``FOR UPDATE`` row locking in place of SQLite's
process-wide write serialisation, and ``COLLATE "C"`` on the ordering keys so
a non-C-collated database still pages in the same order SQLite does.
"""
from __future__ import annotations

from typing import Any, Callable, Mapping, Sequence

from psycopg import errors

from app.repositories.postgres._store_utils import (
    TimestampInput,
    iso_timestamp,
    json_value,
    jsonb,
    normalized_clock,
)
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.ports import (
    AGENT_PROFILE_JOB_TERMINAL_STATUSES,
    AGENT_PROFILE_RESTART_FAILURE_MESSAGE,
    AgentProfileRevisionConflict,
    append_profile_history as _append_history,
)


class AgentProfileStore:
    def __init__(
        self,
        database: PostgresDatabase,
        *,
        new_id: Callable[[str], str],
        now: Callable[[], TimestampInput],
    ) -> None:
        self.database = database
        # Construction parity with the rest of the bundle only — this store
        # synthesises no ids (both tables are keyed by caller-supplied
        # tuples). Same note as the SQLite mirror.
        self.new_id = new_id
        self.now = normalized_clock(now)

    # ----------------------------------------------------------------- blocks
    @staticmethod
    def _block_row(row) -> dict:
        return {
            "notebook_id": row["notebook_id"],
            "owner_id": row["owner_id"],
            "label": row["label"],
            "value": row["value"],
            "evidence": json_value(row["evidence_json"], []) or [],
            "history": json_value(row["history_json"], []) or [],
            "revision": int(row["revision"]),
            "updated_by": row["updated_by"],
            "updated_origin": row["updated_origin"],
            "created_at": iso_timestamp(row["created_at"]),
            "updated_at": iso_timestamp(row["updated_at"]),
        }

    @staticmethod
    def _job_row(row) -> dict:
        return {
            "notebook_id": row["notebook_id"],
            "owner_id": row["owner_id"],
            "status": row["status"],
            "pending_signal": int(row["pending_signal"]),
            "runs": int(row["runs"]),
            "blocks_written": int(row["blocks_written"]),
            "failure_reason": row["failure_reason"],
            "diagnostic": row["diagnostic"],
            "started_at": iso_timestamp(row["started_at"]),
            "finished_at": iso_timestamp(row["finished_at"]),
            "created_at": iso_timestamp(row["created_at"]),
            "updated_at": iso_timestamp(row["updated_at"]),
        }

    def read_blocks(self, notebook_id: str, owner_id: str) -> list[dict]:
        """The shared base layer (``owner_id=''``) plus, when ``owner_id`` is
        non-empty, that one member's overlay — nothing else. Same baked-in
        ``owner_id IN ('', %s)`` predicate as the SQLite mirror."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_notebook_profile "
                "WHERE notebook_id=%s AND owner_id IN ('', %s) "
                'ORDER BY owner_id COLLATE "C", label COLLATE "C"',
                (notebook_id, owner_id),
            ).fetchall()
        return [self._block_row(row) for row in rows]

    def read_block(
        self, notebook_id: str, owner_id: str, label: str
    ) -> dict | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_notebook_profile "
                "WHERE notebook_id=%s AND owner_id=%s AND label=%s",
                (notebook_id, owner_id, label),
            ).fetchone()
        return self._block_row(row) if row is not None else None

    def write_block(
        self,
        notebook_id: str,
        owner_id: str,
        label: str,
        *,
        value: str,
        evidence: Sequence[Mapping[str, Any]],
        expected_revision: int,
        origin: str,
        actor: str,
    ) -> dict:
        """Upsert inside ONE write transaction. ``expected_revision=0`` means
        "no row yet"; a concurrent creator racing the same ``INSERT`` is
        caught as ``UniqueViolation`` and re-raised as
        ``AgentProfileRevisionConflict`` rather than left to bubble as a raw
        DB error. Any other ``expected_revision`` is compared, under
        ``FOR UPDATE``, against the row's stored ``revision``."""
        now = self.now()
        evidence_list = list(evidence)
        expected = int(expected_revision)
        with self.database.write() as connection:
            if expected == 0:
                history = _append_history(
                    [], None, value, origin, actor, iso_timestamp(now), 1
                )
                try:
                    connection.execute(
                        "INSERT INTO agent_notebook_profile "
                        "(notebook_id,owner_id,label,value,evidence_json,"
                        "history_json,revision,updated_by,updated_origin,"
                        "created_at,updated_at) "
                        "VALUES (%s,%s,%s,%s,%s,%s,1,%s,%s,%s,%s)",
                        (
                            notebook_id,
                            owner_id,
                            label,
                            value,
                            jsonb(evidence_list),
                            jsonb(history),
                            actor,
                            origin,
                            now,
                            now,
                        ),
                    )
                except errors.UniqueViolation as exc:
                    raise AgentProfileRevisionConflict(
                        notebook_id, owner_id, label
                    ) from exc
            else:
                row = connection.execute(
                    "SELECT value, revision, history_json FROM agent_notebook_profile "
                    "WHERE notebook_id=%s AND owner_id=%s AND label=%s FOR UPDATE",
                    (notebook_id, owner_id, label),
                ).fetchone()
                if row is None or int(row["revision"]) != expected:
                    raise AgentProfileRevisionConflict(notebook_id, owner_id, label)
                new_revision = expected + 1
                history = _append_history(
                    json_value(row["history_json"], []),
                    row["value"],
                    value,
                    origin,
                    actor,
                    iso_timestamp(now),
                    new_revision,
                )
                connection.execute(
                    "UPDATE agent_notebook_profile SET value=%s,evidence_json=%s,"
                    "history_json=%s,revision=%s,updated_by=%s,updated_origin=%s,"
                    "updated_at=%s "
                    "WHERE notebook_id=%s AND owner_id=%s AND label=%s",
                    (
                        value,
                        jsonb(evidence_list),
                        jsonb(history),
                        new_revision,
                        actor,
                        origin,
                        now,
                        notebook_id,
                        owner_id,
                        label,
                    ),
                )
            result = connection.execute(
                "SELECT * FROM agent_notebook_profile "
                "WHERE notebook_id=%s AND owner_id=%s AND label=%s",
                (notebook_id, owner_id, label),
            ).fetchone()
        return self._block_row(result)

    def clear_block(
        self,
        notebook_id: str,
        owner_id: str,
        label: str,
        *,
        expected_revision: int,
        actor: str,
    ) -> dict:
        """Blank a block's value AND evidence while KEEPING the row and its
        history. Same CAS as ``write_block``; raises ``KeyError`` if the
        block was never written. ``updated_origin`` is fixed to ``'user'`` —
        see the SQLite mirror for why there is no ``origin`` parameter."""
        now = self.now()
        expected = int(expected_revision)
        with self.database.write() as connection:
            row = connection.execute(
                "SELECT value, revision, history_json FROM agent_notebook_profile "
                "WHERE notebook_id=%s AND owner_id=%s AND label=%s FOR UPDATE",
                (notebook_id, owner_id, label),
            ).fetchone()
            if row is None:
                raise KeyError(label)
            if int(row["revision"]) != expected:
                raise AgentProfileRevisionConflict(notebook_id, owner_id, label)
            new_revision = expected + 1
            history = _append_history(
                json_value(row["history_json"], []),
                row["value"],
                "",
                "user",
                actor,
                iso_timestamp(now),
                new_revision,
            )
            connection.execute(
                "UPDATE agent_notebook_profile SET value='',evidence_json=%s,"
                "history_json=%s,revision=%s,updated_by=%s,"
                "updated_origin='user',updated_at=%s "
                "WHERE notebook_id=%s AND owner_id=%s AND label=%s",
                (
                    jsonb([]),
                    jsonb(history),
                    new_revision,
                    actor,
                    now,
                    notebook_id,
                    owner_id,
                    label,
                ),
            )
            result = connection.execute(
                "SELECT * FROM agent_notebook_profile "
                "WHERE notebook_id=%s AND owner_id=%s AND label=%s",
                (notebook_id, owner_id, label),
            ).fetchone()
        return self._block_row(result)

    def clear_all(self, notebook_id: str, owner_id: str) -> int:
        """Delete every block row for one (notebook, owner) scope outright.
        No CAS: a full-scope wipe. Returns the row count deleted."""
        with self.database.write() as connection:
            cursor = connection.execute(
                "DELETE FROM agent_notebook_profile WHERE notebook_id=%s AND owner_id=%s",
                (notebook_id, owner_id),
            )
        return cursor.rowcount

    # ------------------------------------------------------------------- jobs
    def job_row(self, notebook_id: str, owner_id: str) -> dict | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_profile_jobs WHERE notebook_id=%s AND owner_id=%s",
                (notebook_id, owner_id),
            ).fetchone()
        return self._job_row(row) if row is not None else None

    def bump_signal(self, notebook_id: str, owner_id: str, delta: int = 1) -> int:
        """Zero-scan primary-key upsert; returns the new ``pending_signal``
        count. The row is created with ``status='idle'`` (column default) on
        first touch. Negative ``delta`` is rejected — see the SQLite mirror
        for why an uncontrolled decrement is not clamped but refused."""
        if int(delta) < 0:
            raise ValueError("agent profile pending_signal delta must not be negative")
        now = self.now()
        with self.database.write() as connection:
            row = connection.execute(
                "INSERT INTO agent_profile_jobs "
                "(notebook_id,owner_id,pending_signal,created_at,updated_at) "
                "VALUES (%s,%s,%s,%s,%s) "
                "ON CONFLICT (notebook_id,owner_id) DO UPDATE SET "
                "pending_signal=agent_profile_jobs.pending_signal+EXCLUDED.pending_signal,"
                "updated_at=EXCLUDED.updated_at "
                "RETURNING pending_signal",
                (notebook_id, owner_id, int(delta), now, now),
            ).fetchone()
        return int(row["pending_signal"])

    def claim(self, notebook_id: str, owner_id: str) -> int | None:
        """Take the single-flight slot and return the ``pending_signal``
        snapshot, or ``None`` when it was already held. See the SQLite mirror
        for the full reasoning (row-ensure so a never-signalled chain is
        claimable, rowcount-decided CAS, previous-run reset, and why the
        snapshot comes back from the winning statement itself).

        ``ON CONFLICT DO NOTHING`` is this backend's ``INSERT OR IGNORE``;
        ``finished_at`` resets to ``NULL`` rather than ``''`` because it is a
        nullable ``timestamptz`` here — ``_job_row`` normalises that back to
        the shared ``""`` sentinel on read."""
        now = self.now()
        with self.database.write() as connection:
            connection.execute(
                "INSERT INTO agent_profile_jobs "
                "(notebook_id,owner_id,created_at,updated_at) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (notebook_id,owner_id) DO NOTHING",
                (notebook_id, owner_id, now, now),
            )
            row = connection.execute(
                "UPDATE agent_profile_jobs SET status='running',started_at=%s,"
                "finished_at=NULL,failure_reason='',diagnostic='',blocks_written=0,"
                "updated_at=%s WHERE notebook_id=%s AND owner_id=%s "
                "AND status NOT IN ('queued','running') "
                "RETURNING pending_signal",
                (now, now, notebook_id, owner_id),
            ).fetchone()
        return int(row["pending_signal"]) if row is not None else None

    def settle(
        self,
        notebook_id: str,
        owner_id: str,
        status: str,
        *,
        failure_reason: str = "",
        diagnostic: str = "",
        blocks_written: int = 0,
        consumed: int,
    ) -> bool:
        """See the SQLite mirror for the ``consumed``/CAS reasoning.
        ``GREATEST`` is this backend's spelling of SQLite's two-argument
        ``MAX`` — same floor, same single statement."""
        if status not in AGENT_PROFILE_JOB_TERMINAL_STATUSES:
            raise ValueError("agent profile job terminal status is not recognised")
        now = self.now()
        written = max(0, int(blocks_written))
        taken = max(0, int(consumed))
        with self.database.write() as connection:
            cursor = connection.execute(
                "UPDATE agent_profile_jobs SET status=%s,failure_reason=%s,"
                "diagnostic=%s,blocks_written=%s,runs=runs+1,"
                "pending_signal=GREATEST(0, pending_signal - %s),"
                "finished_at=%s,updated_at=%s WHERE notebook_id=%s AND owner_id=%s "
                "AND status IN ('queued','running')",
                (
                    status, failure_reason, diagnostic, written, taken,
                    now, now, notebook_id, owner_id,
                ),
            )
        return cursor.rowcount == 1

    def sweep_stale_on_start(self) -> int:
        """Startup crash recovery — see the SQLite mirror. The reason text is
        the shared ``AGENT_PROFILE_RESTART_FAILURE_MESSAGE`` constant, not a
        literal in this SQL: it is user-facing, and a per-backend copy would
        drift. Returns the row count swept."""
        now = self.now()
        with self.database.write() as connection:
            cursor = connection.execute(
                "UPDATE agent_profile_jobs SET status='failed',"
                "failure_reason=%s,finished_at=%s,updated_at=%s "
                "WHERE status IN ('queued','running')",
                (AGENT_PROFILE_RESTART_FAILURE_MESSAGE, now, now),
            )
        return cursor.rowcount

    def clear_job_row(self, notebook_id: str, owner_id: str) -> int:
        """Delete this chain's status/threshold row outright — see the port
        docstring (the job-table counterpart of ``clear_all``). Primary-key
        delete, at most one row."""
        with self.database.write() as connection:
            cursor = connection.execute(
                "DELETE FROM agent_profile_jobs WHERE notebook_id=%s AND owner_id=%s",
                (notebook_id, owner_id),
            )
        return cursor.rowcount
