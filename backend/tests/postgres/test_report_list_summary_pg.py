"""The report list reads summary columns only, on PostgreSQL.

Runs the scenarios of ``tests/report_list_summary_cases.py`` with ``create_app()``
on the production PostgreSQL repository, so ``section_count`` and
``generation_started_at`` are derived from the jsonb columns in SQL.
"""
from __future__ import annotations

import pytest

from tests.mcp_answer_link_cases import build_env, run_case
from tests.report_list_summary_cases import CASES

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_report_list_summary"),
]


@pytest.fixture
def env(postgres_scope, tmp_path, monkeypatch):
    from app.api.deps import repository
    from app.core.config import get_settings

    built = build_env(postgres_scope.url, tmp_path, monkeypatch)
    try:
        yield built
    finally:
        repository().close()
        repository.cache_clear()
        get_settings.cache_clear()


@pytest.mark.anyio
@pytest.mark.parametrize("case", sorted(CASES))
async def test_report_list_summary_scenario_pg(env, case):
    await run_case(env, CASES[case])
