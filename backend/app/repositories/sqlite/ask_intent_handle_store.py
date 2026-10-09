"""SQLite persistence for the MCP ``ask`` clarification handles (v90)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Callable

from app.repositories.sqlite.database import SqliteDatabase


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    # One fixed-width UTC spelling, so the stored text orders as the instant.
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f+00:00")


class AskIntentHandleStore:
    """Clarification contracts kept server-side under an opaque token.

    ``put`` purges every expired row in the same write; ``get`` never returns
    an expired row or another owner's. Nothing is consumed by a read.
    """

    def __init__(self, database: SqliteDatabase, *, now: Callable[[], str]) -> None:
        self.database = database
        self.now = now

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
        now = _instant(self.now())
        with self.database.write(operation="ask_intent_handles.put") as db:
            db.execute(
                "DELETE FROM ask_intent_handles WHERE expires_at <= ?", (_iso(now),)
            )
            db.execute(
                "INSERT INTO ask_intent_handles (token, owner_id, scope_key, "
                "question_sha256, contract_json, understanding_ms, created_at, "
                "expires_at) VALUES (?,?,?,?,?,?,?,?)",
                (
                    token, owner_id, scope_key, question_sha256,
                    json.dumps(contract, ensure_ascii=False),
                    int(understanding_ms), _iso(now),
                    _iso(now + timedelta(seconds=ttl_seconds)),
                ),
            )

    def get_intent_handle(self, token: str, *, owner_id: str) -> dict[str, Any] | None:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT token, owner_id, scope_key, question_sha256, contract_json, "
                "understanding_ms, created_at, expires_at FROM ask_intent_handles "
                "WHERE token = ? AND owner_id = ? AND expires_at > ?",
                (token, owner_id, _iso(_instant(self.now()))),
            ).fetchone()
        if row is None:
            return None
        return {
            "token": row["token"],
            "owner_id": row["owner_id"],
            "scope_key": row["scope_key"],
            "question_sha256": row["question_sha256"],
            "contract": json.loads(row["contract_json"]),
            "understanding_ms": int(row["understanding_ms"]),
            "created_at": row["created_at"],
            "expires_at": row["expires_at"],
        }
