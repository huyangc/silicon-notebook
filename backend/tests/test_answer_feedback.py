from __future__ import annotations

import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'feedback.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    from app.api import deps
    from app.core.config import get_settings
    from app.main import create_app

    get_settings.cache_clear()
    deps.repository.cache_clear()
    return TestClient(create_app())


def test_answer_feedback_is_persisted_and_changes_notebook_analytics(client):
    registration = client.post(
        "/api/auth/register",
        json={"username": "f00000001", "password": "pw"},
    ).json()
    headers = {"Authorization": f"Bearer {registration['token']}"}
    notebook = client.post(
        "/api/notebooks",
        headers=headers,
        json={"name": "反馈验证"},
    ).json()

    from app.api.deps import repository

    created_at = "2026-08-31T10:00:00+00:00"
    with repository()._write() as db:
        db.execute(
            "INSERT INTO answers(id,notebook_id,question,payload,created_at) "
            "VALUES (?,?,?,?,?)",
            ("answer-feedback", notebook["id"], "这个回答有用吗？", "{}", created_at),
        )

    response = client.post(
        "/api/answers/answer-feedback/feedback",
        headers=headers,
        json={"rating": "useful", "comment": ""},
    )
    assert response.status_code == 200
    assert response.json()["rating"] == "useful"

    analytics = client.get(
        f"/api/notebooks/{notebook['id']}/analytics",
        headers=headers,
    ).json()
    assert analytics["feedback_useful"] == 1
    assert analytics["feedback_not_useful"] == 0
    assert analytics["usefulness_rate"] == 1.0
