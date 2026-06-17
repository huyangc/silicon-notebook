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
    resolved_payload: dict | None = None,
    confidence: float = 0.88,
    rationale: str = "Left is better supported.",
) -> str:
    return json.dumps({
        "conflict_type": conflict_type,
        "resolution": resolution,
        "winner_ref": winner_ref,
        "resolved_payload": resolved_payload,
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
    """mutual → discard, winner_ref set, resolved_payload None."""

    def test_mutual_parsed(self):
        response = _verdict(conflict_type="mutual", resolution="discard", winner_ref="r1")
        verdicts = review_conflict_candidates(_FakeLLM(response), [_item()])
        assert len(verdicts) == 1
        v = verdicts[0]
        assert v["conflict_type"] == "mutual"
        assert v["resolution"] == "discard"
        assert v["winner_ref"] == "r1"
        assert v["resolved_payload"] is None
        assert v["confidence"] == pytest.approx(0.88)
        assert isinstance(v["rationale"], str) and v["rationale"]


class TestTemporalConflict:
    """temporal → modify, resolved_payload carries scoped statement."""

    def test_temporal_parsed(self):
        payload = {"name": "A —supports→ B [2000–2005]"}
        response = _verdict(
            conflict_type="temporal",
            resolution="modify",
            winner_ref=None,
            resolved_payload=payload,
            confidence=0.75,
        )
        verdicts = review_conflict_candidates(_FakeLLM(response), [_item()])
        v = verdicts[0]
        assert v["conflict_type"] == "temporal"
        assert v["resolution"] == "modify"
        assert v["winner_ref"] is None
        assert v["resolved_payload"] == payload
        assert v["confidence"] == pytest.approx(0.75)


class TestGranularityConflict:
    """granularity → modify, resolved_payload annotates both."""

    def test_granularity_parsed(self):
        payload = {"annotation": "granularity: Shanghai ⊂ China"}
        response = _verdict(
            conflict_type="granularity",
            resolution="modify",
            winner_ref=None,
            resolved_payload=payload,
            confidence=0.82,
        )
        verdicts = review_conflict_candidates(_FakeLLM(response), [_item()])
        v = verdicts[0]
        assert v["conflict_type"] == "granularity"
        assert v["resolution"] == "modify"
        assert v["resolved_payload"] == payload


class TestNoneConflict:
    """none → keep, winner_ref None, resolved_payload None."""

    def test_none_parsed(self):
        response = _verdict(
            conflict_type="none",
            resolution="keep",
            winner_ref=None,
            resolved_payload=None,
            confidence=0.95,
        )
        verdicts = review_conflict_candidates(_FakeLLM(response), [_item()])
        v = verdicts[0]
        assert v["conflict_type"] == "none"
        assert v["resolution"] == "keep"
        assert v["winner_ref"] is None
        assert v["resolved_payload"] is None


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
            "resolved_payload", "confidence", "rationale",
            "left_ref", "right_ref",
        }
        assert set(verdicts[0].keys()) == required
