from types import SimpleNamespace

import pytest

from app.services.prompts import (
    REPORT_SYNTHESIS_SCHEMA_HINT,
    report_synthesis_prompt,
)
from app.services.report_synthesis import (
    annotate_trend_evidence,
    blueprint_for_section,
    exclusive_frame_conflicts,
    fair_editor_context,
    localize_blueprint_evidence,
    normalize_claim_ledger,
    normalize_report_frame,
    normalize_synthesis_blueprint,
    synthesis_evidence_payload,
)


def _frame():
    return normalize_report_frame({
        "subject_kind": "模型实例",
        "facets": [{
            "id": "mixer", "name": "序列建模机制",
            "values": ["Attention", "SSM"], "exclusive": True,
        }],
        "axes": [{
            "id": "efficiency", "name": "效率",
            "condition_fields": ["上下文长度", "批量"],
        }],
        "instance_policy": "模型实例可同时具有不同层级机制",
    })


def test_frame_is_bounded_and_model_invalid_output_fails_open():
    frame = _frame()
    assert frame["facets"][0]["exclusive"] is True
    assert normalize_report_frame("bad") is None
    with pytest.raises(ValueError):
        normalize_report_frame({"facets": "bad"}, strict=True)


def test_synthesis_payload_is_grouped_by_section_not_source():
    hit = SimpleNamespace(
        object_id="o1", object_type="Claim", relevance=0.9,
        payload={"name": "Claim one", "definition": "Evidence body"},
    )
    element = SimpleNamespace(
        element_id="e1", score=0.8, source_title="Paper", text="Direct text",
    )
    result = SimpleNamespace(top_hits=[hit], elements=[element], chunks=[])
    payload, legal = synthesis_evidence_payload(
        [{"title": "A", "scope": "S", "intent_ids": ["i1"]}], [result]
    )
    assert payload[0]["section_id"] == "section-1"
    assert [row["evidence_id"] for row in payload[0]["evidence"]] == ["o1", "e1"]
    assert legal == {"o1", "e1"}


def test_synthesis_payload_honors_a_small_caller_context_ceiling():
    hit = SimpleNamespace(
        object_id="o1", object_type="Claim", relevance=0.9,
        payload={"name": "Claim", "definition": "x" * 900},
    )
    result = SimpleNamespace(top_hits=[hit], elements=[], chunks=[])

    payload, legal = synthesis_evidence_payload(
        [{"title": "A"}, {"title": "B"}], [result, result], max_chars=100
    )

    assert payload[0]["evidence"] == [] and payload[1]["evidence"] == []
    assert legal == set()


def _blueprint():
    return {
        "central_answer": "Use orthogonal dimensions.",
        "shared_definitions": [{
            "term": "Sequence mixer", "definition": "Token interaction mechanism",
            "evidence_keys": ["o1"],
        }],
        "claims": [{
            "id": "c1", "statement": "Attention is one mixer.", "type": "fact",
            "facet_id": "mixer", "evidence_keys": ["o1"],
            "counterevidence_keys": [], "conditions": [],
            "owner_section_id": "section-1",
        }],
        "sections": [{
            "section_id": "section-1", "thesis": "Separate the dimensions.",
            "claim_ids": ["c1"], "must_contrast": ["mixer vs capacity"],
            "handoff": "Next compare conditions.", "do_not_repeat": [],
        }],
    }


def _blueprint_with_claim_counts(counts):
    claims = []
    sections = []
    for section_index, count in enumerate(counts, 1):
        section_id = f"section-{section_index}"
        claim_ids = []
        for claim_index in range(1, count + 1):
            claim_id = f"c{section_index}-{claim_index}"
            claim_ids.append(claim_id)
            claims.append({
                "id": claim_id,
                "statement": f"Claim {section_index}-{claim_index}",
                "type": "fact",
                "facet_id": "",
                "evidence_keys": ["o1"],
                "counterevidence_keys": [],
                "conditions": [],
                "owner_section_id": section_id,
            })
        sections.append({
            "section_id": section_id,
            "thesis": f"Thesis {section_index}",
            "claim_ids": claim_ids,
            "must_contrast": [],
            "handoff": "",
            "do_not_repeat": [],
        })
    return {
        "central_answer": "Bounded blueprint",
        "shared_definitions": [],
        "claims": claims,
        "sections": sections,
    }


def _normalize_counted_blueprint(counts):
    return normalize_synthesis_blueprint(
        _blueprint_with_claim_counts(counts),
        outline=[{"title": f"S{index}"} for index in range(1, len(counts) + 1)],
        legal_evidence_ids={"o1"},
        frame=None,
    )


def test_blueprint_enforces_report_and_per_section_claim_limits():
    assert _normalize_counted_blueprint([12]) is not None
    assert _normalize_counted_blueprint([13]) is None
    assert _normalize_counted_blueprint([12, 12, 12, 12, 12]) is not None
    assert _normalize_counted_blueprint([12, 12, 12, 12, 12, 1]) is None


def test_blueprint_rejects_unknown_evidence_atomically():
    good = normalize_synthesis_blueprint(
        _blueprint(), outline=[{"title": "A"}],
        legal_evidence_ids={"o1"}, frame=_frame(),
    )
    assert good and blueprint_for_section(good, 0)["claims"][0]["id"] == "c1"
    bad = _blueprint()
    bad["claims"][0]["evidence_keys"] = ["invented"]
    assert normalize_synthesis_blueprint(
        bad, outline=[{"title": "A"}],
        legal_evidence_ids={"o1"}, frame=_frame(),
    ) is None


def test_blueprint_requires_every_confirmed_section_once():
    assert normalize_synthesis_blueprint(
        _blueprint(), outline=[{"title": "A"}, {"title": "B"}],
        legal_evidence_ids={"o1"}, frame=_frame(),
    ) is None


def test_report_wide_evidence_ids_are_translated_to_local_writer_anchors():
    section = blueprint_for_section(
        normalize_synthesis_blueprint(
            _blueprint(), outline=[{"title": "A"}],
            legal_evidence_ids={"o1"}, frame=_frame(),
        ),
        0,
    )
    localized = localize_blueprint_evidence(
        section, {"k7": {"object_id": "o1"}}
    )
    assert localized["claims"][0]["evidence_keys"] == ["k7"]


def test_claim_ledger_binds_exact_statement_and_same_sentence_anchor():
    statement = "Attention is one mixer [k1]."
    claims, status = normalize_claim_ledger([{
        "claim_id": "c1", "statement": statement, "type": "fact",
        "entities": ["Attention"], "evidence_keys": ["k1"],
        "conditions": [], "confidence": 0.8,
        "frame_assignments": {"mixer": "Attention"},
    }], markdown=f"## A\n\n{statement}", legal_anchor_keys={"k1"},
        blueprint_claim_ids={"c1"}, frame=_frame())
    assert status == "available"
    assert claims[0]["statement_hash"]

    _, invalid = normalize_claim_ledger([{
        "claim_id": "c1", "statement": "Attention is one mixer.", "type": "fact",
        "entities": ["Attention"], "evidence_keys": ["k1"],
    }], markdown="Attention is one mixer. Elsewhere [k1].",
        legal_anchor_keys={"k1"}, blueprint_claim_ids={"c1"}, frame=_frame())
    assert invalid == "invalid"


def test_trend_confidence_is_capped_by_distinguishable_cited_documents():
    claim = {
        "claim_id": "trend-1", "statement": "该路线必将全面替代现有架构 [k1]。",
        "type": "trend", "evidence_keys": ["k1"],
    }
    audited = annotate_trend_evidence(
        [claim],
        id_map={"k1": {"source_id": "s1"}},
        family_by_source={"s1": "paper-title:one"},
    )[0]
    assert audited["trend_level"] == "research"
    assert audited["trend_wording_violation"] is True

    supported = annotate_trend_evidence(
        [{**claim, "evidence_keys": ["k1", "k2", "k3"]}],
        id_map={
            "k1": {"source_id": "s1"},
            "k2": {"source_id": "s2"},
            "k3": {"source_id": "s3"},
        },
        family_by_source={"s1": "f1", "s2": "f2", "s3": "f3"},
    )[0]
    assert supported["trend_level"] == "high_confidence"
    assert supported["trend_wording_violation"] is False

    unknown = annotate_trend_evidence(
        [{**claim, "evidence_keys": ["k1", "k2", "k3"]}],
        id_map={"k1": {}, "k2": {}, "k3": {}},
        family_by_source={},
    )[0]
    assert unknown["independent_family_count"] == 0
    assert unknown["source_identity_unknown"] is True
    assert unknown["trend_level"] == "research"


def test_only_exclusive_frame_assignments_create_cross_section_conflicts():
    frame = _frame()
    sections = [{"claims": [{
        "entities": ["Mamba"], "frame_assignments": {"mixer": "SSM"},
    }]}, {"claims": [{
        "entities": ["Mamba"], "frame_assignments": {"mixer": "Attention"},
    }]}]
    assert "冲突取值" in exclusive_frame_conflicts(sections, frame)[0]
    frame["facets"][0]["exclusive"] = False
    assert exclusive_frame_conflicts(sections, frame) == []


def test_editor_context_includes_late_prose_and_claim_ledger():
    late = "LATE-LOAD-BEARING-CONCLUSION"
    section = {
        "title": "A",
        "markdown": "start " + ("x" * 5000) + late,
        "claims": [{"claim_id": "c1", "statement": late}],
        "claim_ledger_status": "available",
        "citation_audit": {"cited": 1, "total": 1},
    }
    block = fair_editor_context([section], max_chars=8000)
    assert late in block
    assert "claim_ledger_status" in block


def test_blueprint_narrows_id_value_composite_facet_to_legal_prefix():
    payload = _blueprint()
    payload["claims"][0]["facet_id"] = "mixer:attention"
    normalized = normalize_synthesis_blueprint(
        payload, outline=[{"title": "A"}],
        legal_evidence_ids={"o1"}, frame=_frame(),
    )
    assert normalized is not None
    assert blueprint_for_section(normalized, 0)["claims"][0]["facet_id"] == "mixer"


def test_blueprint_narrows_fullwidth_colon_composite_facet_to_legal_prefix():
    payload = _blueprint()
    payload["claims"][0]["facet_id"] = "mixer：attention"
    normalized = normalize_synthesis_blueprint(
        payload, outline=[{"title": "A"}],
        legal_evidence_ids={"o1"}, frame=_frame(),
    )
    assert normalized is not None
    assert blueprint_for_section(normalized, 0)["claims"][0]["facet_id"] == "mixer"


def test_blueprint_still_rejects_composite_facet_with_illegal_prefix():
    payload = _blueprint()
    payload["claims"][0]["facet_id"] = "capacity:foo"
    assert normalize_synthesis_blueprint(
        payload, outline=[{"title": "A"}],
        legal_evidence_ids={"o1"}, frame=_frame(),
    ) is None


def test_blueprint_clears_composite_facet_when_frame_has_no_facets():
    payload = _blueprint()
    payload["claims"][0]["facet_id"] = "family:Transformer"
    normalized = normalize_synthesis_blueprint(
        payload, outline=[{"title": "A"}],
        legal_evidence_ids={"o1"}, frame=None,
    )
    assert normalized is not None
    assert blueprint_for_section(normalized, 0)["claims"][0]["facet_id"] == ""


def test_synthesis_prompt_pins_facet_id_contract_and_schema_hint():
    prompt = report_synthesis_prompt("Q", "intent", "{}", "evidence")
    assert "never an `id:value` composite" in prompt
    assert '"facet_id":""' in REPORT_SYNTHESIS_SCHEMA_HINT
