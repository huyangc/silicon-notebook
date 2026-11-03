"""Measure actual sampler work after a deep cursor, without latency thresholds."""
from __future__ import annotations

import pytest

from app.domain.instant_order import utc_microsecond_key
from tests.memory_suggest_job_cases import CONV, MEMBER, NB, at
from tests.test_memory_suggest_corrected import env as env


@pytest.mark.parametrize("kind", ["clarified", "repeated", "unfinished", "corrected"])
def test_exact_utc_sampling_seeks_past_history_before_reading_a_bounded_page(env, kind, record_property):
    env.seed()
    database = env.runtime.database
    store = env.runtime.memory_suggest_store
    with database.write(operation="test.seed") as db:
        db.executemany(
            "INSERT INTO ask_jobs(id,notebook_id,conversation_id,created_by,mode,question,"
            "status,answer_id,created_at,updated_at,submitted_via,output,report_id) "
            "VALUES (?,?,?,?,'chunk','question',?,?,?,?,'web','answer','')",
            [(f"ask-{n:05d}", NB, CONV, MEMBER, "running" if kind == "unfinished" else "done",
              f"ans-{n:05d}", at(n), at(n)) for n in range(1, 10001)],
        )
    calls = 0
    ticks = 0
    statements = []

    def counted_key(value):
        nonlocal calls
        calls += 1
        return utc_microsecond_key(value)

    def progress():
        nonlocal ticks
        ticks += 1
        return 0

    cursor = dict(cursor_created_at=at(9990), cursor_id="ask-09990", inclusive=False)
    with database.connect() as db:
        db.create_function("sn_utc_us", 1, counted_key, deterministic=True)
        db.set_progress_handler(progress, 100)
        db.set_trace_callback(statements.append)
        try:
            if kind == "clarified":
                rows = store.clarified_scan(MEMBER, NB, **cursor, limit=10)
            elif kind == "repeated":
                rows = store.repeated_window(MEMBER, NB, lower=at(9991), limit=10)
            elif kind == "unfinished":
                row = store.earliest_unfinished_ask(MEMBER, NB, **cursor)
                rows = [row] if row else []
            else:
                rows = store.corrected_scan(MEMBER, NB, **cursor, window_started_at=at(1), limit=10)
        finally:
            db.set_trace_callback(None)
            db.set_progress_handler(None, 0)
            db.create_function("sn_utc_us", 1, utc_microsecond_key, deterministic=True)
        reads = [statement for statement in statements if "FROM ask_jobs a" in statement]
        assert len(reads) == 1
        plan = [str(row["detail"]) for row in db.execute("EXPLAIN QUERY PLAN " + reads[0])]

    expected = [f"ask-{n:05d}" for n in range(9991, 10001)]
    if kind == "repeated":
        expected.reverse()
    elif kind == "unfinished":
        expected = expected[:1]
    assert [row["id"] for row in rows] == expected
    if kind == "corrected":
        assert [row["previous_answer_id"] for row in rows] == [f"ans-{n:05d}" for n in range(9990, 10000)]
    record_property("utc_key_calls", calls)
    record_property("vm_steps_rounded_to_100", ticks * 100)
    record_property("query_plan", " | ".join(plan))
    # A fixed page after 9,990 historical rows must not evaluate or walk them.
    # VM work catches a full expression-index walk even when cached keys avoid
    # UDF calls; callbacks catch hidden Python timestamp parsing as well.
    assert calls < 200, (kind, calls, plan)
    assert ticks * 100 < 5000, (kind, ticks * 100, plan)
