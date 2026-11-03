"""SQLite ``MemorySuggestStore`` (v106), reached through the runtime seat
``repo._runtime.memory_suggest_store``. PostgreSQL twin:
``tests/postgres/test_memory_suggest_store_pg.py``; shared cases:
``tests/memory_suggest_store_cases.py``."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.request_context import set_request_user
from app.domain.memory_suggest_contracts import NEIGHBOR_VECTOR_SCAN_MAX
from app.services.sqlite_repository import SQLiteRepository
from tests import memory_suggest_store_cases as cases


@pytest.fixture(autouse=True)
def _clear_request_user():
    yield
    set_request_user(None)


@pytest.fixture
def env(tmp_path: Path) -> cases.Env:
    repo = SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'test.db'}",
            storage_dir=str(tmp_path / "storage"),
            _env_file=None,
            event_log_enabled=False,
            llm_log_enabled=False,
        )
    )
    database = repo._runtime.database

    def run(sql: str, params: tuple = ()) -> None:
        with database.write(operation="test.seed") as db:
            db.execute(sql, params)

    def rows(sql: str, params: tuple = ()) -> list[dict]:
        with database.connect() as db:
            return [dict(row) for row in db.execute(sql, params).fetchall()]

    return cases.Env(
        store=repo._runtime.memory_suggest_store,
        run=run,
        rows=rows,
        json=lambda value: json.dumps(value, ensure_ascii=False),
    )


def test_set_memory_mode_single_transaction_clears_jobs(env):
    cases.assert_set_memory_mode_single_transaction_clears_jobs(env)


def test_quota_resets_on_utc_day_change(env):
    cases.assert_quota_resets_on_utc_day_change(env)


def test_quota_zero_rows_at_cap(env):
    cases.assert_quota_zero_rows_at_cap(env)


def test_quota_reservation_never_exceeds_cap_across_notebooks(env):
    """SQLite serialises writers; the cap holds across any interleaving of
    reservations made on behalf of different notebooks."""
    env.seed()
    cases.enable(env)
    granted = [
        env.store.reserve_daily_quota(
            cases.OWNER, today_utc="2026-10-01", cap=3, now=cases.at(index))
        for index in range(8)
    ]
    assert granted.count(True) == 3


def test_note_signal_single_statement_takes_trigger_ask_created_at(env):
    cases.assert_note_signal_takes_trigger_ask_created_at(env)


def test_note_signal_respects_current_opt_in(env):
    cases.assert_note_signal_respects_current_opt_in(env)


def test_note_signal_foreign_ask_id_falls_back_to_now(env):
    cases.assert_note_signal_foreign_ask_id_falls_back_to_now(env)


def test_note_signal_increments_only_on_conflict(env):
    cases.assert_note_signal_increments_only_on_conflict(env)


def test_note_signal_counts_only_while_the_switch_is_on(env):
    cases.assert_note_signal_counts_only_while_the_switch_is_on(env)


def test_note_signal_without_read_access_writes_nothing(env):
    cases.assert_note_signal_without_read_access_writes_nothing(env)


def test_claim_settle_cas_rejects_stale_token(env):
    cases.assert_claim_settle_cas_rejects_stale_token(env)


def test_sweep_resets_running(env):
    cases.assert_sweep_resets_running(env)


def test_clear_job_rows(env):
    cases.assert_clear_job_rows(env)


def test_clarified_scan_ascending_after_cursor_inclusive_start(env):
    cases.assert_clarified_scan_ascending_after_cursor_inclusive_start(env)


def test_cursor_ties_on_the_instant_break_only_on_id(env):
    """Two asks at the same instant spelled with different UTC offsets:
    The sampling cursor uses UTC microseconds plus id; stored text spelling
    never breaks the tie, and is preserved for the returned row."""
    env.seed()
    for job_id, created_at in (
        ("job-z", "2026-10-01T08:01:00+00:00"),
        ("job-a", "2026-10-01T16:01:00+08:00"),
    ):
        env.run(
            "INSERT INTO ask_jobs(id,notebook_id,conversation_id,created_by,mode,question,"
            "status,answer_id,created_at,updated_at,submitted_via,output,report_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (job_id, cases.NB, cases.CONV, cases.OWNER, "reasoning", "q", "done", "",
             created_at, created_at, "web", "answer", ""),
        )
    ascending = env.store.clarified_scan(
        cases.OWNER, cases.NB, cursor_created_at=cases.at(0), cursor_id="",
        inclusive=True, limit=10)
    assert [row["id"] for row in ascending] == ["job-a", "job-z"]
    after_first = env.store.clarified_scan(
        cases.OWNER, cases.NB, cursor_created_at="2026-10-01T08:01:00+00:00",
        cursor_id="job-a", inclusive=False, limit=10)
    assert [row["id"] for row in after_first] == ["job-z"]
    window = env.store.repeated_window(cases.OWNER, cases.NB, lower=cases.at(0), limit=10)
    assert [row["id"] for row in window] == ["job-z", "job-a"]
    assert ascending[0]["created_at"] == "2026-10-01T16:01:00+08:00"


def test_repeated_window_descending_latest_40_within_lower_bound(env):
    cases.assert_repeated_window_descending_latest_within_lower_bound(env)


def test_sampling_predicate_excludes_mcp_evidence_report_unfinished_foreign(env):
    cases.assert_sampling_predicate_excludes_mcp_evidence_report_unfinished_foreign(env)


def test_answer_projection_owner_only(env):
    cases.assert_answer_projection_owner_only(env)


def test_latest_feedback_owner_and_latest(env):
    cases.assert_latest_feedback_owner_and_latest(env)


def test_answers_with_memory_any_status_owner_only(env):
    cases.assert_answers_with_memory_any_status_owner_only(env)


def test_unadopted_candidate_count(env):
    cases.assert_unadopted_candidate_count(env)


def test_fingerprint_declined_upgrades_to_proposed(env):
    cases.assert_fingerprint_declined_upgrades_to_proposed(env)


def test_fingerprint_proposed_never_downgrades(env):
    cases.assert_fingerprint_proposed_never_downgrades(env)


def test_fingerprint_declined_extends_to_later_expiry_only(env):
    cases.assert_fingerprint_declined_extends_to_later_expiry_only(env)


def test_fingerprint_presence_rules(env):
    cases.assert_fingerprint_presence_rules(env)


def test_neighbors_vector_side_latest_500_by_updated_at(env):
    cases.assert_neighbors_vector_side_latest_scan_by_updated_at(
        env, NEIGHBOR_VECTOR_SCAN_MAX + 1)


def test_neighbors_lexical_includes_rejected_and_deprecated(env):
    cases.assert_neighbors_lexical_includes_rejected_and_deprecated(env)


def test_neighbors_rejects_unknown_status(env):
    cases.assert_neighbors_rejects_unknown_status(env)


def test_expire_overdue_only_suggested_candidates(env):
    cases.assert_expire_overdue_only_suggested_candidates(env)


def test_expire_overdue_scoped_to_owner_notebook(env):
    cases.assert_expire_overdue_scoped_to_owner_notebook(env)


def test_answer_projection_sources_owner_only(env):
    cases.assert_answer_projection_sources_owner_only(env)


def test_earliest_unfinished_ask_owner_and_predicate(env):
    cases.assert_earliest_unfinished_ask_owner_and_predicate(env)


def test_sampling_orders_offsets_and_microseconds_like_cursor(env):
    cases.assert_sampling_orders_offsets_and_microseconds_like_cursor(env)


def test_invalid_sampling_timestamp_remains_unordered_instead_of_failing_batch(env):
    env.seed()
    env.ask("valid", 1)
    env.ask("invalid", 2)
    env.run("UPDATE ask_jobs SET created_at='not-an-instant' WHERE id='invalid'")
    rows = env.store.clarified_scan(cases.OWNER, cases.NB, cursor_created_at=cases.at(0),
                                    cursor_id="", inclusive=True, limit=10)
    assert [row["id"] for row in rows] == ["valid"]
    assert env.rows("SELECT created_at FROM ask_jobs WHERE id='invalid'") == [{"created_at": "not-an-instant"}]


def test_sampling_vm_work_scales_linearly_with_matching_rows(env):
    from datetime import timedelta, timezone

    env.seed()
    database = env.store.database

    def insert(first, end):
        with database.write() as db:
            db.executemany(
                "INSERT INTO ask_jobs(id,notebook_id,conversation_id,created_by,mode,question,"
                "status,answer_id,created_at,updated_at,submitted_via,output,report_id) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [(f"scale-{index:05d}", cases.NB, cases.CONV, cases.OWNER, "chunk", "q",
                  "running" if index % 2 else "done", "",
                  (cases.T0 + timedelta(microseconds=index + 1)).astimezone(timezone(timedelta(hours=index % 2))).isoformat(),
                  cases.at(0), "web", "answer", "") for index in range(first, end)],
            )

    def measure():
        steps = 0

        def tick():
            nonlocal steps
            steps += 1
            return 0

        with database.connect() as db:
            db.set_progress_handler(tick, 1)
            try:
                args = {"cursor_created_at": cases.at(0), "cursor_id": "", "inclusive": True}
                ascending = env.store.clarified_scan(cases.OWNER, cases.NB, **args, limit=40)
                descending = env.store.repeated_window(cases.OWNER, cases.NB, lower=cases.at(0), limit=40)
                pending = env.store.earliest_unfinished_ask(cases.OWNER, cases.NB, **args)
            finally:
                db.set_progress_handler(None, 0)
        assert ascending[0]["id"] == "scale-00000" and len(ascending) == len(descending) == 40
        assert pending["id"] == "scale-00001"
        return steps

    insert(0, 512)
    small = measure()
    insert(512, 2048)
    large = measure()
    # Four times the rows cannot approach quadratic (sixteen times) VM work.
    assert large < small * 6, (small, large)
    print(f"memory sampling VM steps: rows512={small}; rows2048={large}")
