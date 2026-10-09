"""Shared behaviour of the ``ask_intent_handles`` store (SQLite v90 /
PostgreSQL 0070), run by ``test_ask_intent_handle_store.py`` and its
PostgreSQL twin against a store whose clock the case controls."""
from __future__ import annotations

CONTRACT = {"objective": "问题", "resolved_question": "问题", "ambiguities": [
    {"id": "a1", "question": "指哪一个？", "required": True},
]}


class Clock:
    def __init__(self, value: str = "2026-10-09T00:00:00+00:00") -> None:
        self.value = value

    def __call__(self) -> str:
        return self.value


def _put(store, token: str, *, owner: str = "user-a", ttl: int = 3600) -> None:
    store.put_intent_handle(
        token=token, owner_id=owner, scope_key="nb-1",
        question_sha256="ab" * 32, contract=CONTRACT, understanding_ms=42,
        ttl_seconds=ttl,
    )


def assert_round_trip_is_owner_bound_and_non_consuming(store) -> None:
    _put(store, "tok-1")
    first = store.get_intent_handle("tok-1", owner_id="user-a")
    assert first is not None
    assert first["scope_key"] == "nb-1"
    assert first["question_sha256"] == "ab" * 32
    assert first["contract"] == CONTRACT
    assert first["understanding_ms"] == 42
    # Reading consumes nothing: a failed run can be retried with the handle.
    assert store.get_intent_handle("tok-1", owner_id="user-a") == first
    assert store.get_intent_handle("tok-1", owner_id="user-b") is None
    assert store.get_intent_handle("tok-unknown", owner_id="user-a") is None


def assert_expiry_hides_then_purges(store, clock: Clock, count_rows) -> None:
    _put(store, "tok-old", ttl=60)
    clock.value = "2026-10-09T00:00:59+00:00"
    assert store.get_intent_handle("tok-old", owner_id="user-a") is not None
    clock.value = "2026-10-09T00:01:00+00:00"
    assert store.get_intent_handle("tok-old", owner_id="user-a") is None
    assert count_rows() == 1  # hidden, not yet purged
    _put(store, "tok-new")
    assert count_rows() == 1  # the write purged the expired row
    assert store.get_intent_handle("tok-new", owner_id="user-a") is not None
