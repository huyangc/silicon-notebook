"""``KnowledgeLifecycleService.rebuild_unified_kg`` characterization on real PostgreSQL.

The twin of ``tests/test_unified_kg_rebuild_characterization.py``: the same world,
scenarios and behaviour assertions from ``tests/unified_kg_rebuild_characterization_cases.py``;
this module only says which tables the isolated schema has.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.repositories.factory import create_repository
from app.repositories.postgres import knowledge_counts_cache
from tests import unified_kg_rebuild_characterization_cases as cases

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_unified_kg_rebuild_characterization"),
]


def _tables(repo) -> list[str]:
    with repo._runtime.database.connect() as db:
        rows = db.execute(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE' "
            "ORDER BY table_name"
        ).fetchall()
    return [row["table_name"] for row in rows]


BACKEND = cases.Backend(
    name="postgres",
    placeholder="%s",
    tables=_tables,
    invalidate_caches=knowledge_counts_cache.invalidate,
)


@pytest.mark.parametrize("scenario", cases.SCENARIOS, ids=lambda s: s.name)
def test_rebuild_unified_kg_characterization_pg(scenario, postgres_scope, tmp_path,
                                                monkeypatch):
    storage = tmp_path / "storage"
    monkeypatch.setenv("SILICON_NOTEBOOK_ENV_FILE", "")
    monkeypatch.setenv("DATABASE_URL", postgres_scope.url)
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(storage))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", str(cases.EMBED_DIM))
    for key, value in scenario.env.items():
        monkeypatch.setenv(key, value)
    repo = create_repository(Settings())
    try:
        observation = cases.observe(repo, BACKEND, scenario, storage, monkeypatch)
    finally:
        repo.close()
    scenario.expect(observation)

