"""SQLite count readers and unadopted suggestions (plan T5): card counts, the
admin overview, the deletion-window notice and its audience, reached through
``repo._runtime``. PostgreSQL twin:
``tests/postgres/test_memory_suggest_counts_pg.py``; shared cases:
``tests/memory_suggest_count_cases.py``."""
from __future__ import annotations

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from tests import memory_suggest_count_cases as cases


@pytest.fixture
def env(tmp_path, monkeypatch) -> cases.Env:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'counts.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repository = SQLiteRepository(Settings(_env_file=None))
    try:
        yield cases.Env(repo=repository, postgres=False)
    finally:
        repository.close()


def test_card_counts_exclude_unadopted(env):
    cases.assert_card_counts_exclude_unadopted(env)


def test_catalog_prefilter_still_superset_of_confirmed(env):
    cases.assert_catalog_prefilter_still_superset_of_confirmed(env)


def test_admin_memory_count_subtracts_only_unadopted_candidates(env):
    cases.assert_admin_memory_count_subtracts_only_unadopted_candidates(env)


def test_delete_window_notice_skips_draft_only_readers(env):
    cases.assert_delete_window_notice_skips_draft_only_readers(env)


def test_delete_notice_audience_superset_unchanged(env):
    cases.assert_delete_notice_audience_superset_unchanged(env)


def test_readers_unchanged_without_suggestions(env):
    cases.assert_readers_unchanged_without_suggestions(env)


def test_count_readers_floor_the_difference(env):
    cases.assert_count_readers_floor_the_difference(env)
