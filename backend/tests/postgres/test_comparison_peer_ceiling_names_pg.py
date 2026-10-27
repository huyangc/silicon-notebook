"""PostgreSQL twin of ``tests/test_comparison_peer_ceiling_names.py``: rule
R3 of the comparison-peer name exit (a peer is named only from in-ceiling
members) on PostgreSQL's own stores -- the ``comparison_peer_members`` LATERAL
read, the ``with_canonical_id`` shapes of the two peer reads, both peer paths,
the chunk sub-queries and a real reasoning run. Same world and scenarios
(``tests/comparison_peer_names_cases.py``).

The schema migration and the world's ``rebuild_unified_kg`` dominate a
PostgreSQL test's cost (seconds each), so each test builds one world and runs
its tuple of scenarios on it in turn; the tuples are ordered for that (see the
cases module).
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.embedding import FakeEmbedder
from app.services.repository_factory import create_repository
from tests import comparison_peer_names_cases as cases
from tests.model_testkit import bind_all_embedding_clients

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_comparison_peer_names"),
]


@pytest.fixture
def world(postgres_scope, tmp_path, monkeypatch):
    monkeypatch.setenv("SILICON_NOTEBOOK_ENV_FILE", "")
    monkeypatch.setenv("DATABASE_URL", postgres_scope.url)
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    for key in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
                "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL",
                "REASONING_LLM_MODEL"):
        monkeypatch.setenv(key, "")
    repository = create_repository(Settings())
    try:
        bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
        yield cases.build_world(repository, postgres=True)
    finally:
        repository.close()


def test_comparison_peer_names_pg(world, monkeypatch):
    for scenario in cases.SCENARIOS:
        scenario(world, monkeypatch)


@pytest.mark.parametrize("path", cases.PATHS)
def test_comparison_peer_names_per_path_pg(world, monkeypatch, path):
    for scenario in cases.PATH_SCENARIOS:
        scenario(world, monkeypatch, path)
