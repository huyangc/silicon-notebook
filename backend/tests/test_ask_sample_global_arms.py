"""The three post-completion samplers also see GLOBAL asks (SQLite).

A global Ask leaves the same record a notebook Ask leaves and advances the
same three learning chains, so the reads those chains sample from must see
it too -- under the same actor predicate, the same bounds and the same
projections. PostgreSQL twins live in
``tests/postgres/test_content_store_conformance.py``.
"""
from __future__ import annotations

import json

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository

USER = "user-a"
OTHER = "user-b"


@pytest.fixture
def repo(tmp_path):
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path / 't.db'}",
                        storage_dir=str(tmp_path / "s"))
    repo = SQLiteRepository(settings)
    with repo._write() as db:
        for user in (USER, OTHER):
            db.execute(
                "INSERT INTO users(id,email,display_name,role,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?)",
                (user, f"{user}@x", user, "user", "active", "2026-08-01T00:00:00", "2026-08-01T00:00:00"))
        for nb in ("nb-a", "nb-b"):
            db.execute(
                "INSERT INTO notebooks(id,name,created_by,status,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (nb, nb, USER, "ready", "2026-08-01T00:00:00", "2026-08-01T00:00:00"))
    yield repo
    repo.close()


def _step(summary, kind="retrieve"):
    """The persisted ``TraceStep`` shape (app.models.ask.TraceStep)."""
    return {"step_type": kind, "summary": summary, "detail": {"count": 1}, "duration_ms": 5}


def _ask(db, job_id, notebook_id, user_id, created_at, *, question="nb?", status="done",
         mode="reasoning", steps=()):
    db.execute(
        "INSERT INTO ask_jobs(id,notebook_id,conversation_id,created_by,mode,question,status,"
        "answer_id,error,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, notebook_id, "", user_id, mode, question, status, "", "", created_at, created_at))
    for seq, step in enumerate(steps):
        db.execute(
            "INSERT INTO ask_trace_steps(job_id,seq,step_json,created_at) VALUES(?,?,?,?)",
            (job_id, seq, json.dumps(step), created_at))


def _global(db, job_id, user_id, created_at, *, notebook_ids, question="global?",
            status="done", mode="reasoning", steps=(), searched=None, cited=(), skipped=()):
    db.execute(
        "INSERT INTO global_ask_conversations(id,user_id,title,scope_json,submitted_via,"
        "created_at,updated_at) VALUES(?,?,?,?,?,?,?) ON CONFLICT(id) DO NOTHING",
        (f"conv-{user_id}", user_id, "", "{}", "web", created_at, created_at))
    payload = {
        "job_id": job_id, "conversation_id": f"conv-{user_id}", "status": status,
        "question": question, "created_at": created_at, "mode": mode,
        "notebook_scope": {"mode": "include", "notebook_ids": list(notebook_ids)},
        "resolved_notebook_ids": list(notebook_ids),
        "searched_notebook_ids": list(notebook_ids) if searched is None else list(searched),
        "cited_notebook_ids": list(cited),
        "skipped_notebooks": [{"notebook_id": nb, "reason": "timeout"} for nb in skipped],
        "answer": {"answer": "a", "reasoning_trace": list(steps)},
    }
    db.execute(
        "INSERT INTO global_ask_jobs(id,conversation_id,user_id,client_request_id,request_json,"
        "status,payload_json,created_at,submitted_via,asked_at,updated_at,error_detail,mode) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (job_id, f"conv-{user_id}", user_id, None, "{}", status,
         json.dumps(payload, ensure_ascii=False), created_at, "web", "", created_at, "", mode))


def test_overlay_trace_sample_includes_the_members_global_asks_that_touched_the_notebook(repo):
    with repo._write() as db:
        _ask(db, "ask-1", "nb-a", USER, "2026-08-01T10:00:00+00:00", question="notebook one",
             steps=[_step("nb step")])
        _global(db, "gask-mine", USER, "2026-08-01T11:00:00+00:00", notebook_ids=["nb-a", "nb-b"],
                question="global mine", steps=[_step("g1"), _step("g2")])
        _global(db, "gask-elsewhere", USER, "2026-08-01T12:00:00+00:00", notebook_ids=["nb-b"],
                question="global elsewhere")
        _global(db, "gask-other", OTHER, "2026-08-01T13:00:00+00:00", notebook_ids=["nb-a"],
                question="someone else")
    ask_state = repo._runtime.ask_state
    rows = ask_state.recent_user_ask_traces("nb-a", USER, job_limit=10, step_limit=600)
    assert [row["job_id"] for row in rows] == ["gask-mine", "ask-1"]
    assert rows[0]["question"] == "global mine"
    assert [step["summary"] for step in rows[0]["steps"]] == ["g1", "g2"]
    assert [step["summary"] for step in rows[1]["steps"]] == ["nb step"]
    # Bounds hold across both arms: the newest job wins the job cap, and the
    # step cap drops the OLDEST job's tail.
    assert [row["job_id"] for row in ask_state.recent_user_ask_traces(
        "nb-a", USER, job_limit=1, step_limit=600)] == ["gask-mine"]
    capped = ask_state.recent_user_ask_traces("nb-a", USER, job_limit=10, step_limit=2)
    assert [len(row["steps"]) for row in capped] == [2, 0]


def test_a_member_whose_only_asks_in_a_library_were_global_still_gets_a_sample(repo):
    """The overlay's core new scenario: no ``ask_jobs`` row in this library at
    all. The global arm must run regardless of the notebook arm (the
    notebook-only early return used to swallow it), and a running job's
    streamed ``trace`` is read where a finished one's ``answer.reasoning_trace``
    would be -- the ``global_answer_trace`` rule."""
    with repo._write() as db:
        _global(db, "gask-only", USER, "2026-08-01T11:00:00+00:00", notebook_ids=["nb-a"],
                question="global only", steps=[_step("g")])
    with repo._write() as db:
        db.execute(
            "INSERT INTO global_ask_jobs(id,conversation_id,user_id,client_request_id,"
            "request_json,status,payload_json,created_at,submitted_via,asked_at,updated_at,"
            "error_detail,mode) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            ("gask-running", f"conv-{USER}", USER, None, "{}", "running",
             json.dumps({"job_id": "gask-running", "conversation_id": f"conv-{USER}",
                         "status": "running", "question": "still running",
                         "created_at": "2026-08-01T12:00:00+00:00", "mode": "reasoning",
                         "notebook_scope": {"mode": "include", "notebook_ids": ["nb-a"]},
                         "resolved_notebook_ids": ["nb-a"], "trace": [_step("streamed")]}),
             "2026-08-01T12:00:00+00:00", "web", "", "2026-08-01T12:00:00+00:00", "", "reasoning"))
    rows = repo._runtime.ask_state.recent_user_ask_traces("nb-a", USER, job_limit=10, step_limit=600)
    assert [row["job_id"] for row in rows] == ["gask-running", "gask-only"]
    assert [step["summary"] for step in rows[0]["steps"]] == ["streamed"]
    assert rows[0]["steps"][0]["step_type"] == "retrieve"
    assert [step["summary"] for step in rows[1]["steps"]] == ["g"]


def test_global_experience_partition_samples_global_reasoning_runs_but_a_notebook_partition_does_not(repo):
    with repo._write() as db:
        _ask(db, "ask-1", "nb-a", USER, "2026-08-01T10:00:00+00:00", steps=[_step("nb")])
        _global(db, "gask-1", USER, "2026-08-01T11:00:00+00:00", notebook_ids=["nb-a", "nb-b"],
                steps=[_step("g")])
        _global(db, "gask-chunk", USER, "2026-08-01T12:00:00+00:00", notebook_ids=["nb-a"],
                mode="chunk", steps=[_step("c")])
        _global(db, "gask-running", USER, "2026-08-01T13:00:00+00:00", notebook_ids=["nb-a"],
                status="running", steps=[_step("r")])
    ask_state = repo._runtime.ask_state
    everything = ask_state.recent_completed_ask_runs(job_limit=40, step_limit=600)
    assert [run["run_id"] for run in everything] == ["gask-1", "ask-1"]
    assert set(everything[0]) == {"run_id", "mode", "steps"}
    assert everything[0]["mode"] == "reasoning" and len(everything[0]["steps"]) == 1
    partitioned = ask_state.recent_completed_ask_runs(
        job_limit=40, step_limit=600, notebook_id="nb-a")
    assert [run["run_id"] for run in partitioned] == ["ask-1"]


def test_language_sample_includes_the_persons_global_asks_only(repo):
    with repo._write() as db:
        _ask(db, "ask-zh", "nb-a", USER, "2026-08-01T10:00:00+00:00", question="这是中文问题")
        _global(db, "gask-en", USER, "2026-08-01T11:00:00+00:00", notebook_ids=["nb-a"],
                question="an english question about circuits")
        _global(db, "gask-other", OTHER, "2026-08-01T12:00:00+00:00", notebook_ids=["nb-a"],
                question="另一个人的问题")
    ask_state = repo._runtime.ask_state
    rows = ask_state.recent_user_ask_languages(USER, limit=30)
    assert rows == [{"language": "en"}, {"language": "zh"}]
    assert ask_state.recent_user_ask_languages(USER, limit=1) == [{"language": "en"}]


def test_overlay_sample_follows_the_completion_attribution_rule(repo):
    """A library the run resolved but neither searched nor cited (skipped, or
    simply not reached) gets neither the completion note nor the run in its
    member's overlay sample -- the same ``touched_notebook_ids`` rule; a run
    with no federated call falls back to participants minus skipped ones."""
    with repo._write() as db:
        _global(db, "gask-skipped-b", USER, "2026-08-01T10:00:00+00:00",
                notebook_ids=["nb-a", "nb-b"], searched=["nb-a"], skipped=["nb-b"],
                question="searched a, skipped b", steps=[_step("a")])
        _global(db, "gask-cited-b", USER, "2026-08-01T11:00:00+00:00",
                notebook_ids=["nb-a", "nb-b"], searched=[], cited=["nb-b"],
                question="cited b only")
        _global(db, "gask-overview", USER, "2026-08-01T12:00:00+00:00",
                notebook_ids=["nb-a", "nb-b"], searched=[], skipped=["nb-b"],
                question="no federated call")
    ask_state = repo._runtime.ask_state
    in_a = [row["job_id"] for row in ask_state.recent_user_ask_traces("nb-a", USER, job_limit=10, step_limit=600)]
    in_b = [row["job_id"] for row in ask_state.recent_user_ask_traces("nb-b", USER, job_limit=10, step_limit=600)]
    assert in_a == ["gask-overview", "gask-skipped-b"]
    assert in_b == ["gask-cited-b"]


def test_the_attribution_predicate_runs_before_the_limit(repo):
    """Starvation guard: the member's newest global runs merely RESOLVED nb-b
    (they searched nb-a), and an older run really searched nb-b. With the
    predicate after ``LIMIT`` the window would fill with the dropped rows and
    nb-b would never see its qualifying run; with the predicate in SQL it does."""
    with repo._write() as db:
        _global(db, "gask-real-b", USER, "2026-08-01T09:00:00+00:00",
                notebook_ids=["nb-a", "nb-b"], searched=["nb-b"], question="really b")
        for i in range(5):
            _global(db, f"gask-noise-{i}", USER, f"2026-08-01T1{i}:00:00+00:00",
                    notebook_ids=["nb-a", "nb-b"], searched=["nb-a"], question="a only")
    rows = repo._runtime.ask_state.recent_user_ask_traces("nb-b", USER, job_limit=2, step_limit=600)
    assert [row["job_id"] for row in rows] == ["gask-real-b"]


def test_the_sql_attribution_predicate_agrees_with_the_python_rule(repo):
    """Every combination the rule distinguishes, seen by SQL and by
    ``touched_notebook_ids`` alike."""
    from app.domain.global_ask_attribution import touched_notebook_ids
    cases = {
        "searched": dict(notebook_ids=["nb-a", "nb-b"], searched=["nb-b"]),
        "cited": dict(notebook_ids=["nb-a", "nb-b"], searched=[], cited=["nb-b"]),
        "resolved-only-with-search-elsewhere": dict(notebook_ids=["nb-a", "nb-b"], searched=["nb-a"]),
        "no-call-not-skipped": dict(notebook_ids=["nb-a", "nb-b"], searched=[]),
        "no-call-skipped": dict(notebook_ids=["nb-a", "nb-b"], searched=[], skipped=["nb-b"]),
        "not-a-participant": dict(notebook_ids=["nb-a"], searched=["nb-a"]),
    }
    with repo._write() as db:
        for index, (name, spec) in enumerate(cases.items()):
            _global(db, f"gask-{name}", USER, f"2026-08-0{index + 1}T00:00:00+00:00",
                    question=name, **spec)
    sampled = {row["job_id"] for row in repo._runtime.ask_state.recent_user_ask_traces(
        "nb-b", USER, job_limit=50, step_limit=600)}
    expected = {
        f"gask-{name}" for name, spec in cases.items()
        if "nb-b" in touched_notebook_ids(
            spec["notebook_ids"], spec.get("searched", spec["notebook_ids"]),
            spec.get("cited", []), spec.get("skipped", []))
    }
    assert sampled == expected == {"gask-searched", "gask-cited", "gask-no-call-not-skipped"}


def test_the_step_budget_bounds_what_leaves_the_database_for_global_rows(repo):
    """Two global rows with five steps each and ``step_limit=3``: the newest row
    contributes three (sliced in SQL), the older one nothing, and the budget
    is spent newest-first exactly as the notebook arm's ``LIMIT`` spends it."""
    with repo._write() as db:
        _global(db, "gask-old", USER, "2026-08-01T10:00:00+00:00", notebook_ids=["nb-a"],
                steps=[_step(f"o{i}") for i in range(5)])
        _global(db, "gask-new", USER, "2026-08-01T11:00:00+00:00", notebook_ids=["nb-a"],
                steps=[_step(f"n{i}") for i in range(5)])
    ask_state = repo._runtime.ask_state
    rows = ask_state.recent_user_ask_traces("nb-a", USER, job_limit=10, step_limit=3)
    assert [(row["job_id"], [s["summary"] for s in row["steps"]]) for row in rows] == [
        ("gask-new", ["n0", "n1", "n2"]), ("gask-old", []),
    ]
    runs = ask_state.recent_completed_ask_runs(job_limit=10, step_limit=4)
    assert [(run["run_id"], len(run["steps"])) for run in runs] == [("gask-new", 4), ("gask-old", 0)]
    # What actually crossed the store boundary: the budget is ONE across
    # jobs (3 elements total, not 3 per job), so the merge decodes exactly
    # three elements, all from the newest row -- and the older row is not
    # decoded at all once the budget is spent.
    from app.repositories import ask_sample_merge as merge_module
    received = []
    original = merge_module._trace_steps

    def counting(raw):
        steps = original(raw)
        received.append(len(steps))
        return steps

    merge_module._trace_steps = counting
    try:
        ask_state.recent_user_ask_traces("nb-a", USER, job_limit=10, step_limit=3)
    finally:
        merge_module._trace_steps = original
    assert received == [3]
