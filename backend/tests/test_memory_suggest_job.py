"""SQLite: the auto user-memory suggestion run (plan T7b) over a real
repository runtime. PostgreSQL twin:
``tests/postgres/test_memory_suggest_job_pg.py``; shared cases:
``tests/memory_suggest_job_cases.py``. Pure ``settle_plan`` cases live here
only (no database)."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.core.config import Settings
from app.core.request_context import set_request_user
from app.domain.memory_suggest_contracts import (
    OUTCOME_BACKLOG,
    OUTCOME_DAILY_CAP,
    OUTCOME_DISABLED,
    OUTCOME_DONE,
    OUTCOME_FAILED,
    OUTCOME_MODEL_REJECTED,
    OUTCOME_MODEL_UNAVAILABLE,
    OUTCOME_NO_SIGNAL,
    OUTCOME_SKIPPED,
)
from app.services import background_jobs
from app.services.memory_suggest_job import settle_plan
from app.services.memory_suggest_signals import SignalCursor
from app.services.sqlite_repository import SQLiteRepository
from tests import memory_suggest_job_cases as cases


@pytest.fixture(autouse=True)
def _clear_request_user():
    yield
    set_request_user(None)


@pytest.fixture
def env(tmp_path: Path):
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

    try:
        yield cases.Env(
            runtime=repo._runtime,
            run=run,
            rows=rows,
            json=lambda value: json.dumps(value, ensure_ascii=False),
        )
    finally:
        repo.close()


# ------------------------------------------------------------------ pure
END = SignalCursor("2026-10-01T08:05:00+00:00", "ask-5", inclusive=False)


@pytest.mark.parametrize("outcome", [OUTCOME_DONE, OUTCOME_NO_SIGNAL])
def test_settle_plan_progress_moves_and_resets(outcome):
    assert settle_plan(outcome, 2, END) == ((END.created_at, END.id, False), 0, False)
    assert settle_plan(outcome, 0, None) == (None, 0, False)


@pytest.mark.parametrize(
    "outcome", [OUTCOME_SKIPPED, OUTCOME_BACKLOG, OUTCOME_DISABLED, OUTCOME_DAILY_CAP]
)
def test_settle_plan_gates_move_nothing(outcome):
    assert settle_plan(outcome, 2, END) == (None, 2, False)


@pytest.mark.parametrize(
    "outcome", [OUTCOME_MODEL_REJECTED, OUTCOME_MODEL_UNAVAILABLE, OUTCOME_FAILED]
)
def test_settle_plan_failures_count_then_give_up(outcome):
    assert settle_plan(outcome, 0, END) == (None, 1, False)
    assert settle_plan(outcome, 1, END) == (None, 2, False)
    assert settle_plan(outcome, 2, END) == ((END.created_at, END.id, False), 0, True)


def test_the_run_goes_to_the_light_pool():
    assert background_jobs._maintenance_pool("memorysuggest-nb-1") == (
        "light", "memorysuggest",
    )


# ------------------------------------------------------------------ cursor table
def test_success_cursor_to_last_fully_processed(env):
    cases.assert_success_cursor_to_last_fully_processed(env)


def test_no_signal_advances_to_end_of_ascending_scan_no_quota(env):
    cases.assert_no_signal_advances_to_end_of_ascending_scan_no_quota(env)


def test_skipped_backlog_disabled_dailycap_cursor_unchanged(env):
    cases.assert_skipped_backlog_disabled_dailycap_cursor_unchanged(env)


def test_model_rejected_retries_same_batch_no_negative_cache(env):
    cases.assert_model_rejected_retries_same_batch_no_negative_cache(env)


def test_three_failures_give_up_event_and_cursor_passes(env):
    cases.assert_three_failures_give_up_event_and_cursor_passes(env)


def test_interrupted_run_counts_as_one_failure(env):
    cases.assert_interrupted_run_counts_as_one_failure(env)


def test_single_flight_concurrent_start(env):
    cases.assert_single_flight_concurrent_start(env)


def test_submission_failure_settles_the_claim(env):
    cases.assert_submission_failure_settles_the_claim(env)


# ------------------------------------------------------------------ zero-call paths
def test_backlog_gate_no_model_call(env):
    cases.assert_backlog_gate_no_model_call(env)


def test_delete_window_skips(env):
    cases.assert_delete_window_skips(env)


def test_lost_read_skips(env):
    cases.assert_lost_read_skips(env)


def test_switch_off_between_claim_and_run_skips(env):
    cases.assert_switch_off_between_claim_and_run_skips(env)


# ------------------------------------------------------------------ revocation mid-call
def test_disable_during_model_call_writes_nothing(env):
    cases.assert_disable_during_model_call_writes_nothing(env)


def test_member_removed_during_call_writes_nothing(env):
    cases.assert_member_removed_during_call_writes_nothing(env)


# ------------------------------------------------------------------ caps and caches
def test_at_most_three_candidates_per_run(env):
    cases.assert_at_most_three_candidates_per_run(env)


def test_kept_over_cap_proposed_next_run_without_duplicates(env):
    cases.assert_kept_over_cap_proposed_next_run_without_duplicates(env)


def test_cursor_held_at_an_earlier_unfinished_ask(env):
    cases.assert_cursor_held_at_an_earlier_unfinished_ask(env)


def test_running_ask_before_the_window_holds_nothing(env):
    cases.assert_running_ask_before_the_window_holds_nothing(env)


@pytest.mark.parametrize("later_clarified", [False, True])
def test_finished_between_sampling_reads_keeps_early_fence(env, later_clarified):
    cases.assert_finished_between_sampling_reads_keeps_early_fence(env, later_clarified=later_clarified)


def test_old_running_ask_cannot_hide_in_window_running_ask(env):
    cases.assert_old_running_ask_cannot_hide_in_window_running_ask(env)


@pytest.mark.parametrize("write_cap", [1, 3])
def test_same_batch_content_fingerprint_drafts_once(env, write_cap):
    cases.assert_same_batch_content_fingerprint_drafts_once(env, write_cap=write_cap)


@pytest.mark.parametrize("submillisecond", [False, True])
def test_offset_microsecond_rescan_drafts_each_ask_once(env, submillisecond):
    cases.assert_offset_microsecond_rescan_drafts_each_ask_once(env, submillisecond=submillisecond)


def test_failed_settle_resend_writes_no_duplicates(env):
    cases.assert_failed_settle_resend_writes_no_duplicates(env)


def test_crashed_batch_given_up_after_three_restarts(env):
    cases.assert_crashed_batch_given_up_after_three_restarts(env)


def test_declined_cache_not_written_after_the_exit(env):
    cases.assert_declined_cache_not_written_after_the_exit(env)


def test_declined_not_resent_within_30_days_then_resent(env):
    cases.assert_declined_not_resent_within_30_days_then_resent(env)


def test_no_re_suggest_after_reject_expire_delete_unchanged_delete(env):
    cases.assert_no_re_suggest_after_reject_expire_delete_unchanged_delete(env)


def test_existing_memory_answers_filtered(env):
    cases.assert_existing_memory_answers_filtered(env)


def test_repeated_cluster_binds_the_chosen_answer(env):
    cases.assert_repeated_cluster_binds_the_chosen_answer(env)


# ------------------------------------------------------------------ cursor and switch
def test_cursor_window_start_includes_trigger(env):
    cases.assert_cursor_window_start_includes_trigger(env)


def test_reenable_does_not_backfill(env):
    cases.assert_reenable_does_not_backfill(env)


def test_disabled_class_zero_model_calls(env):
    cases.assert_disabled_class_zero_model_calls(env)


# ------------------------------------------------------------------ telemetry and degradation
def test_event_keys_whitelisted(env):
    cases.assert_event_keys_whitelisted(env)


def test_model_unavailable_terminal(env):
    cases.assert_model_unavailable_terminal(env)


def test_embedding_unavailable_lexical_only(env):
    cases.assert_embedding_unavailable_lexical_only(env)


# ------------------------------------------------------------------ end to end
def test_end_to_end_clarified_web_ask(env):
    cases.assert_end_to_end_clarified_web_ask(env)
