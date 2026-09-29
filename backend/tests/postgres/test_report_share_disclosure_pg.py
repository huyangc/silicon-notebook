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
    assert store.memory_ids_for_source_ids_sql().count("%s") == 2
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


def _plan(world, wanted: list[str]) -> str:
    store = world.repo._runtime.memory_store
    with world.repo._runtime.database.connect() as db:
        db.execute("SET LOCAL enable_seqscan=off")
        db.execute("SET LOCAL enable_bitmapscan=off")
        return "\n".join(
            str(row["QUERY PLAN"])
            for row in db.execute(
                "EXPLAIN (COSTS OFF) " + store.memory_ids_for_source_ids_sql(),
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
