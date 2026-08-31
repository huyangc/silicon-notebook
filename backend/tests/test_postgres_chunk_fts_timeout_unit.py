from __future__ import annotations

from types import SimpleNamespace

import pytest
from psycopg import errors
from pydantic import ValidationError

from app.core.config import Settings
from app.repositories.ports import ChunkLexicalSearchTimeout
from app.repositories.postgres.knowledge_store import KnowledgeStore


class _Rows:
    def __init__(self, rows=()):
        self._rows = list(rows)

    def fetchall(self):
        return list(self._rows)

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Connection:
    def __init__(self, *, timeout: bool):
        self.timeout = timeout
        self.calls = []
        self.usable = True

    def execute(self, statement, params=None):
        text = str(statement)
        self.calls.append((text, params))
        if "current_setting('statement_timeout')" in text:
            return _Rows([{"value": "30s"}])
        if "CROSS JOIN LATERAL" in text:
            if self.timeout:
                self.usable = False
                raise errors.QueryCanceled("private server diagnostic")
            return _Rows()
        if text.startswith("ROLLBACK TO SAVEPOINT"):
            self.usable = True
        if text == "SELECT 1" and not self.usable:
            raise RuntimeError("transaction remains aborted")
        return _Rows()


def _store(timeout_seconds: float = 1.0) -> KnowledgeStore:
    database = SimpleNamespace(
        settings=SimpleNamespace(
            postgres_chunk_fts_timeout_seconds=timeout_seconds
        )
    )
    return KnowledgeStore(database, SimpleNamespace())


def test_chunk_fts_timeout_cannot_exceed_pool_statement_timeout():
    with pytest.raises(ValidationError):
        Settings(
            postgres_statement_timeout_seconds=1,
            postgres_chunk_fts_timeout_seconds=2,
        )


def test_chunk_fts_timeout_default_and_env(monkeypatch):
    assert Settings(_env_file=None).postgres_chunk_fts_timeout_seconds == 1.0
    monkeypatch.setenv("POSTGRES_CHUNK_FTS_TIMEOUT_SECONDS", "0.75")
    assert Settings(_env_file=None).postgres_chunk_fts_timeout_seconds == 0.75


def test_chunk_fts_restores_previous_timeout_after_success():
    connection = _Connection(timeout=False)

    assert _store(0.75).chunk_fts_search(
        connection, "nb", "thermal control", k=5
    ) == []

    configs = [params for text, params in connection.calls if "set_config" in text]
    assert configs == [("750ms",), ("30s",)]
    assert connection.calls[-1][0] == "RELEASE SAVEPOINT chunk_fts_budget"


def test_chunk_fts_timeout_rolls_back_savepoint_and_hides_driver_diagnostic():
    connection = _Connection(timeout=True)

    with pytest.raises(ChunkLexicalSearchTimeout) as caught:
        _store().chunk_fts_search(
            connection, "nb", "thermal control", k=5
        )

    statements = [text for text, _params in connection.calls]
    assert "ROLLBACK TO SAVEPOINT chunk_fts_budget" in statements
    assert "RELEASE SAVEPOINT chunk_fts_budget" in statements
    assert "private server diagnostic" not in str(caught.value)
    connection.execute("SELECT 1")
