from __future__ import annotations

import json

import pytest

from app.core.model_json import ModelJsonRepairError, parse_model_json_object


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


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ('{"answer":"token budget cut', "incomplete_object"),
        ('{"answer":"x","grounded":true', "incomplete_object"),
        ('{answer: "token budget cut}', "incomplete_object"),
        ('{answer: "token budget cut, grounded: true}', "incomplete_object"),
        ('{sub_queries:[{query:"q",types:[]}', "incomplete_object"),
        ('["answer", "grounded"]', "non_object"),
        ('{answer: "x", grounded: true, next_action: delete_all}', "unknown_key"),
        ('{answer: "x", grounded: false_value}', "invalid_boolean"),
        ('{answer: 123, grounded: true}', "invalid_type"),
        ('{answer: true, grounded: true}', "invalid_type"),
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
    ("raw", "reason"),
    [
        ('{sub_queries: "not-a-list"}', "invalid_type"),
        (
            '{sub_queries: [{query: "q", types: [], unexpected: "x"}]}',
            "unknown_key",
        ),
    ],
)
def test_repair_recursively_enforces_schema_example_shape(raw, reason):
    with pytest.raises(ModelJsonRepairError) as caught:
        parse_model_json_object(raw, PLAN_SCHEMA, allow_repair=True)

    assert caught.value.reason == reason


@pytest.mark.parametrize("value", ["true", "{}", "[]", "123"])
def test_null_example_allows_only_optional_string_or_null(value):
    with pytest.raises(ModelJsonRepairError, match="invalid_type"):
        parse_model_json_object(
            f'{{edge_type:{value}}}', OPTIONAL_SCHEMA, allow_repair=True
        )


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
