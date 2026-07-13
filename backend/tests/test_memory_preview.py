from __future__ import annotations

from fastapi.testclient import TestClient

from app.models.schemas import AskResponse


def test_preview_falls_back_deterministically_without_persisting(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'preview.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import create_app

    client = TestClient(create_app())
    registered = client.post(
        "/api/auth/register", json={"username": "g00100007", "password": "pw"}
    ).json()
    headers = {"Authorization": f"Bearer {registered['token']}"}
    notebook_id = client.post(
        "/api/notebooks", headers=headers, json={"name": "Preview"}
    ).json()["id"]

    from app.api.deps import repository

    repo = repository()
    question = "Q" * 90
    answer_id = repo._runtime.ask_state.save_answer(
        notebook_id,
        None,
        question,
        AskResponse(
            conclusion="Use $A_v$ [1], then verify margin [2, 3] and [k4].",
            answer="Use $A_v$ [1], then verify margin [2, 3] and [k4].",
        ),
        registered["user"]["id"],
    )

    preview = client.post(f"/api/answers/{answer_id}/memory-preview", headers=headers)
    assert preview.status_code == 200, preview.text
    assert preview.json() == {
        "title": question[:80],
        "content_md": "Use $A_v$, then verify margin and.",
        "tags": [],
        "provenance_summary": {
            "answer_id": answer_id,
            "notebook_id": notebook_id,
            "evidence_level": "inferred",
            "citation_count": 0,
        },
    }
    assert client.get("/api/memories", headers=headers).json()["total_count"] == 0


def test_preview_llm_failure_uses_same_fallback(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'preview-fail.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import create_app

    client = TestClient(create_app())
    registered = client.post(
        "/api/auth/register", json={"username": "h00100008", "password": "pw"}
    ).json()
    headers = {"Authorization": f"Bearer {registered['token']}"}
    notebook_id = client.post(
        "/api/notebooks", headers=headers, json={"name": "Preview fail"}
    ).json()["id"]

    from app.api.deps import repository

    repo = repository()
    answer_id = repo._runtime.ask_state.save_answer(
        notebook_id,
        None,
        "Fallback title",
        AskResponse(conclusion="Fallback body", answer="Fallback body"),
        registered["user"]["id"],
    )

    class FailingClient:
        configured = True

        def chat_json(self, *args, **kwargs):
            raise RuntimeError("model unavailable")

    repo.llm_client = FailingClient()
    preview = client.post(f"/api/answers/{answer_id}/memory-preview", headers=headers)
    assert preview.status_code == 200
    assert preview.json()["title"] == "Fallback title"
    assert preview.json()["content_md"] == "Fallback body"
