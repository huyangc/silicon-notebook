from __future__ import annotations

from app.eval.memory_agent_ab import run_agent_ab
from app.eval.memory_retrieval import evaluate_cases, run_fixed_gold
from app.models.schemas import MemoryHit
from app.services.memory_retrieval import authority_rank, rerank_eligible_memory_hits


def _hit(memory_id: str, status: str, score: float) -> MemoryHit:
    return MemoryHit(
        memory_id=memory_id,
        title=memory_id,
        text=memory_id,
        status=status,
        authority=1 if status == "candidate" else 3,
        score=score,
        provenance={},
    )


def test_relevance_precedes_authority_for_eligible_memory():
    ranked = rerank_eligible_memory_hits(
        [_hit("relevant-candidate", "candidate", 0.9), _hit("weak-confirmed", "confirmed", 0.2)]
    )

    assert [hit.memory_id for hit in ranked] == [
        "relevant-candidate",
        "weak-confirmed",
    ]


def test_authority_breaks_only_equal_relevance_ties():
    ranked = rerank_eligible_memory_hits(
        [_hit("candidate", "candidate", 0.7), _hit("confirmed", "confirmed", 0.7)]
    )

    assert [hit.memory_id for hit in ranked] == ["confirmed", "candidate"]


def test_conflict_authority_hierarchy_is_deterministic():
    assert [
        authority_rank("candidate"),
        authority_rank("personal_source"),
        authority_rank("confirmed_memory"),
        authority_rank("base"),
    ] == [1, 2, 3, 4]


def test_fixed_eval_has_zero_tolerance_leakage_guards():
    cases = [
        {
            "id": "safe",
            "expected": ["confirmed"],
            "returned": [
                {
                    "memory_id": "confirmed",
                    "status": "confirmed",
                    "created_by": "alice",
                    "notebook_id": "nb-a",
                }
            ],
            "user_id": "alice",
            "notebook_id": "nb-a",
            "plane": "notebook",
        }
    ]

    metrics = evaluate_cases(cases, k=5)

    assert metrics["candidate_to_notebook_leaks"] == 0
    assert metrics["cross_user_leaks"] == 0
    assert metrics["cross_notebook_leaks"] == 0
    assert metrics["recall_at_5"] == 1.0
    assert metrics["mrr"] == 1.0
    assert metrics["ndcg"] == 1.0


def test_committed_gold_has_zero_tolerance_leakage_and_quality_floor():
    metrics = run_fixed_gold()

    assert metrics["candidate_to_notebook_leaks"] == 0
    assert metrics["cross_user_leaks"] == 0
    assert metrics["cross_notebook_leaks"] == 0
    assert metrics["recall_at_5"] >= 0.8
    assert metrics["mrr"] >= 0.8
    assert metrics["ndcg"] >= 0.8


def test_agent_ab_runs_identical_tasks_in_all_three_modes():
    calls = []

    def runner(task, mode):
        calls.append((task["id"], mode))
        return {
            "success": mode == "kb_confirmed_memory",
            "tool_calls": 1,
            "repeated_steps": 0,
            "token_count": 20,
            "citation_valid": True,
        }

    result = run_agent_ab(
        [{"id": "task-a"}, {"id": "task-b"}], runner=runner
    )

    assert set(result) == {"no_memory", "kb_only", "kb_confirmed_memory"}
    assert calls == [
        (task_id, mode)
        for mode in ("no_memory", "kb_only", "kb_confirmed_memory")
        for task_id in ("task-a", "task-b")
    ]
    assert result["kb_confirmed_memory"] == {
        "success_rate": 1.0,
        "tool_calls": 2,
        "repeated_steps": 0,
        "token_count": 40,
        "citation_validity": 1.0,
    }
