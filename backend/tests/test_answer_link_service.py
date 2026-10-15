"""MCP answer links, service layer (T5), on SQLite.

Every scenario lives once in ``tests/answer_link_service_cases.py``; the
PostgreSQL lane runs the same ``CASES`` in
``tests/postgres/test_answer_link_service_pg.py``.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.answer_link_service_cases import CASES, make_env


@pytest.fixture
def env(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.api.deps import repository
    from app.main import app

    return make_env(TestClient(app), repository())


@pytest.mark.parametrize("case", sorted(CASES))
def test_answer_link_service_scenario(env, case):
    CASES[case](env)
