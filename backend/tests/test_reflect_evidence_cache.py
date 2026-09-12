"""Evidence-only reflect keeps retrieval/cache behavior without aspect auditing."""

import json
from types import SimpleNamespace

import pytest

from app.domain.retrieval_termination import (
    TERMINATION_MODEL_DEGRADED, TERMINATION_STALE, TERMINATION_STEP_BUDGET,
    RetrievalTermination,
)
from app.services.reasoning_aspects import (
    build_aspect_ledger, classify_termination, render_termination_block,
    termination_synthesis_detail,
)
from app.services.reasoning_retrieval import ReasoningRetriever, ReflectDecision
from tests.test_reasoning_retrieval import (
    rrepo, _answer, _capture_contexts, _fake_model_end,
    _measured_run, _project, _reflect_details,
    _three_turn_chunks, _three_turn_reflects, _THREE_ASPECTS,
)


EVIDENCE = "prefix_delta_evidence"


def test_evidence_arm_keeps_contract_cards_and_stable_prefix(rrepo, monkeypatch):
    contexts = _capture_contexts(monkeypatch)
    script = [{k: v for k, v in row.items() if k != "assessment"}
              for row in _three_turn_reflects()]
    llm, result = _measured_run(
        rrepo, optimization=EVIDENCE, reflects=script,
        intent_detail=_THREE_ASPECTS, chunk_results=_three_turn_chunks(),
        reasoning_max_chunk_searches=2)

    assert len(llm.message_lists) == len(script)
    assert len(set(llm.system_prompts)) == 1
    assert len({context.contract for context in contexts}) == 1
    assert all(not context.assessment_enabled for context in contexts)
    assert contexts[-1].delta
    assert "key=ck-q0" in llm.user_prompts[0]
    assert "key=ck-q1" in llm.user_prompts[-1]
    for topic in (*_THREE_ASPECTS["mandatory_topics"],
                  *_THREE_ASPECTS["constraints"]):
        assert all(topic in context.contract for context in contexts)
    for messages, hint in zip(llm.message_lists, llm.schema_hints):
        assert "assessment" not in json.loads(hint)
        assert "assessment" not in "\n".join(m["content"] for m in messages)
        assert "current status of every mandatory aspect" not in messages[0]["content"]
    assert llm.turn_actions(0) != llm.turn_actions(2)
    assert "search_chunks" not in llm.turn_actions(2)
    assert all("assessment_rows" not in row and "assessment_absent" not in row
               for row in _reflect_details(result))
    assert not result.termination.assessment_enabled
    assert result.termination.aspects == ()
    assert result.termination.unresolved_aspect_ids == ()
    assert render_termination_block(result.termination) == ""
    detail = termination_synthesis_detail(
        result.termination, admitted_keys={"ck-q0"}, cited_keys={"ck-q0"})
    assert detail["termination_summary"] == "检索结束：模型决定作答"
    assert not any(key.startswith("aspects_") for key in detail)
    terminal = next(step for step in result.trace
                    if "termination" in step.detail)
    assert terminal.detail["aspects_unassessed"] is None
    projected = _project(result, EVIDENCE)
    assert projected["optimization"] == EVIDENCE
    assert projected["assessment_rows_total"] is None


def test_evidence_arm_accepts_silent_close_without_an_audit_call(rrepo):
    llm, result = _measured_run(
        rrepo, optimization=EVIDENCE, intent_detail=_THREE_ASPECTS,
        reflects=[_answer()])
    assert len(llm.message_lists) == 1
    assert not any(step.detail.get("reason") == "missing_assessment"
                   for step in result.trace)
    assert result.termination.model_assessed_sufficient
    assert next(step for step in result.trace
                if "termination" in step.detail).summary == "检索结束：模型决定作答"


def test_disabled_assessment_cannot_mutate_state_or_discard_a_legal_action():
    ledger = build_aspect_ledger(
        _THREE_ASPECTS, "完整问题", assessment_enabled=False)
    initial = ledger.snapshot()
    malformed = {"supported": "not a list"}
    assert not ledger.apply(malformed, allowed_keys=set()).error
    assert ledger.snapshot() == initial
    assert not ledger.note_missing_assessment()
    with pytest.raises(AttributeError):
        ledger.assessment_enabled = True

    state = SimpleNamespace(aspects=ledger)
    decision = ReflectDecision(
        next_action="search_chunks", sufficient=False, chunks_query="test",
        assessment=malformed)
    retriever = object.__new__(ReasoningRetriever)
    actual = retriever._absorb_assessment(
        state, decision, None, False, True)
    assert actual is decision
    assert not actual.invalid_reason
    assert actual.assessment is None


@pytest.mark.parametrize("sufficient", [False, True])
def test_without_assessment_normal_close_does_not_assert_coverage(sufficient):
    ledger = build_aspect_ledger(
        _THREE_ASPECTS, "完整问题", assessment_enabled=False)
    term = classify_termination([_fake_model_end(sufficient)], [], ledger)
    assert term.model_assessed_sufficient is sufficient
    assert not term.aspects and not term.unresolved_aspect_ids
    assert render_termination_block(term) == ""
    detail = termination_synthesis_detail(term, admitted_keys=set(), cited_keys=set())
    assert detail["termination_summary"] == "检索结束：模型决定作答"
    assert "aspects_pending" not in detail


@pytest.mark.parametrize("reason", [
    TERMINATION_MODEL_DEGRADED, TERMINATION_STALE, TERMINATION_STEP_BUDGET,
])
def test_without_assessment_actual_failures_and_budget_stops_remain_visible(reason):
    term = RetrievalTermination(
        reason=reason, assessment_enabled=False,
        unrecovered_channels=("search_chunks",))
    block = render_termination_block(term)
    assert block and "search_chunks" in block
    assert "Questions the retrieval did not resolve" not in block
    assert "Say plainly in the answer which" not in block
    assert "reported supported" not in block


def test_evidence_arm_keeps_no_assessment_contract_after_cache_fallback(
    rrepo, monkeypatch,
):
    import app.services.reasoning_retrieval as module

    original = ReasoningRetriever._reflect_delta_context
    calls = 0

    def force_transition(self, state, *args, **kwargs):
        nonlocal calls
        context = original(self, state, *args, **kwargs)
        calls += 1
        if calls == 2:
            module._delta_fallback(
                state.reflect_delta, module._carried_delta(state.reflect_delta))
        return context

    monkeypatch.setattr(ReasoningRetriever, "_reflect_delta_context", force_transition)
    script = [{k: v for k, v in row.items() if k != "assessment"}
              for row in _three_turn_reflects()]
    llm, result = _measured_run(
        rrepo, optimization=EVIDENCE, reflects=script,
        intent_detail=_THREE_ASPECTS, chunk_results=_three_turn_chunks(),
        reasoning_max_chunk_searches=2)
    details = _reflect_details(result)
    assert any(row["context_fallback"] for row in details)
    assert details[-1]["context_fallback"]
    assert len(set(llm.system_prompts)) == 1
    assert all("assessment" not in hint for hint in llm.schema_hints)
    assert all("assessment" not in prompt for prompt in llm.user_prompts)
    assert not result.termination.assessment_enabled
    assert not any(step.detail.get("reason") == "missing_assessment"
                   for step in result.trace)
