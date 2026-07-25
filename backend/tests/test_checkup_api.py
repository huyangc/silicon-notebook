"""P2·T3: GET /notebooks/{id}/checkup 端点。

service 层(H2–H8 判据/缓存)由 test_checkup_service 充分覆盖;这里只验证端点接线:
只读守卫 + 响应序列化(内部代号 code/fix)+ 健康态 + 一个真损坏(H3)被如实报出。
"""
from __future__ import annotations

from fastapi.testclient import TestClient

_NOW = "2026-01-01T00:00:00"


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'checkup.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")

    from app.api import deps
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    deps.repository.cache_clear()
    return TestClient(create_app())


def _register(client: TestClient, username: str) -> tuple[dict[str, str], str]:
    response = client.post(
        "/api/auth/register", json={"username": username, "password": "pw"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return {"Authorization": f"Bearer {body['token']}"}, body["user"]["id"]


def _notebook(client: TestClient, headers: dict[str, str], name: str) -> str:
    response = client.post("/api/notebooks", headers=headers, json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _seed_damaged_source(repo, notebook_id: str) -> str:
    """有 elements、chunked_at IS NULL、终态已 parsed 的用户源 = H3 缺分块损坏。"""
    sid = "src-damaged"
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "created_at,updated_at,chunked_at) "
            "VALUES (?,?,?,?,'extracted','extracted',?,?,NULL)",
            (sid, notebook_id, "S", "document", _NOW, _NOW),
        )
        db.execute(
            "INSERT INTO source_elements (id,source_id,element_type,location_label,"
            "text,metadata,created_at) VALUES (?,?,'paragraph','p1','body','{}',?)",
            (f"el-{sid}", sid, _NOW),
        )
    return sid


def _check(body: dict, code: str) -> dict:
    return next(c for c in body["checks"] if c["code"] == code)


def test_checkup_healthy_empty_notebook(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    headers, _ = _register(client, "a00200201")
    nb = _notebook(client, headers, "healthy")

    response = client.get(f"/api/notebooks/{nb}/checkup", headers=headers)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["notebook_id"] == nb
    assert body["healthy"] is True
    assert {c["code"] for c in body["checks"]} == {"H2", "H3", "H4", "H5", "H6", "H7", "H8"}
    assert all(c["count"] == 0 for c in body["checks"])


def test_checkup_reports_h3_missing_chunks(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    headers, _ = _register(client, "b00200202")
    nb = _notebook(client, headers, "damaged")

    from app.api.deps import repository

    sid = _seed_damaged_source(repository(), nb)

    body = client.get(f"/api/notebooks/{nb}/checkup", headers=headers).json()
    h3 = _check(body, "H3")
    assert h3["count"] == 1
    assert h3["sample"] == [sid]
    assert h3["fix"] == "reparse"
    assert body["healthy"] is False


def test_checkup_uniform_not_found_for_stranger_and_missing(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_headers, _ = _register(client, "c00200203")
    stranger_headers, _ = _register(client, "d00200204")
    nb = _notebook(client, owner_headers, "private")

    stranger = client.get(f"/api/notebooks/{nb}/checkup", headers=stranger_headers)
    missing = client.get("/api/notebooks/missing/checkup", headers=owner_headers)
    assert stranger.status_code == 404
    assert missing.status_code == 404
    assert stranger.json() == missing.json()
