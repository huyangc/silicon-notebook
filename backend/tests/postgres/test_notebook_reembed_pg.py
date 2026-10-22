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


BACKEND = cases.Backend(name="postgres", insert=_insert, placeholder="%s")


@pytest.fixture
def repo(postgres_scope, tmp_path, monkeypatch):
    monkeypatch.setenv("SILICON_NOTEBOOK_ENV_FILE", "")
    monkeypatch.setenv("DATABASE_URL", postgres_scope.url)
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for key, value in cases.ENV.items():
        monkeypatch.setenv(key, value)
    repository = create_repository(Settings())
    try:
        yield repository
    finally:
        repository.close()


def test_every_vector_comes_back_once_pg(repo):
    cases.check_every_vector_comes_back_once(repo, BACKEND)


def test_backfill_job_keeps_knowhow_chunks_on_their_workload_pg(repo):
    cases.check_backfill_job_keeps_knowhow_chunks_on_their_workload(repo, BACKEND)


def test_object_vectors_mark_the_graph_dirty_pg(repo):
    cases.check_object_vectors_mark_the_graph_dirty(repo, BACKEND)


def test_model_failure_is_visible_and_resumes_pg(repo):
    cases.check_model_failure_is_visible_and_resumes(repo, BACKEND)


def test_unbound_workload_is_visible_pg(repo):
    cases.check_unbound_workload_is_visible(repo, BACKEND)
