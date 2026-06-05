import json

from fastapi.testclient import TestClient


def _make_client(tmp_path, monkeypatch, *, enabled=True, lines=None, channel="llm"):
    logs = tmp_path / "logs"
    logs.mkdir(exist_ok=True)
    if lines is not None:
        (logs / f"{channel}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setenv("EVENT_LOG_DIR", str(logs))  # absolute -> used as-is
    monkeypatch.setenv("DEBUG_LOGS_ENABLED", "true" if enabled else "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.main import create_app

    return TestClient(create_app())


CHAT = json.dumps({
    "ts": "2026-01-01T00:00:00", "id": "llm-a", "kind": "chat", "model": "m1",
    "request": {"messages": [{"role": "system", "content": "SYS"},
                             {"role": "user", "content": "hello world"}], "schema_hint": "H"},
    "status": "ok", "latency_ms": 100, "usage": {"total_tokens": 12},
    "response": {"content": "{}"},
})
EMB = json.dumps({
    "ts": "2026-01-01T00:00:01", "id": "llm-b", "kind": "embed", "model": "e1",
    "status": "ok", "latency_ms": 20, "usage": {"total_tokens": 3},
    "input_chars": 50, "dims": 1024,
})
ERR = json.dumps({
    "ts": "2026-01-01T00:00:02", "id": "llm-c", "kind": "chat", "model": "m1",
    "request": {"messages": [{"role": "user", "content": "boom"}]},
    "status": "error", "latency_ms": 5, "error": "RuntimeError: nope",
})


def test_disabled_returns_404(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, enabled=False, lines=[CHAT])
    assert c.get("/api/debug/logs").status_code == 404
    assert c.get("/api/debug/logs/llm").status_code == 404


def test_unknown_channel_404(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=[CHAT])
    assert c.get("/api/debug/logs/secrets").status_code == 404


def test_list_channels(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=[CHAT, EMB])
    chans = {ch["name"]: ch for ch in c.get("/api/debug/logs").json()["channels"]}
    assert chans["llm"]["exists"] is True and chans["llm"]["count"] == 2
    assert chans["events"]["exists"] is False


def test_list_records_and_stats(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=[CHAT, EMB, ERR])
    body = c.get("/api/debug/logs/llm").json()
    assert body["file_exists"] is True
    assert [r["id"] for r in body["records"]] == ["llm-c", "llm-b", "llm-a"]  # newest seq first
    assert body["stats"]["total"] == 3
    assert body["newest_seq"] == 2
    assert sorted(body["stats"]["facets"]["kinds"]) == ["chat", "embed"]


def test_filters_and_search(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=[CHAT, EMB, ERR])
    assert {r["id"] for r in c.get("/api/debug/logs/llm?kind=embed").json()["records"]} == {"llm-b"}
    assert {r["id"] for r in c.get("/api/debug/logs/llm?status=error").json()["records"]} == {"llm-c"}
    assert {r["id"] for r in c.get("/api/debug/logs/llm?q=HELLO").json()["records"]} == {"llm-a"}


def test_pagination(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=[CHAT, EMB, ERR])
    first = c.get("/api/debug/logs/llm?limit=2").json()
    assert [r["seq"] for r in first["records"]] == [2, 1] and first["has_more"] is True
    older = c.get("/api/debug/logs/llm?before=1").json()
    assert [r["seq"] for r in older["records"]] == [0]
    newer = c.get("/api/debug/logs/llm?since=1").json()
    assert [r["seq"] for r in newer["records"]] == [2]


def test_detail_by_id_and_404(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=[CHAT, EMB])
    rec = c.get("/api/debug/logs/llm/llm-a").json()
    assert rec["request"]["messages"][0]["content"] == "SYS"
    assert c.get("/api/debug/logs/llm/llm-zzz").status_code == 404


def test_missing_file_empty(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=None)
    body = c.get("/api/debug/logs/llm").json()
    assert body["file_exists"] is False and body["records"] == []
    assert body["stats"]["total"] == 0


def test_malformed_line_skipped(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=[CHAT, "NOT JSON", EMB])
    body = c.get("/api/debug/logs/llm").json()
    assert body["stats"]["malformed_lines"] == 1
    assert len(body["records"]) == 2


def test_list_channels_count_excludes_malformed(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=[CHAT, "NOT JSON", EMB])
    chans = {ch["name"]: ch for ch in c.get("/api/debug/logs").json()["channels"]}
    assert chans["llm"]["count"] == 2  # parsed records only; matches stats.total
    assert c.get("/api/debug/logs/llm").json()["stats"]["total"] == 2
