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

    assert PostgresMigrator(database).migrate() == 86
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


def test_card_basis_ignores_the_session_time_zone(env, database, monkeypatch):
    """The PostgreSQL session renders ``timestamptz`` in its own zone; the card
    ``basis`` must not move with it, or a time-zone change would invalidate every
    card."""
    env.seed()
    env.element("e2", "2026-10-28T12:30:00+00:00")
    baseline = env.store.source_card_basis(cases.SRC)
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
        assert env.store.source_card_basis(cases.SRC) == baseline
        assert datetime.fromisoformat(baseline).utcoffset() == timedelta(0)
    monkeypatch.undo()
    # And a card stored under that basis is still found valid afterwards.
    env.card(basis=baseline)
    assert env.read()[cases.SRC]["basis"] == baseline


def test_failed_write_after_a_committed_ready_leaves_the_ready_card(env, database):
    """Deterministic interleaving: the ``failed`` writer's transaction opens and
    holds its place while a ``ready`` commits; the ``failed`` statement then
    executes. Whichever order PostgreSQL serialises them in, the end state is the
    ready card (the conditional ``WHERE`` runs against the committed row)."""
    env.seed()
    basis = env.basis()
    ready_committed = threading.Event()
    failed_started = threading.Event()
    errors: list[BaseException] = []
    original = database.write

    class Gate:
        """Wrap the connection of the thread that writes ``failed``: signal once its
        transaction has begun, and run its upsert only after the ready commit."""

        def __init__(self, connection):
            self._connection = connection
            self._armed = True

        def __getattr__(self, name):
            return getattr(self._connection, name)

        def execute(self, statement, *args, **kwargs):
            if self._armed and "INSERT INTO source_paper_cards" in str(statement):
                self._armed = False
                failed_started.set()
                assert ready_committed.wait(timeout=10)
            return self._connection.execute(statement, *args, **kwargs)

    role = threading.local()

    @contextmanager
    def write(*args, **kwargs):
        with original(*args, **kwargs) as connection:
            yield Gate(connection) if getattr(role, "failed", False) else connection

    database.write = write

    def failed():
        role.failed = True
        try:
            env.card(basis=basis, status="failed", card={}, raw_json={"reason": "late"})
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    try:
        thread = threading.Thread(target=failed)
        thread.start()
        assert failed_started.wait(timeout=10)  # failed's txn is open, parent rows locked
        env.card(basis=basis, status="ready")  # KEY SHARE locks do not block each other
        ready_committed.set()
        thread.join(timeout=30)
    finally:
        database.write = original
    assert not errors
    row = env.read()[cases.SRC]
    assert row["status"] == "ready" and row["card"] == cases.CARD and row["attempts"] == 0


def test_thread_race_of_failed_and_ready_writes_ends_ready(env):
    """Supplement to the deterministic test above (this one depends on scheduling,
    so it proves little on its own): four ``failed`` writers and one ``ready``
    released together always end at the ready card."""
    env.seed()
    basis = env.basis()
    errors: list[BaseException] = []

    def writer(status):
        def run():
            try:
                barrier.wait(timeout=10)
                if status == "ready":
                    env.card(basis=basis, status="ready")
                else:
                    env.card(basis=basis, status="failed", card={})
            except BaseException as exc:  # pragma: no cover - surfaced below
                errors.append(exc)
        return run

    threads = [threading.Thread(target=writer("failed")) for _ in range(4)]
    threads.append(threading.Thread(target=writer("ready")))
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
