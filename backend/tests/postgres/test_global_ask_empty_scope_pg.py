"""PostgreSQL twin of ``tests/test_global_ask_empty_scope.py``.

Global Ask refuses, at submission, a scope with nothing a global run could
search -- every participant's frozen visible-source ceiling empty. The
scenarios are shared and backend-neutral; this file runs them on the canonical
backend (see the SQLite file's module docstring for the contract).
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from tests.test_global_ask_empty_scope import SCENARIOS, build_world, run


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_global_ask_empty_scope"),
]


def _settings(url: str, storage: Path) -> Settings:
    return Settings(
        _env_file=None,
        database_url=url,
        storage_dir=str(storage),
        postgres_pool_min_size=1,
        postgres_pool_max_size=4,
        postgres_pool_acquire_timeout_seconds=2,
        postgres_statement_timeout_seconds=15,
        postgres_lock_timeout_seconds=2,
    )


@pytest.fixture
def postgres_world(postgres_scope, tmp_path, monkeypatch):
    from app.repositories.postgres.repository import PostgresRepository

    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("MODEL_SERVICES_CONFIG", "")
    repository = PostgresRepository(_settings(postgres_scope.url, tmp_path / "storage"))
    try:
        yield repository, build_world(repository, "%s")
    finally:
        repository.close()


@pytest.mark.parametrize("scenario", SCENARIOS, ids=lambda s: s.__name__)
def test_global_empty_scope_on_postgres(postgres_world, monkeypatch, scenario):
    repository, world = postgres_world
    run(scenario, repository, world, monkeypatch)

