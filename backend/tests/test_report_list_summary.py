"""The report list reads summary columns only (SQLite).

The scenarios live in ``tests/report_list_summary_cases.py`` and also run on
PostgreSQL in ``tests/postgres/test_report_list_summary_pg.py``.
"""
from __future__ import annotations

import pytest

from tests.mcp_answer_link_cases import build_env, run_case
from tests.report_list_summary_cases import CASES


@pytest.fixture
def env(tmp_path, monkeypatch):
    from app.api.deps import repository

    built = build_env(f"sqlite:///{tmp_path / 'reports.db'}", tmp_path, monkeypatch)
    try:
        yield built
    finally:
        repository.cache_clear()


@pytest.mark.anyio
@pytest.mark.parametrize("case", sorted(CASES))
async def test_report_list_summary_scenario(env, case):
    await run_case(env, CASES[case])


@pytest.mark.anyio
async def test_sqlite_empty_json_text_reads_as_its_default(env):
    """SQLite lets a JSON column hold the empty string (``row_to_dict`` reads it
    as ``[]`` / ``{}``); the list's SQL derivation must agree."""
    from tests.report_list_summary_cases import _insert, _oracle, _world

    async def case(env):
        _headers, owner_id, _other, notebook_id = _world(env)
        _insert(
            env, id="rep-ls-blank", notebook_id=notebook_id, question="空串",
            created_by=owner_id, created_at="2026-10-21T08:00:09+00:00",
            updated_at="2026-10-21T08:00:09+00:00", outline_json="",
            understanding_json="",
        )
        store = env.repo._runtime.report_store
        listed = store.list_reports(notebook_id, created_by=owner_id)
        assert listed == _oracle(env, store, notebook_id, owner_id)
        [blank] = [item for item in listed if item["id"] == "rep-ls-blank"]
        assert blank["section_count"] == 0 and blank["generation_started_at"] == ""

    await run_case(env, case)
