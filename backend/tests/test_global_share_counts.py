"""The share dialog's exact numbers for a global conversation, on SQLite.

Every scenario lives in ``tests/share_counts_cases.py`` and runs unchanged on
PostgreSQL in ``postgres/test_global_share_counts_pg.py``.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.share_counts_cases import CASES


@pytest.fixture
def world(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'c.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "cs"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.api import deps
    from app.core.config import get_settings
    from app.main import create_app
    from tests.report_share_disclosure_cases import build_world

    get_settings.cache_clear()
    deps.repository.cache_clear()
    try:
        yield build_world(TestClient(create_app()), monkeypatch)
    finally:
        deps.repository().close()
        deps.repository.cache_clear()
        get_settings.cache_clear()


@pytest.mark.parametrize("case", sorted(CASES))
def test_global_share_counts_scenario(world, case):
    CASES[case](world)
