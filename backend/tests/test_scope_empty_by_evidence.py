"""The empty-scope refusal judges ticked reference libraries by evidence, on
SQLite.  The scenarios live in ``scope_empty_by_evidence_cases.py`` and run,
unchanged, on PostgreSQL in ``postgres/test_scope_empty_by_evidence_pg.py``.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.report_share_disclosure_cases import build_world
from tests.scope_empty_by_evidence_cases import CASES


@pytest.fixture
def client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.api import deps
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    deps.repository.cache_clear()
    return TestClient(create_app())


@pytest.fixture
def world(client, monkeypatch):
    return build_world(client, monkeypatch)


@pytest.mark.parametrize("case", sorted(CASES))
def test_scope_empty_by_evidence_scenario(world, case):
    CASES[case](world)
