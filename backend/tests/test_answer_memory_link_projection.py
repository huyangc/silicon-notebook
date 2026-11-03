"""Defensive duplicate projection behavior without corrupting the answer index."""
from contextlib import contextmanager
from itertools import permutations
from types import SimpleNamespace

import pytest

from app.repositories.postgres.memory_store import MemoryStore as PostgresMemoryStore
from app.repositories.sqlite.memory_store import MemoryStore as SqliteMemoryStore


@pytest.mark.parametrize("store_class", [SqliteMemoryStore, PostgresMemoryStore])
@pytest.mark.parametrize("adopted_status", ["confirmed", "deprecated"])
def test_duplicate_answer_projection_selects_adopted_consistently(store_class, adopted_status):
    rows = [
        {"source_answer_id": "answer", "id": "a-draft", "status": "candidate",
         "suggested_signal": "repeated", "unadopted": True},
        {"source_answer_id": "answer", "id": "z-adopted", "status": adopted_status,
         "suggested_signal": "repeated", "unadopted": False},
        {"source_answer_id": "answer", "id": "b-agent", "status": "candidate",
         "suggested_signal": None, "unadopted": False},
    ]

    @contextmanager
    def connect():
        yield SimpleNamespace(execute=lambda *_: SimpleNamespace(fetchall=lambda: projection))

    store = store_class(SimpleNamespace(connect=connect), new_id=lambda _: "unused", now=lambda: "now")
    for projection in permutations(rows):
        assert store.answer_memory_link_rows("notebook", "owner", ["answer"]) == {
            "links": {"answer": "z-adopted"},
            "states": {"answer": {
                "memory_id": "z-adopted", "status": adopted_status, "suggested": True,
            }},
        }


@pytest.mark.parametrize("store_class", [SqliteMemoryStore, PostgresMemoryStore])
def test_duplicate_projection_ties_have_a_stable_identity(store_class):
    rows = [
        {"source_answer_id": "answer", "id": memory_id, "status": "candidate",
         "suggested_signal": "repeated", "unadopted": True}
        for memory_id in ("z-draft", "a-draft")
    ]

    @contextmanager
    def connect():
        yield SimpleNamespace(execute=lambda *_: SimpleNamespace(fetchall=lambda: projection))

    store = store_class(SimpleNamespace(connect=connect), new_id=lambda _: "unused", now=lambda: "now")
    for projection in permutations(rows):
        view = store.answer_memory_link_rows("notebook", "owner", ["answer"])
        assert view["links"] == {}
        assert view["states"] == {"answer": {
            "memory_id": "a-draft", "status": "candidate", "suggested": True,
        }}
