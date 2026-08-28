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

from app.repositories.ports import (
    AGENT_CALL_RING_MAX,
    AGENT_OBSERVATION_KIND_CALL,
    AGENT_OBSERVATION_KIND_NOTE,
    AGENT_OBSERVATION_RING_MAX,
    project_call_row,
    project_observation_row,
)
from app.repositories.sqlite.database import SqliteDatabase

# Absolute-instant ordering, matching the ``conversations`` table's own
# ``CONVERSATION_ANSWERS_ORDER_DESC`` precedent (``app/repositories/sqlite/
# ask_state_store.py``): ``julianday()`` leads the ORDER BY so two rows are
# compared by the instant they represent rather than by their raw text, which
# would silently reorder rows whose ``created_at`` carries different UTC
# offsets even though they are byte-for-byte the same clock reading in a
# different rendering. ``id`` — not ``rowid`` — is the final tie-break: this
# table's id is a caller-opaque uuid, not an insertion-order column, so unlike
# ``conversations`` there is no insertion-order semantic to preserve here —
# see the port's ``append_observation`` docstring for why plain ``id`` byte
# ordering (not clock precision) is what actually keeps this deterministic
# across backends.
AGENT_OBSERVATIONS_ORDER_DESC = (
    # codex #535 R7 P2:同一绝对时刻、不同 UTC offset 拼写的行(合库/导入可造)
    # 在 julianday 上并列——夹一个文本 created_at 会让 SQLite 按拼写、PG 按 id
    # 各选各的存活行。并列直接落 id,与 PG 的 timestamptz+id 语义逐位一致。
    "ORDER BY julianday(created_at) DESC, id DESC"
)


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
        request_id = str(client_request_id or "")
        if not request_id:
            # An empty/missing client_request_id must never reach the store:
            # it is the one value that would silently fold every no-id write
            # for this (notebook_id, owner_id, agent_profile_id) tuple onto
            # the same partial-unique-index slot the FIRST such write claims
            # (see ``idx_agent_observations_request`` — the index is
            # ``WHERE client_request_id IS NOT NULL``, but an EMPTY STRING is
            # not NULL, so it participates and collides). The caller (the
            # add_observation MCP tool, T3) must reject a missing id before
            # it ever gets here, the same way ``memory_inputs.
            # normalize_client_request_id`` already does for Memory
            # proposals — this is a fail-loud backstop for a caller that
            # forgot to, not a validation path a legitimate request can hit.
            raise ValueError("client_request_id must be a non-empty string")
        now = self.now()
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
                    "client_request_id,created_at,kind) VALUES (?,?,?,?,?,?,?,?)",
                    (
                        observation_id,
                        notebook_id,
                        owner_id,
                        agent_profile_id,
                        text,
                        request_id,
                        now,
                        AGENT_OBSERVATION_KIND_NOTE,
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
            self._evict(
                db,
                notebook_id,
                owner_id,
                kind=AGENT_OBSERVATION_KIND_NOTE,
                ring_max=AGENT_OBSERVATION_RING_MAX,
            )
        return observation_id, False

    def _evict(
        self,
        db: sqlite3.Connection,
        notebook_id: str,
        owner_id: str,
        *,
        kind: str,
        ring_max: int,
    ) -> None:
        """Trim ONE ``(notebook_id, owner_id, kind)`` group down to its own
        ring bound, inside the caller's already-open write transaction.

        ⚠ ``kind`` appears in BOTH the outer DELETE and the inner survivor
        SELECT, and dropping it from either one is a silent data-loss bug in
        a different direction: missing from the DELETE, a note gets evicted
        because too many CALLS arrived; missing from the SELECT, the survivor
        set is computed across both kinds, so the newest N rows may be all
        calls and the DELETE then removes every note in the group.
        """
        db.execute(
            "DELETE FROM agent_observations WHERE notebook_id=? "
            "AND owner_id = ? AND kind=? AND id NOT IN ("
            "SELECT id FROM agent_observations WHERE notebook_id=? "
            "AND owner_id = ? AND kind=? "
            + AGENT_OBSERVATIONS_ORDER_DESC
            + " LIMIT ?)",
            (
                notebook_id,
                owner_id,
                kind,
                notebook_id,
                owner_id,
                kind,
                ring_max,
            ),
        )

    def append_call(
        self,
        notebook_id: str,
        owner_id: str,
        agent_profile_id: str,
        *,
        capability: str,
    ) -> str:
        """See the port docstring for the full contract. This backend's
        specifics:

        ``client_request_id`` is written as SQL NULL — not ``''`` — so the
        row stays outside ``idx_agent_observations_request``'s partial unique
        surface entirely. There is therefore no read-then-insert to
        serialise, and so no ``begin_immediate`` here: unlike
        ``append_observation``, this write has no idempotency decision that a
        second process could interleave with.
        """
        now = self.now()
        call_id = self.new_id("acl")
        with self.database.write() as db:
            db.execute(
                "INSERT INTO agent_observations "
                "(id,notebook_id,owner_id,agent_profile_id,text,"
                "client_request_id,created_at,kind) VALUES (?,?,?,?,?,NULL,?,?)",
                (
                    call_id,
                    notebook_id,
                    owner_id,
                    agent_profile_id,
                    capability,
                    now,
                    AGENT_OBSERVATION_KIND_CALL,
                ),
            )
            self._evict(
                db,
                notebook_id,
                owner_id,
                kind=AGENT_OBSERVATION_KIND_CALL,
                ring_max=AGENT_CALL_RING_MAX,
            )
        return call_id

    def recent_observations(
        self, notebook_id: str, owner_id: str, *, limit: int
    ) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id, agent_profile_id, text, created_at "
                "FROM agent_observations WHERE notebook_id=? "
                "AND owner_id = ? AND kind = ? "
                + AGENT_OBSERVATIONS_ORDER_DESC
                + " LIMIT ?",
                (
                    notebook_id,
                    owner_id,
                    AGENT_OBSERVATION_KIND_NOTE,
                    max(0, int(limit)),
                ),
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
                "AND owner_id = ? AND kind = ? "
                + AGENT_OBSERVATIONS_ORDER_DESC
                + " LIMIT ?",
                (
                    notebook_id,
                    owner_id,
                    AGENT_OBSERVATION_KIND_NOTE,
                    max(0, int(limit)),
                ),
            ).fetchall()
        return [
            project_observation_row(
                row["id"], row["agent_profile_id"], row["text"], row["created_at"]
            )
            for row in rows
        ]

    def list_calls(
        self, notebook_id: str, owner_id: str, *, limit: int
    ) -> list[dict]:
        # Same shape as the two reads above with ``kind`` flipped, and the
        # projection swapped: a call row's ``text`` column holds a capability
        # scope, so it reaches readers as ``capability`` and never as
        # ``text`` (see ``project_call_row``).
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id, agent_profile_id, text, created_at "
                "FROM agent_observations WHERE notebook_id=? "
                "AND owner_id = ? AND kind = ? "
                + AGENT_OBSERVATIONS_ORDER_DESC
                + " LIMIT ?",
                (
                    notebook_id,
                    owner_id,
                    AGENT_OBSERVATION_KIND_CALL,
                    max(0, int(limit)),
                ),
            ).fetchall()
        return [
            project_call_row(
                row["id"], row["agent_profile_id"], row["text"], row["created_at"]
            )
            for row in rows
        ]

    def clear_observations(
        self,
        notebook_id: str,
        owner_id: str,
        *,
        agent_profile_id: str = "",
        kind: str = "",
    ) -> int:
        profile = str(agent_profile_id or "")
        # Both narrowings are OPTIONAL and independent: the SQL is assembled
        # from the two that were actually supplied rather than branching on
        # every combination, so "clear this Agent's call rows only" needs no
        # fourth hand-written statement.
        clauses = ["notebook_id=?", "owner_id=?"]
        params: list[object] = [notebook_id, owner_id]
        if profile:
            clauses.append("agent_profile_id=?")
            params.append(profile)
        if kind:
            clauses.append("kind=?")
            params.append(str(kind))
        with self.database.write() as db:
            cursor = db.execute(
                "DELETE FROM agent_observations WHERE " + " AND ".join(clauses),
                tuple(params),
            )
        return cursor.rowcount
