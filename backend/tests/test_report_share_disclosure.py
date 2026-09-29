"""Report share disclosure on SQLite (E7-2, ruling M4).

The route scenarios live in ``report_share_disclosure_cases.py`` and run here
and, unchanged, on PostgreSQL in ``postgres/test_report_share_disclosure_pg.py``.
This file adds the SQLite side of the new store read and the service rule.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from app.services.share_disclosure import (
    NonAuthorShareRefused,
    ShareDisclosure,
    ShareDisclosureRequired,
    report_share_disclosure,
    require_publishable,
)
from tests.report_share_disclosure_cases import CASES, build_world, make_memory


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.api import deps
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    deps.repository.cache_clear()
    return TestClient(create_app())


@pytest.fixture
def world(client, monkeypatch):
    return build_world(client, monkeypatch)


@pytest.mark.parametrize("case", sorted(CASES))
def test_report_share_disclosure_scenario(world, case):
    CASES[case](world)


# --- the store read ------------------------------------------------------------


def test_memory_ids_for_source_ids_maps_only_the_owners_memory_sources(world):
    store = world.repo._runtime.memory_store
    a1 = make_memory(world, world.alice, "a1")
    make_memory(world, world.owner, "o1")
    alice_source = world.memories["a1"][1]
    owner_source = world.memories["o1"][1]
    wanted = [alice_source, owner_source, world.doc_source, "src-unknown", alice_source]
    assert store.memory_ids_for_source_ids(wanted, world.alice.id) == [a1]
    assert store.memory_ids_for_source_ids(wanted, world.owner.id) == [
        world.memories["o1"][0]
    ]
    assert store.memory_ids_for_source_ids(wanted, "") == []
    assert store.memory_ids_for_source_ids([], world.alice.id) == []
    # An orphaned Memory source (its memory row gone) fails closed for everyone.
    with world.repo._runtime.database.write() as db:
        db.execute("DELETE FROM memory_items WHERE id=?", (a1,))
        orphan = db.execute(
            "SELECT source_type, memory_id FROM sources WHERE id=?", (alice_source,)
        ).fetchone()
    assert (orphan["source_type"], orphan["memory_id"]) == ("memory", a1)
    assert store.memory_ids_for_source_ids([alice_source], world.alice.id) == []


def test_memory_ids_for_source_ids_binds_a_long_citation_list_once(world):
    """Thousands of cited ids are one bound JSON parameter, never one variable
    per id (SQLite's limit is 32,766) and never a Python list."""
    store = world.repo._runtime.memory_store
    a1 = make_memory(world, world.alice, "a1")
    wanted = [f"src-missing-{index}" for index in range(40_000)]
    wanted.append(world.memories["a1"][1])
    assert store.memory_ids_for_source_ids(wanted, world.alice.id) == [a1]
    for lock in (False, True):
        assert store.memory_sources_for_source_ids_sql(lock=lock).count("?") == 2


def test_memory_ids_for_source_ids_probes_sources_by_primary_key(world):
    store = world.repo._runtime.memory_store
    with world.repo._runtime.database.connect() as db:
        plan = [
            str(row["detail"])
            for row in db.execute(
                "EXPLAIN QUERY PLAN " + store.memory_sources_for_source_ids_sql(),
                (json.dumps(["a", "b"]), world.alice.id),
            ).fetchall()
        ]
    joined = "\n".join(plan)
    assert "SCAN wanted" in joined, joined
    assert any(
        line.startswith("SEARCH s USING INDEX") and "(id=?)" in line for line in plan
    ), joined
    assert "SCAN s" not in joined, joined


# --- the service rule ------------------------------------------------------------


class _NoMemorySources:
    def __init__(self):
        self.calls = []

    def memory_ids_for_source_ids(self, source_ids, owner_id):
        self.calls.append((list(source_ids), owner_id))
        return []


def test_disclosure_counts_distinct_memory_objects_and_asks_the_store_as_author():
    reader = _NoMemorySources()
    disclosure = report_share_disclosure(reader, {
        "created_by": "u-author",
        "references": [
            {"object_type": "memory", "object_id": "mem-1"},
            {"object_type": "memory", "object_id": "mem-1"},
            {"object_type": "element", "object_id": "el-1", "source_id": "src-1"},
            {"object_type": "element", "object_id": "el-2", "source_id": "src-2",
             "memory_id": "mem-2", "memory_owner_id": "u-author"},
            {"object_type": "element", "object_id": "el-3", "source_id": "src-3",
             "memory_id": "mem-3", "memory_owner_id": "u-other"},
            "not a reference",
        ],
    })
    assert disclosure == ShareDisclosure(
        "u-author", ("mem-1", "mem-2"),
        known_memory_ids=frozenset({"mem-1", "mem-2"}),
        live_source_ids=("src-1",),
    )
    # Recorded citations are never looked up again; only unrecorded ones are.
    assert reader.calls == [(["src-1"], "u-author")]


def test_only_the_author_may_publish_their_memory():
    disclosure = ShareDisclosure("u-author", ("mem-1", "mem-2"))
    for requester in ("u-owner", "u-admin", ""):
        with pytest.raises(NonAuthorShareRefused):
            require_publishable(disclosure, requester_id=requester, acknowledged=2)
    require_publishable(disclosure, requester_id="u-author", acknowledged=2)
    with pytest.raises(ShareDisclosureRequired) as stale:
        require_publishable(disclosure, requester_id="u-author", acknowledged=1)
    assert stale.value.detail() == {
        "code": "share_disclosure_required", "memory_count": 2, "new_memory_count": 1,
    }
    # Nothing of the author's is cited: anyone who reaches the route may publish.
    require_publishable(
        ShareDisclosure("u-author", ()), requester_id="u-owner", acknowledged=None
    )
