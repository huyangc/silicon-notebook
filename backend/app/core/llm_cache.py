"""Content-addressed SQLite cache for OpenAICompatibleClient.chat_json responses.
Key = sha256(model + messages + schema_hint). Identical extraction/ask prompts
return the cached JSON string instead of re-calling the endpoint. Safe: the key
embeds the full prompt (which carries any retrieved context), so any input change
yields a new key. WAL + serialized writes for the concurrent extraction pool."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
from pathlib import Path
from typing import Dict, List, Optional


def cache_key(model: str, messages: List[Dict[str, str]], schema_hint: str) -> str:
    payload = json.dumps(
        {"model": model, "messages": messages, "schema": schema_hint},
        sort_keys=True, ensure_ascii=False,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class LLMCache:
    def __init__(self, db_path: str) -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.execute(
                "CREATE TABLE IF NOT EXISTS llm_cache ("
                "key TEXT PRIMARY KEY, response TEXT NOT NULL, "
                "created_at TEXT DEFAULT CURRENT_TIMESTAMP)"
            )

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA busy_timeout = 30000")
        return conn

    def get(self, key: str) -> Optional[str]:
        with self._connect() as db:
            row = db.execute(
                "SELECT response FROM llm_cache WHERE key = ?", (key,)
            ).fetchone()
        return row["response"] if row else None

    def put(self, key: str, response: str) -> None:
        with self._lock:
            with self._connect() as db:
                db.execute(
                    "INSERT OR REPLACE INTO llm_cache (key, response) VALUES (?, ?)",
                    (key, response),
                )
