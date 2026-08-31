"""回放对照的 compare 纯函数:exact 逐位比较与 topk 集合重叠。"""
import importlib.util
import pathlib

import pytest

_spec = importlib.util.spec_from_file_location(
    "replay_retrieval",
    pathlib.Path(__file__).resolve().parents[2] / "scripts" / "replay_retrieval.py")
replay = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(replay)


def _rec(ids_scores):
    return {"kg": [{"id": i, "relevance": s} for i, s in ids_scores],
            "ppr_chunks": [{"id": i, "relevance": s} for i, s in ids_scores]}


def test_compare_exact_pass():
    a = {"q1": _rec([("x", 0.9), ("y", 0.5)])}
    b = {"q1": _rec([("x", 0.9), ("y", 0.5)])}
    rep = replay.compare_runs(a, b, mode="exact", k=30)
    assert rep["q1"]["kg"]["pass"] is True and rep["_summary"]["all_pass"] is True


def test_compare_exact_fail_on_reorder():
    a = {"q1": _rec([("x", 0.9), ("y", 0.5)])}
    b = {"q1": _rec([("y", 0.5), ("x", 0.9)])}
    rep = replay.compare_runs(a, b, mode="exact", k=30)
    assert rep["_summary"]["all_pass"] is False


def test_compare_topk_overlap():
    a = {"q1": _rec([("x", 0.9), ("y", 0.5), ("z", 0.1)])}
    b = {"q1": _rec([("x", 0.8), ("z", 0.6), ("y", 0.2)])}
    rep = replay.compare_runs(a, b, mode="topk", k=2)
    # top-2: {x,y} vs {x,z} → overlap 0.5
    assert abs(rep["q1"]["kg"]["overlap"] - 0.5) < 1e-9


def test_summary_only_omits_questions_and_ids_but_keeps_quality_and_timings():
    question = "private question"
    a = {
        question: {
            **_rec([("private-hit-a", 0.9), ("shared", 0.5)]),
            "timings_ms": {"federated": 100, "ppr": 200},
        }
    }
    b = {
        question: {
            **_rec([("private-hit-b", 0.8), ("shared", 0.5)]),
            "timings_ms": {"federated": 80, "ppr": 150},
        }
    }

    rep = replay.compare_runs(a, b, mode="topk", k=2)
    summary = replay.summarize_comparison(rep, a, b)
    rendered = repr(summary)

    assert summary["failed_questions"] == 1
    assert summary["sections"]["kg"]["overlap_mean"] == 0.5
    assert summary["timings"]["federated_ms"]["a"]["p50"] == 100
    assert question not in rendered
    assert "private-hit-a" not in rendered


def test_record_run_requires_retrieval_query_embedding(monkeypatch, capsys):
    class _Repo:
        settings = object()

        def configured(self, workload_id):
            assert workload_id == "retrieval_query_embedding"
            return False

    import app.services.sqlite_repository as repository_module
    monkeypatch.setattr(
        repository_module, "SQLiteRepository", lambda settings: _Repo()
    )

    with pytest.raises(SystemExit) as exc:
        replay.record_run("nb-1", ["q"], False, {}, "admin")

    assert exc.value.code == 2
    assert "retrieval_query_embedding" in capsys.readouterr().err


def test_report_run_enters_report_generation_scope(monkeypatch):
    observed = []

    class _Profile:
        id = "user-1"

    class _Retrieval:
        @staticmethod
        def federated_retrieve(notebook_id, question):
            from app.services.retrieval_run import current_retrieval_run

            run = current_retrieval_run()
            observed.append((notebook_id, question, run.run_kind, run.actor_id))
            return []

        @staticmethod
        def ppr_retrieve(notebook_id, question):
            from app.services.retrieval_run import current_retrieval_run

            run = current_retrieval_run()
            observed.append((notebook_id, question, run.run_kind, run.actor_id))
            return []

    class _Repo:
        settings = object()
        retrieval = _Retrieval()

        @staticmethod
        def configured(workload_id):
            return workload_id == "retrieval_query_embedding"

        @staticmethod
        def get_notebook(notebook_id):
            return {"id": notebook_id}

    import app.core.request_context as request_context
    import app.services.batch_ingest as batch_ingest
    import app.services.sqlite_repository as repository_module

    monkeypatch.setattr(repository_module, "SQLiteRepository", lambda settings: _Repo())
    monkeypatch.setattr(batch_ingest, "_resolve_owner_profile", lambda repo, owner: _Profile())
    monkeypatch.setattr(request_context, "set_request_user", lambda profile: object())
    monkeypatch.setattr(request_context, "reset_request_user", lambda token: None)

    result = replay.record_run(
        "nb-1", ["q"], False, {}, "admin", report_run=True
    )

    assert observed == [
        ("nb-1", "q", "report_generation", "user-1"),
        ("nb-1", "q", "report_generation", "user-1"),
    ]
    assert result["q"]["retrieval_run_kind"] == "report_generation"
