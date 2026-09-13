"""On-demand detail reading leaves ordinary reflect requests unchanged."""

from types import SimpleNamespace

import pytest

from app.models.ask import TraceStep
from app.services.reasoning_context import ReflectDeltaState
from app.services.reasoning_prose_excerpt import render_visible_progress, visible_excerpt_identities
from app.services.reasoning_retrieval import (
    ReasoningRetriever, _carried_delta, _has_continued_retrieval,
    _prose_detail_cards, _prose_visibility_note,
)
from tests.test_reflect_prose_evidence import details, hit, snapshot
from tests.test_reflect_table_evidence import TABLE_ONE
from tests.test_reasoning_retrieval import (
    rrepo, _answer, _capture_contexts, _measured_run, _seed_two_nodes, _v2_run,
)


def search(query):
    return {"next_action": "search_chunks", "sufficient": False,
            "arguments": {"query": query}}


def continued(action="search_chunks", **extra):
    return TraceStep(step_type="reflect", summary="continued",
                     detail={"next_action": action, "sufficient": False, **extra})


def state_for(item, delta=None, trace=None):
    return SimpleNamespace(collected={}, elements=[], chunks=[item],
                           reflect_delta=delta, question="Unmatched question",
                           trace=[continued()] if trace is None else trace)


def demand(state, query="State cache sharing", budget=8000):
    return _prose_detail_cards(SimpleNamespace(), "prefix_delta_evidence", state,
                               SimpleNamespace(last_query=query), 240, 1200, budget)


@pytest.mark.parametrize("item,question", [
    (hit(), "State cache sharing"),
    (hit("table", TABLE_ONE), "OLMo Ours"),
])
@pytest.mark.parametrize("history_budget", [1000, 6000])
def test_first_complete_provider_request_is_identical_with_detail_enabled_or_disabled(
    rrepo, monkeypatch, item, question, history_budget,
):
    contexts = _capture_contexts(monkeypatch)
    runs, tool_calls = [], []
    for enabled in (False, True):
        calls = []
        llm, result = _measured_run(
            rrepo, optimization="prefix_delta_evidence", question=question,
            reflects=[_answer()], calls=calls, chunk_results={question: [item]},
            reasoning_reflect_prose_detail_enabled=enabled,
            reasoning_reflect_state_chars=history_budget)
        runs.append(llm)
        tool_calls.append(calls)
        assert not result.termination.assessment_enabled
    assert len(runs[0].message_lists) == len(runs[1].message_lists) == 1
    assert runs[0].message_lists == runs[1].message_lists
    assert runs[0].schema_hints == runs[1].schema_hints
    assert tool_calls[0] == tool_calls[1]
    assert "可见证据变化" not in contexts[1].turn_state


def test_kg_only_continuations_preserve_every_provider_message(rrepo, monkeypatch):
    nb = _seed_two_nodes(rrepo)
    contexts = _capture_contexts(monkeypatch)
    runs = []
    for enabled in (False, True):
        llm, result = _v2_run(
            rrepo, nb,
            [{"next_action": "add_subquery", "sufficient": False,
              "arguments": {"query": "布局布线步骤"}},
             {"next_action": "add_subquery", "sufficient": False,
              "arguments": {"query": "RTL到GDSII流程的依赖"}}, _answer()],
            reasoning_reflect_optimization="prefix_delta_evidence",
            reasoning_reflect_prose_detail_enabled=enabled,
            reasoning_chunk_search_enabled=False, graph_ppr_enabled=False,
            reasoning_reflect_state_chars=1000)
        runs.append(llm)
        assert _has_continued_retrieval(result.trace)
    assert len(runs[0].message_lists) == len(runs[1].message_lists) == 3
    assert runs[0].message_lists == runs[1].message_lists
    assert runs[0].schema_hints == runs[1].schema_hints
    assert all("抽取摘要" in c.evidence for c in contexts)
    assert all("可见证据变化" not in c.turn_state for c in contexts)


@pytest.mark.parametrize("trace", [
    [], [TraceStep(step_type="search_chunks", summary="seed", detail={"found": 1})],
    [continued("answer", sufficient=True)], [continued("update_outline")],
    [continued("consult_memory")], [continued("_invalid")],
    [continued(fallback_reason="model_degraded")],
])
def test_seed_non_retrieval_and_failed_validation_cannot_enable_details(trace):
    assert demand(state_for(hit(), trace=trace)) is None


def test_invalid_first_action_does_not_change_the_next_request(rrepo):
    runs = []
    for enabled in (False, True):
        llm, result = _measured_run(
            rrepo, optimization="prefix_delta_evidence", question="State cache sharing",
            reflects=[{"next_action": "add_subquery", "sufficient": False,
                       "arguments": {}}, _answer()],
            chunk_results={"State cache sharing": [hit()]},
            reasoning_reflect_prose_detail_enabled=enabled)
        runs.append(llm)
        assert not _has_continued_retrieval(result.trace)
    assert len(runs[0].message_lists) == len(runs[1].message_lists) == 2
    assert runs[0].message_lists == runs[1].message_lists


def test_late_detail_activates_after_a_real_continuation_and_repeat_adds_no_false_progress(
    rrepo, monkeypatch,
):
    contexts = _capture_contexts(monkeypatch)
    runs, tool_calls = [], []
    for enabled in (False, True):
        calls = []
        llm, result = _measured_run(
            rrepo, optimization="prefix_delta_evidence", question="Unmatched question",
            reflects=[search("Unrelated topic"), search("State cache sharing"),
                      search("State cache sharing"), _answer()], calls=calls,
            chunk_results={"Unmatched question": [hit()], "Unrelated topic": [hit()],
                           "State cache sharing": [hit()]},
            reasoning_reflect_prose_detail_enabled=enabled,
            reasoning_max_chunk_searches=4)
        runs.append(llm)
        tool_calls.append(calls)
        assert not result.termination.assessment_enabled
    assert len(runs[0].message_lists) == len(runs[1].message_lists) == 4
    assert runs[0].message_lists[:2] == runs[1].message_lists[:2]
    assert runs[0].schema_hints == runs[1].schema_hints
    assert tool_calls[0] == tool_calls[1]
    enabled_contexts = contexts[4:]
    assert all("可见证据变化" not in c.turn_state for c in enabled_contexts[:2])
    assert "i modulo k" in enabled_contexts[2].delta
    assert "首次展示的摘录视图：1" in enabled_contexts[2].turn_state
    assert "首次展示的摘录视图：0" in enabled_contexts[3].turn_state
    assert enabled_contexts[2].delta in enabled_contexts[3].delta


def test_empty_or_ineffective_prose_candidates_leave_the_whole_enhancement_dormant():
    assert demand(state_for(hit(text="State cache sharing."))) is None
    assert demand(state_for(hit()), query="Unrelated topic") is None
    assert demand(state_for(hit()), budget=1000) is None
    assert demand(state_for(hit("table", TABLE_ONE)), query="OLMo Ours") is None
    formula = hit(text="$" + "State cache sharing " * 150 + "$ follow-up.")
    assert demand(state_for(formula)) is None  # a formula-boundary notice is not evidence


def test_retained_detail_requires_actual_emission_and_current_source_authority():
    item = hit()
    chosen = details([item])
    original = snapshot([item], None)
    expanded = snapshot([item], chosen)
    delta = ReflectDeltaState(snapshot_evidence=expanded.text,
                              frozen_cards=dict(original.cards))
    delta.supplement_latest_cards.update(expanded.cards)
    state = state_for(item, delta)
    assert demand(state, query="Unrelated topic") is None  # render/cache is not emission
    _prose_visibility_note(state, expanded.text, "", "", chosen, 6000)
    assert demand(state, query="Unrelated topic") == {}  # retain append-only protections
    state.chunks = []
    assert demand(state, query="Unrelated topic") is None
    assert not _prose_visibility_note(state, expanded.text, "", "", {}, 6000)


@pytest.mark.parametrize("fallback", [False, True])
def test_history_without_spare_room_is_preserved_and_does_not_lose_visibility(fallback):
    item = hit()
    chosen = details([item])
    expanded = snapshot([item], chosen).text
    history = "已有检索观察；" * 100
    carried_history = "后续检索观察；" * 20
    delta = ReflectDeltaState(
        snapshot_history="" if fallback else history,
        history_chars=len(carried_history) + (0 if fallback else len(history)),
        fallback=fallback)
    state = state_for(item, delta)
    budget = len(history) + len(carried_history)
    note = _prose_visibility_note(state, expanded, "", history, chosen, budget)
    assert not note
    actual = visible_excerpt_identities(expanded)
    assert delta.visible_excerpt_fingerprints == actual
    assert delta.prose_detail_fingerprints == actual
    assert delta.history_chars == len(carried_history) + (0 if fallback else len(history))
    assert delta.snapshot_history == ("" if fallback else history)
    # When space later returns, recovery must not invent first-time evidence.
    note = _prose_visibility_note(state, expanded, "", history, {}, budget + 6000)
    assert "首次展示的摘录视图：0" in note
    assert len(history) + len(carried_history) + len(note) + len("\n\n") <= budget + 6000


def test_budget_rejected_detail_does_not_activate_feedback_or_claim_a_detail_was_seen():
    item = hit()
    chosen = details([item])
    rejected = snapshot([item], chosen, budget=400)
    assert "i modulo k" not in rejected.text and rejected.shown_keys == ("mechanism",)
    state = state_for(item, ReflectDeltaState())
    assert not _prose_visibility_note(state, rejected.text, "", "", chosen, 6000)
    assert not state.reflect_delta.prose_detail_fingerprints
    assert state.reflect_delta.visible_excerpt_fingerprints == visible_excerpt_identities(rejected.text)


def test_real_full_history_keeps_details_without_feedback_or_an_extra_rebuild(rrepo, monkeypatch):
    captured = []
    original_context = ReasoningRetriever._reflect_v2_context

    def capture(self, state, summary, outline):
        context = original_context(self, state, summary, outline)
        delta = state.reflect_delta
        captured.append((context, delta.history_chars, delta.rebuilds,
                         set(delta.prose_detail_fingerprints),
                         _carried_delta(delta).history_chars))
        return context

    monkeypatch.setattr(ReasoningRetriever, "_reflect_v2_context", capture)
    queries = ["Unrelated direction " + str(i) + " x" * 40 for i in range(3)]
    queries.append("State cache sharing")
    decisions = [dict(search(query), reason=(
        "Inspect the remaining evidence before deciding whether the mechanism is supported."))
        for query in queries]
    runs, tool_calls = [], []
    for enabled in (False, True):
        calls = []
        llm, _ = _measured_run(
            rrepo, optimization="prefix_delta_evidence", question="Unmatched question",
            reflects=[*decisions, _answer()], calls=calls, chunk_results={None: [hit()]},
            reasoning_reflect_prose_detail_enabled=enabled,
            reasoning_max_chunk_searches=5, reasoning_reflect_state_chars=1000)
        runs.append(llm)
        tool_calls.append(calls)
    turns = len(decisions) + 1
    assert len(runs[0].message_lists) == len(runs[1].message_lists) == turns
    assert runs[0].message_lists[:-1] == runs[1].message_lists[:-1]
    assert runs[0].schema_hints == runs[1].schema_hints
    assert tool_calls[0] == tool_calls[1]
    baseline, treatment = captured[:turns], captured[turns:]
    assert all(old[0].observations == new[0].observations for old, new in zip(baseline, treatment))
    assert [row[1:3] for row in baseline] == [row[1:3] for row in treatment]
    context, used, rebuilds, detail_seen, carried_history = treatment[-1]
    assert rebuilds == 0 and "i modulo k" in context.delta
    assert "可见证据变化" not in context.turn_state
    assert detail_seen <= visible_excerpt_identities(context.evidence + "\n" + context.delta)
    assert detail_seen and used == len(context.observations) + carried_history
    note = render_visible_progress(set(), context.evidence + "\n" + context.delta)
    assert 0 <= 1000 - used < len(note) + len("\n\n")
