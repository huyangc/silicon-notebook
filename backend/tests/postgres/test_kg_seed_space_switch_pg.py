"""The concept seed-space switch (S1) and the squash candidate tier (P3) inside
real rebuilds on PostgreSQL (the primary environment): the twin of
``tests/test_kg_seed_space_switch.py``, same scenarios
(``tests/kg_seed_space_world.py``), same literal expectations."""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.embedding import FakeEmbedder
from app.services.postgres_repository import PostgresRepository
from tests import kg_seed_space_world as world
from tests.model_testkit import bind_all_embedding_clients

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_kg_seed_space_switch"),
]


@pytest.fixture
def repo(postgres_settings: Settings):
    repository = PostgresRepository(postgres_settings.model_copy(update={"embed_dim": 16}))
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    try:
        yield repository
    finally:
        repository.close()


@pytest.mark.parametrize("name", sorted(world.SCENARIOS))
def test_seed_space_scenario_pg(repo, monkeypatch, name):
    world.SCENARIOS[name](repo, monkeypatch)


def test_a_row_decided_again_mid_rebuild_stays_v1_pg(repo):
    world.assert_a_row_decided_again_mid_rebuild_stays_v1(repo)
