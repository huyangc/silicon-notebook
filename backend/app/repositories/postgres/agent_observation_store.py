"""PostgreSQL row persistence for Agentic Memory P3's ``agent_observations``
— the per-(notebook, owner) log of short lines an external Agent writes via
the ``add_observation`` MCP tool.

A behavioural mirror of ``app/repositories/sqlite/agent_observation_store``:
same method names, same bounds, same return shapes (timestamps normalised to
ISO strings). The genuine differences are the ones the backends disagree
about — ``timestamptz`` instead of ISO strings, ``ON CONFLICT ... DO NOTHING``
in place of SQLite's process-wide write serialisation plus a caught
``IntegrityError``, and ``COLLATE "C"`` on the eviction ordering key so a
non-C-collated database evicts the exact same rows SQLite does.

See ``AgentObservationStorePort`` in ``app/repositories/ports.py`` for the
full contract.
"""
from __future__ import annotations

from typing import Callable

from app.repositories.postgres._store_utils import (
    TimestampInput,
    iso_timestamp,
    normalized_clock,
)
from app.repositories.postgres.database import PostgresDatabase
from app.repositories.ports import AGENT_OBSERVATION_RING_MAX, project_observation_row


class AgentObservationStore:
    def __init__(
        self,
        database: PostgresDatabase,
        *,
        new_id: Callable[[str], str],
        now: Callable[[], TimestampInput],
    ) -> None:
        self.database = database
        self.new_id = new_id
        self.now = normalized_clock(now)

    def append_observation(
        self,
        notebook_id: str,
        owner_id: str,
        agent_profile_id: str,
        *,
        text: str,
        client_request_id: str,
    ) -> tuple[str, bool]:
        """See the port docstring for the full contract. This backend's
        specifics:

        ⚠ No ``FOR UPDATE`` and no caught exception — this is deliberately
        NOT the ``SELECT`` -then- ``INSERT`` shape ``agent_profile_store.
        write_block`` uses. PostgreSQL aborts the whole transaction on the
        first error inside it (no implicit savepoint), so catching a raised
        ``UniqueViolation`` and continuing to issue further statements on the
        SAME connection — as this method must, to re-read the winning row and
        then run the eviction — would fail every one of those follow-up
        statements with "current transaction is aborted". ``ON CONFLICT (...)
        WHERE client_request_id IS NOT NULL DO NOTHING`` sidesteps that
        entirely: a losing concurrent writer's INSERT reports ``rowcount ==
        0`` rather than raising, so the SAME transaction can safely go on to
        re-read the row and, on the winning path, run the eviction.

        The ``WHERE client_request_id IS NOT NULL`` clause on the conflict
        target is required, not decorative: ``idx_agent_observations_request``
        is a PARTIAL unique index, and PostgreSQL only lets ``ON CONFLICT``
        infer a partial index when the clause's predicate is repeated
        verbatim here.

        The eviction only runs on the winning path (``rowcount == 1``) — a
        losing writer changed nothing, so there is nothing new to evict for.
        It is still the last statement before the transaction ends on that
        path, same ordering guarantee as the SQLite mirror.
        """
        now = self.now()
        request_id = str(client_request_id)
        observation_id = self.new_id("obs")
        with self.database.write() as connection:
            insert_cursor = connection.execute(
                "INSERT INTO agent_observations "
                "(id,notebook_id,owner_id,agent_profile_id,text,"
                "client_request_id,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s) "
                "ON CONFLICT (notebook_id,owner_id,agent_profile_id,"
                "client_request_id) WHERE client_request_id IS NOT NULL "
                "DO NOTHING",
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
            if insert_cursor.rowcount == 1:
                connection.execute(
                    "DELETE FROM agent_observations WHERE notebook_id=%s "
                    "AND owner_id = %s AND id NOT IN ("
                    "SELECT id FROM agent_observations WHERE notebook_id=%s "
                    'AND owner_id = %s ORDER BY created_at DESC, '
                    'id COLLATE "C" DESC LIMIT %s)',
                    (
                        notebook_id,
                        owner_id,
                        notebook_id,
                        owner_id,
                        AGENT_OBSERVATION_RING_MAX,
                    ),
                )
                return observation_id, False
            row = connection.execute(
                "SELECT id FROM agent_observations WHERE notebook_id=%s "
                "AND owner_id = %s AND agent_profile_id=%s "
                "AND client_request_id=%s",
                (notebook_id, owner_id, agent_profile_id, request_id),
            ).fetchone()
        return str(row["id"]), True

    def recent_observations(
        self, notebook_id: str, owner_id: str, *, limit: int
    ) -> list[dict]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT id, agent_profile_id, text, created_at "
                "FROM agent_observations WHERE notebook_id=%s "
                'AND owner_id = %s ORDER BY created_at DESC, '
                'id COLLATE "C" DESC LIMIT %s',
                (notebook_id, owner_id, max(0, int(limit))),
            ).fetchall()
        return [
            project_observation_row(
                row["id"],
                row["agent_profile_id"],
                row["text"],
                iso_timestamp(row["created_at"]),
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
                "FROM agent_observations WHERE notebook_id=%s "
                'AND owner_id = %s ORDER BY created_at DESC, '
                'id COLLATE "C" DESC LIMIT %s',
                (notebook_id, owner_id, max(0, int(limit))),
            ).fetchall()
        return [
            project_observation_row(
                row["id"],
                row["agent_profile_id"],
                row["text"],
                iso_timestamp(row["created_at"]),
            )
            for row in rows
        ]

    def clear_observations(
        self, notebook_id: str, owner_id: str, *, agent_profile_id: str = ""
    ) -> int:
        profile = str(agent_profile_id or "")
        with self.database.write() as connection:
            if profile:
                cursor = connection.execute(
                    "DELETE FROM agent_observations WHERE notebook_id=%s "
                    "AND owner_id=%s AND agent_profile_id=%s",
                    (notebook_id, owner_id, profile),
                )
            else:
                cursor = connection.execute(
                    "DELETE FROM agent_observations WHERE notebook_id=%s "
                    "AND owner_id=%s",
                    (notebook_id, owner_id),
                )
        return cursor.rowcount
