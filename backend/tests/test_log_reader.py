from app.services import log_reader


def _write(tmp_path, lines):
    p = tmp_path / "llm.jsonl"
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


CHAT_OK = (
    '{"ts":"2026-01-01T00:00:00","id":"llm-a","kind":"chat","model":"m1",'
    '"request":{"messages":[{"role":"system","content":"SYS"},'
    '{"role":"user","content":"hello world"}],"schema_hint":"H"},'
    '"status":"ok","latency_ms":100,'
    '"usage":{"prompt_tokens":5,"completion_tokens":7,"total_tokens":12},'
    '"response":{"content":"{\\"k\\":1}"}}'
)
EMBED_OK = (
    '{"ts":"2026-01-01T00:00:01","id":"llm-b","kind":"embed","model":"e1",'
    '"status":"ok","latency_ms":20,"usage":{"total_tokens":3},'
    '"input_chars":50,"dims":1024}'
)
CHAT_ERR = (
    '{"ts":"2026-01-01T00:00:02","id":"llm-c","kind":"chat","model":"m1",'
    '"request":{"messages":[{"role":"user","content":"boom"}]},'
    '"status":"error","latency_ms":5,"error":"RuntimeError: nope"}'
)


def test_load_assigns_seq_and_skips_malformed(tmp_path):
    p = _write(tmp_path, [CHAT_OK, "NOT JSON", EMBED_OK, ""])
    records, malformed = log_reader.load_records(p)
    assert [r["id"] for r in records] == ["llm-a", "llm-b"]
    assert [r["seq"] for r in records] == [0, 2]  # line index preserved (append-only)
    assert malformed == 1


def test_load_missing_file(tmp_path):
    records, malformed = log_reader.load_records(tmp_path / "nope.jsonl")
    assert records == [] and malformed == 0


def test_filter_by_kind_status_model(tmp_path):
    records, _ = log_reader.load_records(_write(tmp_path, [CHAT_OK, EMBED_OK, CHAT_ERR]))
    assert {r["id"] for r in log_reader.filter_records(records, kind="chat")} == {"llm-a", "llm-c"}
    assert {r["id"] for r in log_reader.filter_records(records, status="error")} == {"llm-c"}
    assert {r["id"] for r in log_reader.filter_records(records, model="e1")} == {"llm-b"}
    # combined filters AND together
    assert {r["id"] for r in log_reader.filter_records(records, kind="chat", status="ok")} == {"llm-a"}


def test_search_matches_messages_and_error(tmp_path):
    records, _ = log_reader.load_records(_write(tmp_path, [CHAT_OK, CHAT_ERR]))
    assert {r["id"] for r in log_reader.filter_records(records, q="HELLO")} == {"llm-a"}
    assert {r["id"] for r in log_reader.filter_records(records, q="nope")} == {"llm-c"}


def test_summary_and_preview(tmp_path):
    records, _ = log_reader.load_records(_write(tmp_path, [CHAT_OK, EMBED_OK, CHAT_ERR]))
    by_id = {r["id"]: log_reader.to_summary(r) for r in records}
    assert by_id["llm-a"]["total_tokens"] == 12
    assert by_id["llm-a"]["preview"] == "hello world"
    assert "input_chars=50" in by_id["llm-b"]["preview"]
    assert by_id["llm-c"]["preview"] == "RuntimeError: nope"
    assert by_id["llm-c"]["error"] == "RuntimeError: nope"


def test_stats_counts_filtered_facets_full(tmp_path):
    records, malformed = log_reader.load_records(_write(tmp_path, [CHAT_OK, EMBED_OK, CHAT_ERR]))
    filtered = log_reader.filter_records(records, kind="chat")
    stats = log_reader.compute_stats(records, filtered, malformed)
    assert stats["total"] == 3 and stats["filtered"] == 2
    assert stats["by_kind"] == {"chat": 2}              # over filtered set
    assert stats["by_status"] == {"ok": 1, "error": 1}  # over filtered set
    assert stats["total_tokens"] == 12
    assert stats["latency_ms"]["max"] == 100
    assert stats["facets"]["kinds"] == ["chat", "embed"]  # over FULL set, sorted
    assert set(stats["facets"]["models"]) == {"m1", "e1"}


def test_paginate_before_since_limit(tmp_path):
    lines = ['{"id":"llm-%d","kind":"chat","model":"m","status":"ok","latency_ms":1}' % i for i in range(5)]
    records, _ = log_reader.load_records(_write(tmp_path, lines))
    desc = sorted(records, key=lambda r: r["seq"], reverse=True)  # seq 4..0
    page, has_more = log_reader.paginate(desc, before=None, since=None, limit=2)
    assert [r["seq"] for r in page] == [4, 3] and has_more is True
    older, _ = log_reader.paginate(desc, before=3, since=None, limit=10)
    assert [r["seq"] for r in older] == [2, 1, 0]
    newer, _ = log_reader.paginate(desc, before=None, since=2, limit=10)
    assert [r["seq"] for r in newer] == [4, 3]
    # boundary: seq=0 must not leak through `before=0` (regression for falsy-0 bug)
    bottom, has_more_bottom = log_reader.paginate(desc, before=0, since=None, limit=10)
    assert bottom == [] and has_more_bottom is False
    # boundary: since=0 returns strictly-newer records (excludes seq=0)
    newest, _ = log_reader.paginate(desc, before=None, since=0, limit=10)
    assert [r["seq"] for r in newest] == [4, 3, 2, 1]
