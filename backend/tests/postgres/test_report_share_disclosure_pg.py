"""Report share disclosure on PostgreSQL (E7-2, ruling M4).

Runs the scenarios of ``tests/report_share_disclosure_cases.py`` through the
real HTTP routes against the production PostgreSQL repository, plus the
PostgreSQL side of the new store read and its EXPLAIN pin.
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from tests.report_share_disclosure_cases import CASES, build_world, make_memory

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_report_share_disclosure"),
]


@pytest.fixture
def client(postgres_scope, tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", postgres_scope.url)
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.api import deps
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    deps.repository.cache_clear()
    try:
        yield TestClient(create_app())
    finally:
        deps.repository().close()
        deps.repository.cache_clear()
        get_settings.cache_clear()


@pytest.fixture
def world(client, monkeypatch):
    return build_world(client, monkeypatch)


@pytest.mark.parametrize("case", sorted(CASES))
def test_report_share_disclosure_scenario_pg(world, case):
    CASES[case](world)


def test_share_transaction_locks_only_the_cited_memory_pg(world):
    """While the share transaction is between its count and its commit, a
    status change of the cited Memory waits for it, and nothing else does:
    the author's uncited Memory and another user's Memory are confirmed at
    once.  The hold lasts exactly the share transaction."""
    import threading
    import time

    from tests.report_share_disclosure_cases import make_report, share, source_ref

    make_memory(world, world.alice, "a1")
    cited_memory, cited_source = world.memories["a1"]
    rid = make_report(world, world.alice, [source_ref(cited_source, "k1")])
    store = world.repo._runtime.report_store
    real_now = store.now
    inside, release = threading.Event(), threading.Event()

    def held_now():
        inside.set()
        assert release.wait(30)
        return real_now()

    world.monkeypatch.setattr(store, "now", held_now)
    results: dict[str, object] = {}

    def publish():
        results["share"] = share(world, world.alice, rid, 1).status_code

    def deprecate_cited():
        world.repo._runtime.memory_service.deprecate(cited_memory, world.alice.id)
        results["deprecated_at"] = time.monotonic()

    publisher = threading.Thread(target=publish)
    publisher.start()
    assert inside.wait(30), "the share transaction never reached its UPDATE"
    blocked = threading.Thread(target=deprecate_cited)
    blocked.start()
    # Ordinary Memory writes are not held up by the publication.
    started = time.monotonic()
    make_memory(world, world.alice, "a2")          # the author's uncited Memory
    make_memory(world, world.owner, "o1")          # another user's Memory
    assert time.monotonic() - started < 3
    blocked.join(1.0)
    assert blocked.is_alive(), "the cited Memory changed under the share transaction"
    released_at = time.monotonic()
    release.set()
    publisher.join(30)
    blocked.join(30)
    assert results["share"] == 200
    assert results["deprecated_at"] >= released_at


@pytest.mark.parametrize(
    "statement, target",
    [
        # The source half (``FOR SHARE OF s``): removing only the projection row.
        ("DELETE FROM sources WHERE id=%s", "source"),
        # The Memory half (``FOR SHARE OF lm``): hard-deleting only the Memory
        # row (``sources.memory_id`` has no foreign key, so the source row is
        # not touched and the ``s`` lock alone would not hold it).
        ("DELETE FROM memory_items WHERE id=%s", "memory"),
    ],
)
def test_each_half_of_the_share_lock_holds_its_row_pg(world, statement, target):
    """Each input of the in-transaction count is held separately: a write to
    only the cited source row, or only the cited Memory row, waits for the
    share transaction to end."""
    import threading
    import time

    from tests.report_share_disclosure_cases import make_report, share, source_ref

    make_memory(world, world.alice, "a1")
    cited_memory, cited_source = world.memories["a1"]
    rid = make_report(world, world.alice, [source_ref(cited_source, "k1")])
    store = world.repo._runtime.report_store
    real_now = store.now
    inside, release = threading.Event(), threading.Event()

    def held_now():
        inside.set()
        assert release.wait(30)
        return real_now()

    world.monkeypatch.setattr(store, "now", held_now)
    results: dict[str, object] = {}

    def publish():
        results["share"] = share(world, world.alice, rid, 1).status_code

    def write_one_row():
        with world.repo._runtime.database.write() as db:
            db.execute(statement, (cited_source if target == "source" else cited_memory,))
        results["written_at"] = time.monotonic()

    publisher = threading.Thread(target=publish)
    publisher.start()
    assert inside.wait(30), "the share transaction never reached its UPDATE"
    writer = threading.Thread(target=write_one_row)
    writer.start()
    writer.join(1.0)
    assert writer.is_alive(), f"the cited {target} row changed under the share transaction"
    released_at = time.monotonic()
    release.set()
    publisher.join(30)
    writer.join(30)
    assert results["share"] == 200
    assert results["written_at"] >= released_at


def test_memory_ids_for_source_ids_maps_only_the_owners_memory_sources_pg(world):
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
    with world.repo._runtime.database.write() as db:
        db.execute("DELETE FROM memory_items WHERE id=%s", (a1,))
        orphan = db.execute(
            "SELECT source_type, memory_id FROM sources WHERE id=%s", (alice_source,)
        ).fetchone()
    assert (orphan["source_type"], orphan["memory_id"]) == ("memory", a1)
    assert store.memory_ids_for_source_ids([alice_source], world.alice.id) == []


def test_memory_ids_for_source_ids_is_stable_across_prepared_executions_pg(world):
    """One JSON parameter, not one per id and not ``= ANY(list)``: the same
    statement runs past psycopg's prepare threshold and PostgreSQL's
    generic-plan switch with list lengths from 1 to 40,000."""
    store = world.repo._runtime.memory_store
    a1 = make_memory(world, world.alice, "a1")
    source = world.memories["a1"][1]
    for lock in (False, True):
        assert store.memory_sources_for_source_ids_sql(lock=lock).count("%s") == 2
    for length in (1, 3, 300, 40_000, 2, 1, 5, 7, 9, 11, 13, 15):
        wanted = [f"src-missing-{index}" for index in range(length - 1)] + [source]
        assert store.memory_ids_for_source_ids(wanted, world.alice.id) == [a1]


def _seed_sources(world, *, uploads: int, foreign_memories: int) -> None:
    with world.repo._runtime.database.write() as db:
        db.execute("SET LOCAL statement_timeout = '0'")
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,created_at,updated_at) "
            "SELECT 'src-bulk-'||g,%s,'t','upload',now(),now() "
            "FROM generate_series(1,%s) g",
            (world.notebook, uploads),
        )
        db.execute(
            "INSERT INTO memory_items(id,notebook_id,created_by,origin,status,title,"
            "content_md,created_at,updated_at) SELECT 'mem-bulk-'||g,%s,%s,"
            "'ask_answer','confirmed','t','x',now(),now() FROM generate_series(1,%s) g",
            (world.notebook, world.owner.id, foreign_memories),
        )
        db.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,memory_id,created_at,"
            "updated_at) SELECT 'src-mem-bulk-'||g,%s,'t','memory','mem-bulk-'||g,"
            "now(),now() FROM generate_series(1,%s) g",
            (world.notebook, foreign_memories),
        )
        db.execute("ANALYZE sources")
        db.execute("ANALYZE memory_items")


def _plan(world, wanted: list[str], *, lock: bool = False) -> str:
    store = world.repo._runtime.memory_store
    with world.repo._runtime.database.connect() as db:
        db.execute("SET LOCAL enable_seqscan=off")
        db.execute("SET LOCAL enable_bitmapscan=off")
        return "\n".join(
            str(row["QUERY PLAN"])
            for row in db.execute(
                "EXPLAIN (COSTS OFF) "
                + store.memory_sources_for_source_ids_sql(lock=lock),
                (json.dumps(wanted), world.alice.id),
            ).fetchall()
        )


def test_memory_ids_for_source_ids_explain_pin_pg(world):
    """Planner-chosen shapes, judged like ``test_memory_sql_explain_pins.py``
    (seqscan/bitmapscan off, so the assertion is "an index path exists").

    * Few Memory sources in the deployment: the Memory-source index
      (``idx_sources_nb_hidden_type``) is read once and hashed against the id
      list; the owner check is one hashed SubPlan on the owner index.
    * Many Memory sources: the cited id list drives a primary-key probe of
      ``sources`` instead, so the cost follows the citation list, not the
      number of Memory sources.
    """
    wanted = [f"src-bulk-{index}" for index in range(1, 300)]
    _seed_sources(world, uploads=5_000, foreign_memories=0)
    few = _plan(world, wanted)
    assert "Function Scan on jsonb_array_elements_text wanted" in few, few
    assert "Index Scan using idx_sources_nb_hidden_type on sources s" in few, few
    assert "idx_memory_owner_notebook_status on memory_items rm" in few, few
    assert "Seq Scan" not in few, few

    _seed_sources(world, uploads=0, foreign_memories=30_000)
    many = _plan(world, wanted)
    assert "Function Scan on jsonb_array_elements_text wanted" in many, many
    assert "Index Scan using pk_sources on sources s" in many, many
    assert "Seq Scan" not in many, many

    # The share transaction's locking read: the same probe, plus the cited
    # rows' memory_items by primary key, under LockRows.
    locked = _plan(world, wanted, lock=True)
    assert "LockRows" in locked, locked
    assert "Index Scan using pk_sources on sources s" in locked, locked
    assert "pk_memory_items on memory_items lm" in locked, locked
    assert "Seq Scan" not in locked, locked


def test_foreign_memory_sources_map_only_other_members_memory_sources_pg(world):
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
    with world.repo._runtime.database.write() as db:
        db.execute("DELETE FROM memory_items WHERE id=%s", (o1,))
    assert store.foreign_memory_sources_for_source_ids(wanted, world.alice.id) == {}
    assert store.foreign_memory_sources_for_source_ids_sql().count("%s") == 2
    # Past psycopg's prepare threshold and the generic-plan switch.
    for length in (1, 3, 300, 40_000, 2, 1, 5, 7, 9, 11, 13, 15):
        long = [f"src-missing-{index}" for index in range(length - 1)] + [alice_source]
        assert store.foreign_memory_sources_for_source_ids(long, world.owner.id) == {
            alice_source: (a1, world.alice.id)
        }


def _foreign_plan(world, wanted: list[str]) -> str:
    store = world.repo._runtime.memory_store
    with world.repo._runtime.database.connect() as db:
        db.execute("SET LOCAL enable_seqscan=off")
        db.execute("SET LOCAL enable_bitmapscan=off")
        return "\n".join(
            str(row["QUERY PLAN"])
            for row in db.execute(
                "EXPLAIN (COSTS OFF) " + store.foreign_memory_sources_for_source_ids_sql(),
                (json.dumps(wanted), world.alice.id),
            ).fetchall()
        )


def test_foreign_memory_sources_explain_pin_pg(world):
    """Same judging as the author's read: an index path exists for every
    table, the cited id list drives the probe once Memory sources are many."""
    wanted = [f"src-bulk-{index}" for index in range(1, 300)]
    _seed_sources(world, uploads=5_000, foreign_memories=0)
    few = _foreign_plan(world, wanted)
    assert "Function Scan on jsonb_array_elements_text wanted" in few, few
    assert "on memory_items fo" in few, few
    assert "Seq Scan" not in few, few

    _seed_sources(world, uploads=0, foreign_memories=30_000)
    many = _foreign_plan(world, wanted)
    assert "Function Scan on jsonb_array_elements_text wanted" in many, many
    assert "Index Scan using pk_sources on sources s" in many, many
    assert "pk_memory_items on memory_items fo" in many, many
    assert "Seq Scan" not in many, many
