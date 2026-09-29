"""Memory hard delete and member exit purge on PostgreSQL (E5-2).

Runs the exact scenarios of ``tests/test_memory_purge.py`` (defined once in
``tests/memory_purge_cases.py``) against the production PostgreSQL facade.
"""
from __future__ import annotations

import pytest

from tests.memory_purge_cases import (
    CASES,
    EMBED_DIM,
    MONKEYPATCH_CASES,
    build_world,
)


pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def repo(postgres_settings, tmp_path):
    from app.repositories.postgres.repository import PostgresRepository

    postgres_settings.storage_dir = str(tmp_path / "postgres-storage")
    postgres_settings.event_log_enabled = False
    postgres_settings.llm_log_enabled = False
    postgres_settings.embed_dim = EMBED_DIM
    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


@pytest.fixture
def world(repo):
    return build_world(repo, postgres=True)


@pytest.mark.parametrize("case", sorted(CASES))
def test_memory_purge_scenario_pg(world, case):
    CASES[case](world)


@pytest.mark.parametrize("case", sorted(MONKEYPATCH_CASES))
def test_memory_purge_fault_scenario_pg(world, monkeypatch, case):
    MONKEYPATCH_CASES[case](world, monkeypatch)
