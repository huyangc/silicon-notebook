"""PostgreSQL twin of ``tests/test_answer_link_store.py`` (0074)."""
from __future__ import annotations

import pytest

from app.repositories.postgres.answer_link_store import AnswerLinkStore
from tests import answer_link_store_cases as cases

pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def database(request):
    database = request.getfixturevalue("postgres_database")
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(database).migrate() == 85
    return database


@pytest.fixture
def env(database):
    clock, mono = cases.Clock(), cases.Monotonic()
    store = AnswerLinkStore(database, now=clock, monotonic=mono)

    def run(sql: str, params: tuple = ()) -> None:
        with database.write() as db:
            db.execute(sql.replace("?", "%s"), params)

    def count_rows() -> int:
        with database.connect() as db:
            return int(db.execute("SELECT COUNT(*) AS n FROM answer_links").fetchone()["n"])

    return store, clock, mono, cases.Seed(run), count_rows


def test_issue_then_read_back(env):
    store, clock, _, seed, _ = env
    cases.assert_issue_then_read_back(store, clock, seed)


def test_reissue_same_key_reuses_unexpired(env):
    store, clock, _, seed, _ = env
    cases.assert_reissue_same_key_reuses_unexpired(store, clock, seed)


def test_reissue_after_expiry_replaces(env):
    store, clock, _, seed, _ = env
    cases.assert_reissue_after_expiry_replaces(store, clock, seed)


def test_different_agent_token_gets_its_own_link(env):
    store, clock, _, seed, _ = env
    cases.assert_different_agent_token_gets_its_own_link(store, clock, seed)


def test_expired_flag_at_exact_boundary(env):
    store, clock, _, seed, _ = env
    cases.assert_expired_flag_at_exact_boundary(store, clock, seed)


def test_sweep_drops_past_grace_keeps_within_grace(env):
    store, clock, _, seed, _ = env
    cases.assert_sweep_drops_past_grace_keeps_within_grace(store, clock, seed)


def test_sweep_drops_revoked_or_missing_token_rows(env):
    store, clock, _, seed, _ = env
    cases.assert_sweep_drops_revoked_or_missing_token_rows(store, clock, seed)


def test_sweep_drops_rows_whose_job_is_gone(env):
    store, clock, _, seed, _ = env
    cases.assert_sweep_drops_rows_whose_job_is_gone(store, clock, seed)


def test_issue_sweeps_at_most_once_per_interval(env):
    store, clock, mono, seed, _ = env
    cases.assert_issue_sweeps_at_most_once_per_interval(store, clock, mono, seed)


def test_concurrent_same_key_issues_one_link(env):
    store, clock, _, seed, count_rows = env
    cases.assert_concurrent_same_key_issues_one_link(store, clock, seed, count_rows)


def test_failed_issue_does_not_burn_the_sweep_interval(env):
    store, clock, mono, seed, _ = env
    cases.assert_failed_issue_does_not_burn_the_sweep_interval(store, clock, mono, seed)


def test_vanished_winner_is_retried_then_reported(env):
    store, _, _, seed, _ = env
    cases.assert_vanished_winner_is_retried_then_reported(store, seed)
