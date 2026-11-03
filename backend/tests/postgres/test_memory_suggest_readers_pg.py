"""PostgreSQL Memory readers and unadopted suggestions (plan T4a). SQLite
twin: ``tests/test_memory_suggest_readers.py``; shared cases:
``tests/memory_suggest_reader_cases.py``."""
from __future__ import annotations

import pytest

from app.repositories.postgres.memory_store import MemoryStore
from tests import memory_suggest_reader_cases as cases

pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def env(request) -> cases.Env:
    database = request.getfixturevalue("postgres_database")
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(database).migrate() == 88
    counter = iter(range(1, 1_000_000))

    def run(sql: str, params: tuple = ()) -> None:
        with database.write() as db:
            db.execute(sql.replace("?", "%s"), params)

    def rows(sql: str, params: tuple = ()) -> list[dict]:
        with database.connect() as db:
            return [dict(row) for row in db.execute(sql.replace("?", "%s"), params).fetchall()]

    store = MemoryStore(
        database, new_id=lambda prefix: f"{prefix}-{next(counter)}", now=lambda: cases.NOW
    )
    return cases.Env(store=store, run=run, rows=rows)


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
