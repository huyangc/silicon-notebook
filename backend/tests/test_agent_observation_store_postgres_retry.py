"""Unit coverage (no live PostgreSQL) for
``app.repositories.postgres.agent_observation_store.AgentObservationStore
.append_observation``'s loser-reread-``None`` retry path.

T2 修复轮 P1-1:``ON CONFLICT ... DO NOTHING`` does not lock the row it lost
to, so a losing INSERT's re-read SELECT can come back empty if the
conflicting row is deleted (``clear_observations``) in the narrow window
between the losing INSERT and the re-read. This file drives that path with a
scripted fake connection rather than a real PostgreSQL database — the
behaviour under test is pure Python control flow around ``cursor.rowcount``/
``cursor.fetchone()``, so a real connection buys nothing here and would put
this coverage behind ``TEST_POSTGRES_URL`` for no reason (the ``postgres_
integration`` marker is for genuinely backend-specific SQL semantics, not
retry-loop control flow the fake can reproduce exactly).
"""
from __future__ import annotations

import contextlib
from typing import Callable

import pytest

from app.repositories.postgres.agent_observation_store import AgentObservationStore


class _Cursor:
    def __init__(self, rowcount: int = 0, row: dict | None = None) -> None:
        self.rowcount = rowcount
        self._row = row

    def fetchone(self):
        return self._row


class _FakeConnection:
    """Replays a fixed script of cursors, one per ``execute`` call, in
    order — the SAME order ``append_observation`` issues its statements
    (INSERT, [re-read SELECT], [retry INSERT], [retry re-read SELECT],
    [eviction DELETE])."""

    def __init__(self, script: list[Callable[[str, tuple], _Cursor]]) -> None:
        self._script = list(script)
        self.executed_kinds: list[str] = []

    def execute(self, sql: str, params: tuple):
        self.executed_kinds.append(sql.strip().split(None, 1)[0].upper())
        handler = self._script.pop(0)
        return handler(sql, params)


class _FakeDatabase:
    def __init__(self, connection: _FakeConnection) -> None:
        self._connection = connection

    @contextlib.contextmanager
    def write(self):
        yield self._connection


def _store(database: _FakeDatabase) -> AgentObservationStore:
    return AgentObservationStore(
        database,
        new_id=lambda prefix: "obs-retry",
        now=lambda: "2026-08-20T00:00:00+00:00",
    )


def test_retries_once_and_wins_when_the_loser_reread_is_none():
    """First INSERT loses (rowcount=0), the re-read SELECT finds the
    conflicting row already gone (``None`` — deleted between the loss and
    the re-read), the retried SAME INSERT wins outright (rowcount=1) because
    the conflict is now gone, and the method must fall through to the normal
    winning path (eviction DELETE, ``deduplicated=False``) rather than crash
    indexing a ``None`` row."""
    script = [
        lambda sql, params: _Cursor(rowcount=0),  # first INSERT: lost
        lambda sql, params: _Cursor(row=None),  # re-read: conflicting row gone
        lambda sql, params: _Cursor(rowcount=1),  # retry INSERT: wins
        lambda sql, params: _Cursor(rowcount=0),  # eviction DELETE
    ]
    connection = _FakeConnection(script)
    store = _store(_FakeDatabase(connection))

    observation_id, deduplicated = store.append_observation(
        "nb-1", "user-a", "agent-1", text="x", client_request_id="req-1",
    )
    assert observation_id == "obs-retry"
    assert deduplicated is False
    assert connection.executed_kinds == ["INSERT", "SELECT", "INSERT", "DELETE"]


def test_retries_once_and_lands_on_a_fresh_conflicting_row():
    """The retried INSERT can also lose again — this time to a DIFFERENT
    concurrent writer's row that landed in between. That row's id must be
    returned, deduplicated, same as a first-attempt loss would report."""
    script = [
        lambda sql, params: _Cursor(rowcount=0),  # first INSERT: lost
        lambda sql, params: _Cursor(row=None),  # re-read: gone
        lambda sql, params: _Cursor(rowcount=0),  # retry INSERT: lost again
        lambda sql, params: _Cursor(row={"id": "obs-someone-else"}),
    ]
    connection = _FakeConnection(script)
    store = _store(_FakeDatabase(connection))

    observation_id, deduplicated = store.append_observation(
        "nb-1", "user-a", "agent-1", text="x", client_request_id="req-1",
    )
    assert observation_id == "obs-someone-else"
    assert deduplicated is True
    assert connection.executed_kinds == ["INSERT", "SELECT", "INSERT", "SELECT"]


def test_raises_a_named_error_when_the_reread_is_none_twice():
    """The SAME row being deleted out from under both the first AND the
    retried re-read is not a race this method can resolve by retrying
    again — it must fail loudly with a named ``RuntimeError`` rather than
    let an untyped ``TypeError`` (indexing ``None``) escape to the caller."""
    script = [
        lambda sql, params: _Cursor(rowcount=0),
        lambda sql, params: _Cursor(row=None),
        lambda sql, params: _Cursor(rowcount=0),
        lambda sql, params: _Cursor(row=None),
    ]
    connection = _FakeConnection(script)
    store = _store(_FakeDatabase(connection))

    with pytest.raises(RuntimeError):
        store.append_observation(
            "nb-1", "user-a", "agent-1", text="x", client_request_id="req-1",
        )
    assert connection.executed_kinds == ["INSERT", "SELECT", "INSERT", "SELECT"]


def test_no_retry_at_all_when_the_first_insert_wins_outright():
    """The common case must not pay for the retry machinery at all — exactly
    two statements (INSERT + eviction DELETE), no re-read SELECT."""
    script = [
        lambda sql, params: _Cursor(rowcount=1),  # INSERT: wins immediately
        lambda sql, params: _Cursor(rowcount=0),  # eviction DELETE
    ]
    connection = _FakeConnection(script)
    store = _store(_FakeDatabase(connection))

    observation_id, deduplicated = store.append_observation(
        "nb-1", "user-a", "agent-1", text="x", client_request_id="req-1",
    )
    assert observation_id == "obs-retry"
    assert deduplicated is False
    assert connection.executed_kinds == ["INSERT", "DELETE"]


def test_append_observation_rejects_empty_client_request_id():
    store = _store(_FakeDatabase(_FakeConnection([])))
    with pytest.raises(ValueError):
        store.append_observation(
            "nb-1", "user-a", "agent-1", text="x", client_request_id="",
        )
