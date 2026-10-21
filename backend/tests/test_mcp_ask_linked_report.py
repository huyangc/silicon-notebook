"""MCP ``ask(report_id=...)`` on SQLite (plan 2026-10-21 T10).

The scenarios live in ``tests/mcp_ask_linked_report_cases.py`` and run here
and, unchanged, on PostgreSQL in ``tests/postgres/test_mcp_ask_linked_report_pg.py``:
the receipt in ``scope.linked_report`` and in a retrieval-only response, every
refusal, the single-notebook rule, the idempotency key tied to the report, an
extension engine and a closed Memory channel answering without the report, and
the output budget.
"""
from __future__ import annotations

import pytest

from tests.mcp_ask_linked_report_cases import CASES, build_env, run_case


@pytest.fixture
def env(tmp_path, monkeypatch):
    from app.api.deps import repository
    from app.core.config import get_settings

    built = build_env(
        f"sqlite:///{tmp_path / 'mcp.db'}", tmp_path, monkeypatch, postgres=False
    )
    try:
        yield built
    finally:
        repository.cache_clear()
        get_settings.cache_clear()


@pytest.mark.anyio
@pytest.mark.parametrize("case", sorted(CASES))
async def test_mcp_ask_linked_report_scenario(env, case):
    await run_case(env, CASES[case])
