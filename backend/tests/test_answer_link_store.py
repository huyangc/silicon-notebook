"""SQLite ``answer_links`` store (v94). PostgreSQL twin:
``tests/postgres/test_answer_link_store_pg.py``."""
from __future__ import annotations

from pathlib import Path

import pytest

from app.core.capability_tokens import is_answer_link_token
from app.core.config import Settings
from app.repositories.sqlite.answer_link_store import AnswerLinkStore
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SqliteMigrator
from tests import answer_link_store_cases as cases


@pytest.fixture
def database(tmp_path: Path) -> SqliteDatabase:
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}")
    db = SqliteDatabase(settings, tmp_path)
    assert SqliteMigrator(db, settings).migrate()
    return db


@pytest.fixture
def env(database):
    clock, mono = cases.Clock(), cases.Monotonic()
    store = AnswerLinkStore(database, now=clock, monotonic=mono)

    def run(sql: str, params: tuple = ()) -> None:
        with database.write(operation="test.seed") as db:
            db.execute(sql, params)

    def count_rows() -> int:
        with database.connect() as db:
            return int(db.execute("SELECT COUNT(*) FROM answer_links").fetchone()[0])

    return store, clock, mono, cases.Seed(run), count_rows


def test_token_prefix_is_its_own_namespace():
    assert is_answer_link_token("alnk-abc")
    assert not is_answer_link_token("alnkabc")
    assert not is_answer_link_token("cshr-abc") and not is_answer_link_token("gshr-abc")


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
