"""MCP ``ask(report_id=...)`` through the real MCP entry, on PostgreSQL (plan
2026-10-21 T10).

Runs the scenarios of ``tests/mcp_ask_linked_report_cases.py`` with
``create_app()`` on the production PostgreSQL repository: the report gate, the
verdict under the default ceiling, the restore, the receipt saved with the
answer, and the job row's ``report_id`` -- read back by ``get_ask`` -- all
against PostgreSQL's own stores.
"""
from __future__ import annotations

import pytest

from tests.mcp_ask_linked_report_cases import CASES, build_env, run_case

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_mcp_ask_linked_report"),
]


@pytest.fixture
def env(postgres_scope, tmp_path, monkeypatch):
    from app.api.deps import repository
    from app.core.config import get_settings

    built = build_env(postgres_scope.url, tmp_path, monkeypatch, postgres=True)
    try:
        yield built
    finally:
        repository().close()
        repository.cache_clear()
        get_settings.cache_clear()


@pytest.mark.anyio
@pytest.mark.parametrize("case", sorted(CASES))
async def test_mcp_ask_linked_report_scenario_pg(env, case):
    await run_case(env, CASES[case])
