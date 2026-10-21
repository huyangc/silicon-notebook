"""``NotebookCopyService.copy_notebook`` behaviour scenarios on real PostgreSQL.

The twin of ``tests/test_notebook_copy_characterization.py``: the same world, scenarios
and assertions from ``tests/notebook_copy_characterization_cases.py``; this module only
says how PostgreSQL stores a row (JSON columns as jsonb) and which tables the isolated
schema has.
"""
from __future__ import annotations

import pytest
from psycopg.types.json import Jsonb

from app.core.config import Settings
from app.repositories.postgres import knowledge_counts_cache
from app.services.repository_factory import create_repository
from tests import notebook_copy_characterization_cases as cases

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_notebook_copy_characterization"),
]


def _insert(db, table: str, values: dict) -> None:
    values = dict(values)
    if table == "users":
        values.update(username=values["id"], password_hash="", password_salt="",
                      password_iterations=0)
    columns = list(values)
    db.execute(
        f"INSERT INTO {table} ({','.join(columns)}) VALUES ({','.join('%s' for _ in columns)})",
        [Jsonb(values[c]) if c in cases.JSON_COLUMNS else values[c] for c in columns],
    )


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
    insert=_insert,
    tables=_tables,
    invalidate_caches=knowledge_counts_cache.invalidate,
)


@pytest.mark.parametrize("scenario", cases.SCENARIOS, ids=lambda s: s.name)
def test_copy_notebook_behaviour_pg(scenario, postgres_scope, tmp_path, monkeypatch):
    storage = tmp_path / "storage"
    monkeypatch.setenv("SILICON_NOTEBOOK_ENV_FILE", "")
    monkeypatch.setenv("DATABASE_URL", postgres_scope.url)
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(storage))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for key, value in scenario.env.items():
        monkeypatch.setenv(key, value)
    repo = create_repository(Settings())
    try:
        observation = cases.observe(repo, BACKEND, scenario, storage, monkeypatch)
    finally:
        repo.close()
    scenario.expect(observation)
