"""PostgreSQL twin of the ranking executor's S/Q/P and source-scope cases."""
import time

import pytest
from psycopg.errors import QueryCanceled
from psycopg.pq import TransactionStatus

from app.repositories.read_budget import ReadBudgetExceeded, read_budget
from tests.postgres.test_kg_findings_store_pg import postgres_repository as postgres_repository
from tests.test_concept_ranking import check_paths, check_scope

pytestmark = pytest.mark.postgres_integration


def test_budget_before_statement_returns_healthy_connection_to_pool(postgres_repository, monkeypatch):
    database = postgres_repository._runtime.database
    now = time.monotonic()
    with pytest.raises(ReadBudgetExceeded), read_budget(now + 1):
        with database.connect_read() as db:
            connection = db.connection
            assert db.execute("SELECT 1 AS value").fetchone()["value"] == 1
            monkeypatch.setattr(time, "monotonic", lambda: now + 2)
            db.execute("SELECT 1 AS value")
    assert not connection.closed
    assert connection.info.transaction_status == TransactionStatus.IDLE


@pytest.mark.parametrize("check", [check_paths, check_scope])
def test_ranking_executor(postgres_repository, monkeypatch, check):
    check(postgres_repository, monkeypatch, postgres=True)


def test_visible_source_count_honors_frozen_ceiling(postgres_repository):
    from tests.test_concept_ranking import seed_rank_world

    nb = seed_rank_world(postgres_repository, postgres=True)
    sources = postgres_repository._runtime.source_store
    assert sources.visible_source_count(nb) == 12
    assert sources.visible_source_count(nb, ceiling=frozenset()) == 0
    assert sources.visible_source_count(
        nb, ceiling=frozenset({"s00", "s01", "memory", "knowhow", "missing"})) == 2


def test_real_statement_timeout_retains_completed_recount(postgres_repository, monkeypatch):
    from app.services import concept_ranking as ranking_module
    from tests.test_concept_ranking import hits, request, seed_rank_world, service

    repo = postgres_repository
    nb = seed_rank_world(repo, postgres=True)
    store = repo._runtime.unified_kg
    original = store.concept_recount_rows
    monkeypatch.setattr(store, "concept_rank_member_counts", lambda *args: {"wide": 10001, "single": 1})
    now, calls, interrupted = [100.0], [], []
    monkeypatch.setattr(ranking_module.time, "monotonic", lambda: now[0])
    connections = []
    def recount(db, notebook, ids, *, ceiling):
        calls.append(tuple(ids))
        if len(calls) == 1:
            return original(db, notebook, ids, ceiling=ceiling)
        connections.append(db.connection)
        db.statement_ceiling_ms = 1
        try:
            db.execute("SELECT pg_sleep(0.05)")
        except QueryCanceled:
            interrupted.append(True)
            now[0] += 2
            raise
        pytest.fail("the PostgreSQL statement timeout must cancel the real query")
    monkeypatch.setattr(store, "concept_recount_rows", recount)
    lib = request(service(repo), nb, query="主题", retrieval=hits("wide-00", "single")).computation.libraries[0]
    assert interrupted and connections[0].closed
    assert lib.coverage.status == "ok" and not lib.coverage.skipped
    assert lib.coverage.recount_capped and lib.coverage.recount_skipped == 1
    assert [(row.canonical_id, row.source_count) for row in lib.rows] == [("wide", 9)]
