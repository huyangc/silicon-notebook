"""Named sharing API on PostgreSQL (share-edit P1-T6).

Runs every scenario of ``tests/test_notebook_collaborators_api.py`` (defined
once in ``tests/notebook_collaborators_api_cases.py``) through the real routes
against the production PostgreSQL repository.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.memory_purge_cases import EMBED_DIM
from tests.notebook_collaborators_api_cases import (
    CASES,
    MONKEYPATCH_CASES,
    build_world,
)

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_notebook_collaborators_api"),
]


@pytest.fixture
def client(postgres_scope, tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", postgres_scope.url)
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", str(EMBED_DIM))
    from app.api import deps
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    deps.repository.cache_clear()
    try:
        yield TestClient(create_app())
    finally:
        deps.repository().close()
        deps.repository.cache_clear()
        get_settings.cache_clear()


@pytest.mark.parametrize("case", sorted(CASES))
def test_notebook_collaborators_api_pg(client, case):
    CASES[case](build_world(client, postgres=True))


@pytest.mark.parametrize("case", sorted(MONKEYPATCH_CASES))
def test_notebook_collaborators_api_patched_pg(client, monkeypatch, case):
    MONKEYPATCH_CASES[case](build_world(client, postgres=True), monkeypatch)
