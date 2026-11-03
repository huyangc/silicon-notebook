"""The guarded Knowhow cell write's reason codes and post-write values, on
SQLite (share-edit P1-T11, D7). The scenarios live in
``tests/knowhow_cell_conflict_cases.py`` and run unchanged on PostgreSQL through
``tests/postgres/test_knowhow_cell_conflict_pg.py``.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.api import deps
from tests.knowhow_cell_conflict_cases import CASES, build_world


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    deps.repository.cache_clear()
    return TestClient(create_app())


@pytest.mark.parametrize("case", sorted(CASES))
def test_knowhow_cell_conflict(client, case):
    CASES[case](build_world(client, postgres=False))
