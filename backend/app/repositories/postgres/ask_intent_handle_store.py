"""PostgreSQL persistence for the MCP ``ask`` clarification handles (0070)."""
from __future__ import annotations

from datetime import timedelta
from typing import Any, Callable

from app.repositories.postgres._store_utils import (
    TimestampInput,
    iso_timestamp,
    json_value,
    jsonb,
    normalized_clock,
)
from app.repositories.postgres.database import PostgresDatabase


class AskIntentHandleStore:
    """Clarification contracts kept server-side under an opaque token.

    ``put`` purges every expired row in the same write; ``get`` never returns
    an expired row or another owner's. Nothing is consumed by a read.
    """

    def __init__(
        self, database: PostgresDatabase, *, now: Callable[[], TimestampInput]
    ) -> None:
        self.database = database
        self.now = normalized_clock(now)

    def put_intent_handle(
        self,
        *,
        token: str,
        owner_id: str,
        scope_key: str,
        question_sha256: str,
        contract: dict[str, Any],
        understanding_ms: int,
        ttl_seconds: int,
    ) -> None:
        now = self.now()
        with self.database.write() as db:
            db.execute("DELETE FROM ask_intent_handles WHERE expires_at <= %s", (now,))
            db.execute(
                "INSERT INTO ask_intent_handles (token, owner_id, scope_key, "
                "question_sha256, contract_json, understanding_ms, created_at, "
                "expires_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    token, owner_id, scope_key, question_sha256, jsonb(contract),
                    int(understanding_ms), now, now + timedelta(seconds=ttl_seconds),
                ),
            )

    def get_intent_handle(self, token: str, *, owner_id: str) -> dict[str, Any] | None:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT token, owner_id, scope_key, question_sha256, contract_json, "
                "understanding_ms, created_at, expires_at FROM ask_intent_handles "
                "WHERE token = %s AND owner_id = %s AND expires_at > %s",
                (token, owner_id, self.now()),
            ).fetchone()
        if row is None:
            return None
        return {
            "token": str(row["token"]),
            "owner_id": str(row["owner_id"]),
            "scope_key": str(row["scope_key"]),
            "question_sha256": str(row["question_sha256"]),
            "contract": json_value(row["contract_json"], {}),
            "understanding_ms": int(row["understanding_ms"]),
            "created_at": iso_timestamp(row["created_at"]),
            "expires_at": iso_timestamp(row["expires_at"]),
        }
