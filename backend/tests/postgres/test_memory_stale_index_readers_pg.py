"""PostgreSQL twin of tests/test_memory_stale_index_readers.py (E4-4, plan
correction 8): a pre-isolation index's id of another member's Memory object
or relation is stopped where each ``allow_stale`` reader hydrates it."""
from __future__ import annotations

import pytest

from app.repositories.postgres import knowledge_counts_cache
from app.services.embedding import FakeEmbedder
from tests import memory_kg_reader_cases as cases
from tests.model_testkit import bind_all_embedding_clients

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_memory_stale_index_readers"),
]


@pytest.fixture
def world(postgres_settings):
    from app.repositories.postgres.repository import PostgresRepository

    settings = postgres_settings.model_copy(
        update={"embed_dim": 16, "relation_retrieval_enabled": True})
    repository = PostgresRepository(settings)
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    knowledge_counts_cache.invalidate()
    try:
        yield cases.build_world(repository, postgres=True)
    finally:
        repository.close()


def test_kg_ann_stale_label_of_foreign_memory_is_dropped_at_hydration_pg(world, monkeypatch):
    cases.check_stale_kg_ann_label(world, monkeypatch)


def test_relation_ann_stale_label_of_foreign_memory_is_dropped_at_the_boundary_pg(
    world, monkeypatch,
):
    cases.check_stale_relation_ann_label(world, monkeypatch)


def test_relation_completion_hydrates_only_the_completed_sources_objects_pg(world):
    cases.check_stale_completion_candidates(world)
