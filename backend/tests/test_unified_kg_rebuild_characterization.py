"""``KnowledgeLifecycleService.rebuild_unified_kg`` characterization on SQLite.

The world, the scenarios, the observation and the behaviour assertions live in
``tests/unified_kg_rebuild_characterization_cases.py`` (its docstring explains what is
observed and what is asserted); PostgreSQL runs the same scenarios in
``tests/postgres/test_unified_kg_rebuild_characterization_pg.py``. This module only says
which tables SQLite has.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.repositories.sqlite import knowledge_counts_cache
from app.services.sqlite_repository import SQLiteRepository
from tests import unified_kg_rebuild_characterization_cases as cases

_FTS_SHADOW = ("_fts_config", "_fts_content", "_fts_data", "_fts_docsize", "_fts_idx")


def _tables(repo) -> list[str]:
    with repo._runtime.database.connect() as db:
        names = [row[0] for row in db.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")]
    return [n for n in names if not n.startswith("sqlite_") and not n.endswith(_FTS_SHADOW)]


BACKEND = cases.Backend(
    name="sqlite",
    placeholder="?",
    tables=_tables,
    invalidate_caches=knowledge_counts_cache.invalidate,
)


@pytest.mark.parametrize("scenario", cases.SCENARIOS, ids=lambda s: s.name)
def test_rebuild_unified_kg_characterization(scenario, tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    monkeypatch.setenv("SILICON_NOTEBOOK_ENV_FILE", "")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'rebuild.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(storage))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", str(cases.EMBED_DIM))
    for key, value in scenario.env.items():
        monkeypatch.setenv(key, value)
    repo = SQLiteRepository(Settings())
    try:
        observation = cases.observe(repo, BACKEND, scenario, monkeypatch)
    finally:
        repo.close()
    scenario.expect(observation)

