"""E4-1 on the real PostgreSQL backend: the chunk store's write methods and the
chunking service refuse a Memory source. Mirrors ``tests/test_memory_chunk_guard.py`` (SQLite) -- the scenario
functions are shared and backend-neutral; see that file's module docstring for
the contract and for why the two layers (service, store) are pinned apart.
"""
from __future__ import annotations

import pytest

from tests.test_memory_chunk_guard import (
    scenario_memory_confirmation_ingests_without_chunks,
    scenario_service_entrypoints_write_nothing,
    scenario_service_leaves_planted_rows_untouched,
    scenario_service_skips_memory_and_emits,
    scenario_service_still_chunks_ordinary_source,
    scenario_store_guard_matches_memory_exactly,
    scenario_store_insert_rows_refuses_memory,
    scenario_store_probe_is_once_per_source_per_call,
    scenario_store_probe_shares_the_writes_connection_and_transaction,
    scenario_store_refusal_leaves_planted_rows_untouched,
    scenario_store_replace_refuses_memory,
    scenario_store_still_writes_ordinary_and_knowhow,
)


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_memory_chunk_guard"),
]


@pytest.fixture
def postgres_repository(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


def test_store_replace_refuses_memory(postgres_repository):
    scenario_store_replace_refuses_memory(postgres_repository)


def test_store_insert_rows_refuses_memory(postgres_repository):
    scenario_store_insert_rows_refuses_memory(postgres_repository)


def test_store_refusal_leaves_planted_rows_untouched(postgres_repository):
    scenario_store_refusal_leaves_planted_rows_untouched(postgres_repository)


def test_store_still_writes_ordinary_and_knowhow(postgres_repository):
    scenario_store_still_writes_ordinary_and_knowhow(postgres_repository)


def test_store_probe_is_once_per_source_per_call(postgres_repository):
    scenario_store_probe_is_once_per_source_per_call(postgres_repository)


def test_service_skips_memory_and_emits(postgres_repository):
    scenario_service_skips_memory_and_emits(postgres_repository)


def test_service_entrypoints_write_nothing(postgres_repository):
    scenario_service_entrypoints_write_nothing(postgres_repository)


def test_service_leaves_planted_rows_untouched(postgres_repository):
    scenario_service_leaves_planted_rows_untouched(postgres_repository)


def test_service_still_chunks_ordinary_source(postgres_repository):
    scenario_service_still_chunks_ordinary_source(postgres_repository)


def test_store_probe_shares_the_writes_connection_and_transaction(postgres_repository):
    scenario_store_probe_shares_the_writes_connection_and_transaction(postgres_repository)


def test_store_guard_matches_memory_exactly(postgres_repository):
    scenario_store_guard_matches_memory_exactly(postgres_repository)


def test_memory_confirmation_still_ingests_without_chunks(postgres_repository, monkeypatch):
    scenario_memory_confirmation_ingests_without_chunks(postgres_repository, monkeypatch)
