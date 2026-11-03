"""PostgreSQL: the auto user-memory suggestion run (plan T7b) over a real
``PostgresRepository`` runtime. SQLite twin:
``tests/test_memory_suggest_job.py``; shared cases:
``tests/memory_suggest_job_cases.py``."""
from __future__ import annotations

import pytest
from psycopg.types.json import Jsonb

from app.core.request_context import set_request_user
from tests import memory_suggest_job_cases as cases

pytestmark = pytest.mark.postgres_integration


@pytest.fixture(autouse=True)
def _clear_request_user():
    yield
    set_request_user(None)


@pytest.fixture
def env(postgres_settings):
    from app.services.postgres_repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    database = repository._runtime.database

    def run(sql: str, params: tuple = ()) -> None:
        with database.write() as db:
            db.execute(sql.replace("?", "%s"), params)

    def rows(sql: str, params: tuple = ()) -> list[dict]:
        with database.connect() as db:
            return [dict(row) for row in db.execute(sql.replace("?", "%s"), params).fetchall()]

    try:
        yield cases.Env(runtime=repository._runtime, run=run, rows=rows, json=Jsonb)
    finally:
        repository.close()


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
