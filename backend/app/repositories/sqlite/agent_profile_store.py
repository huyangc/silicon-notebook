"""SQLite row persistence for Agentic Memory P1's per-notebook "understanding":
a handful of named text blocks (``agent_notebook_profile``) plus one
consolidation-chain status row per (notebook, owner) (``agent_profile_jobs``).

Row-level only, mirroring ``catalog_store.py``'s split: what the blocks MEAN,
when a chain should fire, and what a consolidation run does with the result
all belong to the future service layer (T3-T5). This module owns exactly the
two tables and every read it exposes is bounded by construction — see
``AgentProfileStorePort`` in ``app/repositories/ports.py`` for the full
contract.
"""
from __future__ import annotations

import json
import sqlite3
from typing import Any, Callable, Mapping, Sequence

from app.repositories.ports import (
    AGENT_PROFILE_JOB_TERMINAL_STATUSES,
    AGENT_PROFILE_RESTART_FAILURE_MESSAGE,
    AgentProfileRevisionConflict,
    append_profile_history as _append_history,
)
from app.repositories.sqlite.database import SqliteDatabase


def _loads_list(raw: object) -> list:
    if isinstance(raw, list):
        return raw
    try:
        value = json.loads(str(raw or "[]"))
    except (TypeError, ValueError):
        return []
    return value if isinstance(value, list) else []


def _block_row(row) -> dict:
    return {
        "notebook_id": row["notebook_id"],
        "owner_id": row["owner_id"],
        "label": row["label"],
        "value": row["value"],
        "evidence": _loads_list(row["evidence_json"]),
        "history": _loads_list(row["history_json"]),
        "revision": int(row["revision"]),
        "updated_by": row["updated_by"],
        "updated_origin": row["updated_origin"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


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
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
    }


class AgentProfileStore:
    def __init__(
        self,
        database: SqliteDatabase,
        *,
        new_id: Callable[[str], str],
        now: Callable[[], str],
    ) -> None:
        self.database = database
        # Kept for construction parity with every other store in the bundle
        # (bundle.py hands all of them the same seams); this store synthesises
        # no ids of its own — both its tables are keyed by caller-supplied
        # (notebook_id, owner_id[, label]) tuples.
        self.new_id = new_id
        self.now = now

    # ----------------------------------------------------------------- blocks
    def read_blocks(self, notebook_id: str, owner_id: str) -> list[dict]:
        """The shared base layer (``owner_id=''``) plus, when ``owner_id`` is
        non-empty, that one member's overlay — nothing else. The
        ``owner_id IN ('', ?)`` predicate is baked into the SQL text itself so
        it can never be bypassed or forgotten by a caller: passing ``''``
        degrades to "base only" through the exact same statement, it does not
        take a different code path."""
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT * FROM agent_notebook_profile "
                "WHERE notebook_id=? AND owner_id IN ('', ?) "
                "ORDER BY owner_id, label",
                (notebook_id, owner_id),
            ).fetchall()
        return [_block_row(row) for row in rows]

    def read_block(
        self, notebook_id: str, owner_id: str, label: str
    ) -> dict | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_notebook_profile "
                "WHERE notebook_id=? AND owner_id=? AND label=?",
                (notebook_id, owner_id, label),
            ).fetchone()
        return _block_row(row) if row is not None else None

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
        """Upsert inside ONE write transaction: the value/evidence change and
        the ``revision`` CAS bump happen together, and the transaction's LAST
        step appends a before/after entry to ``history_json``.

        ``expected_revision=0`` means "no row yet, this write creates it";
        any other value is compared against the stored ``revision`` and
        raises ``AgentProfileRevisionConflict`` on any mismatch, including
        "the row does not exist yet" (a concurrent writer beat us to
        creating it, or the caller is stale).

        ⚠ ``begin_immediate`` opens the SQLite write transaction at the top of
        the block, before the read: ``write()``'s mutex only serialises THIS
        process's writers, and the read-modify-write here (read revision →
        decide → write) is exactly the shape another process sharing the file
        (an offline CLI, a maintenance run) can interleave with. The CAS would
        then pass against a revision that a second writer had already
        superseded — a silent lost update, since both writes "succeed".

        The ``expected_revision=0`` INSERT additionally translates
        ``sqlite3.IntegrityError`` into ``AgentProfileRevisionConflict``, so
        both backends raise the same domain error for "someone created this
        row first" (PostgreSQL catches ``UniqueViolation`` for the same
        branch). With ``begin_immediate`` in place that race is closed on this
        backend, but the translation costs nothing and keeps a raw DB error
        from ever escaping the port's contract."""
        now = self.now()
        evidence_list = list(evidence)
        expected = int(expected_revision)
        with self.database.write() as db:
            self.database.begin_immediate(db)
            row = db.execute(
                "SELECT value, revision, history_json FROM agent_notebook_profile "
                "WHERE notebook_id=? AND owner_id=? AND label=?",
                (notebook_id, owner_id, label),
            ).fetchone()
            if row is None:
                if expected != 0:
                    raise AgentProfileRevisionConflict(notebook_id, owner_id, label)
                history = _append_history([], None, value, origin, actor, now, 1)
                try:
                    db.execute(
                        "INSERT INTO agent_notebook_profile "
                        "(notebook_id,owner_id,label,value,evidence_json,history_json,"
                        "revision,updated_by,updated_origin,created_at,updated_at) "
                        "VALUES (?,?,?,?,?,?,1,?,?,?,?)",
                        (
                            notebook_id,
                            owner_id,
                            label,
                            value,
                            json.dumps(evidence_list, ensure_ascii=False),
                            json.dumps(history, ensure_ascii=False),
                            actor,
                            origin,
                            now,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError as exc:
                    # Primary-key collision: another writer created this block
                    # between the SELECT above and this INSERT. Same domain
                    # error the PostgreSQL mirror raises for its caught
                    # UniqueViolation — never a raw DB error out of the port.
                    raise AgentProfileRevisionConflict(
                        notebook_id, owner_id, label
                    ) from exc
            else:
                if int(row["revision"]) != expected:
                    raise AgentProfileRevisionConflict(notebook_id, owner_id, label)
                new_revision = expected + 1
                history = _append_history(
                    _loads_list(row["history_json"]),
                    row["value"],
                    value,
                    origin,
                    actor,
                    now,
                    new_revision,
                )
                db.execute(
                    "UPDATE agent_notebook_profile SET value=?,evidence_json=?,"
                    "history_json=?,revision=?,updated_by=?,updated_origin=?,"
                    "updated_at=? WHERE notebook_id=? AND owner_id=? AND label=?",
                    (
                        value,
                        json.dumps(evidence_list, ensure_ascii=False),
                        json.dumps(history, ensure_ascii=False),
                        new_revision,
                        actor,
                        origin,
                        now,
                        notebook_id,
                        owner_id,
                        label,
                    ),
                )
            result = db.execute(
                "SELECT * FROM agent_notebook_profile "
                "WHERE notebook_id=? AND owner_id=? AND label=?",
                (notebook_id, owner_id, label),
            ).fetchone()
        return _block_row(result)

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
        history — the single-block counterpart of ``clear_all``'s full-scope
        wipe. Same CAS as ``write_block``; raises ``KeyError`` if the block
        was never written (nothing to clear). ``updated_origin`` is fixed to
        ``'user'``: the consolidation job only ever calls ``write_block``, so
        a clear is by construction a human action and there is no ``origin``
        parameter for a caller to get wrong.

        ``begin_immediate`` for the same reason as ``write_block``: this is a
        read-modify-write whose CAS would otherwise be decided against a
        revision another process could have superseded mid-block."""
        now = self.now()
        expected = int(expected_revision)
        with self.database.write() as db:
            self.database.begin_immediate(db)
            row = db.execute(
                "SELECT value, revision, history_json FROM agent_notebook_profile "
                "WHERE notebook_id=? AND owner_id=? AND label=?",
                (notebook_id, owner_id, label),
            ).fetchone()
            if row is None:
                raise KeyError(label)
            if int(row["revision"]) != expected:
                raise AgentProfileRevisionConflict(notebook_id, owner_id, label)
            new_revision = expected + 1
            history = _append_history(
                _loads_list(row["history_json"]),
                row["value"],
                "",
                "user",
                actor,
                now,
                new_revision,
            )
            db.execute(
                "UPDATE agent_notebook_profile SET value='',evidence_json='[]',"
                "history_json=?,revision=?,updated_by=?,updated_origin='user',"
                "updated_at=? WHERE notebook_id=? AND owner_id=? AND label=?",
                (
                    json.dumps(history, ensure_ascii=False),
                    new_revision,
                    actor,
                    now,
                    notebook_id,
                    owner_id,
                    label,
                ),
            )
            result = db.execute(
                "SELECT * FROM agent_notebook_profile "
                "WHERE notebook_id=? AND owner_id=? AND label=?",
                (notebook_id, owner_id, label),
            ).fetchone()
        return _block_row(result)

    def clear_all(self, notebook_id: str, owner_id: str) -> int:
        """Delete every block row for one (notebook, owner) scope outright —
        "start this chain's understanding over", not a per-block clear. No
        CAS: this is a full-scope wipe. Returns the row count deleted."""
        with self.database.write() as db:
            cursor = db.execute(
                "DELETE FROM agent_notebook_profile WHERE notebook_id=? AND owner_id=?",
                (notebook_id, owner_id),
            )
        return cursor.rowcount

    # ------------------------------------------------------------------- jobs
    def job_row(self, notebook_id: str, owner_id: str) -> dict | None:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM agent_profile_jobs WHERE notebook_id=? AND owner_id=?",
                (notebook_id, owner_id),
            ).fetchone()
        return _job_row(row) if row is not None else None

    def bump_signal(self, notebook_id: str, owner_id: str, delta: int = 1) -> int:
        """Zero-scan primary-key upsert: creates the chain's row (``status=
        'idle'`` by column default) on first touch, otherwise increments
        ``pending_signal`` in place. Returns the new count.

        A negative ``delta`` is rejected outright rather than clamped: the
        only legitimate way this counter goes DOWN is ``settle(consumed=...)``,
        which is CAS'd on the chain actually being claimed. A negative bump
        would be an uncontrolled decrement racing an in-flight run — and
        clamping it to zero would hide the caller's bug instead of surfacing
        it."""
        if int(delta) < 0:
            raise ValueError("agent profile pending_signal delta must not be negative")
        now = self.now()
        with self.database.write() as db:
            row = db.execute(
                "INSERT INTO agent_profile_jobs "
                "(notebook_id,owner_id,pending_signal,created_at,updated_at) "
                "VALUES (?,?,?,?,?) "
                "ON CONFLICT(notebook_id,owner_id) DO UPDATE SET "
                "pending_signal=pending_signal+excluded.pending_signal,"
                "updated_at=excluded.updated_at "
                "RETURNING pending_signal",
                (notebook_id, owner_id, int(delta), now, now),
            ).fetchone()
        return int(row["pending_signal"])

    def claim(self, notebook_id: str, owner_id: str) -> int | None:
        """Take this chain's single-flight slot; return the ``pending_signal``
        SNAPSHOT at the moment of the claim, or ``None`` when the slot was
        already held.

        Three things happen in one transaction, and each one matters:

        1. ``INSERT OR IGNORE`` ensures the row exists. A chain that was never
           signalled must still be claimable — that is precisely what a manual
           "rebuild now" is, and requiring a fake threshold bump first would
           make the manual path corrupt the automatic path's counter.
        2. The CAS to ``'running'`` is decided on the UPDATE's own rowcount,
           never on a prior read, so two racing callers can never both get an
           int back.
        3. The same UPDATE clears the PREVIOUS run's leftovers
           (``failure_reason``/``diagnostic``/``blocks_written``/
           ``finished_at``). Without it a successful run would keep showing
           the last failure's reason on screen.

        ``RETURNING pending_signal`` reads the snapshot inside the very
        statement that wins the CAS — a separate SELECT could observe a
        different value than the run is entitled to consume. The winner hands
        this number back as ``settle(consumed=...)``, which is how signals
        arriving mid-run survive to trigger the next round.

        ``begin_immediate`` covers the INSERT/UPDATE pair against another
        process sharing the file.
        """
        now = self.now()
        with self.database.write() as db:
            self.database.begin_immediate(db)
            db.execute(
                "INSERT OR IGNORE INTO agent_profile_jobs "
                "(notebook_id,owner_id,created_at,updated_at) VALUES (?,?,?,?)",
                (notebook_id, owner_id, now, now),
            )
            row = db.execute(
                "UPDATE agent_profile_jobs SET status='running',started_at=?,"
                "finished_at='',failure_reason='',diagnostic='',blocks_written=0,"
                "updated_at=? WHERE notebook_id=? AND owner_id=? "
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
        """Move a claimed chain to a terminal status, stamping
        ``finished_at`` and incrementing ``runs``.

        ``consumed`` is subtracted from ``pending_signal``, floored at zero.
        A successful run passes back the snapshot ``claim`` returned it — so
        it consumes exactly the signal it actually looked at, and anything
        that arrived WHILE it ran survives to trigger the next round. (The
        old ``reset_signal=True`` zeroed the counter outright, which silently
        discarded every mid-run change: twelve files uploaded during a
        consolidation left the counter at 0 and the corpus un-reconsolidated
        until twelve more arrived.) A failed run passes ``0``: it consumed
        nothing, so its triggering changes still count toward the retry.

        The floor is in SQL (``MAX(0, ...)``, ``GREATEST`` on PostgreSQL) so
        the read and the subtraction are the same statement — computing it in
        Python would need a separate read and could go negative under a
        concurrent bump. CAS'd on ``status IN ('queued','running')``: a
        repeated settle touches 0 rows and returns ``False`` rather than
        overwriting the outcome that already landed."""
        if status not in AGENT_PROFILE_JOB_TERMINAL_STATUSES:
            raise ValueError("agent profile job terminal status is not recognised")
        now = self.now()
        written = max(0, int(blocks_written))
        taken = max(0, int(consumed))
        with self.database.write() as db:
            cursor = db.execute(
                "UPDATE agent_profile_jobs SET status=?,failure_reason=?,"
                "diagnostic=?,blocks_written=?,runs=runs+1,"
                "pending_signal=MAX(0, pending_signal - ?),"
                "finished_at=?,updated_at=? WHERE notebook_id=? AND owner_id=? "
                "AND status IN ('queued','running')",
                (
                    status, failure_reason, diagnostic, written, taken,
                    now, now, notebook_id, owner_id,
                ),
            )
        return cursor.rowcount == 1

    def sweep_stale_on_start(self) -> int:
        """Startup crash recovery: this in-process job has no cross-process
        liveness, so every ``queued``/``running`` row left over from a prior
        process is a stranded row, not one actually still running. Force-
        settles them all to ``failed`` with
        ``AGENT_PROFILE_RESTART_FAILURE_MESSAGE`` — a named constant rather
        than a literal inside this SQL, because that sentence reaches the
        screen and two embedded copies (one per backend) drift silently.
        Returns the row count swept.

        ``queued`` is defensive-only in P1 (nothing writes it today); it stays
        in the predicate so a future queue-then-run split cannot strand rows
        this sweep does not recognise."""
        now = self.now()
        with self.database.write() as db:
            cursor = db.execute(
                "UPDATE agent_profile_jobs SET status='failed',"
                "failure_reason=?,finished_at=?,updated_at=? "
                "WHERE status IN ('queued','running')",
                (AGENT_PROFILE_RESTART_FAILURE_MESSAGE, now, now),
            )
        return cursor.rowcount
