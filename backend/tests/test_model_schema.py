from __future__ import annotations

import importlib
import itertools
import json

import pytest
from jsonschema import Draft202012Validator

from app.core.model_schema import (
    StructuredModelResponseError,
    response_format_for_hint,
    validate_structured_response,
)
from app.services import prompts


def _format(hint: str) -> dict:
    response_format = response_format_for_hint(hint)
    assert response_format is not None
    Draft202012Validator.check_schema(response_format["json_schema"]["schema"])
    return response_format


def _valid(hint: str, payload) -> bool:
    return validate_structured_response(json.dumps(payload), _format(hint))


def _assert_strict_objects(schema: dict) -> None:
    if "object" in schema.get("type", []):
        assert schema["additionalProperties"] is False
        assert set(schema["required"]) == set(schema["properties"])
    for field in schema.get("properties", {}).values():
        _assert_strict_objects(field)
    if "items" in schema:
        _assert_strict_objects(schema["items"])
    for variant in schema.get("anyOf", []):
        _assert_strict_objects(variant)


def test_current_declared_hints_have_valid_strict_formats_or_explicit_safe_fallback():
    modules = (
        "app.services.prompts", "app.services.kg.extract",
        "app.services.concept_merge_review", "app.services.kg.conflict_review",
        "app.services.paper_meta", "app.services.chunk_question_index",
        "app.services.document_guide", "app.services.catalog_job",
        "app.services.knowhow.api", "app.services.kg.graph_reason",
    )
    unsupported = {
        ("app.services.prompts", "REPORT_SECTION_SCHEMA_HINT"),
        ("app.services.kg.conflict_review", "_SCHEMA"),
    }
    seen = set()
    for module_name in modules:
        module = importlib.import_module(module_name)
        for name, hint in vars(module).items():
            if not isinstance(hint, str) or not (name.endswith("SCHEMA_HINT") or name == "_SCHEMA"):
                continue
            identity = (module_name, name)
            seen.add(identity)
            result = response_format_for_hint(hint)
            if identity in unsupported:
                assert result is None, identity
                continue
            assert result is not None, identity
            assert result["type"] == "json_schema"
            assert result["json_schema"]["strict"] is True
            schema = result["json_schema"]["schema"]
            Draft202012Validator.check_schema(schema)
            _assert_strict_objects(schema)
    assert unsupported <= seen
    assert ("app.services.prompts", "AGENT_PROFILE_SCHEMA_HINT") in seen
    assert ("app.services.paper_meta", "PAPER_META_SCHEMA_HINT") in seen


def test_every_core_reflect_branch_combination_has_a_valid_schema():
    for outline, memory, chunks, kg in itertools.product((False, True), repeat=4):
        for kinds in ((), ("text", "table")):
            hint = prompts.reflect_schema_hint(
                kinds, ("Concept", "Claim"), outline, memory, chunks, kg,
            )
            schema = _format(hint)["json_schema"]["schema"]
            _assert_strict_objects(schema)


def test_required_answer_fields_remain_typed_and_closed():
    hint = prompts.ANSWER_SCHEMA_HINT
    assert _valid(hint, {"answer": "已找到答案", "grounded": False})
    for payload in (
        {"answer": "text"}, {"answer": None, "grounded": True},
        {"answer": [], "grounded": True}, {"answer": "text", "grounded": "false"},
        {"answer": "text", "grounded": 1},
        {"answer": "text", "grounded": True, "surprise": "extra"},
    ):
        assert not _valid(hint, payload), payload


def test_reflect_unused_branches_are_nullable_instead_of_mandatory_actions():
    hint = prompts.reflect_schema_hint(outline=True)
    schema = _format(hint)["json_schema"]["schema"]
    payload = dict.fromkeys(schema["properties"])
    payload.update(sufficient=True, next_action="answer", reason="证据充分")
    assert _valid(hint, payload)
    payload["sufficient"] = None
    assert not _valid(hint, payload)


def test_non_paper_response_can_leave_all_bibliography_fields_unused():
    from app.services.paper_meta import PAPER_META_SCHEMA_HINT

    schema = _format(PAPER_META_SCHEMA_HINT)["json_schema"]["schema"]
    payload = dict.fromkeys(schema["properties"])
    payload["is_paper"] = False
    assert _valid(PAPER_META_SCHEMA_HINT, payload)
    payload["is_paper"] = "false"
    assert not _valid(PAPER_META_SCHEMA_HINT, payload)


def test_agent_block_variants_preserve_both_updates_and_retirements():
    hint = prompts.AGENT_PROFILE_SCHEMA_HINT
    assert _valid(hint, {"blocks": [
        {"label": "key_entities", "value": "电路设计", "evidence": ["src-2"], "retire": None},
        {"label": "corpus_gaps", "value": None, "evidence": None, "retire": True},
    ]})
    assert not _valid(hint, {"blocks": [
        {"label": "corpus_gaps", "value": None, "evidence": None, "retire": "yes"},
    ]})


def test_heterogeneous_array_examples_merge_fields_without_freezing_sample_values():
    hint = (
        '{"entries":[{"op":"ADD","situation":"s0","action":"exact_lookup",'
        '"polarity":"bad","rationale":""},{"op":"NOOP","situation":"s1"}]}'
    )
    assert _valid(hint, {"entries": [
        {"op": "NOOP", "situation": "s9", "action": None, "polarity": None, "rationale": None},
        {"op": "ADD", "situation": "s42", "action": "search_chunks", "polarity": "good", "rationale": "有效"},
    ]})
    # Literal samples are neither enums nor constants; only pipe syntax is an enum.
    assert _valid('{"reference":"k5001","confidence":"low","count":0,"keep":true}', {
        "reference": "k9999", "confidence": "high", "count": 17, "keep": False,
    })


def test_pipe_enums_allow_the_unused_empty_value_but_reject_other_values():
    hint = '{"verdicts":[{"action":"keep|supplement|external"}]}'
    for action in ("", "keep", "supplement", "external"):
        assert _valid(hint, {"verdicts": [{"action": action}]})
    assert not _valid(hint, {"verdicts": [{"action": "invented_action"}]})
    assert not _valid(hint, {"verdicts": [{"action": 1}]})
    assert _valid('{"endpoint":"<source|target>"}', {"endpoint": "node-7"})


def test_required_root_enums_admit_only_the_listed_values():
    hint = '{"action":"keep|supplement|external"}'
    for action in ("keep", "supplement", "external"):
        assert _valid(hint, {"action": action})
    for action in ("", None, "invented_action"):
        assert not _valid(hint, {"action": action})
    # Root enum arrays are item vocabularies, not the root decision itself.
    assert _valid('{"kinds":["text|table"]}', {"kinds": [""]})


def test_reflect_decision_is_required_while_unused_branch_enums_stay_open():
    hint = prompts.reflect_schema_hint(("text", "table"), outline=True)
    schema = _format(hint)["json_schema"]["schema"]
    properties = schema["properties"]
    assert "" not in properties["next_action"]["enum"]
    # Optional root branches and nested enums keep "" and null for "unused".
    assert "" in properties["enumerate"]["properties"]["kind"]["enum"]
    payload = dict.fromkeys(properties)
    payload.update(sufficient=False, next_action="", reason="r")
    assert not _valid(hint, payload)
    payload["next_action"] = "answer"
    assert _valid(hint, payload)


@pytest.mark.parametrize("hint", [
    prompts.QUERY_INTENT_SCHEMA_HINT, prompts.REPORT_INTENT_SCHEMA_HINT,
])
def test_intent_classification_enums_admit_only_the_listed_values(hint):
    properties = _format(hint)["json_schema"]["schema"]["properties"]
    for field in ("intent_type", "result_scope"):
        assert "" not in properties[field]["enum"], field
        assert None not in properties[field]["enum"], field


def test_optional_root_enums_keep_the_unused_empty_value():
    # A paper-meta style root: every field but the classifier is optional.
    hint = '{"is_paper":true,"venue_kind":"journal|conference"}'
    assert _valid(hint, {"is_paper": False, "venue_kind": ""})
    assert _valid(hint, {"is_paper": False, "venue_kind": None})
    assert not _valid(hint, {"is_paper": None, "venue_kind": "journal"})


def test_resolved_payload_is_no_longer_a_placeholder_key():
    # A string-valued resolved_* field is ordinary text; only a null example
    # with an unknown type still falls back to JSON mode.
    assert _valid('{"resolved_payload":"name"}', {"resolved_payload": "其他名称"})
    assert response_format_for_hint('{"resolved_payload":null}') is None


@pytest.mark.parametrize("field", ["parent", "edge_type", "suggestion_md"])
def test_known_null_examples_describe_nullable_text(field):
    hint = json.dumps({field: None})
    assert _valid(hint, {field: None})
    assert _valid(hint, {field: "value"})
    assert not _valid(hint, {field: {"value": "text"}})


@pytest.mark.parametrize("field", [
    "keywords", "tags", "high_level_keywords", "low_level_keywords", "types", "region",
    "assumptions", "based_on_row_ids", "evidence_keys", "columns",
])
def test_known_empty_lists_allow_string_items_instead_of_only_empty_arrays(field):
    hint = json.dumps({field: []})
    assert _valid(hint, {field: []})
    assert _valid(hint, {field: ["first", "second"]})
    assert not _valid(hint, {field: [42]})


def test_spreadsheet_filters_use_the_explicit_scalar_cell_contract():
    hint = (
        '{"source_id":"id","sheet":"name","operation":"profile",'
        '"aggregation":"count","measure":"","group_by":"",'
        '"sort_by":"","order":"desc","filters":[],"columns":[]}'
    )
    payload = json.loads(hint)
    payload.update(operation="filter", columns=["Voltage"])
    for value in ("high", 1, 2.5, False, None):
        payload["filters"] = [{"column": "Voltage", "operator": "eq", "value": value}]
        assert _valid(hint, payload)
    payload["filters"][0]["operator"] = "execute"
    assert not _valid(hint, payload)
    payload["filters"][0].update(operator="eq", value={"unexpected": "container"})
    assert not _valid(hint, payload)


@pytest.mark.parametrize("hint", [
    "", "invalid JSON", "[]", "null", "true", "{}", '{"x": NaN}',
    '{"x": Infinity}', '{"x": 1e999}', '{"x":1,"x":2}',
    '{"source_queries":{"<source_id>":["<query>"]}}',
    '{"claims":[{"frame_assignments":{"facet-id":"value"}}]}',
    '{"frame_assignments":{"real-id":"value"}}',
    '{"resolved_payload":null}', '{"arbitrary":{}}',
    '{"unknown_list":[]}', '{"unknown_null":null}',
    '{"<node-id>":"value"}', '{"filters":[]}',
])
def test_unsupported_or_ambiguous_examples_preserve_legacy_fallback(hint):
    assert response_format_for_hint(hint) is None


def test_heterogeneous_scalar_array_examples_preserve_all_declared_types():
    hint = '{"values":["example",1,false]}'
    assert _valid(hint, {"values": ["new text", 2, True]})
    assert not _valid(hint, {"values": [2.5]})
    assert not _valid(hint, {"values": [{}]})


def test_response_names_follow_schema_identity_and_returns_are_independent():
    first = _format('{"answer":"sample","grounded":true}')
    equivalent = _format('{"answer":"different","grounded":false}')
    changed = _format('{"answer":"","grounded":""}')
    assert first["json_schema"]["name"] == equivalent["json_schema"]["name"]
    assert first["json_schema"]["name"] != changed["json_schema"]["name"]
    first["json_schema"]["schema"]["properties"].clear()
    assert equivalent["json_schema"]["schema"]["properties"]


@pytest.mark.parametrize("content", [
    '{"answer":"text","grounded":NaN}',
    '{"answer":"text","grounded":true,"answer":"other"}',
    '```json\n{"answer":"text","grounded":true}\n```',
    '{"answer":"text","grounded":true} trailing',
    '[{"answer":"text","grounded":true}]',
])
def test_raw_structured_validation_rejects_repairs_duplicates_and_non_finite_values(content):
    assert not validate_structured_response(content, _format(prompts.ANSWER_SCHEMA_HINT))


def test_structured_validation_rejects_overflow_even_when_schema_accepts_numbers():
    response_format = _format('{"confidence":0.0}')
    for content in ('{"confidence":1e999}', '{"confidence":Infinity}', '{"confidence":NaN}'):
        assert not validate_structured_response(content, response_format)
    assert validate_structured_response('{"confidence":0.75}', response_format)


def test_bad_response_formats_fail_validation_without_raising():
    for response_format in ({}, {"type": "json_object"}, {"type": "json_schema"}, {
        "type": "json_schema", "json_schema": {"schema": {"type": "invalid"}},
    }):
        assert not validate_structured_response('{}', response_format)


def test_structured_error_keeps_raw_content_out_of_public_exception_text():
    raw = '{"private": "source content"}'
    error = StructuredModelResponseError(raw)
    assert error.content == raw
    assert error.reason == "invalid_type"
    assert raw not in str(error)
    assert "source content" not in repr(error)
