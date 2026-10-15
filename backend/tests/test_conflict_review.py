"""Tests for conflict_review.py — LLM-based conflict adjudicator (Task 3).

All fixtures use a fake/mock LLM client. No real endpoint, no DB, no I/O.
"""
from __future__ import annotations

import json
import pytest

from app.services.kg.conflict_review import review_conflict_candidates


# ---------------------------------------------------------------------------
# Fake LLM client helpers
# ---------------------------------------------------------------------------

class _FakeLLM:
    """Minimal mock that mirrors concept_merge_review's LLM interface."""
    configured = True

    def __init__(self, response: str):
        self._response = response
        self.last_messages: list[dict] = []
        self.last_schema: str = ""

    def chat_json(self, messages: list[dict], response_schema_hint: str) -> str:
        self.last_messages = messages
        self.last_schema = response_schema_hint
        return self._response


class _UnconfiguredLLM:
    configured = False

    def chat_json(self, messages, response_schema_hint):
        raise AssertionError("Should never be called")


# ---------------------------------------------------------------------------
# Shared test fixtures
# ---------------------------------------------------------------------------

def _item(
    kind: str = "edge",
    left_ref: str = "r1",
    right_ref: str = "r2",
    signal: str = "shared_head",
    left_text: str = "A —supports→ B",
    left_source: str = "NMOS increases gain under high Vgs.",
    right_text: str = "A —contrasts_with→ B",
    right_source: str = "NMOS decreases gain under low Vgs.",
    left_obj_type: str | None = "Concept",
    right_obj_type: str | None = "Concept",
    left_tier: str = "base",
    right_tier: str = "base",
) -> dict:
    return {
        "candidate": {"kind": kind, "left_ref": left_ref, "right_ref": right_ref, "signal": signal},
        "left": {
            "text": left_text,
            "source_text": left_source,
            "object_type": left_obj_type,
            "tier": left_tier,
        },
        "right": {
            "text": right_text,
            "source_text": right_source,
            "object_type": right_obj_type,
            "tier": right_tier,
        },
    }


def _verdict(
    conflict_type: str = "mutual",
    resolution: str = "discard",
    winner_ref: str | None = "r1",
    resolved_name: str = "",
    confidence: float = 0.88,
    rationale: str = "Left is better supported.",
) -> str:
    return json.dumps({
        "conflict_type": conflict_type,
        "resolution": resolution,
        "winner_ref": winner_ref,
        "resolved_name": resolved_name,
        "confidence": confidence,
        "rationale": rationale,
    })


# ---------------------------------------------------------------------------
# Prompt content assertions
# ---------------------------------------------------------------------------

class TestPromptContents:
    """The built prompt must embed both sides' text and source_text."""

    def test_prompt_contains_left_text(self):
        llm = _FakeLLM(_verdict())
        item = _item(left_text="A —supports→ B")
        review_conflict_candidates(llm, [item])
        assert "A —supports→ B" in llm.last_messages[0]["content"]

    def test_prompt_contains_right_text(self):
        llm = _FakeLLM(_verdict())
        item = _item(right_text="A —contrasts_with→ B")
        review_conflict_candidates(llm, [item])
        assert "A —contrasts_with→ B" in llm.last_messages[0]["content"]

    def test_prompt_contains_left_source_text(self):
        llm = _FakeLLM(_verdict())
        item = _item(left_source="Evidence passage for left side.")
        review_conflict_candidates(llm, [item])
        assert "Evidence passage for left side." in llm.last_messages[0]["content"]

    def test_prompt_contains_right_source_text(self):
        llm = _FakeLLM(_verdict())
        item = _item(right_source="Evidence passage for right side.")
        review_conflict_candidates(llm, [item])
        assert "Evidence passage for right side." in llm.last_messages[0]["content"]

    def test_prompt_contains_refs(self):
        llm = _FakeLLM(_verdict())
        item = _item(left_ref="r-left-99", right_ref="r-right-99")
        review_conflict_candidates(llm, [item])
        prompt = llm.last_messages[0]["content"]
        assert "r-left-99" in prompt
        assert "r-right-99" in prompt

    def test_llm_called_once_per_item(self):
        """review_conflict_candidates calls the LLM once per item (not batched)."""
        calls = []

        class _CountingLLM:
            configured = True
            def chat_json(self, messages, schema):
                calls.append(1)
                return _verdict()

        items = [_item(), _item(left_ref="x1", right_ref="x2")]
        review_conflict_candidates(_CountingLLM(), items)
        assert len(calls) == 2


# ---------------------------------------------------------------------------
# Verdict parsing — happy paths
# ---------------------------------------------------------------------------

class TestMutualConflict:
    """mutual → discard, winner_ref set, resolved_name None."""

    def test_mutual_parsed(self):
        response = _verdict(conflict_type="mutual", resolution="discard", winner_ref="r1")
        verdicts = review_conflict_candidates(_FakeLLM(response), [_item()])
        assert len(verdicts) == 1
        v = verdicts[0]
        assert v["conflict_type"] == "mutual"
        assert v["resolution"] == "discard"
        assert v["winner_ref"] == "r1"
        assert v["resolved_name"] is None
        assert v["confidence"] == pytest.approx(0.88)
        assert isinstance(v["rationale"], str) and v["rationale"]


class TestTemporalConflict:
    """temporal → modify one side: winner_ref names it, resolved_name is its
    statement with the time range appended."""

    def test_temporal_parsed(self):
        response = _verdict(
            conflict_type="temporal",
            resolution="modify",
            winner_ref="r2",
            resolved_name="  A —supports→ B [2000–2005] ",
            confidence=0.75,
        )
        verdicts = review_conflict_candidates(_FakeLLM(response), [_item(kind="node")])
        v = verdicts[0]
        assert v["conflict_type"] == "temporal"
        assert v["resolution"] == "modify"
        assert v["winner_ref"] == "r2"
        assert v["resolved_name"] == "A —supports→ B [2000–2005]"
        assert v["confidence"] == pytest.approx(0.75)

    def test_empty_strings_mean_no_winner_and_no_rewrite(self):
        response = _verdict(
            conflict_type="temporal", resolution="modify",
            winner_ref="", resolved_name="",
        )
        [v] = review_conflict_candidates(_FakeLLM(response), [_item(kind="node")])
        assert v["winner_ref"] is None
        assert v["resolved_name"] is None

    def test_name_outside_modify_is_ignored(self):
        response = _verdict(resolution="discard", resolved_name="stray text")
        [v] = review_conflict_candidates(_FakeLLM(response), [_item()])
        assert v["resolved_name"] is None


class TestGranularityConflict:
    """granularity → keep: both entries coexist unchanged."""

    def test_granularity_parsed(self):
        response = _verdict(
            conflict_type="granularity",
            resolution="keep",
            winner_ref="",
            confidence=0.82,
        )
        verdicts = review_conflict_candidates(_FakeLLM(response), [_item()])
        v = verdicts[0]
        assert v["conflict_type"] == "granularity"
        assert v["resolution"] == "keep"
        assert v["winner_ref"] is None
        assert v["resolved_name"] is None


class TestNoneConflict:
    """none → keep, winner_ref None, resolved_name None."""

    def test_none_parsed(self):
        response = _verdict(
            conflict_type="none",
            resolution="keep",
            winner_ref="",
            confidence=0.95,
        )
        verdicts = review_conflict_candidates(_FakeLLM(response), [_item()])
        v = verdicts[0]
        assert v["conflict_type"] == "none"
        assert v["resolution"] == "keep"
        assert v["winner_ref"] is None
        assert v["resolved_name"] is None


# ---------------------------------------------------------------------------
# Confidence clamping
# ---------------------------------------------------------------------------

class TestConfidenceClamping:
    def test_confidence_above_1_clamped_to_1(self):
        response = _verdict(confidence=1.5)
        verdicts = review_conflict_candidates(_FakeLLM(response), [_item()])
        assert verdicts[0]["confidence"] <= 1.0

    def test_confidence_below_0_clamped_to_0(self):
        response = _verdict(confidence=-0.3)
        verdicts = review_conflict_candidates(_FakeLLM(response), [_item()])
        assert verdicts[0]["confidence"] >= 0.0

    def test_confidence_missing_defaults_to_0(self):
        obj = json.loads(_verdict())
        del obj["confidence"]
        response = json.dumps(obj)
        verdicts = review_conflict_candidates(_FakeLLM(response), [_item()])
        assert verdicts[0]["confidence"] == 0.0


# ---------------------------------------------------------------------------
# Malformed / empty LLM output — safe degradation
# ---------------------------------------------------------------------------

class TestMalformedOutput:
    """Malformed or empty LLM output must degrade gracefully (no crash)."""

    def test_empty_string_degrades_safely(self):
        verdicts = review_conflict_candidates(_FakeLLM(""), [_item()])
        assert len(verdicts) == 1
        v = verdicts[0]
        assert v["conflict_type"] == "none"
        assert v["confidence"] <= 0.1
        assert isinstance(v["rationale"], str)

    def test_non_json_degrades_safely(self):
        verdicts = review_conflict_candidates(_FakeLLM("Not JSON at all!"), [_item()])
        assert len(verdicts) == 1
        v = verdicts[0]
        assert v["conflict_type"] == "none"

    def test_json_missing_required_keys_degrades_safely(self):
        # JSON but only has some fields
        response = json.dumps({"confidence": 0.5})
        verdicts = review_conflict_candidates(_FakeLLM(response), [_item()])
        assert len(verdicts) == 1
        v = verdicts[0]
        assert v["conflict_type"] == "none"

    def test_invalid_conflict_type_degrades_safely(self):
        """Unknown conflict_type from LLM → degrade to none."""
        obj = json.loads(_verdict())
        obj["conflict_type"] = "bogus_type"
        verdicts = review_conflict_candidates(_FakeLLM(json.dumps(obj)), [_item()])
        v = verdicts[0]
        assert v["conflict_type"] == "none"

    def test_invalid_resolution_degrades_safely(self):
        """Unknown resolution from LLM → degrade to keep."""
        obj = json.loads(_verdict())
        obj["resolution"] = "destroy"
        verdicts = review_conflict_candidates(_FakeLLM(json.dumps(obj)), [_item()])
        v = verdicts[0]
        assert v["resolution"] == "keep"

    def test_json_array_instead_of_object_degrades(self):
        """LLM returns array instead of object → degrade."""
        verdicts = review_conflict_candidates(_FakeLLM("[]"), [_item()])
        assert len(verdicts) == 1
        v = verdicts[0]
        assert v["conflict_type"] == "none"


# ---------------------------------------------------------------------------
# Per-item resilience — one transient LLM error must not abort the batch
# ---------------------------------------------------------------------------

class _RaisingOnceLLM:
    """Raises on the first chat_json call, succeeds on subsequent calls."""
    configured = True

    def __init__(self, success_response: str):
        self._success = success_response
        self.calls = 0

    def chat_json(self, messages, response_schema_hint):
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("transient LLM error")
        return self._success


def test_transient_llm_error_does_not_abort_batch():
    """A chat_json exception on one item yields a safe fallback for that item
    and the batch still processes the remaining items."""
    success = json.dumps({
        "conflict_type": "mutual",
        "resolution": "discard",
        "winner_ref": "r3",     # item 1 has refs r3/r4
        "resolved_name": "",
        "confidence": 0.9,
        "rationale": "ok",
    })
    llm = _RaisingOnceLLM(success)
    items = [_item(), _item(left_ref="r3", right_ref="r4")]
    verdicts = review_conflict_candidates(llm, items)

    assert llm.calls == 2                       # both items attempted, no early abort
    assert len(verdicts) == 2
    # item 0 raised → safe fallback
    assert verdicts[0]["conflict_type"] == "none"
    assert verdicts[0]["resolution"] == "keep"
    assert verdicts[0]["confidence"] == 0.0
    # item 1 parsed normally
    assert verdicts[1]["conflict_type"] == "mutual"
    assert verdicts[1]["winner_ref"] == "r3"


# ---------------------------------------------------------------------------
# Unconfigured / empty inputs
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_unconfigured_llm_returns_empty(self):
        verdicts = review_conflict_candidates(_UnconfiguredLLM(), [_item()])
        assert verdicts == []

    def test_empty_items_returns_empty(self):
        verdicts = review_conflict_candidates(_FakeLLM(_verdict()), [])
        assert verdicts == []

    def test_output_length_matches_input_length(self):
        items = [_item(), _item(left_ref="a1", right_ref="a2")]
        verdicts = review_conflict_candidates(_FakeLLM(_verdict()), items)
        assert len(verdicts) == len(items)

    def test_verdict_preserves_candidate_refs(self):
        """Each verdict dict must carry the original left_ref and right_ref."""
        item = _item(left_ref="ref-L", right_ref="ref-R")
        verdicts = review_conflict_candidates(_FakeLLM(_verdict()), [item])
        v = verdicts[0]
        assert v["left_ref"] == "ref-L"
        assert v["right_ref"] == "ref-R"

    def test_verdict_output_keys(self):
        """Verdicts must contain exactly the documented output keys."""
        verdicts = review_conflict_candidates(_FakeLLM(_verdict()), [_item()])
        required = {
            "conflict_type", "resolution", "winner_ref",
            "resolved_name", "confidence", "rationale",
            "left_ref", "right_ref",
        }
        assert set(verdicts[0].keys()) == required


def test_confidence_from_a_non_number_never_reaches_the_auto_apply_threshold():
    """codex #720 R4 P1: the lenient shape boundary delivers ``true`` /
    ``"Infinity"`` / NaN unchanged; a confidence that can auto-apply a
    destructive resolution must read them as 0.0."""
    from app.services.kg.conflict_review import _confidence

    for bogus in (True, False, "Infinity", "-inf", "nan", float("inf"), float("nan"),
                  "high", [0.99], {"v": 0.99}, None, ""):
        assert _confidence(bogus) == 0.0, bogus
    assert _confidence(0.97) == 0.97
    assert _confidence("0.97") == 0.97
    assert _confidence(7) == 1.0
    assert _confidence(-3) == 0.0


def test_container_valued_rationale_is_never_persisted_as_text():
    """codex #720 R13: a delivered ``rationale: []`` must read as "" rather
    than the adjudication explanation "[]"."""
    from app.services.kg.conflict_review import review_conflict_candidates

    class _ListRationale:
        configured = True

        def chat_json(self, messages, schema_hint, **kwargs):
            return json.dumps({
                "conflict_type": "temporal", "resolution": "keep",
                "winner_ref": None, "resolved_name": "",
                "confidence": 0.5, "rationale": ["新旧版本"],
            })

    [decision] = review_conflict_candidates(_ListRationale(), [_item()])
    assert decision["rationale"] == ""
    assert decision["conflict_type"] == "temporal"


def test_prompt_matches_what_the_executor_applies():
    """Prompt/executor alignment: a modify rewrites only the winner side's
    name; granularity keeps both; an edge cannot be modified; "none" is
    spelled "" because the strict schema keeps root fields non-null."""
    from app.services.kg.conflict_review import _SCHEMA

    llm = _FakeLLM(_verdict())
    review_conflict_candidates(llm, [_item(kind="edge")])
    prompt = llm.last_messages[0]["content"]
    assert "resolved_payload" not in prompt and "null" not in prompt
    assert "set winner_ref to the ref of the side you rewrite" in prompt
    assert "Only that side changes" in prompt
    assert "Resolution: keep — both entries stay as they are." in prompt
    assert "annotate granularity" not in prompt
    assert "Kind edge: modify is not supported" in prompt
    assert "Kind: edge" in prompt
    assert 'or "" when the resolution is keep' in prompt
    assert 'write "" for every other resolution' in prompt
    assert "rationale: one short sentence, as a string" in prompt
    hint = json.loads(_SCHEMA)
    assert hint["winner_ref"] == "" and hint["resolved_name"] == ""
    assert "resolved_payload" not in hint


def test_conflict_hint_projects_to_a_strict_schema():
    from app.core.model_schema import response_format_for_hint, validate_structured_response
    from app.services.kg.conflict_review import _SCHEMA

    response_format = response_format_for_hint(_SCHEMA)
    assert response_format is not None
    reply = {
        "conflict_type": "temporal", "resolution": "modify", "winner_ref": "r2",
        "resolved_name": "A [2000–2005]", "confidence": 0.9, "rationale": "x",
    }
    assert validate_structured_response(json.dumps(reply), response_format)
    keep = {**reply, "resolution": "keep", "winner_ref": "", "resolved_name": ""}
    assert validate_structured_response(json.dumps(keep), response_format)
    assert not validate_structured_response(
        json.dumps({**keep, "winner_ref": None}), response_format
    )
