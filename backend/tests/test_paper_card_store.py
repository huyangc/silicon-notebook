"""SQLite ``source_paper_cards`` store (v104). PostgreSQL twin:
``tests/postgres/test_paper_card_store_pg.py``; shared cases:
``tests/paper_card_store_cases.py``."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from app.core.config import Settings
from app.repositories.paper_card_rows import utc_iso
from app.repositories.sqlite.database import SqliteDatabase
from app.repositories.sqlite.migrations import SqliteMigrator
from app.repositories.sqlite.source_store import SourceStore
from tests import paper_card_store_cases as cases


@pytest.fixture
def env(tmp_path: Path) -> cases.Env:
    settings = Settings(database_url=f"sqlite:///{tmp_path / 'test.db'}")
    database = SqliteDatabase(settings, tmp_path)
    assert SqliteMigrator(database, settings).migrate()

    def run(sql: str, params: tuple = ()) -> None:
        with database.write(operation="test.seed") as db:
            db.execute(sql, params)

    def rows(sql: str, params: tuple = ()) -> list[dict]:
        with database.connect() as db:
            return [dict(row) for row in db.execute(sql, params).fetchall()]

    return cases.Env(
        store=SourceStore(database, now=lambda: cases.NOW),
        run=run,
        rows=rows,
        connect=database.connect,
    )


def test_ready_roundtrip(env):
    cases.assert_ready_roundtrip(env)


def test_read_is_narrowed_and_batched(env):
    cases.assert_read_is_narrowed_and_batched(env)


def test_upsert_checks_ownership(env):
    cases.assert_upsert_checks_ownership(env)


def test_source_delete_cascades(env):
    cases.assert_source_delete_cascades(env)


def test_notebook_delete_cascades(env):
    cases.assert_notebook_delete_cascades(env)


def test_clear_paper_meta_deletes_the_card(env):
    cases.assert_clear_paper_meta_deletes_the_card(env)


def test_failed_never_overwrites_a_ready_card_of_the_same_basis(env):
    cases.assert_failed_never_overwrites_a_ready_card_of_the_same_basis(env)


def test_failed_for_another_card_version_overwrites(env):
    cases.assert_failed_for_another_card_version_overwrites(env)


def test_attempts_count_in_sql_and_reset_on_ready(env):
    cases.assert_attempts_count_in_sql_and_reset_on_ready(env)


def test_generation_utc_is_max_element_stamp_in_utc(env):
    cases.assert_generation_utc_is_max_element_stamp_in_utc(env)


def test_utc_iso_renders_aware_and_naive_stamps_as_utc():
    assert utc_iso(None) == "" and utc_iso("") == ""
    aware = utc_iso("2026-10-28T20:00:00+09:00")
    assert datetime.fromisoformat(aware) == datetime(2026, 10, 28, 11, tzinfo=timezone.utc)
    assert datetime.fromisoformat(aware).utcoffset() == timedelta(0)
    # A naive stamp (what the SQLite clock writes) is read as system-local time.
    naive = datetime(2026, 10, 28, 20, 0, 0)
    assert datetime.fromisoformat(utc_iso(naive.isoformat())) == naive.astimezone(timezone.utc)
    assert utc_iso(datetime(2026, 10, 28, 11, tzinfo=timezone.utc)) == utc_iso("2026-10-28T11:00:00+00:00")


def test_migration_104_creates_the_table_and_index(env):
    tables = {r["name"] for r in env.rows("SELECT name FROM sqlite_master WHERE type='table'")}
    indexes = {r["name"] for r in env.rows("SELECT name FROM sqlite_master WHERE type='index'")}
    assert "source_paper_cards" in tables
    assert "idx_source_paper_cards_nb" in indexes
    assert [r["name"] for r in env.rows("PRAGMA table_info(source_paper_cards)")] == [
        "source_id", "notebook_id", "basis", "card_version", "status", "card",
        "raw_json", "model", "attempts", "created_at", "updated_at",
    ]
