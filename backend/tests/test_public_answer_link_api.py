"""MCP answer links on the anonymous public routes (T6), on SQLite.

Every scenario lives once in ``tests/public_answer_link_api_cases.py``; the
PostgreSQL lane runs the same ``CASES`` in
``tests/postgres/test_public_answer_link_api_pg.py``.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.public_answer_link_api_cases import CASES, make_env


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("MINERU_RETURN_IMAGES", "true")
    from app.api import deps
    from app.core.config import get_settings
    from app.main import app

    # conftest clears both at test start; rebuild against the env just set.
    get_settings.cache_clear()
    deps.repository.cache_clear()
    return make_env(TestClient(app), deps.repository())


@pytest.mark.parametrize("case", sorted(CASES))
def test_public_answer_link_scenario(env, case):
    CASES[case](env)
