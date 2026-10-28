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


def test_a_stale_basis_is_refused_and_keeps_the_newer_card(env):
    cases.assert_a_stale_basis_is_refused_and_keeps_the_newer_card(env)


def test_failed_for_another_card_version_overwrites(env):
    cases.assert_failed_for_another_card_version_overwrites(env)


def test_attempts_count_in_sql_and_reset_on_ready(env):
    cases.assert_attempts_count_in_sql_and_reset_on_ready(env)


def test_attempts_restart_when_basis_or_version_changes(env):
    cases.assert_attempts_restart_when_basis_or_version_changes(env)


def test_unknown_status_is_rejected_before_any_sql(env):
    cases.assert_unknown_status_is_rejected_before_any_sql(env)


def test_card_basis_is_the_max_element_stamp(env):
    cases.assert_card_basis_is_the_catalog_witness_on_sqlite_and_max_stamp_in_general(env)


def test_card_bases_batch_matches_the_single_read(env):
    cases.assert_card_bases_batch_matches_the_single_read(env)


def test_sqlite_card_basis_is_the_catalog_witness_verbatim(env, tmp_path):
    """No time-zone interpretation: the basis is the raw ``MAX(created_at)`` text,
    character for character what the catalog's witness token is."""
    from app.repositories.sqlite.catalog_store import CatalogStore

    env.seed()
    env.element("e-naive", "2026-10-28T20:00:00.123456")  # naive text, as the clock writes
    basis = env.store.source_card_basis(cases.SRC)
    assert basis == "2026-10-28T20:00:00.123456"
    with env.connect() as db:
        assert env.store.source_card_bases(db, [cases.SRC]) == {cases.SRC: basis}
    catalog = CatalogStore(env.store.database, new_id=lambda prefix: prefix, now=lambda: cases.NOW)
    assert catalog.source_element_generation(cases.SRC) == basis
    raw = env.rows("SELECT MAX(created_at) AS m FROM source_elements WHERE source_id=?", (cases.SRC,))
    assert raw[0]["m"] == basis


def test_paper_cards_read_plan_stays_on_the_primary_key(env):
    """Like ``test_paper_meta_hydration_plan_stays_on_source_id``: the ``+notebook_id``
    narrowing keeps the page-level IN list driving through the primary key, with
    and without ``sqlite_stat1`` (production has none)."""
    env.seed()
    env.card()
    captured: list[tuple[str, list]] = []
    with env.connect() as db:
        original = db.execute

        def spy(sql, params=()):
            if "source_paper_cards" in str(sql):
                captured.append((str(sql), list(params)))
            return original(sql, params)

        db.execute = spy
        try:
            env.store.paper_cards_for_sources(db, [cases.SRC, "src-x"], notebook_id=cases.NB)
        finally:
            del db.execute
        assert len(captured) == 1
        sql, params = captured[0]
        assert "+notebook_id = ?" in sql and params[-1] == cases.NB

        def plan() -> str:
            return "\n".join(r["detail"] for r in db.execute(f"EXPLAIN QUERY PLAN {sql}", params))

        for with_stats in (False, True):
            if with_stats:
                env.run("ANALYZE", ())
            text = plan()
            assert (
                "SEARCH source_paper_cards USING INDEX sqlite_autoindex_source_paper_cards_1 "
                "(source_id=?)" in text
            ), (with_stats, text)
            assert "idx_source_paper_cards_nb" not in text, (with_stats, text)


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
