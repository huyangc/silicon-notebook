from __future__ import annotations

import json

import pytest

from app.core.model_json import (
    ModelJsonRepairError,
    parse_model_json_object,
    validate_model_json_shape,
)


ANSWER_SCHEMA = '{"answer":"","grounded":true}'
REFLECT_SCHEMA = (
    '{"sufficient":false,"next_action":"answer|expand_graph","reason":""}'
)
PLAN_SCHEMA = '{"sub_queries":[{"query":"","types":[]}]}'
OPTIONAL_SCHEMA = '{"edge_type":null}'


@pytest.mark.parametrize(
    ("raw", "schema", "expected"),
    [
        (
            '{answer: "完整答案 [k1]", grounded: true}',
            ANSWER_SCHEMA,
            {"answer": "完整答案 [k1]", "grounded": True},
        ),
        (
            "{'answer': 'single quoted answer', 'grounded': false,}",
            ANSWER_SCHEMA,
            {"answer": "single quoted answer", "grounded": False},
        ),
        (
            '{sufficient: false next_action: answer, reason: "enough"}',
            REFLECT_SCHEMA,
            {"sufficient": False, "next_action": "answer", "reason": "enough"},
        ),
    ],
)
def test_conservative_repair_recovers_common_complete_object_faults(
    raw, schema, expected
):
    result = parse_model_json_object(raw, schema, allow_repair=True)

    assert result.repaired is True
    assert json.loads(result.content) == expected


def test_valid_json_is_returned_byte_for_byte():
    raw = '{ "answer": "原样保留", "grounded": true }'

    result = parse_model_json_object(raw, ANSWER_SCHEMA, allow_repair=True)

    assert result.repaired is False
    assert result.content == raw


@pytest.mark.parametrize("raw", ["{}", '{"unrelated": 1}'])
def test_a_reply_with_none_of_the_expected_keys_is_rejected(raw):
    # The one usability gate the shared boundary keeps: a reply naming none
    # of the hint's top-level fields cannot be consumed by anyone.
    with pytest.raises(ModelJsonRepairError) as caught:
        validate_model_json_shape(raw, ANSWER_SCHEMA)

    assert caught.value.reason == "missing_expected_key"


@pytest.mark.parametrize(
    ("raw", "schema", "expected"),
    [
        (
            '{"answer": [], "grounded": true}', ANSWER_SCHEMA,
            [("answer", "invalid_type", "")],
        ),
        (
            '{"items":[{}]}', '{"items":[{"index":0}]}',
            [("items[0]", "missing_expected_key", "")],
        ),
        (
            '{"sufficient": false, "next_action": "delete_all"}', REFLECT_SCHEMA,
            [("next_action", "invalid_enum", "")],
        ),
        (
            '{"sub_queries": "not-a-list"}', PLAN_SCHEMA,
            [("sub_queries", "invalid_type", "")],
        ),
        (
            '{"sub_queries": [{"query": 5, "types": {"a": 1}}]}', PLAN_SCHEMA,
            [
                ("sub_queries[0].query", "invalid_type", ""),
                ("sub_queries[0].types", "invalid_type", ""),
            ],
        ),
        ('{"edge_type": 7}', OPTIONAL_SCHEMA, [("edge_type", "invalid_type", "")]),
    ],
)
def test_field_level_drift_with_more_than_one_reading_is_delivered_as_written(
    raw, schema, expected,
):
    # Harness principle (2026-09-14): the prompt is precise, the boundary
    # tolerates what a model plausibly does, and the domain parser decides.
    # A wrong-typed or off-enum field used to fail the whole reply; it is now
    # delivered byte-for-byte and reported as a located deviation.
    shape = validate_model_json_shape(raw, schema)

    assert shape.content == raw
    assert shape.normalised is False
    assert [(d.path, d.reason, d.fix) for d in shape.deviations] == expected


@pytest.mark.parametrize(
    ("raw", "schema", "content", "expected"),
    [
        # null where a value was advertised: absent and null mean the same
        # thing to every consumer, and a bare str() downstream would have
        # turned it into the literal text "None".
        (
            '{"answer": null, "grounded": true}', ANSWER_SCHEMA,
            {"grounded": True},
            [("answer", "invalid_type", "dropped")],
        ),
        (
            '{"answer": "ok", "grounded": null}', ANSWER_SCHEMA,
            {"answer": "ok"},
            [("grounded", "invalid_boolean", "dropped")],
        ),
        # quoted booleans / numbers have exactly one reading
        (
            '{"answer": "ok", "grounded": "true"}', ANSWER_SCHEMA,
            {"answer": "ok", "grounded": True},
            [("grounded", "invalid_boolean", "coerced")],
        ),
        (
            '{"answer": "ok", "grounded": " False "}', ANSWER_SCHEMA,
            {"answer": "ok", "grounded": False},
            [("grounded", "invalid_boolean", "coerced")],
        ),
        (
            '{"items":[{"index":"2"},{"index":3.0}]}', '{"items":[{"index":0}]}',
            {"items": [{"index": 2}, {"index": 3}]},
            [
                ("items[0].index", "invalid_type", "coerced"),
                ("items[1].index", "invalid_type", "coerced"),
            ],
        ),
        (
            '{"score":"0.75"}', '{"score":0.0}',
            {"score": 0.75},
            [("score", "invalid_type", "coerced")],
        ),
        # one scalar where a list of scalars was advertised
        (
            '{"sub_queries": [{"query": "q", "types": "concept"}]}', PLAN_SCHEMA,
            {"sub_queries": [{"query": "q", "types": ["concept"]}]},
            [("sub_queries[0].types", "invalid_type", "wrapped")],
        ),
        # a null list item is removed, the rest survives
        (
            '{"sub_queries": [null, {"query": "q"}]}', PLAN_SCHEMA,
            {"sub_queries": [{"query": "q"}]},
            [("sub_queries[0]", "invalid_type", "dropped")],
        ),
        # extra keys ride along untouched through a rebuild
        (
            '{"answer": null, "grounded": true, "note": {"k": [1]}}', ANSWER_SCHEMA,
            {"grounded": True, "note": {"k": [1]}},
            [("answer", "invalid_type", "dropped")],
        ),
    ],
)
def test_single_reading_deviations_are_absorbed_and_reported(
    raw, schema, content, expected,
):
    shape = validate_model_json_shape(raw, schema)

    assert shape.normalised is True
    assert json.loads(shape.content) == content
    assert [(d.path, d.reason, d.fix) for d in shape.deviations] == expected


def test_a_reply_whose_advertised_fields_are_all_null_is_unusable():
    with pytest.raises(ModelJsonRepairError) as caught:
        validate_model_json_shape('{"answer": null, "grounded": null}', ANSWER_SCHEMA)

    assert caught.value.reason == "missing_expected_key"


@pytest.mark.parametrize(
    ("raw", "reason"),
    [("[]", "non_object"), ('"text"', "non_object"), ("not json", "invalid_json")],
)
def test_the_boundary_still_refuses_non_object_replies(raw, reason):
    # Consumers call ``json.loads(raw).get(...)`` on the delivered text: the
    # lenient boundary keeps the object-ness guarantee they depend on.
    with pytest.raises(ModelJsonRepairError) as caught:
        validate_model_json_shape(raw, ANSWER_SCHEMA)

    assert caught.value.reason == reason


def test_schema_shape_validation_allows_optional_and_provider_extra_fields():
    raw = '{"answer":"ok","provider_note":"extra"}'
    shape = validate_model_json_shape(raw, ANSWER_SCHEMA)

    assert shape.deviations == ()
    assert shape.content == raw


def test_shape_deviation_report_is_bounded_but_normalisation_is_not():
    from app.core.model_json import SHAPE_DEVIATIONS_MAX

    raw = json.dumps({"sub_queries": [{"query": index} for index in range(200)]})
    shape = validate_model_json_shape(raw, PLAN_SCHEMA)
    assert len(shape.deviations) == SHAPE_DEVIATIONS_MAX
    assert shape.deviations[0].path == "sub_queries[0].query"

    # Every null is dropped even past the report cap.
    raw = json.dumps({"sub_queries": [{"query": None, "types": []}] * 200})
    shape = validate_model_json_shape(raw, PLAN_SCHEMA)
    assert len(shape.deviations) == SHAPE_DEVIATIONS_MAX
    assert json.loads(shape.content) == {"sub_queries": [{"types": []}] * 200}


@pytest.mark.parametrize(
    ("raw", "schema"),
    [
        (
            '{"conflict_type":"temporal","resolution":"modify",'
            '"winner_ref":null,"resolved_payload":{"valid_from":"2020"}}',
            '{"conflict_type":"none|mutual|temporal|granularity",'
            '"resolution":"keep|discard|modify",'
            '"winner_ref":"<left_ref or right_ref or null>",'
            '"resolved_payload":null}',
        ),
        (
            '{"sections":[],"frame":{}}',
            '{"sections":[{"title":"","sub_queries":[""]}],'
            '"frame":{"subject_kind":"","facets":[]}}',
        ),
    ],
)
def test_schema_shape_validation_accepts_current_union_contracts(raw, schema):
    validate_model_json_shape(raw, schema)


def test_schema_shape_validation_accepts_dynamic_report_frame_assignments():
    validate_model_json_shape(
        '{"markdown":"ok","claims":[{"claim_id":"c1",'
        '"frame_assignments":{"mixer":"SSM"}}]}',
        '{"markdown":"","claims":[{"claim_id":"",'
        '"frame_assignments":{"facet-id":"value"}}]}',
    )


def test_schema_shape_validation_accepts_empty_optional_validity_scope():
    validate_model_json_shape(
        '{"nodes":[{"name":"claim","validity_scope":{}}]}',
        '{"nodes":[{"name":"","validity_scope":'
        '{"region":[],"assumptions":[],"approximation":"","range":""}}]}',
    )


def test_schema_shape_validation_reports_non_string_frame_assignment_values():
    shape = validate_model_json_shape(
        '{"markdown":"ok","claims":[{"claim_id":"c1",'
        '"frame_assignments":{"mixer":["SSM"]}}]}',
        '{"markdown":"","claims":[{"claim_id":"",'
        '"frame_assignments":{"facet-id":"value"}}]}',
    )
    assert [(d.path, d.reason, d.fix) for d in shape.deviations] == [
        ("claims[0].frame_assignments[0]", "invalid_type", ""),
    ]


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ('{"answer":"token budget cut', "incomplete_object"),
        ('{"answer":"x","grounded":true', "incomplete_object"),
        ('{answer: "token budget cut}', "incomplete_object"),
        ('{answer: "token budget cut, grounded: true}', "incomplete_object"),
        ('{sub_queries:[{query:"q",types:[]}', "incomplete_object"),
        ('["answer", "grounded"]', "non_object"),
        ('{conclusion: "no advertised field"}', "missing_expected_key"),
        ('{answer: "x", garbage, grounded: true}', "unsupported_syntax"),
        (
            '{answer: "the garbage token appears", garbage, grounded: true}',
            "unsupported_syntax",
        ),
        ('{answer: "orphan", orphan, grounded: true}', "unsupported_syntax"),
        ('{answer: "x"; grounded: true}', "unsupported_syntax"),
        ('{answer: "x", grounded: True}', "unsupported_syntax"),
        ('{answer: "True", grounded: True}', "unsupported_syntax"),
        ('{answer: "False", grounded: False}', "unsupported_syntax"),
        ('{answer: "x" // comment\n, grounded: true}', "unsupported_syntax"),
        ('{grounded:true garbage, answer:"garbage"}', "unsupported_syntax"),
    ],
)
def test_repair_refuses_incomplete_or_schema_unsafe_responses(raw, reason):
    with pytest.raises(ModelJsonRepairError) as caught:
        parse_model_json_object(raw, ANSWER_SCHEMA, allow_repair=True)

    assert caught.value.reason == reason


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (
            '{answer: "x", grounded: true, next_action: delete_all}',
            {"answer": "x", "grounded": True, "next_action": "delete_all"},
        ),
        ('{answer: "x", grounded: false_value}', {"answer": "x", "grounded": "false_value"}),
        ('{answer: 123, grounded: true}', {"answer": 123, "grounded": True}),
        ('{answer: true, grounded: true}', {"answer": True, "grounded": True}),
    ],
)
def test_repair_delivers_off_shape_fields_for_the_parser_to_judge(raw, expected):
    # Repair restores delimiters. Whether ``answer: 123`` is acceptable is the
    # consumer's call; the boundary reports it (see
    # ``test_field_level_drift_is_reported_not_rejected``) but no longer
    # turns a recoverable reply into a malformed_response.
    result = parse_model_json_object(raw, ANSWER_SCHEMA, allow_repair=True)

    assert result.repaired is True
    assert json.loads(result.content) == expected


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ('{sub_queries: "not-a-list"}', {"sub_queries": "not-a-list"}),
        (
            '{sub_queries: [{query: "q", types: [], unexpected: "x"}]}',
            {"sub_queries": [{"query": "q", "types": [], "unexpected": "x"}]},
        ),
    ],
)
def test_repair_keeps_extra_and_off_type_nested_fields(raw, expected):
    result = parse_model_json_object(raw, PLAN_SCHEMA, allow_repair=True)

    assert json.loads(result.content) == expected


@pytest.mark.parametrize("value", ["true", "{}", "[]", "123"])
def test_null_example_reports_non_string_values(value):
    result = parse_model_json_object(
        f'{{edge_type:{value}}}', OPTIONAL_SCHEMA, allow_repair=True
    )
    shape = validate_model_json_shape(result.content, OPTIONAL_SCHEMA)

    assert [(d.path, d.reason, d.fix) for d in shape.deviations] == [
        ("edge_type", "invalid_type", ""),
    ]


@pytest.mark.parametrize("value", ['"supports"', "null"])
def test_null_example_accepts_optional_string_or_null(value):
    result = parse_model_json_object(
        f'{{edge_type:{value}}}', OPTIONAL_SCHEMA, allow_repair=True
    )

    assert json.loads(result.content)["edge_type"] in {"supports", None}


def test_repair_off_preserves_strict_rejection():
    with pytest.raises(ModelJsonRepairError, match="invalid_json"):
        parse_model_json_object(
            '{answer: "not accepted"}', ANSWER_SCHEMA, allow_repair=False
        )


def test_repaired_string_values_must_remain_verbatim(monkeypatch):
    monkeypatch.setattr(
        "app.core.model_json.json_repair.loads",
        lambda *_args, **_kwargs: {"answer": "changed", "grounded": True},
    )

    with pytest.raises(ModelJsonRepairError, match="string_changed"):
        parse_model_json_object(
            '{answer: "original", grounded: true}',
            ANSWER_SCHEMA,
            allow_repair=True,
        )


def test_repaired_string_may_match_its_json_escaped_spelling():
    raw = r'{answer: "line\nnext and \"quoted\"", grounded: true}'

    result = parse_model_json_object(raw, ANSWER_SCHEMA, allow_repair=True)

    assert json.loads(result.content) == {
        "answer": 'line\nnext and "quoted"',
        "grounded": True,
    }


def test_nested_planning_string_must_remain_verbatim(monkeypatch):
    monkeypatch.setattr(
        "app.core.model_json.json_repair.loads",
        lambda *_args, **_kwargs: {"sub_queries": [{"query": "changed"}]},
    )

    with pytest.raises(ModelJsonRepairError, match="string_changed"):
        parse_model_json_object(
            '{sub_queries: [{query: "original"}]}',
            '{"sub_queries":[{"query":""}]}',
            allow_repair=True,
        )


# ── codex #720 R1 ──────────────────────────────────────────────────────────


def test_absorption_does_not_depend_on_the_diagnostic_cap():
    from app.core.model_json import SHAPE_DEVIATIONS_MAX

    # 32 report-only deviations fill the cap; a null AFTER them must still be
    # dropped from the delivered content.
    items = [{"query": index} for index in range(SHAPE_DEVIATIONS_MAX)]
    items.append({"query": None, "types": []})
    shape = validate_model_json_shape(json.dumps({"sub_queries": items}), PLAN_SCHEMA)

    assert len(shape.deviations) == SHAPE_DEVIATIONS_MAX
    assert shape.normalised is False  # the report never saw the fix …
    assert json.loads(shape.content)["sub_queries"][-1] == {"types": []}  # … but it happened


def test_frame_assignment_keys_never_reach_the_diagnostic_path():
    shape = validate_model_json_shape(
        '{"markdown":"ok","claims":[{"claim_id":"c1",'
        '"frame_assignments":{"private source text":["SSM"]}}]}',
        '{"markdown":"","claims":[{"claim_id":"",'
        '"frame_assignments":{"facet-id":"value"}}]}',
    )
    [deviation] = shape.deviations
    assert deviation.path == "claims[0].frame_assignments[0]"
    assert "private" not in deviation.path


def test_non_finite_numeric_strings_are_not_coerced():
    shape = validate_model_json_shape('{"score":"1e999"}', '{"score":0.0}')

    assert shape.content == '{"score":"1e999"}'
    assert [(d.path, d.reason, d.fix) for d in shape.deviations] == [
        ("score", "invalid_type", ""),
    ]


def test_a_rewritten_reply_carrying_nan_fails_through_the_malformed_path():
    with pytest.raises(ModelJsonRepairError) as caught:
        validate_model_json_shape(
            '{"score": NaN, "note": null}', '{"score":0.0,"note":""}'
        )

    assert caught.value.reason == "non_finite_number"


@pytest.mark.parametrize(
    "raw",
    ['{"score": NaN}', '{"score": Infinity}', '{"score": -Infinity}', '{"score": 1e999}',
     '{"score": 0.5, "nested": {"x": [NaN]}}'],
)
def test_non_finite_numbers_are_rejected_on_the_strict_path(raw):
    # codex #720 R7: json.loads accepts these; a +inf confidence would clamp
    # to 1.0 downstream. One boundary check, no per-consumer guards.
    with pytest.raises(ModelJsonRepairError) as caught:
        validate_model_json_shape(raw, '{"score":0.0,"nested":{"x":[0.0]}}')

    assert caught.value.reason == "non_finite_number"
