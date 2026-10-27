"""Real PostgreSQL recovery and cold KNN detection under one lexical budget."""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from psycopg.types.json import Jsonb

from app.repositories.postgres import search
from app.repositories.postgres.knowledge_store import KnowledgeStore
from app.repositories.postgres.lexical_budget import (
    KnowledgeLexicalBudget,
    KnowledgeLexicalBudgetExpired,
)
from app.repositories.postgres.migrator import PostgresMigrator

pytestmark = pytest.mark.postgres_integration
_NOW = "2026-10-27T00:00:00+00:00"


@pytest.fixture
def budget_store(postgres_database):
    PostgresMigrator(postgres_database).migrate()
    store = KnowledgeStore(postgres_database, SimpleNamespace())
    with postgres_database.write() as connection:
        connection.execute(
            "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at,"
            "username,password_hash,password_salt,password_iterations) "
            "VALUES ('user-budget','budget@example.test','Budget','admin','active',"
            "%s,%s,'s12345678','','',0)",
            (_NOW, _NOW),
        )
        connection.execute(
            "INSERT INTO notebooks(id,name,purpose,primary_domain,status,created_by,"
            "created_at,updated_at,tier) VALUES ('nb-budget','Budget','','','ready',"
            "'user-budget',%s,%s,'personal')",
            (_NOW, _NOW),
        )
        connection.execute(
            "INSERT INTO sources(id,notebook_id,title,source_type,status,parse_status,"
            "file_name,summary,created_at,updated_at) VALUES ('source-budget','nb-budget',"
            "'Budget','file','ready','ready','budget.md','',%s,%s)",
            (_NOW, _NOW),
        )
        for object_id, name, status in (
            ("ko-budget-a", "thermal", "approved"),
            ("ko-budget-b", "thermal pad", "approved"),
            ("ko-budget-c", "thermal pad grid matrix layout extension appendix rev nine", "approved"),
            ("ko-budget-deprecated", "thermals", "deprecated"),
        ):
            connection.execute(
                "INSERT INTO knowledge_objects(id,notebook_id,object_type,status,payload,"
                "evidence,source_id,created_at,updated_at) VALUES (%s,'nb-budget','claim',"
                "%s,%s,%s,'source-budget',%s,%s)",
                (object_id, status, Jsonb({"name": name}), Jsonb([]), _NOW, _NOW),
            )
        connection.execute(
            "CREATE INDEX idx_budget_knn ON knowledge_objects USING gist "
            "((((payload ->> 'name') COLLATE \"C\")) public.gist_trgm_ops(siglen=128)) "
            "WHERE status!='deprecated'"
        )
    search.reset_knn_index_cache()
    try:
        yield store
    finally:
        search.reset_knn_index_cache()


@pytest.mark.parametrize("allow_knn", [False, True])
def test_budgeted_results_match_unbudgeted_with_or_without_knn(budget_store, allow_knn):
    with budget_store.database.connect() as connection:
        before = connection.execute("SHOW statement_timeout").fetchone()["statement_timeout"]
        connection.execute("CREATE TEMP TABLE caller_lexical_state(value int)")
        connection.execute("INSERT INTO caller_lexical_state VALUES (7)")
        stats = {}
        budgeted = budget_store.fts_search(
            connection, "nb-budget", "thermal", 12, allow_knn=allow_knn,
            time_budget_seconds=1.5, routing_stats=stats,
        )
        ordinary = budget_store.fts_search(
            connection, "nb-budget", "thermal", 12, allow_knn=allow_knn,
        )
        assert budgeted == ordinary
        assert {row["object_id"] for row in budgeted} == {
            "ko-budget-a", "ko-budget-b", "ko-budget-c",
        }
        assert bool(stats["knn_term_count"]) is allow_knn
        assert stats["budget_exhausted"] == 0
        assert connection.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == before
        assert connection.execute("SELECT value FROM caller_lexical_state").fetchone()["value"] == 7


def test_real_query_cancellation_preserves_rows_and_caller_timeout(budget_store, monkeypatch):
    def slow_ilike(connection, *_args, **_kwargs):
        connection.execute("SELECT pg_sleep(1)")
        raise AssertionError("sleep should have been cancelled")

    monkeypatch.setattr(search, "_knowledge_ilike_rows_for_terms", slow_ilike)
    budget_store.database._pool.resize(1, 1)
    with budget_store.database.connect() as connection:
        connection.execute("CREATE TEMP TABLE caller_cancel_state(value int)")
        connection.execute("INSERT INTO caller_cancel_state VALUES (17)")
        connection.execute("SET LOCAL statement_timeout='500ms'")
        stats = {}
        rows = budget_store.fts_search(
            connection, "nb-budget", "thermal", 12, allow_knn=True,
            time_budget_seconds=1, routing_stats=stats,
        )
        assert {row["object_id"] for row in rows} == {"ko-budget-a", "ko-budget-b"}
        assert stats["budget_exhausted"] == 1
        assert connection.execute("SELECT 1 AS value").fetchone()["value"] == 1
        assert connection.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "500ms"
        assert connection.execute("SELECT value FROM caller_cancel_state").fetchone()["value"] == 17

    with budget_store.database.connect() as connection:
        assert connection.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "2s"


def test_nested_availability_timeout_does_not_poison_transaction(budget_store, monkeypatch):
    def slow_index_row(connection, _oid):
        return connection.execute("SELECT pg_sleep(0.2)").fetchone()

    monkeypatch.setattr(search, "_knn_index_row", slow_index_row)
    with budget_store.database.connect() as connection:
        stats = {}
        assert budget_store.fts_search(
            connection, "nb-budget", "thermal", 12, allow_knn=True,
            time_budget_seconds=0.05, routing_stats=stats,
        ) == []
        assert stats["budget_exhausted"] == 1
        assert connection.execute("SELECT 1 AS value").fetchone()["value"] == 1
        assert connection.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "2s"


def test_connection_view_restores_timeout_after_success_and_cancellation(budget_store):
    with budget_store.database.connect() as connection:
        connection.execute("SET LOCAL statement_timeout='60ms'")
        with KnowledgeLexicalBudget(connection, 1) as budget:
            # Fetch after the private savepoint has been rolled back: normal
            # psycopg cursors must retain their already-received result rows.
            assert budget.execute("SELECT 7 AS value").fetchone()["value"] == 7
            with pytest.raises(KnowledgeLexicalBudgetExpired):
                budget.execute("SELECT pg_sleep(0.2)")
        assert budget.exhausted is True
        assert connection.execute("SHOW statement_timeout").fetchone()["statement_timeout"] == "60ms"
        assert connection.execute("SELECT 1 AS value").fetchone()["value"] == 1
