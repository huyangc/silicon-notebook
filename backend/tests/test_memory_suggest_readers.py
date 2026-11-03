"""SQLite Memory readers and unadopted suggestions (plan T4a), reached through
``repo._runtime.memory_store``. PostgreSQL twin:
``tests/postgres/test_memory_suggest_readers_pg.py``; shared cases:
``tests/memory_suggest_reader_cases.py``."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.request_context import set_request_user
from app.services.sqlite_repository import SQLiteRepository
from tests import memory_suggest_reader_cases as cases


@pytest.fixture(autouse=True)
def _clear_request_user():
    yield
    set_request_user(None)


@pytest.fixture
def env(tmp_path: Path) -> cases.Env:
    repo = SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'test.db'}",
            storage_dir=str(tmp_path / "storage"),
            _env_file=None,
            event_log_enabled=False,
            llm_log_enabled=False,
        )
    )
    database = repo._runtime.database

    def run(sql: str, params: tuple = ()) -> None:
        with database.write(operation="test.seed") as db:
            db.execute(sql, params)

    def rows(sql: str, params: tuple = ()) -> list[dict]:
        with database.connect() as db:
            return [dict(row) for row in db.execute(sql, params).fetchall()]

    return cases.Env(store=repo._runtime.memory_store, run=run, rows=rows)


def test_overview_excludes_unadopted(env):
    cases.assert_overview_excludes_unadopted(env)


def test_adopted_suggestion_counts_as_yours(env):
    cases.assert_adopted_suggestion_counts_as_yours(env)


def test_export_omits_unadopted_and_reports_count(env):
    cases.assert_export_omits_unadopted_and_reports_count(env)


def test_group_exit_preview_counts_only_your_memories(env):
    cases.assert_group_exit_preview_counts_only_your_memories(env)


def test_answer_links_exclude_drafts_states_include_them(env):
    cases.assert_answer_links_exclude_drafts_states_include_them(env)


def test_origin_filter_suggested_totals_match_with_and_without_query(env):
    cases.assert_origin_filter_suggested_totals_match_with_and_without_query(env)


def test_readers_unchanged_without_suggestions(env):
    cases.assert_readers_unchanged_without_suggestions(env, cases.OLD_OVERVIEW_SQL)
