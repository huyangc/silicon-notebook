"""Bound actual SQLite predecessor work without wall-clock thresholds."""
from time import perf_counter

from app.domain.memory_suggest_contracts import CLARIFIED_SCAN_MAX
from tests.memory_suggest_corrected_cases import corrected_world, scan
from tests.memory_suggest_job_cases import CONV, MEMBER, NB, at
from tests.test_memory_suggest_corrected import env as env


def _append_asks(database, first, last):
    with database.write(operation="test.seed") as db:
        db.executemany(
            "INSERT INTO ask_jobs(id,notebook_id,conversation_id,created_by,mode,question,"
            "status,answer_id,created_at,updated_at,submitted_via,output,report_id) "
            "VALUES (?,?,?,?,'chunk','question','done',?,?,?,'web','answer','')",
            [(f"ask-{n:05d}", NB, CONV, MEMBER, f"ans-{n:05d}", at(n), at(n))
             for n in range(first, last + 1)],
        )


def _sample_cost(env):
    database = env.runtime.memory_suggest_store.database
    statements = []
    ticks = 0

    def progress():
        nonlocal ticks
        ticks += 1
        return 0

    with database.connect() as db:
        db.set_progress_handler(progress, 1000)
        db.set_trace_callback(statements.append)
        try:
            rows = scan(env, limit=CLARIFIED_SCAN_MAX)
        finally:
            db.set_progress_handler(None, 0)
            db.set_trace_callback(None)
        reads = [sql for sql in statements if "FROM ask_jobs a" in sql]
        assert len(reads) == 1
        plan = [str(row["detail"]) for row in db.execute("EXPLAIN QUERY PLAN " + reads[0])]
    assert len(rows) == CLARIFIED_SCAN_MAX
    assert rows[-1]["previous_answer_id"] == f"ans-{CLARIFIED_SCAN_MAX - 1:05d}"
    return ticks * 1000, plan


def test_corrected_predecessor_work_is_bounded_by_the_sample_prefix(env, record_property):
    corrected_world(env)
    database = env.runtime.memory_suggest_store.database
    env.run("INSERT INTO memory_suggest_read_fences(notebook_id,owner_id,lost_at) VALUES (?,?,?)",
            (NB, MEMBER, at(0)))
    _append_asks(database, 1, 1000)
    small, _ = _sample_cost(env)
    start = perf_counter()
    scan(env, limit=CLARIFIED_SCAN_MAX)
    small_seconds = perf_counter() - start
    _append_asks(database, 1001, 5000)
    large, plan = _sample_cost(env)
    start = perf_counter()
    scan(env, limit=CLARIFIED_SCAN_MAX)
    large_seconds = perf_counter() - start
    record_property("vm_steps_1000_turns", small)
    record_property("vm_steps_5000_turns", large)
    # Observations only, with progress/trace handlers disabled. UTC-key UDF
    # callback work is invisible in VM step counts; these are local samples,
    # not latency acceptance thresholds or production performance claims.
    record_property("local_uninstrumented_seconds_1000_turns", small_seconds)
    record_property("local_uninstrumented_seconds_5000_turns", large_seconds)
    record_property("query_plan", " | ".join(plan))

    # Five times the backlog must stay bounded, rather than resolving one
    # predecessor for every backlog row before LIMIT applies.
    assert large < 7 * small, (small, large, plan)
    assert large < 100 * 5000 * CLARIFIED_SCAN_MAX, (small, large, plan)
    assert any("MATERIALIZE prefix" in step for step in plan), plan
    assert any("SEARCH previous USING" in step and "conversation_id=? AND <expr><?" in step for step in plan), plan
