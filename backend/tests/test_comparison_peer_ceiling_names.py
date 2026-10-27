"""Rule R3 of the comparison-peer name exit on SQLite: under a per-library
source ceiling a peer is named only from in-ceiling members.

The scenarios and the world are backend-neutral
(``tests/comparison_peer_names_cases.py``); the PostgreSQL twin is
``tests/postgres/test_comparison_peer_ceiling_names_pg.py``. The SQL source
gate that decides WHICH peers survive is pinned by
``tests/test_comparison_peer_ceiling.py``.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository
from tests import comparison_peer_names_cases as cases
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    for key in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
                "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL",
                "REASONING_LLM_MODEL"):
        monkeypatch.setenv(key, "")
    repository = SQLiteRepository(Settings())
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    return cases.build_world(repository, postgres=False)


@pytest.mark.parametrize("scenario", cases.SCENARIOS, ids=lambda s: s.__name__)
def test_comparison_peer_names(world, monkeypatch, scenario):
    scenario(world, monkeypatch)


@pytest.mark.parametrize("path", cases.PATHS)
@pytest.mark.parametrize("scenario", cases.PATH_SCENARIOS, ids=lambda s: s.__name__)
def test_comparison_peer_names_per_path(world, monkeypatch, scenario, path):
    scenario(world, monkeypatch, path)
