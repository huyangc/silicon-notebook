"""PostgreSQL twin of the P4-T5 cases in tests/test_rebuild_desc_cache.py: the
concept-description cache is looked up by its input signature, so a rebuild
that moves every canonical id (S1's seed switch) while names and quotes stay
the same reuses every description -- zero model calls -- and a changed
signature still regenerates. The previous descriptions are read through the
PostgreSQL ``cluster_description_rows`` (published generation), so the same
scenario runs end to end on this backend."""
from __future__ import annotations

import pytest

from app.services.embedding import FakeEmbedder
from tests.model_testkit import bind_all_embedding_clients
from tests.test_rebuild_desc_cache import (
    assert_changed_signature_still_regenerates,
    assert_moved_ids_reuse_every_description,
)

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_rebuild_desc_cache"),
]


@pytest.fixture
def repo(postgres_settings):
    from app.services.postgres_repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    try:
        yield repository
    finally:
        repository.close()


def test_pg_moved_canonical_ids_reuse_descriptions_by_signature(repo, monkeypatch):
    assert_moved_ids_reuse_every_description(repo, monkeypatch)


def test_pg_moved_canonical_id_with_a_changed_signature_regenerates(repo, monkeypatch):
    assert_changed_signature_still_regenerates(repo, monkeypatch)
