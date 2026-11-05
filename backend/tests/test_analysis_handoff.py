"""Generic analysis snapshots survive updates and disclose missing support."""
import json
from dataclasses import FrozenInstanceError, replace

import pytest

from app.domain.analysis_handoff import (
    ANALYSIS_EVIDENCE_KEYS_MAX,
    ANALYSIS_FINDINGS_MAX,
    ANALYSIS_KEY_MAX_CHARS,
    ANALYSIS_TEXT_MAX_CHARS,
    ANALYSIS_UNRESOLVED_MAX,
    AnalysisEvidence,
    AnalysisFinding,
    AnalysisHandoff,
    analysis_evidence_keys,
    parse_analysis_update,
    render_analysis_handoff,
)


def update(**changes):
    finding = {
        "id": "constraint", "aspect": "适用条件", "conclusion": "仅在低温下成立",
        "status": "supported", "evidence_keys": ["chunk-cold", "element-limit"],
    }
    finding.update(changes)
    return {"findings": [finding], "unresolved": ["高温结果尚未确定"]}


def test_valid_update_is_an_immutable_complete_snapshot_with_host_stop_reason():
    old = AnalysisHandoff(stop_reason="budget_exhausted")
    result = parse_analysis_update(update(), old)
    assert result.findings == (AnalysisFinding(
        "constraint", "适用条件", "仅在低温下成立", "supported",
        ("chunk-cold", "element-limit"),
    ),)
    assert result.unresolved == ("高温结果尚未确定",)
    assert result.stop_reason == "budget_exhausted"
    with pytest.raises(FrozenInstanceError):
        result.stop_reason = "sufficient"
    replacement = parse_analysis_update({"findings": [], "unresolved": []}, result)
    assert not replacement.findings and not replacement.unresolved
    assert replacement.stop_reason == old.stop_reason


def test_absent_update_keeps_previous_analysis_without_adding_an_issue():
    old = parse_analysis_update(update(), AnalysisHandoff())
    assert parse_analysis_update(None, old) is old


@pytest.mark.parametrize("invalid", [
    [], "an analysis", {}, {"findings": [], "unresolved": "missing"},
    update(id=""), update(aspect=" "), update(conclusion=None),
    update(status="false"), update(evidence_keys="chunk-cold"),
    update(evidence_keys=[1]), update(evidence_keys=[""]),
    update(id="i" * (ANALYSIS_KEY_MAX_CHARS + 1)),
    update(evidence_keys=["r" * (ANALYSIS_KEY_MAX_CHARS + 1)]),
    update(evidence_keys=[str(i) for i in range(ANALYSIS_EVIDENCE_KEYS_MAX + 1)]),
    update(conclusion="x" * (ANALYSIS_TEXT_MAX_CHARS + 1)),
    dict(update(), unresolved=["gap"] * (ANALYSIS_UNRESOLVED_MAX + 1)),
    dict(update(), unresolved=["g" * (ANALYSIS_TEXT_MAX_CHARS + 1)]),
    {"findings": update()["findings"] * 2, "unresolved": []},
    {"findings": [dict(update()["findings"][0], id=str(i))
                  for i in range(ANALYSIS_FINDINGS_MAX + 1)], "unresolved": []},
])
def test_rejected_update_preserves_prior_findings_and_discloses_without_content(invalid):
    old = replace(parse_analysis_update(update(), AnalysisHandoff()),
                  stop_reason="retrieval_exhausted")
    rejected = parse_analysis_update(invalid, old)
    assert rejected.findings == old.findings
    assert rejected.unresolved == old.unresolved
    assert rejected.stop_reason == old.stop_reason
    assert rejected.issues == ("invalid_analysis_update",)
    assert parse_analysis_update(invalid, rejected) == rejected


def test_model_cannot_override_the_host_stop_reason():
    value = dict(update(), stop_reason="sufficient")
    result = parse_analysis_update(value, AnalysisHandoff(stop_reason="model_failed"))
    assert result.stop_reason == "model_failed"


def test_evidence_keys_follow_first_appearance_and_do_not_duplicate_support():
    value = update()
    value["findings"].append(dict(value["findings"][0], id="other",
                                  evidence_keys=["element-limit", "kg-final"]))
    result = parse_analysis_update(value, AnalysisHandoff())
    assert analysis_evidence_keys(result) == ("chunk-cold", "element-limit", "kg-final")


def test_rendering_remaps_actual_citations_and_marks_missing_evidence_for_review():
    analysis = replace(parse_analysis_update(update(), AnalysisHandoff()),
                       stop_reason="budget_exhausted")
    block = render_analysis_handoff(analysis, {"chunk-cold": "k18001"})
    assert "[k18001]" in block
    assert "高温结果尚未确定" in block
    assert "budget_exhausted" in block
    payload = json.loads(block.split("\n", 1)[1])
    finding = payload["findings"][0]
    assert finding["missing_evidence_keys"] == ["element-limit"]
    assert finding["review_required"] is True
    assert finding["available_evidence"] == [
        {"key": "chunk-cold", "citation": "[k18001]"},
    ]
    assert "not evidence" in block
    assert "uncertain" in block and "refuted" in block


def test_rendering_does_not_treat_a_malformed_binding_as_available_evidence():
    analysis = parse_analysis_update(update(), AnalysisHandoff())
    payload = json.loads(render_analysis_handoff(
        analysis, {"chunk-cold": "invented", "element-limit": "k2"},
    ).split("\n", 1)[1])
    assert payload["findings"][0]["missing_evidence_keys"] == ["chunk-cold"]


def test_stop_reason_and_rejected_update_are_visible_without_findings():
    analysis = AnalysisHandoff(stop_reason="model_failed", issues=("invalid_analysis_update",))
    block = render_analysis_handoff(analysis, {})
    assert "model_failed" in block and "invalid_analysis_update" in block
    assert not json.loads(block.split("\n", 1)[1])["findings"]


def test_dependency_envelope_keeps_identity_and_cannot_be_rebound():
    evidence = object()
    dependency = AnalysisEvidence("chunk", "chunk-cold", evidence)
    assert dependency.value is evidence
    with pytest.raises(FrozenInstanceError):
        dependency.key = "a-different-dependency"
