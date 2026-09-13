"""Previously read evidence survives compaction or re-enters append-only D."""

from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.services.reasoning_context import (
    DELTA_BLOCK_TITLE, ReflectDeltaState, build_delta_evidence_block,
    build_evidence_block, render_pool_cards, select_frozen_card_views,
)
from app.services.reasoning_retrieval import (
    ReasoningRetriever, _build_delta_snapshot, _delta_cards,
)
from tests.test_reflect_table_evidence import TABLE_ONE
from tests.test_reasoning_retrieval import (
    rrepo, _answer, _chunk_hit, _measured_run,
)


def hit(key, text, relevance=0.5):
    return replace(_chunk_hit(key, relevance=relevance), text=text)


def freeze(items, question):
    return dict(render_pool_cards(
        collected={}, elements=[], chunks=items,
        keys=[item.chunk_id for item in items], question=question,
        action_query="", excerpt_chars=240, table_excerpt_chars=1200))


def test_rebuild_retains_real_comparison_table_and_leaves_room_for_new_evidence():
    question = "本文模型在标准基准上的结果如何，与 OLMo 相比怎样？"
    table = hit("comparison", TABLE_ONE, 0.7)
    frozen = freeze([table], question)
    noise = [hit(f"noise-{i}", (
        f"Table {i + 8}. Training dataset distribution and reference sources. "
        "Corpus | Tokens | Fraction ; Web | 100 | 20 ; Code | 400 | 80"
    ), 0.95) for i in range(24)]
    state = SimpleNamespace(collected={}, elements=[], chunks=[table, *noise],
                            chains=[], question=question)
    delta = ReflectDeltaState()
    delta.note_shown("comparison", frozen["comparison"])
    delta.snapshot_keys.add("comparison")
    observer = SimpleNamespace(rows=(), last_query="Training dataset distribution")
    selection = _build_delta_snapshot(
        state, delta, observer, bound_keys=[],
        fresh_keys=[item.chunk_id for item in noise], budget=8000,
        state_chars=6000, excerpt_chars=240, recent=6, ratio=0.5,
        table_excerpt_chars=1200)
    assert selection.shown_keys[0] == "comparison"
    assert frozen["comparison"] in selection.text
    assert "OLMo-7B │ 7B │ 2.5T │ 68.81" in selection.text
    assert "Ours, (r = 32) │ 3.5B │ 0.8T │ 69.91" in selection.text
    assert any(key.startswith("noise-") for key in selection.shown_keys)
    assert len(selection.text) <= 4000
    assert selection.omitted > 0
    # Existing modes keep their original fresh-first selection unchanged.
    old = build_evidence_block(
        collected={}, elements=[], chunks=[table, *noise], chains=[],
        bound_keys=[], fresh_keys=[item.chunk_id for item in noise],
        question=question, action_query=observer.last_query,
        budget_chars=4000, excerpt_chars=240, frozen_cards=frozen)
    assert old.shown_keys[0] == "noise-0"


def test_recovery_and_fresh_both_get_admission_and_old_modes_do_not_recover():
    historical = [hit(f"old-{i}", f"target-z result {i}: 17.5 versus 20.0")
                  for i in range(8)]
    fresh = [hit(f"new-{i}", f"new observation {i}") for i in range(8)]
    frozen = freeze(historical, "target-z")
    args = dict(
        collected={}, elements=[], chunks=[*historical, *fresh], bound_keys=[],
        fresh_keys=[item.chunk_id for item in fresh], already_shown=[],
        question="comparison", action_query="target-z", budget_chars=2000,
        excerpt_chars=240, max_cards=2, frozen_cards=frozen)
    selected = build_delta_evidence_block(**args, table_excerpt_chars=1200)
    assert selected.shown_keys == ("new-0", "old-0")
    assert len(selected.text) + len(DELTA_BLOCK_TITLE) + 1 <= 2000
    assert build_delta_evidence_block(**args).shown_keys == ("new-0", "new-1")
    # A repeated query only considers the currently invisible remainder.
    args["already_shown"] = selected.shown_keys
    again = build_delta_evidence_block(**args, table_excerpt_chars=1200)
    assert again.shown_keys == ("new-1", "old-1")


@pytest.mark.parametrize("binding", [False, True])
def test_one_remaining_card_slot_keeps_real_binding_or_fresh_ahead_of_recovery(binding):
    old = hit("old", "target-z measured comparison")
    current = hit("current", "independent newly relevant material")
    selected = build_delta_evidence_block(
        collected={}, elements=[], chunks=[old, current],
        bound_keys=["current"] if binding else [],
        fresh_keys=[] if binding else ["current"], already_shown=[],
        question="comparison", action_query="target-z", budget_chars=2000,
        excerpt_chars=240, max_cards=1, frozen_cards=freeze([old], "target-z"),
        table_excerpt_chars=1200)
    assert selected.shown_keys == ("current",)


def test_true_outline_binding_precedes_retained_cards_without_exceeding_budget():
    bound = hit("bound", "outline support")
    table = hit("table", TABLE_ONE, 0.99)
    fresh = hit("new", "new facts")
    selected = build_evidence_block(
        collected={}, elements=[], chunks=[table, bound, fresh], chains=[],
        bound_keys=["bound"], fresh_keys=["new"], question="OLMo", action_query="",
        budget_chars=2000, excerpt_chars=240, table_excerpt_chars=1200,
        frozen_cards=freeze([table], "OLMo"))
    assert selected.shown_keys[0] == "bound"
    assert "new" in selected.shown_keys and len(selected.text) <= 2000


def test_rebuild_can_restore_original_comparison_view_after_unrelated_supplement():
    original = {"same": "target-z comparison 17.5 versus 20.0"}
    latest = {"same": "supplement about training-data distributions"}
    assert select_frozen_card_views(original, latest, ["target-z"]) == original
    assert select_frozen_card_views(original, latest, ["training-data"]) == latest
    assert select_frozen_card_views(original, latest, []) == latest


@pytest.mark.parametrize("budget", [0, 100, 300, 500, 1500])
def test_recovery_uses_latest_bytes_and_never_reinstates_removed_pool_items(budget):
    kept = hit("kept", "target-z comparison 13 versus 21")
    removed = hit("removed", "target-z source no longer admitted")
    state = SimpleNamespace(collected={}, elements=[], chunks=[kept],
                            question="comparison")
    delta = ReflectDeltaState()
    for key, text in freeze([kept, removed], "target-z").items():
        delta.note_shown(key, text)
    newer = delta.supplement_for("kept", delta.frozen_cards["kept"] + " updated")
    delta.supplement_latest_cards["kept"] = newer
    delta.snapshot_evidence = "unchanged K"
    delta.blocks = ["unchanged D"]
    before = (dict(delta.frozen_cards), dict(delta.card_versions))
    selected = _delta_cards(
        state, delta, SimpleNamespace(last_query="target-z"),
        bound_keys=[], fresh_keys=[], budget=budget, max_cards=2,
        excerpt_chars=240, table_excerpt_chars=1200)
    assert "removed" not in selected.shown_keys
    if selected.text:
        assert selected.cards == (("kept", newer),)
        assert len(selected.text) + len(DELTA_BLOCK_TITLE) + 1 <= budget
    assert before == (delta.frozen_cards, delta.card_versions)
    assert delta.snapshot_evidence == "unchanged K"
    assert delta.blocks == ["unchanged D"]


def test_unrelated_or_unqueried_history_does_not_fill_delta():
    item = hit("old", "target-z measurements")
    for query in ("", "unrelated-w"):
        selected = build_delta_evidence_block(
            collected={}, elements=[], chunks=[item], bound_keys=[], fresh_keys=[],
            already_shown=[], question="target-z", action_query=query,
            budget_chars=2000, excerpt_chars=240, max_cards=2,
            frozen_cards=freeze([item], "target-z"), table_excerpt_chars=1200)
        assert not selected.text and not selected.shown_keys


def test_real_turns_restore_a_hidden_repeat_hit_without_extra_search_or_model_calls(
    rrepo, monkeypatch,
):
    question = "main comparison"
    primary = hit("primary", "main comparison " + "alpha observation " * 30, 0.9)
    secondary = hit("secondary", "recovery-z " + "secondary observation " * 30, 0.1)
    fresh = [hit(f"fresh-{i}", f"noise-{i} " + "new observation " * 30)
             for i in range(8)]
    captures = []
    original = ReasoningRetriever._reflect_v2_context

    def capture(self, state, summary, outline):
        context = original(self, state, summary, outline)
        delta = state.reflect_delta
        captures.append((context.evidence, context.delta, delta.rebuilds,
                         delta.evidence_chars, set(delta.frozen_cards),
                         state.record.observer.fresh_result_ids()))
        return context

    monkeypatch.setattr(ReasoningRetriever, "_reflect_v2_context", capture)
    queries = ("noise batch", "next batch", "recovery-z")
    calls = []
    llm, result = _measured_run(
        rrepo, optimization="prefix_delta_evidence", question=question,
        calls=calls, reflects=[{
            "next_action": "search_chunks", "sufficient": False,
            "arguments": {"query": query}, "reason": "inspect evidence",
        } for query in queries] + [_answer()],
        chunk_results={question: [primary, secondary],
                       queries[0]: fresh[:4], queries[1]: fresh[4:],
                       queries[2]: [secondary]},
        reasoning_max_chunk_searches=3,
        reasoning_reflect_delta_cards_by_effort={"standard": 1},
        reasoning_reflect_evidence_chars_by_effort={"standard": 1200},
        reasoning_reflect_state_chars=6000)
    assert len(llm.message_lists) == 4
    assert len(calls) == 4  # one seed and exactly the three chosen searches
    assert "key=secondary" in captures[0][0] + captures[0][1]
    assert any("key=secondary" not in evidence + delta and rebuilds > 0
               for evidence, delta, rebuilds, *_ in captures[1:-1]), captures
    assert "secondary" not in captures[-1][-1]  # repeat hit remains non-fresh
    assert "key=secondary" in captures[-1][1], captures
    assert captures[-1][0] == captures[-2][0]
    assert captures[-1][1].startswith(captures[-2][1])
    assert all(chars <= 1200 for _, _, _, chars, *_ in captures)
    assert not result.termination.assessment_enabled
