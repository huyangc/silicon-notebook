"""Reasoning models (e.g. MiniMax-M2.7) prepend a <think>...</think> chain-of-thought
block before the JSON, even under response_format=json_object. The JSON normalizers
must strip it so downstream json.loads / safe_json don't choke at char 0.

Regression for: _answer_mix's `json.loads(raw)` raising JSONDecodeError on
`<think>...</think>\n\n{...}` output.
"""
import json

from app.core.llm import strip_json_fences
from app.services.kg.client import safe_json


# A faithful shape of the failing MiniMax-M2.7 output: a long think block (with
# newlines, quotes AND stray braces inside it) followed by the real JSON.
THINK_THEN_JSON = (
    '<think>用户问的是…(一大段推理)\n'
    'note: a stray brace { and "quotes" inside the thoughts\n'
    '</think>\n\n{"answer":"DeepSeek-V4 ...","grounded":true}'
)


def test_strip_json_fences_removes_think_and_parses():
    out = strip_json_fences(THINK_THEN_JSON)
    data = json.loads(out)  # must NOT raise (this is what _answer_mix:5644 does)
    assert data == {"answer": "DeepSeek-V4 ...", "grounded": True}


def test_strip_json_fences_think_plus_code_fence():
    s = '<think>reasoning here</think>\n```json\n{"a": 1}\n```'
    assert json.loads(strip_json_fences(s)) == {"a": 1}


def test_strip_json_fences_think_with_braces_not_mistaken_for_json():
    # The braces inside <think> must be removed with the block, so the {...}
    # fallback locks onto the real answer object, not the thought's braces.
    s = '<think>maybe {bad: 1} here</think>{"answer":"x","grounded":false}'
    assert json.loads(strip_json_fences(s)) == {"answer": "x", "grounded": False}


def test_strip_json_fences_plain_json_unchanged():
    assert json.loads(strip_json_fences('{"a": 1}')) == {"a": 1}


def test_strip_json_fences_trailing_text_trimmed():
    assert json.loads(strip_json_fences('{"a": 1}\n\nThat is my answer.')) == {"a": 1}


def test_safe_json_strips_think():
    assert safe_json('<think>r</think>\n{"answer":"y"}') == {"answer": "y"}


def test_safe_json_plain_still_works():
    assert safe_json('{"answer":"z"}') == {"answer": "z"}


def test_safe_json_garbage_still_empty():
    assert safe_json('<think>only thoughts, no json</think>') == {}
