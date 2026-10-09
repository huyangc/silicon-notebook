"""PostgreSQL twin of tests/test_memory_kg_readers_e2e.py (E4-8): the same
world, the same cases, through the real HTTP routes on a real PostgreSQL
schema -- member A viewing a shared notebook where member B owns a confirmed
Memory, B browsing its own, a token without ``memory:read``, a notebook still
awaiting its isolated rebuild, a notebook without Memory, and MCP
``search`` (``include="formal"``).

Plus the EXPLAIN pin of the one statement this task changed on the
PostgreSQL side: the rebuild's end-state counts (``finish_rebuild_state``).
"""
from __future__ import annotations

import pytest

from tests.test_memory_kg_readers_e2e import (
    ISOLATED_CASES,
    LEGACY_CASES,
    World,
    build_world,
    case_no_memory_notebook_is_byte_identical,
    run_mcp_cases,
)

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_memory_kg_readers_e2e"),
]


@pytest.fixture
def pg_world(postgres_scope, tmp_path, monkeypatch):
    """``build(kind) -> World`` over a PostgreSQL schema of its own."""
    from fastapi.testclient import TestClient

    from app.api.deps import repository
    from app.core.config import get_settings
    from app.main import create_app

    monkeypatch.setenv("DATABASE_URL", postgres_scope.url)
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("POSTGRES_POOL_MAX_SIZE", "4")
    monkeypatch.setenv("EMBED_DIM", "16")
    monkeypatch.setenv("MCP_PUBLIC_URL", "https://memory.example.test/mcp")
    monkeypatch.setenv("MCP_REQUIRE_HTTPS", "1")
    get_settings.cache_clear()
    client = TestClient(create_app())
    repo = repository()
    assert type(repo).__name__ == "PostgresRepository"

    def build(kind: str) -> World:
        return build_world(client, repo, lambda sql: sql.replace("?", "%s"),
                           postgres=True, kind=kind)

    try:
        yield build
    finally:
        repo.close()


@pytest.mark.parametrize("case", ISOLATED_CASES, ids=lambda c: c.__name__)
def test_pg_isolated_notebook_through_the_routes(case, pg_world, monkeypatch):
    case(pg_world("isolated"), monkeypatch)


@pytest.mark.parametrize("case", LEGACY_CASES, ids=lambda c: c.__name__)
def test_pg_notebook_awaiting_its_isolated_rebuild_through_the_routes(
    case, pg_world, monkeypatch,
):
    case(pg_world("legacy"), monkeypatch)


def test_pg_a_notebook_without_memory_is_byte_identical_through_the_routes(
    pg_world, monkeypatch,
):
    case_no_memory_notebook_is_byte_identical(pg_world("plain"), monkeypatch)


@pytest.mark.anyio
async def test_pg_mcp_search_formal_knowledge_leg(pg_world, monkeypatch):
    await run_mcp_cases(pg_world("isolated"), monkeypatch)


# --------------------------------------------------------------------------
# EXPLAIN pin: the rebuild's end-state counts leave Memory out
# --------------------------------------------------------------------------

class _CapturingConnection:
    """Records each statement a store method issues, then runs it."""

    def __init__(self, connection):
        self.connection = connection
        self.statements: list[tuple[str, tuple]] = []

    def __getattr__(self, name):
        return getattr(self.connection, name)

    def execute(self, sql, params=None):
        self.statements.append((str(sql), tuple(params or ())))
        return self.connection.execute(sql, params)


def _plan(connection, sql: str, params: tuple) -> str:
    connection.execute("SET LOCAL enable_seqscan=off")
    connection.execute("SET LOCAL enable_bitmapscan=off")
    rows = connection.execute(f"EXPLAIN (COSTS OFF) {sql}", params).fetchall()
    return "\n".join(str(row["QUERY PLAN"]) for row in rows)


def test_pg_the_end_state_counts_exclude_memory_by_index_probes(pg_world):
    from app.repositories.postgres.unified_kg_store import UnifiedKgStore

    world = pg_world("isolated")
    database = world.repo._runtime.database
    with database.write() as db:
        captured = _CapturingConnection(db)
        UnifiedKgStore.finish_rebuild_state(
            captured, world.nb, "version", 3, "2026-09-30T00:00:00+00:00", 0, input_seq=0)
        db.rollback()
    counts = [s for s in captured.statements if s[0].startswith("SELECT COUNT(*)")]
    assert len(counts) == 2, captured.statements
    objects_sql, relations_sql = counts
    assert "knowledge_objects" in objects_sql[0] and "knowledge_relations" in relations_sql[0]
    # the answers: B's Memory is not in the snapshot
    with database.connect() as db:
        stored = db.execute(
            "SELECT object_count, relation_count FROM unified_kg_state WHERE notebook_id=%s",
            (world.nb,)).fetchone()
    assert (stored["object_count"], stored["relation_count"]) == world.ids.shared_counts
    # the plans: the Memory exclusion is an anti join on the sources key, never
    # a scan of the site-wide source table
    with database.connect() as db:
        for name, (sql, params) in (("objects", objects_sql), ("relations", relations_sql)):
            plan = _plan(db, sql, params)
            assert "Seq Scan" not in plan, (name, plan)
            assert "Anti Join" in plan, (name, plan)
            assert "pk_sources" in plan or "idx_sources" in plan, (name, plan)
