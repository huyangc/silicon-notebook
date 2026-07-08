import json
from datetime import datetime

from fastapi.testclient import TestClient

# 端点现按天分文件、且 date 缺省=今天；测试种子日志一律写今天的日期分文件，
# 靠端点默认参数读到（与 test_event_logging.py 的既有做法一致）。
_TODAY = datetime.now().strftime("%Y-%m-%d")


def _make_client(tmp_path, monkeypatch, *, enabled=True, lines=None, channel="llm", owner="user-local"):
    logs = tmp_path / "logs"
    (logs / owner).mkdir(parents=True, exist_ok=True)
    if lines is not None:
        (logs / owner / f"{channel}-{_TODAY}.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    monkeypatch.setenv("EVENT_LOG_DIR", str(logs))  # absolute -> used as-is
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")  # 隔离 DB（seeded admin=user-local）
    if enabled is not None:
        monkeypatch.setenv("DEBUG_LOGS_ENABLED", "true" if enabled else "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.core.config import get_settings
    get_settings.cache_clear()
    from app.api import deps
    deps.repository.cache_clear()
    from app.main import create_app
    return TestClient(create_app())


def _login(username="a00123456"):
    """在 client 的同一 tmp DB 单例里造用户并发 session token。返回 (user, token)。"""
    from app.api import deps
    repo = deps.repository()
    user = repo.create_user(username, "pw")
    return user, repo.create_session(user.id)


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


def test_debug_logs_are_disabled_by_default(tmp_path, monkeypatch):
    monkeypatch.delenv("DEBUG_LOGS_ENABLED", raising=False)
    c = _make_client(tmp_path, monkeypatch, enabled=None, lines=[CHAT])
    assert c.get("/api/debug/logs").status_code == 404


def test_unknown_channel_404(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=[CHAT])
    assert c.get("/api/debug/logs/secrets").status_code == 404


def test_list_channels(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=[CHAT, EMB])
    expected_bytes = len((CHAT + "\n" + EMB + "\n").encode("utf-8"))
    chans = {ch["name"]: ch for ch in c.get("/api/debug/logs").json()["channels"]}
    # count（解析后记录数）已改为 bytes（今天文件的原始字节数，不解析）——见任务 6 契约变更。
    assert chans["llm"]["exists"] is True and chans["llm"]["bytes"] == expected_bytes
    assert chans["events"]["exists"] is False and chans["events"]["bytes"] == 0


def test_list_records_and_stats(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=[CHAT, EMB, ERR])
    # seq 已从「行索引」改为「文件内字节偏移」（按天窗口读取，支持 O(1) 尾部切片）——
    # 见 log_reader._parse_blob；末行（ERR）的字节偏移 = 前两行(含换行)的字节长度。
    err_seq = len((CHAT + "\n" + EMB + "\n").encode("utf-8"))
    body = c.get("/api/debug/logs/llm").json()
    assert body["file_exists"] is True
    assert [r["id"] for r in body["records"]] == ["llm-c", "llm-b", "llm-a"]  # newest seq first
    assert body["stats"]["total"] == 3
    assert body["newest_seq"] == err_seq
    assert sorted(body["stats"]["facets"]["kinds"]) == ["chat", "embed"]


def test_filters_and_search(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=[CHAT, EMB, ERR])
    assert {r["id"] for r in c.get("/api/debug/logs/llm?kind=embed").json()["records"]} == {"llm-b"}
    assert {r["id"] for r in c.get("/api/debug/logs/llm?status=error").json()["records"]} == {"llm-c"}
    assert {r["id"] for r in c.get("/api/debug/logs/llm?q=HELLO").json()["records"]} == {"llm-a"}


def test_pagination(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=[CHAT, EMB, ERR])
    # seq 现为字节偏移（非行索引）：seq0=CHAT 行起点=0；seq1=EMB 行起点；seq2=ERR 行起点。
    # before/since 用 seq1（中间记录的 seq）当游标，与原测试「以中间记录为界」的意图一致。
    seq0 = 0
    seq1 = len((CHAT + "\n").encode("utf-8"))
    seq2 = len((CHAT + "\n" + EMB + "\n").encode("utf-8"))
    first = c.get("/api/debug/logs/llm?limit=2").json()
    assert [r["seq"] for r in first["records"]] == [seq2, seq1] and first["has_more"] is True
    older = c.get(f"/api/debug/logs/llm?before={seq1}").json()
    assert [r["seq"] for r in older["records"]] == [seq0]
    newer = c.get(f"/api/debug/logs/llm?since={seq1}").json()
    assert [r["seq"] for r in newer["records"]] == [seq2]


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


def test_list_channels_bytes_is_raw_size_but_stats_total_excludes_malformed(tmp_path, monkeypatch):
    # channel 列表的 bytes 是原始文件字节数（不解析，含 malformed 行）；
    # 而 /{channel} 详情的 stats.total 仍只统计解析成功的记录（malformed 行被排除）。
    # 原测试名断言 count（解析记录数）排除 malformed——该指标已被 bytes 取代，
    # 这里改为验证两个指标各自的正确行为，不再是同一枚数字的两处引用。
    c = _make_client(tmp_path, monkeypatch, lines=[CHAT, "NOT JSON", EMB])
    expected_bytes = len((CHAT + "\n" + "NOT JSON" + "\n" + EMB + "\n").encode("utf-8"))
    chans = {ch["name"]: ch for ch in c.get("/api/debug/logs").json()["channels"]}
    assert chans["llm"]["bytes"] == expected_bytes
    assert c.get("/api/debug/logs/llm").json()["stats"]["total"] == 2


def test_normal_user_sees_only_own(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=None)
    user, token = _login("a00123456")
    (tmp_path / "logs" / user.id).mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs" / user.id / f"llm-{_TODAY}.jsonl").write_text(CHAT + "\n", encoding="utf-8")
    h = {"Authorization": f"Bearer {token}"}
    body = c.get("/api/debug/logs/llm", headers=h).json()
    assert [r["id"] for r in body["records"]] == ["llm-a"]


def test_normal_user_cannot_read_others_owner(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=None)
    user, token = _login("a00123456")
    h = {"Authorization": f"Bearer {token}"}
    # 合法 id 形态但不是自己 → 403（非 admin）
    assert c.get("/api/debug/logs/llm?owner=user-deadbeef01", headers=h).status_code == 403


def test_admin_can_read_any_owner(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=None)  # 无 token → seeded admin
    (tmp_path / "logs" / "user-deadbeef01").mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs" / "user-deadbeef01" / f"llm-{_TODAY}.jsonl").write_text(CHAT + "\n", encoding="utf-8")
    body = c.get("/api/debug/logs/llm?owner=user-deadbeef01").json()
    assert [r["id"] for r in body["records"]] == ["llm-a"]


def test_requests_channel_admin_only(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=None)
    user, token = _login("a00123456")
    h = {"Authorization": f"Bearer {token}"}
    assert c.get("/api/debug/logs/requests", headers=h).status_code == 403
    assert c.get("/api/debug/logs/requests").status_code == 200  # admin（无 token）可读


def test_admin_owner_traversal_rejected(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=None)
    assert c.get("/api/debug/logs/llm?owner=../../etc").status_code == 404


def test_requests_channel_hidden_from_normal_user_list(tmp_path, monkeypatch):
    c = _make_client(tmp_path, monkeypatch, lines=None)
    user, token = _login("a00123456")
    h = {"Authorization": f"Bearer {token}"}
    names = {ch["name"] for ch in c.get("/api/debug/logs", headers=h).json()["channels"]}
    assert "requests" not in names and {"events", "llm"} <= names
