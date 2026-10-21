"""Report reference restore on a real SQLite repository (plan 2026-10-21 T4).

A follow-up question about a finished deep report reads the report's cited
items back from the notebook's current text, and the retrieval layer judges
every one of them under the run's installed ceiling -- called here directly,
with no report link verdict in front of it. The scenarios are shared with the
PostgreSQL twin (``tests/postgres/test_report_reference_restore_pg.py``); the
world they run in is described in ``tests/report_reference_restore_cases.py``.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from tests import report_reference_restore_cases as cases


@pytest.fixture
def world(tmp_path, monkeypatch):
    from app.services.sqlite_repository import SQLiteRepository

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'restore.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings(_env_file=None))
    try:
        yield cases.build_world(repo, postgres=False)
    finally:
        repo.close()


@pytest.mark.parametrize(
    "scenario", cases.SCENARIOS, ids=lambda scenario: scenario.__name__,
)
def test_report_reference_restore(world, scenario):
    scenario(world)
