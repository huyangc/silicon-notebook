"""Named sharing API on SQLite (share-edit P1-T6).

The scenarios live in ``tests/notebook_collaborators_api_cases.py`` and run
unchanged on PostgreSQL through
``tests/postgres/test_notebook_collaborators_api_pg.py``.
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


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
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
    return TestClient(create_app())


@pytest.mark.parametrize("case", sorted(CASES))
def test_notebook_collaborators_api(client, case):
    CASES[case](build_world(client, postgres=False))


@pytest.mark.parametrize("case", sorted(MONKEYPATCH_CASES))
def test_notebook_collaborators_api_patched(client, monkeypatch, case):
    MONKEYPATCH_CASES[case](build_world(client, postgres=False), monkeypatch)
