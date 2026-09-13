"""Answer output examples must model the strict JSON accepted at transport."""
from __future__ import annotations

import json

import pytest

from app.core.model_json import ModelJsonRepairError, parse_model_json_object
from app.services.prompts import ANSWER_SCHEMA_HINT, answer_prompt


def test_answer_prompt_example_is_one_object_with_lossless_text_escaping():
    prompt = answer_prompt("Explain the result.", "k1: The result.")
    example = prompt.rsplit("Example (format only):\n", 1)[1]

    # json.loads rejects a second object and any unescaped newline in a string.
    result = json.loads(example)
    assert set(result) == {"answer", "grounded"}
    assert result["grounded"] is True
    assert "\n\n" in result["answer"]
    assert '"quoted"' in result["answer"]
    assert r"$\alpha$" in result["answer"]
    assert "[k1]" in result["answer"]
    assert parse_model_json_object(
        example, ANSWER_SCHEMA_HINT, allow_repair=True
    ).repaired is False


def test_answer_prompt_requires_sibling_fields_and_escaped_string_content():
    prompt = answer_prompt("Explain the result.", "k1: The result.")

    assert 'both "answer" (a string) and "grounded" (a boolean, true or false)' in prompt
    assert "at the same root" in prompt
    assert "Do not emit a second object" in prompt
    assert r"line and paragraph breaks as \n" in prompt
    assert r'each LaTeX backslash as \\' in prompt
    assert "Do not put raw line breaks inside the string" in prompt


@pytest.mark.parametrize(
    ("raw", "reason"),
    [
        ('{"answer":"Complete answer."}\n{"grounded":true}', "non_object"),
        ('{"answer":"Paragraph one.\nParagraph two.\\nParagraph three.",'
         '"grounded":true}', "string_changed"),
    ],
)
def test_observed_answer_format_failures_still_fail_closed(raw, reason):
    with pytest.raises(ModelJsonRepairError) as caught:
        parse_model_json_object(raw, ANSWER_SCHEMA_HINT, allow_repair=True)

    assert caught.value.reason == reason
