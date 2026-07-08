import json
from pathlib import Path
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("DEBUG_LOGS_ENABLED", "true")
    monkeypatch.setenv("EVENT_LOG_DIR", str(tmp_path / "logs"))
    from app.core.config import get_settings
    get_settings.cache_clear()
    from app.api import deps
    deps.repository.cache_clear()
    from app.main import create_app
    return TestClient(create_app())


def _auth_admin(client):
    t = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    return {"Authorization": f"Bearer {t}"}


def _seed_day(tmp_path, owner, channel, day, objs):
    d = tmp_path / "logs" / owner
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{channel}-{day}.jsonl").write_text(
        "".join(json.dumps(o) + "\n" for o in objs), encoding="utf-8")


def test_days_endpoint_lists_days(client, tmp_path):
    admin = _auth_admin(client)
    # admin 自己的 owner 目录名 = 其 user.id；先查出来
    me = client.get("/api/me", headers=admin).json()
    owner = me["id"]
    _seed_day(tmp_path, owner, "llm", "2026-07-07", [{"id": "a", "kind": "chat"}])
    r = client.get("/api/debug/logs/llm/days", headers=admin)
    assert r.status_code == 200 and "2026-07-07" in r.json()["days"]


def test_date_param_reads_that_day_and_rejects_bad(client, tmp_path):
    admin = _auth_admin(client)
    owner = client.get("/api/me", headers=admin).json()["id"]
    _seed_day(tmp_path, owner, "llm", "2026-07-07", [{"id": "a", "kind": "chat"}])
    ok = client.get("/api/debug/logs/llm?date=2026-07-07", headers=admin)
    assert ok.status_code == 200 and ok.json()["date"] == "2026-07-07"
    assert len(ok.json()["records"]) == 1 and "truncated" in ok.json()
    bad = client.get("/api/debug/logs/llm?date=../etc", headers=admin)
    assert bad.status_code in (400, 404, 422)


def test_get_record_unknown_channel_404(client):
    admin = _auth_admin(client)
    r = client.get("/api/debug/logs/nope/some-id", headers=admin)
    assert r.status_code == 404 and "unknown channel" in r.json()["detail"]


def test_get_record_by_seq_direct_read(client, tmp_path):
    admin = _auth_admin(client)
    owner = client.get("/api/me", headers=admin).json()["id"]
    _seed_day(tmp_path, owner, "llm", "2026-07-07",
              [{"id": "a", "kind": "chat"}, {"id": "b", "kind": "embed"}])
    listed = client.get("/api/debug/logs/llm?date=2026-07-07", headers=admin).json()
    rec_b = next(r for r in listed["records"] if r["id"] == "b")
    detail = client.get(
        f"/api/debug/logs/llm/b?date=2026-07-07&seq={rec_b['seq']}", headers=admin)
    assert detail.status_code == 200 and detail.json()["id"] == "b"
