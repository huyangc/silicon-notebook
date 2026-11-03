"""PostgreSQL count readers and unadopted suggestions (plan T5). SQLite twin:
``tests/test_memory_suggest_counts.py``; shared cases:
``tests/memory_suggest_count_cases.py``. Adds the one case only real
concurrent connections can stage: the counts under interleaved adoption,
dismissal and deletion."""
from __future__ import annotations

import pytest

from tests import memory_suggest_count_cases as cases

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_memory_suggest_counts"),
]


@pytest.fixture
def env(postgres_settings, tmp_path) -> cases.Env:
    from app.services.postgres_repository import PostgresRepository

    postgres_settings.storage_dir = str(tmp_path / "postgres-storage")
    postgres_settings.event_log_enabled = False
    postgres_settings.llm_log_enabled = False
    postgres_settings.postgres_pool_max_size = 6
    repository = PostgresRepository(postgres_settings)
    try:
        yield cases.Env(repo=repository, postgres=True)
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


def test_counts_never_negative_under_interleaving(env):
    cases.assert_counts_never_negative_under_interleaving(env)
