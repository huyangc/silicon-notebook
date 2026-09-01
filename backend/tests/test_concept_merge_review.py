import json
import threading

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


def test_parallel_review_propagates_model_artifact_scope():
    from app.services.model_work import (
        ModelPriority,
        make_model_work_context,
        model_artifact_scope,
    )

    class _ScopeCapturingLLM(_CountingLLM):
        def __init__(self):
            super().__init__()
            self.notebook_ids = []

        def chat_json(self, messages, response_schema_hint):
            context = make_model_work_context(
                workload_id="kg_merge_review",
                priority=ModelPriority.BACKGROUND,
            )
            self.notebook_ids.append(context.notebook_id)
            return super().chat_json(messages, response_schema_hint)

    llm = _ScopeCapturingLLM()
    with model_artifact_scope(notebook_id="nb-parallel-review"):
        review_merge_candidates(
            llm,
            _cands(4),
            batch_size=1,
            max_workers=4,
        )

    assert llm.notebook_ids == ["nb-parallel-review"] * 4


def test_unconfigured_or_empty_returns_empty():
    class _Off:
        configured = False

        def chat_json(self, messages, response_schema_hint):  # pragma: no cover
            raise AssertionError("must not be called when unconfigured")

    assert review_merge_candidates(_Off(), _cands(3)) == []
    assert review_merge_candidates(_ReviewLLM(), []) == []


# --- totality: the function must NEVER raise, for ANY LLM output shape ---


class _RawLLM:
    """Returns a fixed raw string from chat_json (controls the exact LLM output)."""

    configured = True

    def __init__(self, raw: str):
        self.raw = raw

    def chat_json(self, messages, response_schema_hint):
        return self.raw


def test_decisions_non_iterable_does_not_raise():
    # {"decisions": 5} -> `for item in decisions` would TypeError.
    out = review_merge_candidates(_RawLLM('{"decisions": 5}'), _cands(1))
    assert out == []


def test_decisions_is_dict_or_string_does_not_raise():
    assert review_merge_candidates(_RawLLM('{"decisions": {"a": 1}}'), _cands(1)) == []
    assert review_merge_candidates(_RawLLM('{"decisions": "nope"}'), _cands(1)) == []


def test_categorical_confidence_does_not_raise():
    # confidence:"high" -> float("high") would ValueError.
    raw = json.dumps({"decisions": [
        {"candidate_id": "ac0", "decision": "merge", "canonical_name": "x",
         "confidence": "high", "rationale": "r"},
    ]})
    out = review_merge_candidates(_RawLLM(raw), _cands(1))
    # No raise. Item either dropped or confidence coerced to 0.0 — never a crash.
    assert isinstance(out, list)
    for d in out:
        assert isinstance(d["confidence"], float)


def test_dict_and_list_confidence_do_not_raise():
    raw = json.dumps({"decisions": [
        {"candidate_id": "ac0", "decision": "merge", "confidence": {"x": 1}},
        {"candidate_id": "ac1", "decision": "merge", "confidence": [1, 2, 3]},
        # a good one alongside the bad ones — should still come through
        {"candidate_id": "ac2", "decision": "merge", "confidence": 0.9},
    ]})
    out = review_merge_candidates(_RawLLM(raw), _cands(3))
    by_id = {d["candidate_id"]: d for d in out}
    assert "ac2" in by_id and by_id["ac2"]["confidence"] == 0.9
    for d in out:
        assert isinstance(d["confidence"], float)


def test_batch_size_none_does_not_raise():
    fake = _CountingLLM()
    out = review_merge_candidates(fake, _cands(3), batch_size=None)
    # falls back to default step; single chunk here.
    assert {d["candidate_id"] for d in out} == {"mc-0", "mc-1", "mc-2"}


def test_parallel_bad_confidence_isolated_does_not_raise():
    # chunks [mc-0,mc-1] [mc-2,mc-3] [mc-4]; the fake injects a categorical
    # confidence for mc-2's chunk. Must not raise; good chunks returned.
    class _BadConfChunk:
        configured = True

        def chat_json(self, messages, response_schema_hint):
            content = messages[0]["content"]
            ids = [tok[3:] for line in content.splitlines()
                   for tok in line.split() if tok.startswith("id=")]
            decisions = []
            for cid in ids:
                conf = "high" if cid == "mc-2" else 0.9
                decisions.append({"candidate_id": cid, "decision": "merge",
                                  "canonical_name": f"c-{cid}", "confidence": conf,
                                  "rationale": "x"})
            return json.dumps({"decisions": decisions})

    out = review_merge_candidates(_BadConfChunk(), _cands(5), batch_size=2, max_workers=4)
    ids = {d["candidate_id"] for d in out}
    # good chunks fully present; mc-2 dropped (bad confidence) but mc-3 (its chunkmate) kept.
    assert {"mc-0", "mc-1", "mc-3", "mc-4"} <= ids
    for d in out:
        assert isinstance(d["confidence"], float)


def test_worker_exception_is_swallowed():
    # A fake whose chat_json raises inside the worker thread. The parallel path's
    # fut.result() must not re-raise.
    class _Boom:
        configured = True

        def chat_json(self, messages, response_schema_hint):
            raise RuntimeError("boom from worker")

    out = review_merge_candidates(_Boom(), _cands(4), batch_size=1, max_workers=4)
    assert out == []


def test_on_chunk_fires_per_chunk_and_covers_all_decisions():
    """on_chunk 每块调用一次,累计决策 == 返回决策;回调抛异常不影响返回。"""
    seen_chunks = []

    def on_chunk(decs):
        seen_chunks.append(list(decs))
        raise RuntimeError("持久化失败也不能打断 review")  # 必须被吞

    cands = _cands(5)                       # 5 候选
    decisions = review_merge_candidates(
        _ReviewLLM(), cands, batch_size=2, on_chunk=on_chunk)  # → 3 块(2,2,1)

    assert len(seen_chunks) == 3            # 每块一次
    flat = [d for chunk in seen_chunks for d in chunk]
    # on_chunk 收到的决策并集 == 函数返回的决策
    assert sorted(d["candidate_id"] for d in flat) == \
           sorted(d["candidate_id"] for d in decisions)


def test_on_chunk_runs_on_main_thread_even_with_concurrent_workers():
    """并发分支(max_workers>1)的 on_chunk 硬契约:回调永远在主线程执行。

    6 候选 + batch_size=1 -> 6 块;max_workers=4 足以让线程池真正并发调度这些
    块,但 on_chunk 必须每块调用一次、且每次都在主线程内 —— Task 3 要从这个
    回调里直接做 SQLite 写入,写入方必须是发起 review 调用的主线程。
    """
    fire_count = 0
    on_main_thread: list = []

    def on_chunk(decs):
        nonlocal fire_count
        fire_count += 1
        on_main_thread.append(threading.current_thread() is threading.main_thread())

    review_merge_candidates(
        _ReviewLLM(), _cands(6), batch_size=1, max_workers=4, on_chunk=on_chunk)

    assert fire_count == 6              # 每块恰好一次
    assert len(on_main_thread) == 6
    assert all(on_main_thread)           # 每次回调都发生在主线程
