from __future__ import annotations

import json
import pytest

from fastapi.testclient import TestClient

from app.models.schemas import AskResponse
from tests.model_testkit import bind_chat_client
from app.services.model_work import (
    ModelQueueFull, ModelQueueTimeout, ModelServiceUnavailable,
)


def test_citation_cleanup_preserves_code_math_and_array_indexes():
    from app.api.memory_routes import _clean_answer

    text = (
        "Result [1]. `lookup[2]` and $a[3] + b[4]$ stay. a[5] stays.\n"
        "```python\nvalues[6] = lookup[7]\n```\n"
        "Conclusion [2, 3] and [k4]."
    )
    assert _clean_answer(text) == (
        "Result. `lookup[2]` and $a[3] + b[4]$ stay. a[5] stays.\n"
        "```python\nvalues[6] = lookup[7]\n```\n"
        "Conclusion and."
    )


def test_citation_cleanup_preserves_backslash_delimited_formulas():
    from app.api.memory_routes import _clean_answer

    text = r"Inline \(x + [2]\) and display \[y = table[3]\] stay; cite [4]."
    assert _clean_answer(text) == (
        r"Inline \(x + [2]\) and display \[y = table[3]\] stay; cite."
    )


def test_citation_cleanup_strips_adjacent_prose_markers_but_keeps_indexes():
    from app.api.memory_routes import _clean_answer

    text = (
        "English[k1] 中文[k2] grouped[1, 2] 分组[3,4]. "
        "Keep a[1] and x[1]. `code[k3]` $math[2, 3]$."
    )
    assert _clean_answer(text) == (
        "English 中文 grouped 分组. "
        "Keep a[1] and x[1]. `code[k3]` $math[2, 3]$."
    )


def test_citation_cleanup_strips_chinese_bracket_markers():
    from app.api.memory_routes import _clean_answer

    assert _clean_answer("结论【k1】、证据【k2，k3】与编号【1，2】。") == "结论、证据与编号。"


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
        "kg_extract_eligible": False,
    }
    assert client.get("/api/memories", headers=headers).json()["total_count"] == 0


@pytest.mark.parametrize(
    "model_error",
    [
        RuntimeError("model unavailable"),
        ModelQueueFull(support_id="mdl-memory-full"),
        ModelQueueTimeout(support_id="mdl-memory-timeout"),
        ModelServiceUnavailable(support_id="mdl-memory-unavailable"),
    ],
)
def test_preview_llm_failure_uses_same_fallback(
    tmp_path, monkeypatch, model_error
):
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
            raise model_error

    bind_chat_client(repo, "memory_preview", FailingClient())
    preview = client.post(f"/api/answers/{answer_id}/memory-preview", headers=headers)
    assert preview.status_code == 200
    assert preview.json()["title"] == "Fallback title"
    assert preview.json()["content_md"] == "Fallback body"
    assert preview.json()["kg_extract_eligible"] is False
    assert "memory_preview" in repo._runtime.models._test_chat_calls
    streamed = client.post(
        f"/api/answers/{answer_id}/memory-preview/stream", headers=headers
    )
    assert streamed.status_code == 200
    events = [
        json.loads(line) for line in streamed.text.splitlines() if line.strip()
    ]
    assert events[0]["event"] == "started"
    assert events[-1]["event"] == "final"
    assert events[-1]["result"]["title"] == "Fallback title"


def test_preview_reflects_kg_extract_eligible_gate_including_llm_success_path(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'preview-eligibility.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import create_app

    client = TestClient(create_app())
    registered = client.post(
        "/api/auth/register", json={"username": "w00100023", "password": "pw"}
    ).json()
    headers = {"Authorization": f"Bearer {registered['token']}"}
    notebook_id = client.post(
        "/api/notebooks", headers=headers, json={"name": "Eligibility preview"}
    ).json()["id"]

    from app.api.deps import repository

    repo = repository()

    answer_before = repo._runtime.ask_state.save_answer(
        notebook_id,
        None,
        "Before KG?",
        AskResponse(conclusion="Body before", answer="Body before"),
        registered["user"]["id"],
    )
    before = client.post(
        f"/api/answers/{answer_before}/memory-preview", headers=headers
    )
    assert before.status_code == 200, before.text
    assert before.json()["kg_extract_eligible"] is False

    # Cheapest row that flips notebook_has_kg -> True (mirrors
    # test_memory_kg_lifecycle.py's end-to-end gate-opening insert).
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,created_at,updated_at) "
            "VALUES (?,?,?,?,?)",
            ("ko-preview-gate", notebook_id, "concept", "t", "t"),
        )

    answer_after = repo._runtime.ask_state.save_answer(
        notebook_id,
        None,
        "After KG?",
        AskResponse(conclusion="Body after", answer="Body after"),
        registered["user"]["id"],
    )
    after = client.post(f"/api/answers/{answer_after}/memory-preview", headers=headers)
    assert after.status_code == 200, after.text
    assert after.json()["kg_extract_eligible"] is True

    class SuccessfulClient:
        configured = True

        def chat_json(self, *args, **kwargs):
            return json.dumps(
                {"title": "LLM title", "content_md": "LLM body", "tags": ["llm"]}
            )

    bind_chat_client(repo, "memory_preview", SuccessfulClient())
    llm_answer = repo._runtime.ask_state.save_answer(
        notebook_id,
        None,
        "LLM path?",
        AskResponse(conclusion="Body llm", answer="Body llm"),
        registered["user"]["id"],
    )
    llm_preview = client.post(
        f"/api/answers/{llm_answer}/memory-preview", headers=headers
    )
    assert llm_preview.status_code == 200, llm_preview.text
    assert llm_preview.json()["title"] == "LLM title"
    assert llm_preview.json()["kg_extract_eligible"] is True
    assert "memory_preview" in repo._runtime.models._test_chat_calls
