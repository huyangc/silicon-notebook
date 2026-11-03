from __future__ import annotations

import json
import re

from app.eval.memory_agent_ab import run_agent_ab, run_fixed_agent_ab
from app.eval.memory_retrieval import evaluate_cases, run_fixed_gold
from app.models.schemas import MemoryHit
from app.services.memory_retrieval import rerank_eligible_memory_hits
from app.services.prompts import answer_prompt


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


def test_fixed_eval_has_zero_tolerance_leakage_guards():
    confirmed = _hit("confirmed", "confirmed", 0.9)
    cases = [
        {
            "id": "safe",
            "expected": ["confirmed"],
            "returned": [confirmed],
            "user_id": "alice",
            "notebook_id": "nb-a",
            "plane": "notebook",
        }
    ]

    metrics = evaluate_cases(
        cases,
        k=5,
        item_scope={
            "confirmed": {"created_by": "alice", "notebook_id": "nb-a"}
        },
    )

    assert metrics["candidate_to_notebook_leaks"] == 0
    assert metrics["cross_user_leaks"] == 0
    assert metrics["cross_notebook_leaks"] == 0
    assert metrics["recall_at_5"] == 1.0
    assert metrics["mrr"] == 1.0
    assert metrics["ndcg"] == 1.0


def test_fixed_eval_detects_candidate_owner_and_notebook_leaks_from_fixture_metadata():
    leaked = _hit("candidate-leak", "candidate", 0.9)
    metrics = evaluate_cases(
        [{
            "expected": [],
            "returned": [leaked],
            "user_id": "alice",
            "notebook_id": "nb-a",
            "plane": "notebook",
        }],
        item_scope={
            "candidate-leak": {"created_by": "bob", "notebook_id": "nb-b"}
        },
    )

    assert metrics["candidate_to_notebook_leaks"] == 1
    assert metrics["cross_user_leaks"] == 1
    assert metrics["cross_notebook_leaks"] == 1


def test_committed_gold_has_zero_tolerance_leakage_and_quality_floor():
    from app.eval import memory_retrieval

    payload = json.loads(memory_retrieval._gold_path().read_text(encoding="utf-8"))
    assert all("returned" not in case for case in payload["cases"])

    metrics = run_fixed_gold()

    assert metrics["candidate_to_notebook_leaks"] == 0
    assert metrics["unadopted_suggestion_leaks"] == 0
    assert metrics["cross_user_leaks"] == 0
    assert metrics["cross_notebook_leaks"] == 0
    # wrong-but-adopted suggestions never out-rank the right memory; the case
    # count keeps a zero from being vacuous (the gold must keep carrying them)
    assert metrics["distractor_cases"] >= 2
    assert metrics["distractor_displacements"] == 0
    assert metrics["recall_at_5"] >= 0.8
    assert metrics["mrr"] >= 0.8
    assert metrics["ndcg"] >= 0.8


def test_unadopted_suggestions_are_leaks_in_the_formal_plane_by_stored_metadata():
    """A candidate or rejected system suggestion is a draft: in the formal
    plane (``("confirmed",)``) it counts as a leak whether the hit says so
    itself or only the stored row does; an ADOPTED suggestion is not a leak."""
    scope = {
        "draft": {"created_by": "alice", "notebook_id": "nb-a",
                  "status": "candidate", "suggested_signal": "clarified"},
        "dismissed": {"created_by": "alice", "notebook_id": "nb-a",
                      "status": "rejected", "suggested_signal": "repeated"},
        "adopted": {"created_by": "alice", "notebook_id": "nb-a",
                    "status": "confirmed", "suggested_signal": "repeated"},
    }

    def case(hit_ids):
        return [{
            "expected": [], "user_id": "alice", "notebook_id": "nb-a",
            "plane": "notebook", "returned": [_hit(i, "confirmed", 0.5) for i in hit_ids],
        }]

    assert evaluate_cases(case(["adopted"]), item_scope=scope)[
        "unadopted_suggestion_leaks"
    ] == 0
    assert evaluate_cases(case(["adopted"]), item_scope=scope)[
        "candidate_to_notebook_leaks"
    ] == 0
    # the hit claims "confirmed" but the stored row is an unadopted draft
    for leaked in ("draft", "dismissed"):
        metrics = evaluate_cases(case([leaked]), item_scope=scope)
        assert metrics["candidate_to_notebook_leaks"] == 1, leaked
        assert metrics["unadopted_suggestion_leaks"] == 1, leaked
    # a hit that is itself a candidate, identified as a suggestion
    candidate_hit = [{
        "expected": [], "user_id": "alice", "notebook_id": "nb-a", "plane": "notebook",
        "returned": [_hit("draft", "candidate", 0.9)],
    }]
    metrics = evaluate_cases(candidate_hit, item_scope=scope)
    assert metrics["candidate_to_notebook_leaks"] == 1
    assert metrics["unadopted_suggestion_leaks"] == 1


def test_distractor_ranked_ahead_of_expected_memory_is_a_displacement():
    def run(order):
        return evaluate_cases([{
            "expected": ["right"], "distractors": ["wrong"], "plane": "notebook",
            "user_id": "alice", "notebook_id": "nb-a",
            "returned": [_hit(i, "confirmed", 0.5) for i in order],
        }])

    assert run(["right", "wrong"])["distractor_displacements"] == 0
    assert run(["wrong", "right"])["distractor_displacements"] == 1
    # the right memory is absent while the distractor made the list
    assert run(["wrong"])["distractor_displacements"] == 1
    assert run(["right"])["distractor_displacements"] == 0
    assert run(["right"])["distractor_cases"] == 1


def test_leak_detector_bites_when_candidates_enter_the_formal_plane():
    """Self-test of the committed gold's probes: serve the formal-plane cases
    from the candidate-inclusive plane and the unadopted suggestion that
    matches best shows up and is counted (so a zero above is not a blind
    detector)."""
    from app.eval.memory_retrieval import _fixture_retriever

    retriever, scope = _fixture_retriever()
    hits = retriever.agent_memory_hits(
        "carol", "clock",
        "include clock uncertainty margin setup analysis suggestion dismissed",
        True, 5,
    )
    metrics = evaluate_cases([{
        "expected": ["memory-clock-correct"], "user_id": "carol",
        "notebook_id": "clock", "plane": "notebook", "returned": hits,
    }], item_scope=scope)

    assert metrics["candidate_to_notebook_leaks"] >= 1
    assert metrics["unadopted_suggestion_leaks"] >= 1


def test_fixed_gold_invokes_real_memory_retriever(monkeypatch):
    from app.services.memory_retrieval import MemoryRetriever

    calls = []
    original = MemoryRetriever.notebook_memory_hits

    def tracked(self, user_id, notebook_id, query, limit=8):
        calls.append((user_id, notebook_id, query, limit))
        return original(self, user_id, notebook_id, query, limit)

    monkeypatch.setattr(MemoryRetriever, "notebook_memory_hits", tracked)

    metrics = run_fixed_gold()

    assert calls
    assert metrics["candidate_to_notebook_leaks"] == 0


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


def test_fixed_agent_ab_runs_mode_harness_and_real_confirmed_memory_retrieval(monkeypatch):
    from app.services.memory_retrieval import MemoryRetriever

    calls = []
    original = MemoryRetriever.notebook_memory_hits

    def tracked(self, user_id, notebook_id, query, limit=8):
        calls.append((user_id, notebook_id, query, limit))
        return original(self, user_id, notebook_id, query, limit)

    monkeypatch.setattr(MemoryRetriever, "notebook_memory_hits", tracked)

    result = run_fixed_agent_ab()

    assert calls
    assert result["kb_confirmed_memory"]["success_rate"] > result["kb_only"][
        "success_rate"
    ]
    assert result["kb_confirmed_memory"]["citation_validity"] == 1.0


def test_answer_prompt_rule_numbers_are_unique_and_sequential():
    prompt = answer_prompt("question", "(none)")
    numbers = [int(value) for value in re.findall(r"(?m)^(\d+)\. ", prompt)]

    # PR-1 止血追加规则 11(枚举/列举类问题必须逐条列出全部匹配项)。
    # 2026-09-03 纠偏根因整改追加规则 12(问题限定词保真:不得把点名的子部件/
    # 条件/方向/周期性静默泛化;证据只覆盖邻近情形时明说)。
    # T2-c 追加规则 13(推断状态传递:依赖任何（推断）前提的结论/总结句自身也是
    # 推断,须继承（推断）前缀且不挂 [k])。新规则一律追加在末尾,
    # 中间插号会连带改 L1 片段(4/8/9/10)的起始序号契约。
    assert numbers == list(range(1, 14))
