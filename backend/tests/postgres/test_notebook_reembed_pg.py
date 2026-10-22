"""Notebook re-embed job on real PostgreSQL.

The twin of ``tests/test_notebook_reembed.py``: the same world and checks from
``tests/notebook_reembed_cases.py``; this module only says how PostgreSQL stores a row
(JSON columns as jsonb) and binds its placeholders. The single-flight guard is
process-local and backend-neutral, so it is pinned on SQLite only.
"""
from __future__ import annotations

import pytest
from psycopg.types.json import Jsonb

from app.core.config import Settings
from app.repositories.postgres import maintenance as postgres_maintenance
from app.services.notebook_reembed import NotebookReembedService
from app.services.repository_factory import create_repository
from tests import notebook_reembed_cases as cases

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_notebook_reembed"),
]


def _insert(db, table: str, values: dict) -> None:
    columns = list(values)
    db.execute(
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('%s' for _ in columns)})",
        [Jsonb(values[c]) if isinstance(values[c], (dict, list)) else values[c] for c in columns],
    )


BACKEND = cases.Backend(
    name="postgres", insert=_insert, placeholder="%s",
    maintenance_module=postgres_maintenance, relation_page_attr="_EMBEDDING_PAGE_SIZE",
)


@pytest.fixture
def env(postgres_scope, tmp_path, monkeypatch):
    monkeypatch.setenv("SILICON_NOTEBOOK_ENV_FILE", "")
    monkeypatch.setenv("DATABASE_URL", postgres_scope.url)
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for key, value in cases.ENV.items():
        monkeypatch.setenv(key, value)


@pytest.fixture
def repo(env):
    repository = create_repository(Settings())
    try:
        yield repository
    finally:
        repository.close()


def test_every_vector_comes_back_once_pg(repo):
    cases.check_every_vector_comes_back_once(repo, BACKEND)


def test_backfill_job_fills_knowhow_without_touching_the_graph_pg(repo):
    cases.check_backfill_job_fills_knowhow_without_touching_the_graph(repo, BACKEND)


def test_knowhow_fill_skips_a_chunk_rewritten_mid_call_pg(repo):
    cases.check_knowhow_fill_skips_a_chunk_rewritten_mid_call(repo, BACKEND)


def test_offline_missing_only_embed_keeps_knowhow_on_its_workload_pg(repo):
    cases.check_offline_missing_only_embed_keeps_knowhow_on_its_workload(repo, BACKEND)


def test_object_vectors_mark_the_graph_dirty_pg(repo):
    cases.check_object_vectors_mark_the_graph_dirty(repo, BACKEND)


def test_model_failure_is_visible_and_resumes_pg(repo):
    cases.check_model_failure_is_visible_and_resumes(repo, BACKEND)


def test_unbound_workload_is_visible_pg(repo):
    cases.check_unbound_workload_is_visible(repo, BACKEND)


def test_a_failed_discovery_fails_its_leg_pg(repo, monkeypatch):
    cases.check_a_failed_discovery_fails_its_leg(repo, BACKEND, monkeypatch)


def test_a_pipeline_switch_aborts_the_pass_pg(repo, monkeypatch):
    cases.check_a_pipeline_switch_aborts_the_pass(repo, BACKEND, monkeypatch)


def test_relation_backfill_pages_pg(repo, monkeypatch):
    cases.check_relation_backfill_pages(repo, BACKEND, monkeypatch)


def test_object_leg_keeps_its_count_when_it_fails_pg(repo, monkeypatch):
    cases.check_object_leg_keeps_its_count_when_it_fails(repo, BACKEND, monkeypatch)


def test_relation_backfill_in_a_fresh_process_pg(env):
    """A process whose retrieval has never been composed (the offline relation
    backfill, or a job that runs before the first query) still fills relation vectors:
    the backfill composes retrieval itself instead of reading the unset attribute."""
    builder = create_repository(Settings())
    try:
        nb = cases.build_world(builder, BACKEND).notebook_id
        cases.delete_vectors(builder, BACKEND, nb, tables=("relation_embeddings",))
    finally:
        builder.close()
    fresh = create_repository(Settings())
    try:
        assert fresh._runtime.retrieval is None  # nothing has composed retrieval yet
        embedders = cases.bind_embedders(fresh)
        status = NotebookReembedService(fresh).run(nb, legs=["kg_relations"])
        assert status.state == "succeeded", status
        assert status.progress["kg_relations"].embedded >= 3
        assert embedders["relation_embedding"].calls
    finally:
        fresh.close()
