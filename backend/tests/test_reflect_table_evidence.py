"""Evidence visibility regressions: table rows and query-driven supplements."""

from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.services.reasoning_context import (
    DELTA_BLOCK_TITLE, ReflectDeltaState, build_delta_evidence_block,
    build_evidence_block, render_pool_cards,
)
from app.services.reasoning_retrieval import (
    _build_delta_snapshot, _carried_delta, _delta_fallback,
    _delta_frozen_cards, _delta_supplements, _table_excerpt_chars,
)
from tests.test_reasoning_retrieval import (
    rrepo, _answer, _capture_contexts, _measured_run, _multi_marker_hit,
)


# Public paper Table 1, using the reconstructed source's actual separators.
# Its target rows follow many other models rather than fitting in the first
# 240 characters. Numeric cells are facts; no production fixture path is used.
TABLE_ONE = " ; ".join([
    "Results on standard tasks. Model | Param | Tokens | ARC-E | ARC-C | HellaSwag | MMLU | OBQA | PiQA | SciQ | WinoGrande",
    "random | | | 25.0 | 25.0 | 25.0 | 25.0 | 25.0 | 50.0 | 25.0 | 50.0",
    "Amber | 7B | 1.2T | 65.70 | 37.20 | 72.54 | 26.77 | 41.00 | 78.73 | 88.50 | 63.22",
    "Pythia-2.8b | 2.8B | 0.3T | 58.00 | 32.51 | 59.17 | 25.05 | 35.40 | 73.29 | 83.60 | 57.85",
    "Pythia-6.9b | 6.9B | 0.3T | 60.48 | 34.64 | 63.32 | 25.74 | 37.20 | 75.79 | 82.90 | 61.40",
    "Pythia-12b | 12B | 0.3T | 63.22 | 34.64 | 66.72 | 24.01 | 35.40 | 75.84 | 84.40 | 63.06",
    "OLMo-1B | 1B | 3T | 57.28 | 30.72 | 63.00 | 24.33 | 36.40 | 75.24 | 78.70 | 59.19",
    "OLMo-7B | 7B | 2.5T | 68.81 | 40.27 | 75.52 | 28.39 | 42.20 | 80.03 | 88.50 | 67.09",
    "OLMo-7B-0424 | 7B | 2.05T | 75.13 | 45.05 | 77.24 | 47.46 | 41.60 | 80.09 | 96.00 | 68.19",
    "OLMo-7B-0724 | 7B | 2.75T | 74.28 | 43.43 | 77.76 | 50.18 | 41.60 | 80.69 | 95.70 | 67.17",
    "OLMo-2-1124 | 7B | 4T | 82.79 | 57.42 | 80.50 | 60.56 | 46.20 | 81.18 | 96.40 | 74.74",
    "Ours, (r = 4) | 3.5B | 0.8T | 49.07 | 27.99 | 43.46 | 23.39 | 28.20 | 64.96 | 80.00 | 55.24",
    "Ours, (r = 8) | 3.5B | 0.8T | 65.11 | 35.15 | 58.54 | 25.29 | 35.40 | 73.45 | 92.10 | 55.64",
    "Ours, (r = 16) | 3.5B | 0.8T | 69.49 | 37.71 | 64.67 | 31.25 | 37.60 | 75.79 | 93.90 | 57.77",
    "Ours, (r = 32) | 3.5B | 0.8T | 69.91 | 38.23 | 65.21 | 31.38 | 38.80 | 76.22 | 93.50 | 59.43",
])


def chunk(key="table", text=TABLE_ONE):
    return SimpleNamespace(chunk_id=key, text=text, source_title="Paper",
                           source_id="source", section_path="Results", relevance=1)


def cards(items, query, **kwargs):
    return dict(render_pool_cards(
        collected={}, elements=[], chunks=items,
        keys=[item.chunk_id for item in items], question="", action_query=query,
        excerpt_chars=240, **kwargs))


def test_real_long_table_retains_headers_empty_cells_and_tail_comparison_rows():
    old = cards([chunk()], "OLMo Ours")["table"]
    new = cards([chunk()], "OLMo Ours", table_excerpt_chars=1200)["table"]
    assert "表头:" not in old
    assert "表头: Model │ Param │ Tokens │ ARC-E │ ARC-C" in new
    assert "Ours, (r = 32) │ 3.5B │ 0.8T │ 69.91 │ 38.23" in new
    assert "OLMo-1B │ 1B │ 3T │ 57.28 │ 30.72" in new
    assert "局部摘录" in new and "行未展开" in new
    assert len(new.partition("“")[2].removesuffix("”")) <= 1200
    for row in new.splitlines():
        if row.strip().startswith("行:"):
            assert len(row.split(" │ ")) == 11


def test_markdown_table_uses_query_labels_without_model_name_rules():
    rows = ["| Device | Latency ms | Energy J |", "| --- | ---: | ---: |"]
    rows += [f"| filler-{i} | {i + 10}.5 | {i + 20}.7 |" for i in range(80)]
    rows += ["| target-z | 3.25 | 8.75 |", "| control-q | 5.00 | 2.50 |"]
    rendered = cards([chunk(text="\n".join(rows))], "target-z control-q",
                     table_excerpt_chars=400)["table"]
    assert "表头: Device │ Latency ms │ Energy J" in rendered
    assert "行: target-z │ 3.25 │ 8.75" in rendered
    assert "行: control-q │ 5.00 │ 2.50" in rendered
    assert "行未展开" in rendered


@pytest.mark.parametrize("text", [
    "ordinary text without a table " * 40,
    "A | B\n| --- | --- |\nwrong | number | columns",
    "Group | Scores | Scores\nModel | Metric | Metric\n--- | --- | ---\nA | 10 | 20",
    r"Label \| alias | Score ; A | 10 ; B | 20",
])
def test_unrecognized_or_ragged_table_keeps_the_old_excerpt(text):
    items = [chunk(text=text)]
    assert cards(items, "number", table_excerpt_chars=1200) == cards(items, "number")


def test_tight_card_and_total_delta_budgets_fall_back_without_splitting_rows():
    items = [chunk()]
    old = cards(items, "OLMo")["table"]
    assert cards(items, "OLMo", table_excerpt_chars=1200,
                 budget_chars=len(old))["table"] == old
    for budget in (200, 400, 600, 1500):
        selection = build_delta_evidence_block(
            collected={}, elements=[], chunks=items, bound_keys=[],
            fresh_keys=["table"], already_shown=[], question="OLMo", action_query="",
            budget_chars=budget, excerpt_chars=240, max_cards=2,
            table_excerpt_chars=1200)
        assert not selection.text or len(selection.text) + len(DELTA_BLOCK_TITLE) + 1 <= budget


def test_snapshot_omission_disclosure_is_inside_the_evidence_arm_hard_budget():
    selection = build_evidence_block(
        collected={}, elements=[], chunks=[chunk(str(i)) for i in range(3)],
        chains=[], bound_keys=[], fresh_keys=[], question="OLMo Ours", action_query="",
        budget_chars=609, excerpt_chars=240, table_excerpt_chars=1200)
    assert len(selection.text) <= 609
    assert len(selection.shown_keys) + selection.omitted == 3
    assert tuple(key for key, _ in selection.cards) == selection.shown_keys
    assert all(f"key={key}" in selection.text for key in selection.shown_keys)


@pytest.mark.parametrize("text", [
    "Latency in milliseconds, lower is better. Model | Test A | Test B ; X | 10 | 20 ; Y | 30 | 40",
    "Latency in milliseconds, lower is better.\n| Model | Test A | Test B |\n| --- | --- | --- |\n| X | 10 | 20 |\n| Y | 30 | 40 |",
])
def test_table_caption_units_and_comparison_direction_are_never_removed(text):
    new = cards([chunk(text=text)], "X Y", table_excerpt_chars=1200)["table"]
    assert "Latency in milliseconds, lower is better." in new
    assert "表头: Model │ Test A │ Test B" in new
    assert "行: X │ 10 │ 20" in new
    # A long adjacent qualifier must not be trimmed to make a table fit.
    long = text.replace("Latency in milliseconds", "unit qualifier " * 120)
    assert cards([chunk(text=long)], "X Y", table_excerpt_chars=240) == cards([chunk(text=long)], "X Y")


def test_table_budget_configuration_is_validated_and_only_read_for_new_arm(monkeypatch):
    monkeypatch.setenv("REASONING_REFLECT_TABLE_EXCERPT_CHARS", "1500")
    assert Settings().reasoning_reflect_table_excerpt_chars == 1500
    monkeypatch.setenv("REASONING_REFLECT_TABLE_EXCERPT_CHARS", "239")
    with pytest.raises(ValueError):
        Settings()
    class NoRead:
        @property
        def reasoning_reflect_table_excerpt_chars(self):
            raise AssertionError("old arms must not read the new budget")
    for arm in ("off", "prefix_snapshot", "prefix_delta", "prefix_delta_lean"):
        assert _table_excerpt_chars(NoRead(), arm) == 0


def supplement_state():
    hits = [_multi_marker_hit(f"ck-{index}") for index in range(4)]
    state = SimpleNamespace(collected={}, elements=[], chunks=hits, chains=[], question="")
    delta = ReflectDeltaState()
    for key, text in cards(hits, "甲方向").items():
        delta.note_shown(key, text)
    delta.snapshot_keys = {"ck-0", "ck-1", "ck-2"}
    delta.snapshot_evidence = "immutable K"
    delta.blocks = ["immutable D"]
    return state, delta, SimpleNamespace(last_query="乙方向", rows=[])


def supplement(state, delta, observer, **kwargs):
    return _delta_supplements(
        state, delta, observer, candidate_keys=[], max_cards=1,
        excerpt_chars=240, table_excerpt_chars=1200,
        **kwargs)


def test_unbound_visible_keys_continue_rotation_and_append_only_on_new_content():
    state, delta, observer = supplement_state()
    frozen = dict(delta.frozen_cards)
    assert supplement(state, delta, observer, budget_left=5000) == ()  # old arm
    lines = []
    for _ in range(3):
        lines += supplement(state, delta, observer, budget_left=5000,
                            visible_fallback=True)
    assert len(lines) == 3
    assert all("补充摘录 v2" in line for line in lines)
    assert delta.supplement_checked_keys == delta.snapshot_keys
    assert delta.card_versions["ck-3"] == 1  # previously seen, no longer visible
    assert supplement(state, delta, observer, budget_left=5000,
                      visible_fallback=True) == ()
    assert delta.snapshot_evidence == "immutable K" and delta.blocks == ["immutable D"]
    assert delta.frozen_cards == frozen
    observer.last_query = "丙方向"
    assert supplement(state, delta, observer, budget_left=5000,
                      visible_fallback=True)


def test_budget_rejection_does_not_consume_a_supplement_or_its_sweep_slot():
    state, delta, observer = supplement_state()
    assert supplement(state, delta, observer, budget_left=10,
                      visible_fallback=True) == ()
    assert not delta.supplement_checked_keys
    assert all(version == 1 for version in delta.card_versions.values())
    assert supplement(state, delta, observer, budget_left=5000,
                      visible_fallback=True)


def test_rebuild_and_fallback_reuse_the_last_emitted_supplement_bytes():
    state, delta, observer = supplement_state()
    line, = supplement(state, delta, observer, budget_left=5000,
                       visible_fallback=True)
    original = dict(delta.frozen_cards)
    selection = _build_delta_snapshot(
        state, delta, observer, bound_keys=["ck-0"], fresh_keys=[],
        budget=5000, state_chars=1000, excerpt_chars=240, recent=6,
        ratio=0.5, table_excerpt_chars=1200)
    assert line in selection.text
    assert delta.frozen_cards == original
    delta.blocks.append("retained D")
    carried = _carried_delta(delta)
    _delta_fallback(delta, carried)
    assert delta.blocks == ["retained D"]
    assert _delta_frozen_cards(delta, 1200)["ck-0"] == line
    assert _delta_frozen_cards(delta, 0) == original  # existing arms


def test_query_cycle_restores_compacted_excerpt_and_deduplicates_current_versions():
    state, delta, observer = supplement_state()
    state.chunks = state.chunks[:1]
    delta.snapshot_keys = {"ck-0"}
    delta.snapshot_evidence = delta.frozen_cards["ck-0"]
    delta.blocks.clear()
    second, = supplement(state, delta, observer, budget_left=5000,
                         visible_fallback=True)
    delta.blocks.append(second)
    _build_delta_snapshot(
        state, delta, observer, bound_keys=["ck-0"], fresh_keys=[],
        budget=5000, state_chars=1000, excerpt_chars=240, recent=6,
        ratio=0.5, table_excerpt_chars=1200)
    assert second in delta.snapshot_evidence and delta.blocks == []
    before = delta.snapshot_evidence
    observer.last_query = "甲方向"
    restored, = supplement(state, delta, observer, budget_left=5000,
                           visible_fallback=True)
    assert "补充摘录 v3" in restored
    assert delta.snapshot_evidence == before
    delta.blocks.append(restored)
    observer.last_query = "甲方向 other-unmatched-term"
    assert supplement(state, delta, observer, budget_left=5000,
                      visible_fallback=True) == ()
    observer.last_query = "乙方向"
    assert supplement(state, delta, observer, budget_left=5000,
                      visible_fallback=True) == ()  # still present in K
    _build_delta_snapshot(
        state, delta, observer, bound_keys=["ck-0"], fresh_keys=[],
        budget=5000, state_chars=1000, excerpt_chars=240, recent=6,
        ratio=0.5, table_excerpt_chars=1200)
    # The query stayed the same, but rebuilding from the last emitted (A)
    # removed B. A previous sweep must not mask that visibility change.
    fourth, = supplement(state, delta, observer, budget_left=5000,
                         visible_fallback=True)
    assert "补充摘录 v4" in fourth


def test_real_run_supplements_same_key_after_query_change_without_assessment(
    rrepo, monkeypatch,
):
    contexts = _capture_contexts(monkeypatch)
    hit = _multi_marker_hit("ck-0")
    llm, result = _measured_run(
        rrepo, optimization="prefix_delta_evidence", question="完整问题",
        reflects=[
            {"next_action": "search_chunks", "sufficient": False,
             "arguments": {"query": "乙方向"}},
            _answer(),
        ],
        chunk_results={"完整问题": [hit], "乙方向": [hit]},
        reasoning_max_chunk_searches=2)
    assert len(contexts) == 2
    assert contexts[0].evidence == contexts[1].evidence
    assert contexts[1].delta.startswith(contexts[0].delta)
    assert "补充摘录 v2" in contexts[1].delta
    assert "key=ck-0" in contexts[1].delta
    assert len(set(llm.system_prompts)) == 1
    assert not result.termination.assessment_enabled
