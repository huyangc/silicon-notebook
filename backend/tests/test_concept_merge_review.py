import math

from app.services.concept_merge_review import review_merge_candidates


class _ReviewLLM:
    configured = True

    def chat_json(self, messages, response_schema_hint):
        return """
        {
          "decisions": [
            {
              "candidate_id": "mc-1",
              "decision": "merge",
              "canonical_name": "voltage-controlled oscillator",
              "confidence": 0.96,
              "rationale": "VCO is the common acronym for voltage-controlled oscillator."
            },
            {
              "candidate_id": "mc-2",
              "decision": "keep_separate",
              "canonical_name": "",
              "confidence": 0.91,
              "rationale": "current mirror and current source are related but not identical."
            }
          ]
        }
        """


def _cands(n: int) -> list:
    return [
        {"id": f"mc-{i}", "canonical_a": f"K-a{i}", "canonical_b": f"K-b{i}", "score": 0.9}
        for i in range(n)
    ]


def test_review_merge_candidates_parses_decisions():
    candidates = [
        {"id": "mc-1", "canonical_a": "K-vco", "canonical_b": "K-voltage controlled oscillator", "score": 0.93},
        {"id": "mc-2", "canonical_a": "K-current mirror", "canonical_b": "K-current source", "score": 0.88},
    ]

    decisions = review_merge_candidates(_ReviewLLM(), candidates)

    assert decisions[0]["candidate_id"] == "mc-1"
    assert decisions[0]["decision"] == "merge"
    assert decisions[0]["confidence"] == 0.96
    assert decisions[1]["decision"] == "keep_separate"


# --- new: chunk + fail-open behavior (truncated LLM JSON must not crash rebuild) ---


class _TruncatedLLM:
    """chat_json returns a deliberately truncated (invalid) JSON string."""

    configured = True

    def __init__(self):
        self.calls = 0

    def chat_json(self, messages, response_schema_hint):
        self.calls += 1
        return '{"decisions":[{"candidate_id":"ac0","decision":"merge","confidence":0.95},'


class _CountingLLM:
    """Valid JSON; records how many chunks (chat_json calls) it saw.

    Echoes one merge decision per candidate present in the chunk's prompt so we
    can assert decisions from every chunk are returned.
    """

    configured = True

    def __init__(self):
        self.calls = 0

    def chat_json(self, messages, response_schema_hint):
        self.calls += 1
        content = messages[0]["content"]
        ids = [tok[3:] for line in content.splitlines()
               for tok in line.split() if tok.startswith("id=")]
        decisions = [
            {"candidate_id": cid, "decision": "merge",
             "canonical_name": f"c-{cid}", "confidence": 0.9, "rationale": "x"}
            for cid in ids
        ]
        import json as _json
        return _json.dumps({"decisions": decisions})


class _FlakyLLM:
    """Valid JSON except for one specific chunk (identified by a candidate id)."""

    configured = True

    def __init__(self, break_on: str):
        self.break_on = break_on
        self.calls = 0

    def chat_json(self, messages, response_schema_hint):
        self.calls += 1
        content = messages[0]["content"]
        ids = [tok[3:] for line in content.splitlines()
               for tok in line.split() if tok.startswith("id=")]
        if self.break_on in ids:
            return "not json at all <<<"
        import json as _json
        decisions = [
            {"candidate_id": cid, "decision": "merge",
             "canonical_name": f"c-{cid}", "confidence": 0.9, "rationale": "x"}
            for cid in ids
        ]
        return _json.dumps({"decisions": decisions})


def test_truncated_json_does_not_raise():
    fake = _TruncatedLLM()
    # A single candidate; the LLM reply is truncated/invalid -> no crash, no decisions.
    out = review_merge_candidates(fake, _cands(1))
    assert out == []
    assert fake.calls == 1


def test_chunking_splits_and_aggregates():
    fake = _CountingLLM()
    out = review_merge_candidates(fake, _cands(5), batch_size=2)
    # ceil(5/2) == 3 chunks -> 3 chat_json calls
    assert fake.calls == 3
    # every candidate produced a decision, aggregated across chunks
    ids = {d["candidate_id"] for d in out}
    assert ids == {f"mc-{i}" for i in range(5)}


def test_fail_open_isolation_keeps_good_chunks():
    # 5 candidates, batch_size 2 -> chunks [mc-0,mc-1] [mc-2,mc-3] [mc-4].
    # Break the middle chunk (mc-2); good chunks' decisions still returned.
    fake = _FlakyLLM(break_on="mc-2")
    out = review_merge_candidates(fake, _cands(5), batch_size=2)
    ids = {d["candidate_id"] for d in out}
    assert ids == {"mc-0", "mc-1", "mc-4"}
    assert "mc-2" not in ids and "mc-3" not in ids


def test_happy_path_unchanged_validation():
    # keep_separate + merge parsed; unsure kept; unknown decision dropped; empty id dropped.
    class _Mixed:
        configured = True

        def chat_json(self, messages, response_schema_hint):
            import json as _json
            return _json.dumps({"decisions": [
                {"candidate_id": "ok-merge", "decision": "merge", "canonical_name": "x",
                 "confidence": 0.8, "rationale": "r"},
                {"candidate_id": "ok-sep", "decision": "keep_separate", "confidence": 0.7},
                {"candidate_id": "ok-unsure", "decision": "unsure", "confidence": 0.5},
                {"candidate_id": "bad-decision", "decision": "frobnicate", "confidence": 0.99},
                {"candidate_id": "", "decision": "merge", "confidence": 0.99},
            ]})

    out = review_merge_candidates(_Mixed(), _cands(3))
    by_id = {d["candidate_id"]: d for d in out}
    assert set(by_id) == {"ok-merge", "ok-sep", "ok-unsure"}
    assert by_id["ok-merge"]["decision"] == "merge"
    assert by_id["ok-sep"]["decision"] == "keep_separate"
    assert by_id["ok-unsure"]["decision"] == "unsure"


def test_max_workers_matches_serial():
    serial = review_merge_candidates(_CountingLLM(), _cands(7), batch_size=2, max_workers=1)
    parallel = review_merge_candidates(_CountingLLM(), _cands(7), batch_size=2, max_workers=4)
    # order-independent: same set of decisions by candidate_id
    assert {d["candidate_id"] for d in serial} == {d["candidate_id"] for d in parallel}
    assert {d["candidate_id"] for d in serial} == {f"mc-{i}" for i in range(7)}


def test_unconfigured_or_empty_returns_empty():
    class _Off:
        configured = False

        def chat_json(self, messages, response_schema_hint):  # pragma: no cover
            raise AssertionError("must not be called when unconfigured")

    assert review_merge_candidates(_Off(), _cands(3)) == []
    assert review_merge_candidates(_ReviewLLM(), []) == []
