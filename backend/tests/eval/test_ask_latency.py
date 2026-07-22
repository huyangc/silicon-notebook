"""Tests for ask_latency aggregator (TDD first-pass).

Event schema (from .local/logs/events.jsonl / sqlite_repository.py:3222-3225):
    {"kind": "ask_stage", "notebook_id": "...", "stage": "<name>",
     "latency_ms": <int>, "ts": "...", "channel": "events"}

Percentile method: nearest-rank (ceiling), matching speed.py convention.
  p50 = values[ceil(0.50 * n) - 1]  (1-indexed; clamp to last element)
  p95 = values[min(n-1, ceil(0.95 * n) - 1)]
For n=5: p50 = values[2] (3rd), p95 = values[4] (5th).
"""
import json
import pytest
from app.eval.ask_latency import aggregate_stage_latencies, read_ask_stage_records


# ---------------------------------------------------------------------------
# aggregate_stage_latencies
# ---------------------------------------------------------------------------

def _make_record(stage: str, latency_ms: int) -> dict:
    return {"kind": "ask_stage", "stage": stage, "latency_ms": latency_ms,
            "notebook_id": "nb-test", "ts": "2026-06-09T00:00:00", "channel": "events"}


class TestAggregateStageLatencies:
    """Pure-function tests; no I/O."""

    def test_basic_single_stage(self):
        records = [_make_record("score", v) for v in [10, 20, 30, 40, 100]]
        result = aggregate_stage_latencies(records)
        assert "score" in result
        s = result["score"]
        assert s["count"] == 5
        # sorted: [10, 20, 30, 40, 100]
        # p50: ceil(0.50*5)=3 -> idx 2 -> 30
        assert s["p50"] == 30.0
        # p95: ceil(0.95*5)=5 -> idx 4 -> 100
        assert s["p95"] == 100.0
        assert s["max"] == 100.0

    def test_two_stages_independent(self):
        records = (
            [_make_record("score", v) for v in [10, 20, 30, 40, 100]]
            + [_make_record("answer_llm", v) for v in [1000, 2000, 3000]]
        )
        result = aggregate_stage_latencies(records)
        assert set(result.keys()) == {"score", "answer_llm"}

        s = result["score"]
        assert s["count"] == 5
        assert s["p50"] == 30.0
        assert s["p95"] == 100.0
        assert s["max"] == 100.0

        a = result["answer_llm"]
        assert a["count"] == 3
        # sorted: [1000, 2000, 3000]
        # p50: ceil(0.50*3)=2 -> idx 1 -> 2000
        assert a["p50"] == 2000.0
        # p95: ceil(0.95*3)=3 -> idx 2 -> 3000
        assert a["p95"] == 3000.0
        assert a["max"] == 3000.0

    def test_single_element(self):
        """Single value: p50 == p95 == max == that value."""
        result = aggregate_stage_latencies([_make_record("total", 42)])
        t = result["total"]
        assert t["count"] == 1
        assert t["p50"] == 42.0
        assert t["p95"] == 42.0
        assert t["max"] == 42.0

    def test_empty_returns_empty_dict(self):
        assert aggregate_stage_latencies([]) == {}

    def test_non_ask_stage_records_ignored(self):
        """Records with kind != 'ask_stage' must be silently ignored."""
        records = [
            {"kind": "request", "stage": "score", "latency_ms": 999},
            _make_record("score", 50),
        ]
        result = aggregate_stage_latencies(records)
        assert result["score"]["count"] == 1
        assert result["score"]["p50"] == 50.0

    def test_missing_stage_field_ignored(self):
        """Records that lack 'stage' must not crash."""
        records = [
            {"kind": "ask_stage", "latency_ms": 100},  # no 'stage'
            _make_record("load_indexes", 55),
        ]
        result = aggregate_stage_latencies(records)
        assert list(result.keys()) == ["load_indexes"]

    def test_missing_latency_ms_ignored(self):
        """Records that lack 'latency_ms' must not crash."""
        records = [
            {"kind": "ask_stage", "stage": "score"},  # no latency_ms
            _make_record("score", 77),
        ]
        result = aggregate_stage_latencies(records)
        assert result["score"]["count"] == 1


# ---------------------------------------------------------------------------
# read_ask_stage_records (JSONL reader)
# ---------------------------------------------------------------------------

class TestReadAskStageRecords:
    def test_reads_valid_records(self, tmp_path):
        p = tmp_path / "events.jsonl"
        lines = [
            {"kind": "ask_stage", "stage": "score", "latency_ms": 10, "channel": "events"},
            {"kind": "ask_stage", "stage": "total", "latency_ms": 80, "channel": "events"},
        ]
        p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        records = list(read_ask_stage_records(str(p)))
        assert len(records) == 2
        assert records[0]["stage"] == "score"
        assert records[1]["stage"] == "total"

    def test_skips_malformed_lines(self, tmp_path):
        p = tmp_path / "events.jsonl"
        p.write_text(
            '{"kind": "ask_stage", "stage": "score", "latency_ms": 10}\n'
            'this is not json\n'
            '{broken\n'
            '{"kind": "ask_stage", "stage": "total", "latency_ms": 80}\n'
        )
        records = list(read_ask_stage_records(str(p)))
        assert len(records) == 2

    def test_skips_non_ask_stage_kind(self, tmp_path):
        p = tmp_path / "events.jsonl"
        p.write_text(
            '{"kind": "request", "path": "/ask", "latency_ms": 500}\n'
            '{"kind": "ask_stage", "stage": "answer_llm", "latency_ms": 1200}\n'
        )
        records = list(read_ask_stage_records(str(p)))
        assert len(records) == 1
        assert records[0]["stage"] == "answer_llm"

    def test_returns_empty_for_empty_file(self, tmp_path):
        p = tmp_path / "events.jsonl"
        p.write_text("")
        assert list(read_ask_stage_records(str(p))) == []

    def test_returns_empty_for_missing_file(self, tmp_path):
        missing = str(tmp_path / "no_such_file.jsonl")
        assert list(read_ask_stage_records(missing)) == []

    def test_last_n_limits_records(self, tmp_path):
        """--last N keeps only the last N records from the file."""
        p = tmp_path / "events.jsonl"
        lines = [{"kind": "ask_stage", "stage": "score", "latency_ms": i}
                 for i in range(10)]
        p.write_text("\n".join(json.dumps(x) for x in lines) + "\n")
        records = list(read_ask_stage_records(str(p), last_n=3))
        assert len(records) == 3
        # last 3 records have latency_ms 7, 8, 9
        assert [r["latency_ms"] for r in records] == [7, 8, 9]

    def test_blank_lines_skipped(self, tmp_path):
        p = tmp_path / "events.jsonl"
        p.write_text(
            '\n'
            '{"kind": "ask_stage", "stage": "expand", "latency_ms": 5}\n'
            '\n'
        )
        records = list(read_ask_stage_records(str(p)))
        assert len(records) == 1

    def test_aggregates_owner_subdirs(self, tmp_path):
        """全局文件不存在时，聚合读取所有用户子目录下的 events.jsonl。"""
        (tmp_path / "user-3a8f9c2b1d").mkdir()
        (tmp_path / "user-local").mkdir()
        (tmp_path / "user-3a8f9c2b1d" / "events.jsonl").write_text(
            json.dumps({"kind": "ask_stage", "stage": "score", "latency_ms": 10}) + "\n")
        (tmp_path / "user-local" / "events.jsonl").write_text(
            json.dumps({"kind": "ask_stage", "stage": "score", "latency_ms": 20}) + "\n")
        recs = list(read_ask_stage_records(str(tmp_path / "events.jsonl")))
        assert sorted(r["latency_ms"] for r in recs) == [10, 20]

    def test_survives_directory_in_read_set(self, tmp_path):
        """A path in the read-set that is a *directory* (e.g. a per-user subdir
        holding a dir literally named events.jsonl) must be skipped, not crash
        the whole aggregation with IsADirectoryError."""
        (tmp_path / "events.jsonl").write_text(
            json.dumps({"kind": "ask_stage", "stage": "score", "latency_ms": 10}) + "\n")
        # <log_dir>/user-x/events.jsonl exists but is a directory -> glob yields it
        (tmp_path / "user-x").mkdir()
        (tmp_path / "user-x" / "events.jsonl").mkdir()
        recs = list(read_ask_stage_records(str(tmp_path / "events.jsonl")))
        assert sorted(r["latency_ms"] for r in recs) == [10]

    def test_survives_non_utf8_bytes(self, tmp_path):
        """A non-UTF8 byte in a log line must not raise UnicodeDecodeError; the
        offending line is dropped (non-JSON after replacement) and valid lines
        are still read."""
        p = tmp_path / "events.jsonl"
        good = json.dumps({"kind": "ask_stage", "stage": "score", "latency_ms": 5})
        p.write_bytes(good.encode("utf-8") + b"\n\xff\xfe not valid utf8\n")
        recs = list(read_ask_stage_records(str(p)))
        assert [r["latency_ms"] for r in recs] == [5]

    def test_reads_gzipped_dated_file(self, tmp_path):
        """expand_channel_paths (log_reader.py) now also returns archived
        dated files (`events-YYYY-MM-DD.jsonl.gz`, gzipped by the background
        archiver once a day rolls over). Previously this reader's own
        `errors="replace"` on a plain read_text() decoded the gzip binary
        bytes to noise that never parses as JSON, so archived days silently
        vanished from the aggregation instead of raising — the very bug this
        aggregator exists to avoid, just relocated to a quieter failure mode."""
        import gzip
        gz_path = tmp_path / "events-2026-07-19.jsonl.gz"
        with gzip.open(gz_path, "wt", encoding="utf-8") as fh:
            fh.write(json.dumps({"kind": "ask_stage", "stage": "score", "latency_ms": 77}) + "\n")
        records = list(read_ask_stage_records(str(tmp_path / "events.jsonl")))
        assert [r["latency_ms"] for r in records] == [77]
