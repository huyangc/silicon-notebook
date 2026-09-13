"""Bounded paragraph reading and progress measured from actual visible prose."""

import json
from dataclasses import replace
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.services.reasoning_context import (
    EVIDENCE_BLOCK_TITLE, ReflectDeltaState, accept_fallback_detail_cards, build_evidence_block,
    prepare_fallback_detail_cards, render_pool_cards, select_excerpt,
    select_prose_detail_cards,
)
from app.services.reasoning_prose_excerpt import (
    render_visible_progress, select_prose_excerpt, visible_excerpt_identities,
    visible_progress_reserve,
)
from app.services.reasoning_retrieval import (
    ReasoningRetriever, _build_delta_snapshot, _carried_delta, _delta_cards,
    _delta_fallback, _delta_supplements, _prose_detail_cards,
)
from tests.test_reasoning_retrieval import (
    rrepo, _answer, _capture_contexts, _chunk_hit, _measured_run,
    _multi_marker_hit,
)
from tests.test_reflect_table_evidence import TABLE_ONE


PARAGRAPH = (
    "State cache sharing can reduce the memory required for iterative decoding. "
    "Ordinary fixed-depth networks attach an independent cache to each block. "
    "The recurrent model instead applies the same transformation repeatedly, "
    "so its projections have matching coordinates across iterations. "
    "This is distinct from evicting earlier words or reusing another request. "
    "The cache budget specifies how many recurrence states each token retains. "
    "At iteration i, read and write the slot i modulo k in the circular buffer. "
    "For example, iteration seventeen overwrites slot one when k is sixteen. "
    "This direct reuse requires no additional training of the model or a scorer. "
    "The method can be used on its own or together with adaptive computation. "
    "Reported experiments used a dialogue benchmark; this does not establish "
    "identical accuracy for every task and every cache budget."
)


def hit(key="mechanism", text=PARAGRAPH, source="source-a", relevance=0.8):
    return replace(_chunk_hit(key, source_id=source, relevance=relevance),
                   text=text, section_path="Implementation")


def details(items, query="State cache sharing", **extra):
    return select_prose_detail_cards(
        collected={}, elements=[], chunks=items, question=query, action_query="",
        excerpt_chars=240, table_excerpt_chars=1200,
        detail_chars=extra.pop("detail_chars", 1000),
        max_cards=extra.pop("max_cards", 2),
        budget_chars=extra.pop("budget_chars", 8000), **extra)


def snapshot(items, detail_cards, budget=4000, **extra):
    return build_evidence_block(
        collected={}, elements=[], chunks=items, chains=[],
        bound_keys=extra.pop("bound_keys", []),
        fresh_keys=extra.pop("fresh_keys", [item.chunk_id for item in items]),
        question="State cache sharing", action_query="", budget_chars=budget,
        excerpt_chars=240, table_excerpt_chars=1200, detail_cards=detail_cards,
        **extra)


def test_short_english_mechanism_exposes_continuation_without_claiming_a_full_source():
    item = hit()
    assert 850 < len(PARAGRAPH) < 1000
    old = snapshot([item], None)
    new = snapshot([item], details([item]))
    assert "i modulo k" not in old.text and "局部摘录" in old.text
    assert PARAGRAPH in new.text
    assert "i modulo k" in new.text and "requires no additional training" in new.text
    assert "局部摘录" not in new.text  # this paragraph, not the whole source
    assert new.shown_keys == old.shown_keys == ("mechanism",)


def test_chinese_mechanism_and_source_diversity_share_one_detail_allowance():
    body = ("循环缓存记录每个输入位置的迭代状态。" + "背景说明不改变输入长度。" * 24
            + "第 i 轮读取并写入 i mod k 槽位；第 k+1 轮覆盖第一轮的状态，无需额外训练。")
    first = hit("cn-1", body, "cn-a", 0.9)
    second = hit("cn-2", body + "另一段介绍。", "cn-a", 0.8)
    peer = hit("peer", body + "另一来源的独立观测。", "cn-b", 0.7)
    chosen = details([first, second, peer], "循环缓存", max_cards=2)
    assert set(chosen) == {"cn-1", "peer"}
    assert all("第 k+1 轮覆盖第一轮" in card for card in chosen.values())
    assert "cn-2" not in chosen


def test_unrelated_long_material_and_kg_summaries_do_not_receive_details():
    assert details([hit(text="unrelated ambient measurements. " * 50)]) == {}
    item = SimpleNamespace(object_id="kg", payload={"definition": PARAGRAPH})
    assert select_prose_detail_cards(
        collected={"kg": item}, elements=[], chunks=[], question="State cache",
        action_query="", excerpt_chars=240, detail_chars=1000,
        max_cards=2, table_excerpt_chars=1200, budget_chars=8000) == {}


@pytest.mark.parametrize("limit", [240, 400, 1000])
def test_long_prose_windows_keep_sentence_and_formula_boundaries_explicit(limit):
    formula = r"The buffer rule is $b_i = b_{i \bmod k}$ and needs no new weights."
    body = "Unrelated introductory sentence. " * 40 + formula + " Additional discussion." * 60
    excerpt, partial = select_prose_excerpt(
        body, ["buffer", "weights"], limit, select_window=select_excerpt)
    assert partial and len(excerpt) <= limit
    assert formula in excerpt and excerpt.count("$") % 2 == 0
    assert excerpt.startswith("…") and excerpt.endswith("…")


def test_overlong_formula_is_not_cut_into_a_different_complete_expression():
    body = "Intro. $" + "cache + " * 100 + "z$ End."
    excerpt, partial = select_prose_excerpt(
        body, ["cache"], 240, select_window=select_excerpt)
    assert partial and len(excerpt) <= 240
    assert excerpt.count("$") % 2 == 0
    assert "cache +" not in excerpt


@pytest.mark.parametrize("budget", [1000, 1200, 2000, 4000, 8000])
def test_detail_budget_adapts_without_losing_fresh_or_true_binding(budget):
    bound = hit("bound", "State cache: verified outline support.")
    fresh = hit("fresh", "New independent measurement.", "source-b")
    items = [bound, hit(), fresh, *[hit(f"noise-{i}") for i in range(30)]]
    selected = snapshot(items, details(items, budget_chars=budget), budget=budget,
                        bound_keys=["bound"], fresh_keys=["fresh"])
    assert selected.shown_keys[0] == "bound"
    assert "fresh" in selected.shown_keys and len(selected.text) <= budget
    assert selected.omitted > 0
    if budget <= 1200:
        assert details(items, budget_chars=budget) == {}
        assert selected == snapshot(items, None, budget=budget,
                                    bound_keys=["bound"], fresh_keys=["fresh"])


def test_tables_retain_their_complete_rows_and_do_not_spend_prose_slots():
    table = hit("table", TABLE_ONE, relevance=0.99)
    prose = hit()
    chosen = details([table, prose], "State cache sharing OLMo Ours")
    assert set(chosen) == {"mechanism"}
    kwargs = dict(collected={}, elements=[], chunks=[table, prose], keys=["table"],
                  question="OLMo Ours", action_query="", excerpt_chars=240,
                  table_excerpt_chars=1200)
    assert render_pool_cards(**kwargs, detail_cards=chosen) == render_pool_cards(**kwargs)


def test_existing_short_card_gets_an_append_only_detail_and_reuses_it_after_rebuild():
    item = hit()
    state = SimpleNamespace(collected={}, elements=[], chunks=[item], chains=[],
                            question="State cache sharing")
    observer = SimpleNamespace(last_query="State cache sharing", rows=[])
    old = snapshot([item], None)
    delta = ReflectDeltaState(snapshot_evidence=old.text,
                              snapshot_keys=set(old.shown_keys))
    for key, text in old.cards:
        delta.note_shown(key, text)
    chosen = details([item])
    lines = _delta_supplements(
        state, delta, observer, candidate_keys=[], max_cards=2,
        excerpt_chars=240, budget_left=3000, table_excerpt_chars=1200,
        visible_fallback=True, detail_cards=chosen)
    assert len(lines) == 1 and "补充摘录 v2" in lines[0]
    assert "i modulo k" in lines[0] and delta.snapshot_evidence == old.text
    assert delta.frozen_cards == dict(old.cards)
    delta.blocks.extend(lines)
    assert _delta_supplements(
        state, delta, observer, candidate_keys=[], max_cards=2,
        excerpt_chars=240, budget_left=3000, table_excerpt_chars=1200,
        visible_fallback=True, detail_cards=chosen) == ()
    _build_delta_snapshot(
        state, delta, observer, bound_keys=[], fresh_keys=[], budget=8000,
        state_chars=6000, excerpt_chars=240, recent=6, ratio=0.5,
        table_excerpt_chars=1200, detail_cards=chosen)
    assert lines[0] in delta.snapshot_evidence
    assert delta.frozen_cards == dict(old.cards)
    # A recovery reuses the emitted bytes, but a removed pool key never returns.
    delta.snapshot_keys.clear()
    recovered = _delta_cards(
        state, delta, observer, bound_keys=[], fresh_keys=[], budget=8000,
        max_cards=2, excerpt_chars=240, table_excerpt_chars=1200, detail_cards=chosen)
    assert recovered.cards == (("mechanism", lines[0]),)
    state.chunks = []
    assert not _delta_cards(
        state, delta, observer, bound_keys=[], fresh_keys=[], budget=8000,
        max_cards=2, excerpt_chars=240, table_excerpt_chars=1200,
        detail_cards=chosen).text


def test_full_paragraph_does_not_acquire_a_shrinking_supplement_after_query_change():
    item = hit()
    chosen = details([item])
    first = snapshot([item], chosen)
    delta = ReflectDeltaState(snapshot_evidence=first.text,
                              snapshot_keys=set(first.shown_keys))
    for key, text in first.cards:
        delta.note_shown(key, text)
    state = SimpleNamespace(collected={}, elements=[], chunks=[item], chains=[],
                            question="State cache sharing")
    # The paragraph is no longer one of this turn's detail candidates. Its
    # already displayed full text still covers the smaller query window.
    observer = SimpleNamespace(last_query="seventeen adaptive", rows=[])
    assert _delta_supplements(
        state, delta, observer, candidate_keys=[], max_cards=2,
        excerpt_chars=240, budget_left=3000, table_excerpt_chars=1200,
        visible_fallback=True, detail_cards={}) == ()
    assert delta.snapshot_evidence == first.text and not delta.blocks
    assert delta.card_versions == {"mechanism": 1}


def test_elements_use_the_same_detail_rule_and_removed_keys_ignore_cached_details():
    element = SimpleNamespace(element_id="element-a", text=PARAGRAPH,
                              source_id="source-a", source_title="Article",
                              location_label="Mechanism", relevance=0.8)
    chosen = select_prose_detail_cards(
        collected={}, elements=[element], chunks=[], question="State cache sharing",
        action_query="", excerpt_chars=240, detail_chars=1000, max_cards=2,
        table_excerpt_chars=1200, budget_chars=8000)
    assert "i modulo k" in chosen["element-a"]
    assert render_pool_cards(collected={}, elements=[], chunks=[],
                             keys=["element-a"], question="State cache sharing",
                             action_query="", excerpt_chars=240,
                             detail_cards=chosen) == ()


@pytest.mark.parametrize("budget", [600, 2000])
@pytest.mark.parametrize("bound_keys", [[], ["bound"]])
def test_detail_cannot_steal_a_bound_or_eligible_supplement_slot(budget, bound_keys):
    items = [hit("detail"), _multi_marker_hit("bound")]
    state = SimpleNamespace(collected={}, elements=[], chunks=items, chains=[],
                            question="State cache sharing")
    first = dict(render_pool_cards(
        collected={}, elements=[], chunks=items, keys=["detail", "bound"],
        question="", action_query="甲方向", excerpt_chars=240))
    delta = ReflectDeltaState(snapshot_evidence="\n".join(first.values()),
                              snapshot_keys=set(first))
    for key, text in first.items():
        delta.note_shown(key, text)
    observer = SimpleNamespace(last_query="乙方向", rows=[])
    chosen = details(items)
    assert len(chosen["detail"]) > 600
    lines = _delta_supplements(
        state, delta, observer, candidate_keys=bound_keys, max_cards=1,
        excerpt_chars=240, budget_left=budget, table_excerpt_chars=1200,
        visible_fallback=True, detail_cards=chosen)
    # A real binding always leads. With no binding, an oversized detail falls
    # back to the already visible short view and must not spend the only slot.
    if bound_keys or budget == 600:
        assert len(lines) == 1 and "key=bound" in lines[0]
        assert "乙方向" in lines[0]
    else:
        assert len(lines) == 1 and "key=detail" in lines[0]


@pytest.mark.parametrize("budget", [400, 4000])
@pytest.mark.parametrize("retained_in_delta", [False, True])
def test_fallback_previews_detail_and_only_registers_a_version_after_final_admission(
    budget, retained_in_delta,
):
    item = hit()
    original = dict(render_pool_cards(
        collected={}, elements=[], chunks=[item], keys=["mechanism"],
        question="Unmatched question", action_query="", excerpt_chars=240))
    delta = ReflectDeltaState(fallback=True)
    delta.note_shown("mechanism", original["mechanism"])
    if retained_in_delta:
        delta.blocks.append(original["mechanism"])
        delta.block_keys.add("mechanism")
    blocks = tuple(delta.blocks)
    chosen = details([item])
    versions = dict(delta.card_versions)
    views, pending, upgrades = prepare_fallback_detail_cards(
        delta, delta.frozen_cards, chosen, delta.block_keys, "\n".join(delta.blocks))
    assert delta.card_versions == versions  # a preview is not a sent view
    selected = snapshot([item], chosen, budget=budget, frozen_cards=views,
                        exclude_keys=delta.block_keys - upgrades,
                        bound_keys=["mechanism"], fresh_keys=[],
                        frozen_fallback_cards={key: text for key, text in original.items()
                                               if key not in delta.block_keys})
    accept_fallback_detail_cards(delta, selected, pending)
    assert tuple(delta.blocks) == blocks and delta.frozen_cards == original
    if budget == 400:
        if retained_in_delta:
            assert not selected.text  # its short card is still visible in D
        else:
            assert selected.shown_keys == ("mechanism",)
            assert selected.cards == tuple(original.items())
            assert len(selected.text) <= budget
        assert delta.card_versions == versions
        assert not delta.supplement_latest_cards
    else:
        assert "i modulo k" in selected.text and "补充摘录 v2" in selected.text
        assert delta.card_versions == {"mechanism": 2}
        again, pending, _ = prepare_fallback_detail_cards(
            delta, delta.frozen_cards, chosen, delta.block_keys, "\n".join(delta.blocks))
        repeated = snapshot([item], chosen, budget=budget, frozen_cards=again)
        accept_fallback_detail_cards(delta, repeated, pending)
        assert repeated.text == selected.text and not pending
        assert delta.card_versions == {"mechanism": 2}
    # A cached preview never grants a removed source admission.
    denied = snapshot([], chosen, budget=4000, frozen_cards=views)
    accept_fallback_detail_cards(delta, denied, pending)
    assert not denied.shown_keys


@pytest.mark.parametrize("room_for_detail", [False, True])
def test_large_fallback_detail_does_not_hide_a_smaller_remaining_binding(room_for_detail):
    items = [hit(), hit("bound", "Independent verified condition. " * 12)]
    original = dict(render_pool_cards(
        collected={}, elements=[], chunks=items, keys=["mechanism", "bound"],
        question="Unmatched question", action_query="", excerpt_chars=240))
    delta = ReflectDeltaState(fallback=True)
    for key, text in original.items():
        delta.note_shown(key, text)
    chosen = details(items)
    views, pending, _ = prepare_fallback_detail_cards(
        delta, original, chosen, set(), "")
    # Both cards fit exactly; the previous observed-minimum shortcut must not
    # treat the large first detail as the minimum size of every following card.
    budget_views = views if room_for_detail else original
    budget = len(EVIDENCE_BLOCK_TITLE) + sum(len(text) + 1 for text in budget_views.values())
    selected = snapshot(items, chosen, budget=budget, frozen_cards=views,
                        frozen_fallback_cards=original,
                        bound_keys=["mechanism", "bound"], fresh_keys=[])
    assert selected.shown_keys == ("mechanism", "bound")
    assert len(selected.text) <= budget
    accept_fallback_detail_cards(delta, selected, pending)
    assert delta.card_versions == {"mechanism": 2 if room_for_detail else 1, "bound": 1}


def test_fallback_keeps_binding_when_disclosure_makes_the_detail_too_large():
    item = hit()
    original = dict(render_pool_cards(
        collected={}, elements=[], chunks=[item], keys=["mechanism"],
        question="Unmatched question", action_query="", excerpt_chars=240))
    delta = ReflectDeltaState(fallback=True)
    delta.note_shown("mechanism", original["mechanism"])
    chosen = details([item])
    views, pending, _ = prepare_fallback_detail_cards(delta, original, chosen, set(), "")
    # The detail fits until the omitted-candidate disclosure is included.
    budget = len(EVIDENCE_BLOCK_TITLE) + len(views["mechanism"]) + 1
    selected = snapshot([item, hit("noise", "Independent observation. " * 30)], chosen,
                        budget=budget, frozen_cards=views, frozen_fallback_cards=original,
                        bound_keys=["mechanism"], fresh_keys=[])
    assert selected.shown_keys == ("mechanism",) and selected.omitted == 1
    assert selected.cards == tuple(original.items())
    assert len(selected.text) <= budget
    accept_fallback_detail_cards(delta, selected, pending)
    assert delta.card_versions == {"mechanism": 1} and not delta.supplement_latest_cards


def test_disclosure_downgrades_an_earlier_detail_before_removing_a_later_binding():
    items = [hit(), hit("bound", "Independent verified condition. " * 12),
             hit("noise", "Unrelated observation. " * 25)]
    original = dict(render_pool_cards(
        collected={}, elements=[], chunks=items, keys=["mechanism", "bound"],
        question="Unmatched question", action_query="", excerpt_chars=240))
    delta = ReflectDeltaState(fallback=True)
    for key, text in original.items():
        delta.note_shown(key, text)
    chosen = details(items)
    views, pending, _ = prepare_fallback_detail_cards(delta, original, chosen, set(), "")
    budget = len(EVIDENCE_BLOCK_TITLE) + sum(len(text) + 1 for text in views.values())
    selected = snapshot(items, chosen, budget=budget, frozen_cards=views,
                        frozen_fallback_cards=original,
                        bound_keys=["mechanism", "bound"], fresh_keys=[])
    assert selected.shown_keys == ("mechanism", "bound")
    assert selected.cards == tuple(original.items()) and selected.omitted == 1
    assert len(selected.text) <= budget
    accept_fallback_detail_cards(delta, selected, pending)
    assert delta.card_versions == {"mechanism": 1, "bound": 1}
    assert not delta.supplement_latest_cards


@pytest.mark.parametrize("enabled", [False, True])
def test_real_fallback_reads_a_previously_short_paragraph_without_mutating_old_bytes(
    rrepo, monkeypatch, enabled,
):
    contexts, states = [], []
    original_context = ReasoningRetriever._reflect_v2_context

    def capture(self, state, summary, outline):
        context = original_context(self, state, summary, outline)
        contexts.append(context)
        delta = state.reflect_delta
        states.append((dict(delta.frozen_cards), tuple(delta.blocks), dict(delta.card_versions)))
        if len(contexts) == 1:
            _delta_fallback(delta, _carried_delta(delta))
        return context

    monkeypatch.setattr(ReasoningRetriever, "_reflect_v2_context", capture)
    calls = []
    llm, result = _measured_run(
        rrepo, optimization="prefix_delta_evidence", question="Unmatched question", calls=calls,
        reflects=[{"next_action": "search_chunks", "sufficient": False,
                   "arguments": {"query": "State cache sharing"}}, _answer()],
        chunk_results={"Unmatched question": [hit()], "State cache sharing": [hit()]},
        reasoning_reflect_prose_detail_enabled=enabled, reasoning_max_chunk_searches=2)
    assert len(llm.message_lists) == len(calls) == 2
    assert states[0][0] == states[1][0] and states[0][1] == states[1][1]
    assert "i modulo k" not in contexts[0].evidence
    if enabled:
        assert "i modulo k" in contexts[1].evidence
        assert "补充摘录 v2" in contexts[1].evidence
        assert "首次展示的摘录视图：1" in contexts[1].turn_state
        assert states[1][2] == {"mechanism": 2}
    else:
        assert "i modulo k" not in contexts[1].evidence
        assert "可见证据变化" not in contexts[1].turn_state
        assert states[1][2] == {"mechanism": 1}
    assert not result.termination.assessment_enabled


def test_visible_progress_ignores_metadata_versions_recovery_and_unshown_candidates():
    item = hit()
    short = snapshot([item], None).text
    expanded = snapshot([item], details([item])).text
    seen = set()
    assert "首次展示的摘录视图：1" in render_visible_progress(seen, short)
    before = set(seen)
    changed_header = short.replace("Doc", "Renamed source").replace(
        " | key=mechanism | ", " | key=mechanism | 补充摘录 v8（上文同 key 的卡未被改写） | ")
    assert "首次展示的摘录视图：0" in render_visible_progress(seen, changed_header)
    assert before == seen
    assert "首次展示的摘录视图：1" in render_visible_progress(seen, short + "\n" + expanded)
    assert "首次展示的摘录视图：0" in render_visible_progress(seen, "")
    restored = render_visible_progress(seen, expanded)
    assert "首次展示的摘录视图：0" in restored and "零新增不等于问题已解决" in restored
    assert len(restored) <= visible_progress_reserve(8000)
    assert visible_excerpt_identities(short + "\n工具新增 50 个候选") == before
    # Source-controlled Unicode cannot turn this projection into model fallback.
    assert visible_excerpt_identities(short.replace("State", "State\ud800"))


def test_configuration_is_validated_and_old_arms_do_not_read_it(monkeypatch):
    monkeypatch.setenv("REASONING_REFLECT_PROSE_DETAIL_ENABLED", "false")
    assert not Settings().reasoning_reflect_prose_detail_enabled
    for name, invalid in (("CHARS", "239"), ("CHARS", "4001"),
                          ("CARDS", "0"), ("CARDS", "5")):
        with monkeypatch.context() as local:
            local.setenv("REASONING_REFLECT_PROSE_DETAIL_" + name, invalid)
            with pytest.raises(ValueError):
                Settings()
    class NoRead:
        def __getattr__(self, _):
            raise AssertionError("Other arms must not read prose configuration")
    for arm in ("off", "prefix_snapshot", "prefix_delta", "prefix_delta_lean"):
        assert _prose_detail_cards(NoRead(), arm, None, None, 240, 0, 8000) is None
    assert _prose_detail_cards(SimpleNamespace(reasoning_reflect_prose_detail_enabled=False),
                               "prefix_delta_evidence", None, None, 240, 1200, 8000) is None


@pytest.mark.parametrize("enabled", [False, True])
def test_real_run_reads_mechanism_once_without_extra_calls_or_assessment(rrepo, monkeypatch, enabled):
    contexts = _capture_contexts(monkeypatch)
    question, item = "State cache sharing", hit()
    calls = []
    llm, result = _measured_run(
        rrepo, optimization="prefix_delta_evidence", question=question, calls=calls,
        reflects=[{"next_action": "search_chunks", "sufficient": False,
                   "arguments": {"query": "State cache"}}, _answer()],
        chunk_results={question: [item], "State cache": [item]},
        reasoning_reflect_prose_detail_enabled=enabled,
        reasoning_max_chunk_searches=2)
    assert len(llm.message_lists) == len(calls) == 2
    assert contexts[0].evidence == contexts[1].evidence
    assert contexts[1].delta.startswith(contexts[0].delta)
    assert len(set(llm.system_prompts)) == 1
    assert not result.termination.assessment_enabled
    assert all("assessment" not in json.loads(hint) for hint in llm.schema_hints)
    if enabled:
        assert "i modulo k" in contexts[0].evidence + contexts[0].delta
        assert "首次展示的摘录视图：1" in contexts[0].turn_state
        assert "首次展示的摘录视图：0" in contexts[1].turn_state
    else:
        assert all("i modulo k" not in context.evidence for context in contexts)
        assert all("可见证据变化" not in context.turn_state for context in contexts)


def test_real_query_change_exposes_detail_with_no_new_pool_key(rrepo, monkeypatch):
    contexts = _capture_contexts(monkeypatch)
    item = hit()
    calls = []
    llm, result = _measured_run(
        rrepo, optimization="prefix_delta_evidence", question="Unmatched question", calls=calls,
        reflects=[{"next_action": "search_chunks", "sufficient": False,
                   "arguments": {"query": "State cache sharing"}}, _answer()],
        chunk_results={"Unmatched question": [item], "State cache sharing": [item]},
        reasoning_max_chunk_searches=2)
    assert len(llm.message_lists) == len(calls) == 2
    assert "i modulo k" not in contexts[0].evidence
    assert "i modulo k" in contexts[1].delta
    assert "补充摘录 v2" in contexts[1].delta
    assert contexts[0].evidence == contexts[1].evidence
    assert "首次展示的摘录视图：1" in contexts[1].turn_state
    assert not result.termination.assessment_enabled
