"""``AskService._run_reasoning_stage`` / ``_draft_reasoning_response`` characterization
on real PostgreSQL.

The twin of ``tests/test_reasoning_stage_characterization.py``: the same world,
scenarios and behaviour assertions from ``tests/reasoning_stage_characterization_cases.py``;
this module only says which tables the isolated schema has.
"""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.repositories.postgres import knowledge_counts_cache
from app.services.repository_factory import create_repository
from tests import reasoning_stage_characterization_cases as cases
from tests.postgres.reasoning_snapshot import read_table_rows

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_reasoning_stage_characterization"),
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
    read_table_rows=read_table_rows,
)


@pytest.mark.parametrize("scenario", cases.SCENARIOS, ids=lambda s: s.name)
def test_run_reasoning_stage_characterization_pg(scenario, postgres_scope, tmp_path,
                                                 monkeypatch):
    cases.scrub_environment(monkeypatch, tmp_path, postgres_scope.url)
    for key, value in scenario.env.items():
        monkeypatch.setenv(key, value)
    repo = create_repository(
        Settings(_env_file=None),
        gap_consult_host=cases.make_gap_host() if scenario.gap_plugin else None,
    )
    try:
        observation = cases.observe(repo, BACKEND, scenario, tmp_path, monkeypatch)
    finally:
        repo.close()
    scenario.expect(observation)
