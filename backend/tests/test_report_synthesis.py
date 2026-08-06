import inspect
from types import SimpleNamespace

import pytest

from app.services.prompts import (
    REPORT_SYNTHESIS_SCHEMA_HINT,
    report_section_prompt,
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
        frame=_frame())
    assert status == "available"
    assert claims[0]["statement_hash"]

    # A single submitted row whose statement is not verbatim prose has
    # nothing left to keep: the whole (non-empty) submission is invalid, not
    # because grounding is section-level, but because zero rows survived.
    _, invalid = normalize_claim_ledger([{
        "claim_id": "c1", "statement": "Attention is one mixer.", "type": "fact",
        "entities": ["Attention"], "evidence_keys": ["k1"],
    }], markdown="Attention is one mixer. Elsewhere [k1].",
        legal_anchor_keys={"k1"}, frame=_frame())
    assert invalid == "invalid"


def test_one_illegal_frame_assignment_degrades_itself_not_the_whole_ledger():
    kept = "Mamba uses an SSM mixer [k1]."
    reworded = "Transformer 使用 Attention 变体 [k1]。"
    markdown = f"## A\n\n{kept}\n\n{reworded}"
    ledger = [{
        "claim_id": "c1", "statement": kept, "type": "fact",
        "entities": ["Mamba"], "evidence_keys": ["k1"],
        "frame_assignments": {"mixer": "SSM"},
    }, {
        "claim_id": "c2", "statement": reworded, "type": "fact",
        "entities": ["Transformer"], "evidence_keys": ["k1"],
        # One declared value reworded, one facet key the frame never declared.
        "frame_assignments": {"mixer": "Attention变体", "invented": "x"},
    }]
    claims, status = normalize_claim_ledger(
        ledger, markdown=markdown, legal_anchor_keys={"k1"}, frame=_frame())

    assert status == "available"
    assert [row["claim_id"] for row in claims] == ["c1", "c2"]
    assert claims[0]["frame_assignments"] == {"mixer": "SSM"}
    assert claims[1]["frame_assignments"] == {}

    # A non-object payload is the same class of shape miss, and a frame that
    # declares no facets leaves the tag with no organizational meaning at all.
    shapeless = [{**ledger[0], "frame_assignments": ["mixer"]}]
    assert normalize_claim_ledger(
        shapeless, markdown=markdown, legal_anchor_keys={"k1"},
        frame=_frame()) == (
            [{**claims[0], "frame_assignments": {}}], "available")
    assert normalize_claim_ledger(
        ledger, markdown=markdown, legal_anchor_keys={"k1"},
        frame=None)[1] == "available"


def test_degraded_frame_tags_keep_trend_capping_and_conflict_detection_alive():
    """The whole point of dropping one tag instead of the ledger."""
    trend = "SSM 必将全面替代现有架构 [k1]。"
    tagged = "Mamba is an SSM model [k1]."
    claims, status = normalize_claim_ledger([{
        "claim_id": "c1", "statement": trend, "type": "trend",
        "entities": ["SSM"], "evidence_keys": ["k1"],
        "frame_assignments": {"mixer": "SSM 系"},
    }, {
        "claim_id": "c2", "statement": tagged, "type": "fact",
        "entities": ["Mamba"], "evidence_keys": ["k1"],
        "frame_assignments": {"mixer": "SSM"},
    }], markdown=f"{trend}\n\n{tagged}", legal_anchor_keys={"k1"}, frame=_frame())
    assert status == "available"

    audited = annotate_trend_evidence(
        claims, id_map={"k1": {"source_id": "s1"}}, family_by_source={"s1": "f1"},
    )
    assert audited[0]["trend_level"] == "research"
    assert audited[0]["trend_wording_violation"] is True

    elsewhere = {"claims": [{
        "entities": ["Mamba"], "frame_assignments": {"mixer": "Attention"},
    }]}
    assert "冲突取值" in exclusive_frame_conflicts(
        [{"claims": claims}, elsewhere], _frame())[0]


def test_illegal_evidence_grounding_discards_only_that_row():
    grounded = "Attention is one mixer [k1]."
    invented_anchor = "SSM is another mixer [k9]."
    markdown = f"## A\n\n{grounded}\n\n{invented_anchor}"
    ledger = [{
        "claim_id": "c1", "statement": grounded, "type": "fact",
        "entities": ["Attention"], "evidence_keys": ["k1"],
        "frame_assignments": {"mixer": "Attention"},
    }, {
        "claim_id": "c2", "statement": invented_anchor, "type": "fact",
        "entities": ["SSM"], "evidence_keys": ["k9"],
        "frame_assignments": {"mixer": "SSM"},
    }]
    assert normalize_claim_ledger(
        ledger, markdown=markdown, legal_anchor_keys={"k1", "k9"},
        frame=_frame())[1] == "available"
    # k9 is emitted in its own sentence but is not a legal anchor of this
    # section: only that one row is dropped, the audit layer for the rest of
    # the section (this is an audit over already-emitted prose, never a
    # drafting input) keeps working.
    claims, status = normalize_claim_ledger(
        ledger, markdown=markdown, legal_anchor_keys={"k1"}, frame=_frame())
    assert status == "partial"
    assert [row["claim_id"] for row in claims] == ["c1"]


def test_mixed_legal_and_illegal_rows_return_partial_not_all_or_nothing():
    legal = "Attention is one mixer [k1]."
    markdown = f"## A\n\n{legal}"
    ledger = [{
        "claim_id": "c1", "statement": legal, "type": "fact",
        "evidence_keys": ["k1"],
    }, {
        # Not verbatim prose (missing the trailing "[k1]." emitted above).
        "claim_id": "c2", "statement": "Attention is one mixer.", "type": "fact",
        "evidence_keys": ["k1"],
    }]
    claims, status = normalize_claim_ledger(
        ledger, markdown=markdown, legal_anchor_keys={"k1"}, frame=None)
    assert status == "partial"
    assert [row["claim_id"] for row in claims] == ["c1"]


def test_all_rows_illegal_or_empty_submission_are_invalid_missing_stays_missing():
    assert normalize_claim_ledger(
        "not-a-list", markdown="x", legal_anchor_keys=set(), frame=None
    ) == ([], "missing")

    # Zero rows audit nothing.  Deliberate change: an empty submission used to
    # count as "available", which inflated the receipt with vacuous ledgers.
    assert normalize_claim_ledger(
        [], markdown="x", legal_anchor_keys=set(), frame=None
    ) == ([], "invalid")

    all_illegal = [{
        "claim_id": "c1", "statement": "never emitted anywhere", "type": "fact",
        "evidence_keys": ["k1"],
    }, {
        "claim_id": "c2", "statement": "also never emitted", "type": "fact",
        "evidence_keys": ["k1"],
    }]
    assert normalize_claim_ledger(
        all_illegal, markdown="## A\nsomething else entirely",
        legal_anchor_keys={"k1"}, frame=None,
    ) == ([], "invalid")


def test_blueprint_claim_ids_parameter_is_gone_and_new_ids_are_legal():
    """A statement beyond any prior commitment is a legitimate new row.

    The function used to require every claim_id to have been pre-declared by
    a synthesis blueprint. That check is gone; pin the removal so it cannot
    silently come back and discard legitimately new claims again.
    """
    assert "blueprint_claim_ids" not in inspect.signature(normalize_claim_ledger).parameters

    statement = "This is prose the writer added beyond any commitment [k1]."
    claims, status = normalize_claim_ledger([{
        "claim_id": "brand-new-id", "statement": statement, "type": "fact",
        "evidence_keys": ["k1"],
    }], markdown=f"## A\n\n{statement}", legal_anchor_keys={"k1"}, frame=None)
    assert status == "available"
    assert claims[0]["claim_id"] == "brand-new-id"


def test_over_limit_submission_truncates_to_first_24_rows_as_partial():
    statements = [f"Claim number {index} holds [k1]." for index in range(1, 26)]
    markdown = "## A\n\n" + "\n\n".join(statements)
    ledger = [{
        "claim_id": f"c{index}", "statement": statement, "type": "fact",
        "evidence_keys": ["k1"],
    } for index, statement in enumerate(statements, 1)]

    claims, status = normalize_claim_ledger(
        ledger, markdown=markdown, legal_anchor_keys={"k1"}, frame=None)
    assert status == "partial"
    assert len(claims) == 24
    # The 25th row never even gets a look — it is dropped by truncation, not
    # a row-level violation, so it must not drag down rows 1-24.
    assert [row["claim_id"] for row in claims] == [f"c{index}" for index in range(1, 25)]


def test_truncated_window_with_zero_kept_rows_is_invalid_not_partial():
    # Zero kept rows means zero auditable content no matter how it happened:
    # the emptiness verdict outranks the truncation one.
    ledger = [{
        "claim_id": f"c{index}", "statement": "never emitted", "type": "fact",
        "evidence_keys": ["k1"],
    } for index in range(1, 26)]
    assert normalize_claim_ledger(
        ledger, markdown="## A\n\nOther prose [k1].",
        legal_anchor_keys={"k1"}, frame=None,
    ) == ([], "invalid")


def test_non_dict_row_is_dropped_not_fatal():
    legal = "Attention is one mixer [k1]."
    claims, status = normalize_claim_ledger(
        ["not-a-dict", {
            "claim_id": "c1", "statement": legal, "type": "fact",
            "evidence_keys": ["k1"],
        }],
        markdown=f"## A\n\n{legal}", legal_anchor_keys={"k1"}, frame=None)
    assert status == "partial"
    assert [row["claim_id"] for row in claims] == ["c1"]


def test_evidence_less_fact_row_is_dropped_not_fatal():
    legal = "Attention is one mixer [k1]."
    bare = "Mamba is another mixer."
    claims, status = normalize_claim_ledger(
        [{
            "claim_id": "c1", "statement": legal, "type": "fact",
            "evidence_keys": ["k1"],
        }, {
            "claim_id": "c2", "statement": bare, "type": "fact",
            "evidence_keys": [],
        }],
        markdown=f"## A\n\n{legal}\n\n{bare}",
        legal_anchor_keys={"k1"}, frame=None)
    assert status == "partial"
    assert [row["claim_id"] for row in claims] == ["c1"]


def test_dropped_row_does_not_reserve_its_claim_id():
    # The model may emit a broken draft of a row and then the corrected one
    # under the same id; the dropped draft must not make the good row a
    # "duplicate".  Pins seen-id bookkeeping to kept rows only.
    good = "Attention is one mixer [k1]."
    claims, status = normalize_claim_ledger(
        [{
            # Missing the trailing "[k1]." → not verbatim → dropped.
            "claim_id": "c3", "statement": "Attention is one mixer.",
            "type": "fact", "evidence_keys": ["k1"],
        }, {
            "claim_id": "c3", "statement": good, "type": "fact",
            "evidence_keys": ["k1"],
        }],
        markdown=f"## A\n\n{good}", legal_anchor_keys={"k1"}, frame=None)
    assert status == "partial"
    assert [row["claim_id"] for row in claims] == ["c3"]
    assert claims[0]["statement"] == good


def test_duplicate_claim_id_keeps_the_first_arriving_row():
    first = "Attention is one mixer [k1]."
    second = "SSM is another mixer [k1]."
    markdown = f"## A\n\n{first}\n\n{second}"
    ledger = [{
        "claim_id": "dup", "statement": first, "type": "fact",
        "evidence_keys": ["k1"],
    }, {
        "claim_id": "dup", "statement": second, "type": "fact",
        "evidence_keys": ["k1"],
    }]
    claims, status = normalize_claim_ledger(
        ledger, markdown=markdown, legal_anchor_keys={"k1"}, frame=None)
    assert status == "partial"
    assert len(claims) == 1
    assert claims[0]["statement"] == first


def test_comparison_without_conditions_drops_only_that_row():
    fact = "Attention is one mixer [k1]."
    comparison = "A is faster than B [k1]."
    markdown = f"## A\n\n{fact}\n\n{comparison}"
    ledger = [{
        "claim_id": "c1", "statement": fact, "type": "fact",
        "evidence_keys": ["k1"],
    }, {
        "claim_id": "c2", "statement": comparison, "type": "comparison",
        "evidence_keys": ["k1"], "conditions": [],
    }]
    claims, status = normalize_claim_ledger(
        ledger, markdown=markdown, legal_anchor_keys={"k1"}, frame=None)
    assert status == "partial"
    assert [row["claim_id"] for row in claims] == ["c1"]


def test_report_section_prompt_teaches_fresh_ids_for_uncovered_statements():
    prompt = report_section_prompt("T", "S", "Q", "CTX")
    assert "fresh unique ids" in prompt


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


def test_blueprint_facet_narrowing_pins_whitespace_and_edge_prefixes():
    # Surrounding whitespace and a value-less colon still narrow to the
    # declared prefix; an empty prefix stays on the conservative discard path.
    for tag in ("mixer : attention", "mixer ：attention", "mixer:"):
        payload = _blueprint()
        payload["claims"][0]["facet_id"] = tag
        normalized = normalize_synthesis_blueprint(
            payload, outline=[{"title": "A"}],
            legal_evidence_ids={"o1"}, frame=_frame(),
        )
        assert normalized is not None, tag
        assert blueprint_for_section(normalized, 0)["claims"][0]["facet_id"] == "mixer", tag

    payload = _blueprint()
    payload["claims"][0]["facet_id"] = ":attention"
    assert normalize_synthesis_blueprint(
        payload, outline=[{"title": "A"}],
        legal_evidence_ids={"o1"}, frame=_frame(),
    ) is None


def test_blueprint_facet_narrowing_prefers_longest_legal_prefix():
    # A frame may declare a facet id that itself contains a separator; the
    # longest declared prefix must win over a shorter accidental one.
    frame = _frame()
    frame["facets"].append({
        "id": "model", "name": "模型", "values": [], "exclusive": False,
    })
    frame["facets"].append({
        "id": "model:family", "name": "模型族", "values": [], "exclusive": False,
    })
    payload = _blueprint()
    payload["claims"][0]["facet_id"] = "model:family:Transformer"
    normalized = normalize_synthesis_blueprint(
        payload, outline=[{"title": "A"}],
        legal_evidence_ids={"o1"}, frame=frame,
    )
    assert normalized is not None
    facet = blueprint_for_section(normalized, 0)["claims"][0]["facet_id"]
    assert facet == "model:family"


def test_synthesis_prompt_pins_facet_id_contract_and_schema_hint():
    prompt = report_synthesis_prompt("Q", "intent", "{}", "evidence")
    assert "never an `id:value` composite" in prompt
    # The composite counter-example must stay a placeholder: a plausible slug
    # (`family:...`) in the only example slot invites the model to copy a bare
    # illegal id, which lands on the atomic-discard path this contract exists
    # to avoid.
    assert "family:Transformer" not in prompt
    assert '"facet_id":""' in REPORT_SYNTHESIS_SCHEMA_HINT
