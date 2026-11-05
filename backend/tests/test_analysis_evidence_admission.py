"""Analysis dependencies are admitted whole or explicitly reported missing."""
from types import SimpleNamespace

import pytest

from app.domain.analysis_handoff import AnalysisEvidence, AnalysisFinding, AnalysisHandoff
from app.services.analysis_handoff import (
    ANALYSIS_EVIDENCE_KEY_BASE,
    assemble_analysis_evidence,
)
from app.services.context_spans import entry_lines, recorded_spans, recording_spans


def _analysis(*keys):
    return AnalysisHandoff(findings=(AnalysisFinding(
        "f", "comparison", "qualified assessment", "supported", tuple(keys)),))


def _evidence(key, text="complete passage", kind="chunk"):
    return AnalysisEvidence(kind, key, SimpleNamespace(text=text))


def _renderer(calls):
    def render(kind, item, *, id_offset, budget_chars):
        calls.append((kind, item, id_offset, budget_chars))
        handle = f"k{id_offset + 1}"
        block = entry_lines([f"{handle}: {item.text}"])
        return block, {handle: {"object_id": item.key}}
    return render


def _run(keys, evidence=(), context="", id_map=None, budget=1000, offset=0, render=None):
    if render is None:
        def render(*args, **kwargs):
            pytest.fail("unnecessary dependency rendering")
    return assemble_analysis_evidence(
        context, {} if id_map is None else id_map, _analysis(*keys), evidence,
        budget_chars=budget, key_offset=offset, render=render,
    )


def test_reuses_current_handles_and_discloses_unknown_refs_without_hydration():
    ids = {"k10001": {"object_id": "known"}, "k16001": {"element_id": "external"}}
    result = _run(("known", "external", "unknown"), id_map=ids)
    assert result.bindings == {"known": "k10001", "external": "k16001"}
    assert result.missing == ("unknown",)
    assert result.added_chars == 0
    assert result.id_map == ids and result.id_map is not ids


def test_retains_dependencies_in_first_use_order_and_uses_section_namespace():
    calls = []
    evidence = [_evidence("second"), _evidence("first")]
    for item in evidence:
        item.value.key = item.key
    result = _run(("first", "unknown", "second", "first"), evidence,
                  context="existing", offset=10000, render=_renderer(calls))
    assert result.bindings == {
        "first": f"k{10000 + ANALYSIS_EVIDENCE_KEY_BASE + 1}",
        "second": f"k{10000 + ANALYSIS_EVIDENCE_KEY_BASE + 3}",
    }
    assert result.missing == ("unknown",)
    assert [row[1].key for row in calls] == ["first", "second"]
    assert result.added_chars == len(result.context_block) - len("existing")
    assert result.added_chars <= 1000


@pytest.mark.parametrize("kind", ["chunk", "element"])
def test_partial_base_evidence_does_not_count_as_complete_support(kind):
    evidence = _evidence("passage", "original text and indispensable condition", kind)
    ids = {"k1": {"object_id": "passage"}}
    result = _run(("passage",), [evidence], context="k1: original text…", id_map=ids, budget=0)
    assert result.bindings == {}
    assert result.missing == ("passage",)
    assert ids == {"k1": {"object_id": "passage"}}
    assert result.context_block == "k1: original text…"


def test_full_base_evidence_is_reused_without_spending_extra_budget():
    evidence = _evidence("passage")
    result = _run(("passage",), [evidence], context="k1: complete passage",
                  id_map={"k1": {"element_id": "passage"}}, budget=0)
    assert result.bindings == {"passage": "k1"} and result.missing == ()


def test_partial_native_render_is_rejected_as_a_whole():
    calls = []
    def render(kind, item, **kwargs):
        calls.append(kind)
        return "k8001: original…", {"k8001": {"object_id": "passage"}}
    result = _run(("passage",), [_evidence("passage", "original qualified text")], render=render)
    assert calls == ["chunk"]
    assert result.context_block == "" and result.id_map == {}
    assert result.bindings == {} and result.missing == ("passage",)


def test_oversized_native_render_cannot_exceed_budget():
    evidence = _evidence("passage", "x" * 500)
    evidence.value.key = evidence.key
    result = _run(("passage",), [evidence], budget=100, render=_renderer([]))
    assert result.context_block == "" and result.added_chars == 0
    assert result.missing == ("passage",)


@pytest.mark.parametrize("rendered_map", [{}, {"k8001": {"object_id": "different"}}])
def test_withheld_or_folded_dependency_is_not_falsely_bound(rendered_map):
    result = _run(("wanted",), [_evidence("wanted", kind="knowledge")],
                  render=lambda *args, **kwargs: ("k8001: native result", rendered_map))
    assert result.missing == ("wanted",) and result.bindings == {}
    assert result.context_block == ""


def test_preserves_recorded_entry_spans_for_evidence_output():
    evidence = _evidence("passage")
    evidence.value.key = evidence.key
    with recording_spans():
        original = entry_lines(["k1: base evidence"])
        result = _run(("passage",), [evidence], context=original,
                      id_map={"k1": {"object_id": "base"}}, render=_renderer([]))
        keys = [span.key for span in recorded_spans(result.context_block) if span.key]
    assert keys == ["k1", f"k{ANALYSIS_EVIDENCE_KEY_BASE + 1}"]


def test_rejects_renderer_handle_collision_without_overwriting_base_map():
    ids = {"k8001": {"object_id": "base"}}
    result = _run(("wanted",), [_evidence("wanted", kind="knowledge")], id_map=ids,
                  render=lambda *args, **kwargs: ("k8001: new evidence", {"k8001": {"object_id": "wanted"}}))
    assert result.id_map == ids and result.missing == ("wanted",)
    assert result.context_block == ""
