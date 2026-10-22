"""Navigation-page marker on PostgreSQL (0082), the canonical definition:
build write, compare-and-set store methods and the background pass. SQLite
twin ``tests/test_navigation_backfill.py``; shared cases in
``tests/navigation_backfill_cases.py``."""
from __future__ import annotations

import pytest

from tests import navigation_backfill_cases as cases

pytestmark = pytest.mark.postgres_integration

Q = "%s"


@pytest.fixture
def repo(postgres_settings):
    from app.services.postgres_repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


def test_migration_adds_the_three_columns(repo):
    with repo._runtime.database.connect() as db:
        rows = {
            (row["table_name"], row["column_name"]): (
                row["data_type"], row["is_nullable"], row["column_default"]
            )
            for row in db.execute(
                "SELECT table_name, column_name, data_type, is_nullable, column_default "
                "FROM information_schema.columns WHERE table_schema=current_schema() "
                "AND (table_name, column_name) IN "
                "(('chunks','navigation'), ('sources','navigation_scanned_at'), "
                "('sources','navigation_version'))"
            ).fetchall()
        }
    assert rows == {
        ("chunks", "navigation"): ("integer", "NO", "0"),
        ("sources", "navigation_scanned_at"): ("timestamp with time zone", "YES", None),
        ("sources", "navigation_version"): ("integer", "NO", "0"),
    }


def test_build_writes_flags_and_stamp(repo):
    cases.case_build_writes_flags_and_stamp(repo, Q)


def test_build_with_detection_off_writes_zero_and_no_stamp(repo, monkeypatch):
    cases.case_build_with_detection_off_writes_zero_and_no_stamp(repo, Q, monkeypatch)


def test_replace_compare_and_set(repo):
    cases.case_replace_compare_and_set(repo, Q)


def test_build_redoes_under_the_lock_when_the_generation_moved(repo, monkeypatch):
    cases.case_build_redoes_under_the_lock_when_the_generation_moved(repo, Q, monkeypatch)


def test_backfill_judges_pending_sources_and_resumes(repo, monkeypatch):
    cases.case_backfill_judges_pending_sources_and_resumes(repo, Q, monkeypatch)


def test_backfill_abandons_a_source_whose_generation_moved(repo, monkeypatch):
    cases.case_backfill_abandons_a_source_whose_generation_moved(repo, Q, monkeypatch)


def test_backfill_retries_busy_sources_once(repo, monkeypatch):
    cases.case_backfill_retries_busy_sources_once(repo, Q, monkeypatch)


def test_backfill_runs_one_page_per_job(repo, monkeypatch):
    cases.case_backfill_runs_one_page_per_job(repo, Q, monkeypatch)


def test_backfill_reads_headings_only(repo, monkeypatch):
    cases.case_backfill_reads_headings_only(repo, Q, monkeypatch)


def test_backfill_switch_off_and_offline_batch(repo, monkeypatch):
    cases.case_backfill_switch_off_and_offline_batch(repo, Q, monkeypatch)


def test_clear_resets_flags_and_stamps(repo):
    cases.case_clear_resets_flags_and_stamps(repo, Q)


def test_mirrored_notebooks_are_left_alone(repo, monkeypatch):
    cases.case_mirrored_notebooks_are_left_alone(repo, Q, monkeypatch)


def test_backfill_write_yields_to_a_row_lock_before_deadlock_detection(repo, monkeypatch):
    """The backfill takes chunk rows, then the source row -- the opposite of a
    source deletion. Its transaction runs under a ``lock_timeout`` below
    ``deadlock_timeout``, so while another transaction holds the source row
    the backfill gives up first (False: abandoned, still pending) instead of
    waiting or making PostgreSQL pick the deletion as a deadlock victim."""
    import threading
    import time

    from app.repositories.postgres.chunk_store import NAVIGATION_BACKFILL_LOCK_TIMEOUT_MS

    _notebook_id, source_id = cases._pending_source(repo, Q, monkeypatch)
    with repo._runtime.database.connect() as db:
        deadlock_ms = db.execute(
            "SELECT setting::int AS ms FROM pg_settings WHERE name='deadlock_timeout'"
        ).fetchone()["ms"]
    assert NAVIGATION_BACKFILL_LOCK_TIMEOUT_MS < deadlock_ms
    store = repo._runtime.chunk_store
    token, chunks = store.navigation_scan_rows(source_id)
    holding, release = threading.Event(), threading.Event()

    def hold_source_row():
        with repo._runtime.database.write() as db:
            db.execute("SELECT 1 FROM sources WHERE id=%s FOR UPDATE", (source_id,))
            holding.set()
            release.wait(10)

    holder = threading.Thread(target=hold_source_row)
    holder.start()
    try:
        assert holding.wait(10)
        started = time.monotonic()
        assert store.apply_navigation_flags(
            source_id, expected_chunked_at=token,
            changes=[(chunks[0]["id"], True)],
        ) is False
        assert time.monotonic() - started < deadlock_ms / 1000
    finally:
        release.set()
        holder.join(10)
    assert cases.stamps(repo, source_id, Q)[1] is None
    assert set(cases.chunk_flags(repo, source_id, Q).values()) == {0}
    # uncontended, the same write commits
    assert store.apply_navigation_flags(
        source_id, expected_chunked_at=token, changes=[(chunks[0]["id"], True)],
    ) is True


def test_clear_runs_under_the_maintenance_statement_timeout_floor(repo, monkeypatch):
    from app.repositories.postgres import chunk_store as module

    seen = []
    real_floor = module.raise_statement_timeout_floor

    def spy(connection):
        real_floor(connection)
        seen.append(connection.execute(
            "SELECT current_setting('statement_timeout') AS t"
        ).fetchone()["t"])

    monkeypatch.setattr(module, "raise_statement_timeout_floor", spy)
    repo._runtime.chunk_store.clear_navigation_flags(notebook_id=None)
    assert seen == ["10min"]
