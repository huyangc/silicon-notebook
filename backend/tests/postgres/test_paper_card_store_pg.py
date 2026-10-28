"""PostgreSQL ``source_paper_cards`` store (0085). SQLite twin:
``tests/test_paper_card_store.py``; shared cases: ``tests/paper_card_store_cases.py``."""
from __future__ import annotations

import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from app.repositories.postgres.source_store import SourceStore
from tests import paper_card_store_cases as cases

pytestmark = pytest.mark.postgres_integration


@pytest.fixture
def database(request):
    database = request.getfixturevalue("postgres_database")
    from app.repositories.postgres.migrator import PostgresMigrator

    assert PostgresMigrator(database).migrate() == 85
    return database


@pytest.fixture
def env(database) -> cases.Env:
    def run(sql: str, params: tuple = ()) -> None:
        with database.write() as db:
            db.execute(sql.replace("?", "%s"), params)

    def rows(sql: str, params: tuple = ()) -> list[dict]:
        with database.connect() as db:
            return [dict(row) for row in db.execute(sql.replace("?", "%s"), params).fetchall()]

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


def test_generation_utc_ignores_the_session_time_zone(env, database, monkeypatch):
    """The PostgreSQL session renders ``timestamptz`` in its own zone; the card
    ``basis`` must not move with it, or a time-zone change would invalidate every
    card."""
    env.seed()
    env.element("e1", "2026-10-28T11:00:00+00:00")
    env.element("e2", "2026-10-28T12:30:00+00:00")
    baseline = env.store.source_element_generation_utc(cases.SRC)
    assert datetime.fromisoformat(baseline) == datetime(2026, 10, 28, 12, 30, tzinfo=timezone.utc)

    original = database.connect

    def in_zone(zone: str):
        @contextmanager
        def connect(*args, **kwargs):
            with original(*args, **kwargs) as connection:
                connection.execute(f"SET TIME ZONE '{zone}'")
                yield connection

        return connect

    for zone in ("Asia/Tokyo", "America/Los_Angeles", "UTC"):
        monkeypatch.setattr(database, "connect", in_zone(zone))
        # The raw aggregate does move with the session zone (this is the trap) ...
        with database.connect() as db:
            raw = db.execute(
                "SELECT MAX(created_at) AS g FROM source_elements WHERE source_id=%s",
                (cases.SRC,),
            ).fetchone()["g"]
        assert raw == datetime(2026, 10, 28, 12, 30, tzinfo=timezone.utc)
        assert raw.utcoffset() != timedelta(0) or zone == "UTC"
        # ... the UTC basis does not.
        assert env.store.source_element_generation_utc(cases.SRC) == baseline
        assert datetime.fromisoformat(baseline).utcoffset() == timedelta(0)
    monkeypatch.undo()
    # And a card stored under that basis is still found valid afterwards.
    env.card(basis=baseline)
    assert env.read()[cases.SRC]["basis"] == baseline


def test_concurrent_failed_writes_never_clobber_a_ready_card(env):
    """A ``failed`` racing a ``ready`` of the same basis and version: whatever the
    interleaving, the end state is the ready card."""
    env.seed()
    errors: list[BaseException] = []

    def failed():
        try:
            barrier.wait(timeout=10)
            env.card(status="failed", card={}, raw_json={"reason": "late"})
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    def ready():
        try:
            barrier.wait(timeout=10)
            env.card(status="ready")
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=failed) for _ in range(4)] + [threading.Thread(target=ready)]
    barrier = threading.Barrier(len(threads))
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not errors
    row = env.read()[cases.SRC]
    assert row["status"] == "ready" and row["card"] == cases.CARD


def test_table_shape_and_foreign_key_names(env):
    columns = {
        r["column_name"]: (r["data_type"], r["is_nullable"])
        for r in env.rows(
            "SELECT column_name,data_type,is_nullable FROM information_schema.columns "
            "WHERE table_schema=current_schema() AND table_name='source_paper_cards'"
        )
    }
    assert columns == {
        "source_id": ("text", "NO"), "notebook_id": ("text", "NO"), "basis": ("text", "NO"),
        "card_version": ("integer", "NO"), "status": ("text", "NO"), "card": ("jsonb", "NO"),
        "raw_json": ("jsonb", "NO"), "model": ("text", "NO"), "attempts": ("integer", "NO"),
        "created_at": ("timestamp with time zone", "NO"),
        "updated_at": ("timestamp with time zone", "NO"),
    }
    constraints = {
        r["conname"]: r["definition"]
        for r in env.rows(
            "SELECT conname, pg_get_constraintdef(oid) AS definition FROM pg_constraint "
            "WHERE conrelid='source_paper_cards'::regclass"
        )
    }
    assert set(constraints) == {
        "pk_source_paper_cards",
        "fk_source_paper_cards_notebook_id__notebooks",
        "fk_source_paper_cards_source_id__sources",
    }
    assert all(
        "ON DELETE CASCADE" in definition
        for name, definition in constraints.items() if name.startswith("fk_")
    )
    assert [r["indexname"] for r in env.rows(
        "SELECT indexname FROM pg_indexes WHERE tablename='source_paper_cards' "
        "AND indexname LIKE 'idx_%%'"
    )] == ["idx_source_paper_cards_nb"]
