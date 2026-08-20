"""SQLite row persistence for Agentic Memory P3's ``agent_observations`` —
the per-(notebook, owner) log of short lines an external Agent writes via the
``add_observation`` MCP tool.

Row-level only, mirroring ``agent_profile_store.py``'s split: what an
observation MEANS to the overlay consolidation job (T4), and how the
``add_observation`` tool validates its input (T3), both belong elsewhere.
This module owns exactly one table, and every read it exposes is bounded by
construction — see ``AgentObservationStorePort`` in ``app/repositories/
ports.py`` for the full contract, including the idempotency key, the ring
eviction ordering and why the projected row never carries ``owner_id``.
"""
from __future__ import annotations

import sqlite3
from typing import Callable

from app.repositories.ports import AGENT_OBSERVATION_RING_MAX, project_observation_row
from app.repositories.sqlite.database import SqliteDatabase


class AgentObservationStore:
    def __init__(
        self,
        database: SqliteDatabase,
        *,
        new_id: Callable[[str], str],
        now: Callable[[], str],
    ) -> None:
        self.database = database
        self.new_id = new_id
        self.now = now

    def append_observation(
        self,
        notebook_id: str,
        owner_id: str,
        agent_profile_id: str,
        *,
        text: str,
        client_request_id: str,
    ) -> tuple[str, bool]:
        """See the port docstring for the full contract (idempotency key,
        ``created_at`` handling, eviction ordering). This backend's
        specifics:

        ⚠ ``begin_immediate`` opens the write transaction BEFORE the
        idempotency read: ``write()``'s mutex only serialises this process's
        writers, and the read-then-insert here is exactly the shape another
        process sharing the file (an offline CLI, a second backend worker)
        can interleave with. Without it, two concurrent calls carrying the
        same ``client_request_id`` could both see "no existing row" and both
        insert — the very duplicate the idempotency index exists to prevent.

        The INSERT branch additionally translates ``sqlite3.IntegrityError``
        into a re-read of the winning row: with ``begin_immediate`` in place
        that race is unreachable on this backend, but the translation costs
        nothing and keeps both backends ending in the same state (the
        PostgreSQL mirror's ``ON CONFLICT ... DO NOTHING`` has to cover the
        same case without ``begin_immediate``'s process-wide lock to lean
        on).

        The eviction DELETE is the transaction's LAST statement, so a row
        this very call just inserted can itself be the one evicted if the
        group was already at ``AGENT_OBSERVATION_RING_MAX`` — correct: a ring
        buffer keeps the newest N rows, not "the newest N plus whatever was
        just added".
        """
        now = self.now()
        request_id = str(client_request_id)
        observation_id = self.new_id("obs")
        with self.database.write() as db:
            self.database.begin_immediate(db)
            existing = db.execute(
                "SELECT id FROM agent_observations WHERE notebook_id=? "
                "AND owner_id = ? AND agent_profile_id=? AND client_request_id=?",
                (notebook_id, owner_id, agent_profile_id, request_id),
            ).fetchone()
            if existing is not None:
                return str(existing["id"]), True
            try:
                db.execute(
                    "INSERT INTO agent_observations "
                    "(id,notebook_id,owner_id,agent_profile_id,text,"
                    "client_request_id,created_at) VALUES (?,?,?,?,?,?,?)",
                    (
                        observation_id,
                        notebook_id,
                        owner_id,
                        agent_profile_id,
                        text,
                        request_id,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                existing = db.execute(
                    "SELECT id FROM agent_observations WHERE notebook_id=? "
                    "AND owner_id = ? AND agent_profile_id=? "
                    "AND client_request_id=?",
                    (notebook_id, owner_id, agent_profile_id, request_id),
                ).fetchone()
                if existing is not None:
                    return str(existing["id"]), True
                raise
            db.execute(
                "DELETE FROM agent_observations WHERE notebook_id=? "
                "AND owner_id = ? AND id NOT IN ("
                "SELECT id FROM agent_observations WHERE notebook_id=? "
                "AND owner_id = ? ORDER BY created_at DESC, id DESC LIMIT ?)",
                (
                    notebook_id,
                    owner_id,
                    notebook_id,
                    owner_id,
                    AGENT_OBSERVATION_RING_MAX,
                ),
            )
        return observation_id, False

    def recent_observations(
        self, notebook_id: str, owner_id: str, *, limit: int
    ) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id, agent_profile_id, text, created_at "
                "FROM agent_observations WHERE notebook_id=? "
                "AND owner_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
                (notebook_id, owner_id, max(0, int(limit))),
            ).fetchall()
        return [
            project_observation_row(
                row["id"], row["agent_profile_id"], row["text"], row["created_at"]
            )
            for row in rows
        ]

    def list_observations(
        self, notebook_id: str, owner_id: str, *, limit: int
    ) -> list[dict]:
        # Byte-identical query to ``recent_observations`` — see the port
        # docstring for why this stays a separate method rather than a
        # pass-through alias.
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id, agent_profile_id, text, created_at "
                "FROM agent_observations WHERE notebook_id=? "
                "AND owner_id = ? ORDER BY created_at DESC, id DESC LIMIT ?",
                (notebook_id, owner_id, max(0, int(limit))),
            ).fetchall()
        return [
            project_observation_row(
                row["id"], row["agent_profile_id"], row["text"], row["created_at"]
            )
            for row in rows
        ]

    def clear_observations(
        self, notebook_id: str, owner_id: str, *, agent_profile_id: str = ""
    ) -> int:
        profile = str(agent_profile_id or "")
        with self.database.write() as db:
            if profile:
                cursor = db.execute(
                    "DELETE FROM agent_observations WHERE notebook_id=? "
                    "AND owner_id=? AND agent_profile_id=?",
                    (notebook_id, owner_id, profile),
                )
            else:
                cursor = db.execute(
                    "DELETE FROM agent_observations WHERE notebook_id=? "
                    "AND owner_id=?",
                    (notebook_id, owner_id),
                )
        return cursor.rowcount
