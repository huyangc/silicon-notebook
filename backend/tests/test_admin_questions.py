from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'questions.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
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
    assert response.status_code == 200
    body = response.json()
    return {"Authorization": f"Bearer {body['token']}"}, body["user"]["id"]


def _admin(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin"}
    )
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_admin_questions_combines_ask_and_report_with_filters(client):
    user_headers, user_id = _register(client, "d00000004")
    notebook = client.post(
        "/api/notebooks", headers=user_headers, json={"name": "模拟电路"}
    ).json()
    from app.api.deps import repository

    now = "2026-08-31T08:00:00+00:00"
    full_ask_question = "如何降低噪声？这是历史已完成提问的尾部检索词"
    with repository()._write() as db:
        db.execute(
            "INSERT INTO answers(id,notebook_id,question,payload,created_at) "
            "VALUES (?,?,?,?,?)",
            ("answer-global", notebook["id"], full_ask_question, "{}", now),
        )
        db.execute(
            "INSERT INTO ask_jobs(id,notebook_id,conversation_id,created_by,mode,question,status,answer_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("ask-global", notebook["id"], "", user_id, "chunk", "如何降低噪声？", "completed", "answer-global", now, now),
        )
        db.execute(
            "INSERT INTO reports(id,notebook_id,question,created_by,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("report-global", notebook["id"], "分析放大器稳定性", user_id, "done", now, now),
        )

    assert client.get("/api/admin/questions", headers=user_headers).status_code == 403
    admin = _admin(client)
    response = client.get("/api/admin/questions", headers=admin)
    assert response.status_code == 200
    body = response.json()
    assert body["stats"] == {"total": 2, "asks": 1, "reports": 1, "active_users": 1}
    assert {item["type"] for item in body["items"]} == {"ask", "report"}
    assert all(item["username"] == "d00000004" for item in body["items"])
    assert all(item["notebook_name"] == "模拟电路" for item in body["items"])
    ask_item = next(item for item in body["items"] if item["type"] == "ask")
    assert ask_item["question"] == full_ask_question

    ask_tail = client.get("/api/admin/questions?kind=ask&q=尾部检索词", headers=admin).json()
    assert ask_tail["total"] == 1
    assert ask_tail["items"][0]["question"] == full_ask_question

    filtered = client.get("/api/admin/questions?kind=report&q=稳定", headers=admin).json()
    assert filtered["total"] == 1
    assert filtered["stats"] == {"total": 1, "asks": 0, "reports": 1, "active_users": 1}
    assert filtered["items"][0]["question"] == "分析放大器稳定性"

    # 批 3·W1 PR-3:DELETE 现在是 202(tombstone CAS 立即返回),实际归档由
    # 后台删除作业完成——drain 等它跑完,下面的断言才看得到 retained_user_activity。
    assert client.delete(f"/api/notebooks/{notebook['id']}", headers=user_headers).status_code == 202
    from app.services import background_jobs
    background_jobs._drain_maintenance_executors_for_tests(timeout=10.0)
    retained = client.get("/api/admin/questions", headers=admin).json()
    assert retained["stats"] == {"total": 2, "asks": 1, "reports": 1, "active_users": 1}
    assert {item["id"] for item in retained["items"]} == {"ask-global", "report-global"}
    assert all(item["notebook_name"] == "模拟电路" for item in retained["items"])
    retained_ask = next(item for item in retained["items"] if item["type"] == "ask")
    assert retained_ask["question"] == full_ask_question

    assert client.get(
        f"/api/admin/questions?q={'😀' * 200}&limit=200", headers=admin
    ).status_code == 200
    assert client.get(
        f"/api/admin/questions?q={'😀' * 201}", headers=admin
    ).status_code == 422
    assert client.get("/api/admin/questions?limit=201", headers=admin).status_code == 422
