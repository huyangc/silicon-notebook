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
    ForeignMemoryShareRefused,
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


def test_foreign_memory_sources_map_only_other_members_memory_sources(world):
    store = world.repo._runtime.memory_store
    a1 = make_memory(world, world.alice, "a1")
    o1 = make_memory(world, world.owner, "o1")
    alice_source = world.memories["a1"][1]
    owner_source = world.memories["o1"][1]
    wanted = [alice_source, owner_source, world.doc_source, "src-unknown", owner_source]
    assert store.foreign_memory_sources_for_source_ids(wanted, world.alice.id) == {
        owner_source: (o1, world.owner.id)
    }
    assert store.foreign_memory_sources_for_source_ids(wanted, world.owner.id) == {
        alice_source: (a1, world.alice.id)
    }
    assert store.foreign_memory_sources_for_source_ids(wanted, "") == {}
    assert store.foreign_memory_sources_for_source_ids([], world.alice.id) == {}
    # An orphaned Memory source has no known owner: it maps to nothing.
    with world.repo._runtime.database.write() as db:
        db.execute("DELETE FROM memory_items WHERE id=?", (o1,))
    assert store.foreign_memory_sources_for_source_ids(wanted, world.alice.id) == {}
    long = [f"src-missing-{index}" for index in range(40_000)] + [alice_source]
    assert store.foreign_memory_sources_for_source_ids(long, world.owner.id) == {
        alice_source: (a1, world.alice.id)
    }
    assert store.foreign_memory_sources_for_source_ids_sql().count("?") == 2


def test_foreign_memory_sources_probe_sources_by_primary_key(world):
    store = world.repo._runtime.memory_store
    with world.repo._runtime.database.connect() as db:
        plan = [
            str(row["detail"])
            for row in db.execute(
                "EXPLAIN QUERY PLAN " + store.foreign_memory_sources_for_source_ids_sql(),
                (json.dumps(["a", "b"]), world.alice.id),
            ).fetchall()
        ]
    joined = "\n".join(plan)
    assert "SCAN wanted" in joined, joined
    assert any(
        line.startswith("SEARCH s USING INDEX") and "(id=?)" in line for line in plan
    ), joined
    assert any(line.startswith("SEARCH fo USING") for line in plan), joined
    assert "SCAN s" not in joined and "SCAN fo" not in joined, joined


def test_understanding_bytes_of_a_report_without_memory_are_unchanged(world):
    """The planner-record preservation only acts when a record exists: a
    report without one stores exactly the JSON text it stored before."""
    from tests.report_share_disclosure_cases import _new_report

    repo, nb = world.repo, world.notebook
    rid = _new_report(world, world.alice)
    understanding = {"objective": "环路", "note": None, "list": [1, 2]}
    repo.update_report(nb, rid, understanding=understanding)
    with repo._runtime.database.connect() as db:
        raw = db.execute(
            "SELECT understanding_json FROM reports WHERE id=?", (rid,)
        ).fetchone()[0]
    assert raw == json.dumps(understanding, ensure_ascii=False)


# --- the retrieval record ----------------------------------------------------------


def test_the_retrieval_record_reads_every_shape_retrieval_returns():
    from app.domain.retrieval import RetrievedElement, RetrievedKnowledge
    from app.models.common import Evidence
    from app.services.report_memory_use import RetrievalSourceLog

    class _Port:
        def hits(self):
            return [
                RetrievedKnowledge(
                    object_id="o1", object_type="concept",
                    payload={"source_id": "src-payload", "nested": [{"source_id": "src-deep"}]},
                    evidence=[Evidence(
                        source_id="src-evidence", source_title="t", element_id="",
                        element_type="paragraph", location_label="", quoted_span="",
                        confidence=1.0,
                    )],
                ),
                RetrievedElement("e1", "src-element", "t", "", "paragraph", "x"),
            ]

        def context(self):
            return "block", {"k1": {"source_id": "src-context"}}

        flag = True

    log = RetrievalSourceLog()
    port = log.watch(_Port())
    assert port.flag is True
    port.hits()
    port.context()
    assert log.source_ids() == [
        "src-context", "src-deep", "src-element", "src-evidence", "src-payload",
    ]
    call = log.watch_call(lambda: ({"source_id": "src-call"},))
    call()
    assert "src-call" in log.source_ids()


def test_the_retrieval_record_proxy_writes_through_and_restores(monkeypatch):
    """A method replaced through the proxy replaces it on the port (as before
    the port was watched), and undoing that leaves the port as it was."""
    from app.services.report_memory_use import RetrievalSourceLog

    class _Port:
        def hits(self):
            return [{"source_id": "src-real"}]

    inner = _Port()
    port = RetrievalSourceLog().watch(inner)
    with monkeypatch.context() as patch:
        patch.setattr(port, "hits", lambda: [{"source_id": "src-fake"}])
        assert inner.hits() == [{"source_id": "src-fake"}]
    assert "hits" not in vars(inner)
    assert inner.hits() == [{"source_id": "src-real"}]


# --- the service rule ------------------------------------------------------------


class _NoMemorySources:
    def __init__(self):
        self.calls = []

    def memory_ids_for_source_ids(self, source_ids, owner_id):
        self.calls.append((list(source_ids), owner_id))
        return []

    def foreign_memory_sources_for_source_ids(self, source_ids, member_id):
        self.calls.append(("foreign", list(source_ids), member_id))
        return {}


def test_disclosure_counts_distinct_memory_objects_and_asks_the_store_as_author():
    reader = _NoMemorySources()
    disclosure = report_share_disclosure(reader, {
        "created_by": "u-author",
        "memory_used": ["mem-1", "mem-4"],
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
        "u-author", ("mem-1", "mem-2", "mem-4"),
        known_memory_ids=frozenset({"mem-1", "mem-2", "mem-4"}),
        live_source_ids=("src-1",),
        foreign_memory_ids=("mem-3",),
    )
    # Recorded citations are never looked up again; only unrecorded ones are.
    assert reader.calls == [
        (["src-1"], "u-author"), ("foreign", ["src-1"], "u-author"),
    ]
    with pytest.raises(ForeignMemoryShareRefused):
        require_publishable(disclosure, requester_id="u-author", acknowledged=3)


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
