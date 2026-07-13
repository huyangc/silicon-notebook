from __future__ import annotations

import json

from fastapi.testclient import TestClient

from app.models.schemas import AskResponse


def _register(client: TestClient, username: str) -> tuple[dict, str]:
    response = client.post(
        "/api/auth/register", json={"username": username, "password": "pw"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return {"Authorization": f"Bearer {body['token']}"}, body["user"]["id"]


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'memory-api.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import create_app

    return TestClient(create_app())


def _answer(repo, notebook_id: str, question: str = "What remains stable?") -> str:
    return repo._runtime.ask_state.save_answer(
        notebook_id,
        None,
        question,
        AskResponse(
            conclusion="The loop remains stable [1].",
            answer="The loop remains stable [1].",
            mode="chunk",
            llm_mode="test-model",
            evidence_level="grounded",
        ),
        "unused-by-storage",
    )


def test_owner_memory_crud_is_private_and_bounded(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_headers, _owner_id = _register(client, "a00100001")
    foreign_headers, _foreign_id = _register(client, "b00100002")
    notebook_id = client.post(
        "/api/notebooks", headers=owner_headers, json={"name": "Memory API"}
    ).json()["id"]

    from app.api.deps import repository

    answer_id = _answer(repository(), notebook_id)
    created = client.post(
        f"/api/notebooks/{notebook_id}/memories/from-answer",
        headers=owner_headers,
        json={
            "answer_id": answer_id,
            "title": "Stable loop",
            "content_md": "Saved body",
            "tags": ["analog"],
        },
    )
    assert created.status_code == 201, created.text
    memory = created.json()

    listed = client.get("/api/memories?limit=1&offset=0", headers=owner_headers)
    assert listed.status_code == 200
    assert [item["id"] for item in listed.json()["items"]] == [memory["id"]]
    assert listed.json()["limit"] == 1
    assert client.get("/api/memories?status=bogus", headers=owner_headers).status_code == 422
    assert client.get("/api/memories?origin=bogus", headers=owner_headers).status_code == 422

    got = client.get(f"/api/memories/{memory['id']}", headers=owner_headers)
    assert got.status_code == 200
    assert client.get(
        f"/api/memories/{memory['id']}", headers=foreign_headers
    ).status_code == 404
    assert client.get("/api/memories", headers=foreign_headers).json()["items"] == []

    updated = client.patch(
        f"/api/memories/{memory['id']}",
        headers=owner_headers,
        json={"title": "Edited"},
    )
    assert updated.status_code == 200
    assert updated.json()["title"] == "Edited"
    deprecated = client.post(
        f"/api/memories/{memory['id']}/deprecate", headers=owner_headers
    )
    assert deprecated.status_code == 200
    assert deprecated.json()["status"] == "deprecated"


def test_reader_can_save_own_private_memory_and_answer_save_is_idempotent(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    owner_headers, _owner_id = _register(client, "c00100003")
    reader_headers, reader_id = _register(client, "d00100004")
    notebook_id = client.post(
        "/api/notebooks", headers=owner_headers, json={"name": "Shared"}
    ).json()["id"]

    from app.api.deps import repository

    repo = repository()
    repo.add_member(notebook_id, reader_id)
    answer_id = _answer(repo, notebook_id, "How should it be compensated?")

    payload = {
        "answer_id": answer_id,
        "title": "Edited preview",
        "content_md": "Reader-owned body",
        "tags": [],
    }
    first = client.post(
        f"/api/notebooks/{notebook_id}/memories/from-answer",
        headers=reader_headers,
        json=payload,
    )
    second = client.post(
        f"/api/notebooks/{notebook_id}/memories/from-answer",
        headers=reader_headers,
        json={**payload, "title": "Ignored duplicate"},
    )
    assert first.status_code == second.status_code == 201
    assert first.json()["id"] == second.json()["id"]
    assert first.json()["created_by"] == reader_id
    assert first.json()["status"] == "confirmed"
    assert second.json()["title"] == "Edited preview"
    assert first.json()["provenance"] == {
        "answer_id": answer_id,
        "question": "How should it be compensated?",
        "answer": "The loop remains stable [1].",
        "conversation_id": None,
        "mode": "chunk",
        "model": "test-model",
        "evidence_level": "grounded",
        "anchors": [],
        "citations": [],
    }
    assert client.get(
        f"/api/notebooks/{notebook_id}/memories", headers=owner_headers
    ).json()["items"] == []


def test_answer_memory_links_are_bounded_owner_private_and_notebook_scoped(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    owner_headers, owner_id = _register(client, "q00100017")
    reader_headers, reader_id = _register(client, "r00100018")
    notebook_a = client.post(
        "/api/notebooks", headers=owner_headers, json={"name": "Links A"}
    ).json()["id"]
    notebook_b = client.post(
        "/api/notebooks", headers=owner_headers, json={"name": "Links B"}
    ).json()["id"]

    from app.api.deps import repository

    repo = repository()
    repo.add_member(notebook_a, reader_id)
    answer_a = _answer(repo, notebook_a, "Owner saved?")
    answer_reader = _answer(repo, notebook_a, "Reader saved?")
    answer_b = _answer(repo, notebook_b, "Wrong notebook?")

    def save(headers, notebook_id, answer_id, title):
        response = client.post(
            f"/api/notebooks/{notebook_id}/memories/from-answer",
            headers=headers,
            json={
                "answer_id": answer_id,
                "title": title,
                "content_md": "Body",
                "tags": [],
            },
        )
        assert response.status_code == 201, response.text
        return response.json()

    owner_memory = save(owner_headers, notebook_a, answer_a, "Owner")
    reader_memory = save(reader_headers, notebook_a, answer_reader, "Reader")
    save(owner_headers, notebook_b, answer_b, "Other notebook")

    owner_links = client.post(
        f"/api/notebooks/{notebook_a}/answer-memory-links",
        headers=owner_headers,
        json={"answer_ids": [answer_a, answer_reader, answer_b, "missing", answer_a]},
    )
    assert owner_links.status_code == 200, owner_links.text
    assert owner_links.json() == {"links": {answer_a: owner_memory["id"]}}

    reader_links = client.post(
        f"/api/notebooks/{notebook_a}/answer-memory-links",
        headers=reader_headers,
        json={"answer_ids": [answer_a, answer_reader]},
    )
    assert reader_links.status_code == 200
    assert reader_links.json() == {"links": {answer_reader: reader_memory["id"]}}
    assert owner_memory["created_by"] == owner_id

    too_many = client.post(
        f"/api/notebooks/{notebook_a}/answer-memory-links",
        headers=owner_headers,
        json={"answer_ids": [f"answer-{index}" for index in range(201)]},
    )
    assert too_many.status_code == 422

    repo.remove_member(notebook_a, reader_id)
    assert client.post(
        f"/api/notebooks/{notebook_a}/answer-memory-links",
        headers=reader_headers,
        json={"answer_ids": [answer_reader]},
    ).status_code == 404


def test_foreign_notebook_and_memory_are_not_disclosed(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_headers, _owner_id = _register(client, "e00100005")
    foreign_headers, _foreign_id = _register(client, "f00100006")
    notebook_id = client.post(
        "/api/notebooks", headers=owner_headers, json={"name": "Private"}
    ).json()["id"]

    from app.api.deps import repository

    answer_id = _answer(repository(), notebook_id)
    assert client.get(
        f"/api/notebooks/{notebook_id}/memories", headers=foreign_headers
    ).status_code == 404
    assert client.post(
        f"/api/notebooks/{notebook_id}/memories/from-answer",
        headers=foreign_headers,
        json={
            "answer_id": answer_id,
            "title": "No",
            "content_md": "No",
            "tags": [],
        },
    ).status_code == 404
    assert client.post(
        f"/api/answers/{answer_id}/memory-preview", headers=foreign_headers
    ).status_code == 404


def test_save_after_preview_returns_conflict_when_answer_was_deleted(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    headers, _user_id = _register(client, "i00100009")
    notebook_id = client.post(
        "/api/notebooks", headers=headers, json={"name": "Deleted answer"}
    ).json()["id"]

    from app.api.deps import repository

    repo = repository()
    answer_id = _answer(repo, notebook_id)
    assert client.post(
        f"/api/answers/{answer_id}/memory-preview", headers=headers
    ).status_code == 200
    with repo._write() as db:
        db.execute("DELETE FROM answers WHERE id=?", (answer_id,))

    saved = client.post(
        f"/api/notebooks/{notebook_id}/memories/from-answer",
        headers=headers,
        json={
            "answer_id": answer_id,
            "title": "Draft",
            "content_md": "Keep this editable",
            "tags": [],
        },
    )
    assert saved.status_code == 409


def test_answer_deleted_during_save_rolls_back_memory(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    headers, _user_id = _register(client, "j00100010")
    notebook_id = client.post(
        "/api/notebooks", headers=headers, json={"name": "Delete race"}
    ).json()["id"]

    from app.api.deps import repository

    repo = repository()
    answer_id = _answer(repo, notebook_id)
    store = repo._runtime.memory_store
    original = store.create_answer_with_initial_revision

    def delete_before_atomic_create(write, changed_by, reason):
        with repo._write() as db:
            db.execute("DELETE FROM answers WHERE id=?", (answer_id,))
        return original(write, changed_by, reason)

    monkeypatch.setattr(
        store, "create_answer_with_initial_revision", delete_before_atomic_create
    )
    saved = client.post(
        f"/api/notebooks/{notebook_id}/memories/from-answer",
        headers=headers,
        json={
            "answer_id": answer_id,
            "title": "Draft",
            "content_md": "Body",
            "tags": [],
        },
    )
    assert saved.status_code == 409
    assert client.get("/api/memories", headers=headers).json()["total_count"] == 0


def test_answer_save_returns_before_background_embedding_runs(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    headers, _user_id = _register(client, "k00100011")
    notebook_id = client.post(
        "/api/notebooks", headers=headers, json={"name": "Async embedding"}
    ).json()["id"]

    from app.api.deps import repository

    repo = repository()
    answer_id = _answer(repo, notebook_id)
    scheduled = []
    repo._runtime.memory_service.embedding_scheduler = (
        lambda fn, item: scheduled.append((fn, item))
    )

    saved = client.post(
        f"/api/notebooks/{notebook_id}/memories/from-answer",
        headers=headers,
        json={
            "answer_id": answer_id,
            "title": "Async",
            "content_md": "Body",
            "tags": [],
        },
    )
    assert saved.status_code == 201
    assert saved.json()["embedding_status"] == "pending"
    assert len(scheduled) == 1


def test_memory_api_rejects_blank_and_oversized_inputs_with_422(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    headers, _user_id = _register(client, "z00100019")
    notebook_id = client.post(
        "/api/notebooks", headers=headers, json={"name": "Input limits"}
    ).json()["id"]
    from app.api.deps import repository
    from app.services.memory_inputs import MEMORY_CONTENT_MAX_CHARS, MEMORY_REASON_MAX_CHARS

    answer_id = _answer(repository(), notebook_id)
    blank = client.post(
        f"/api/notebooks/{notebook_id}/memories/from-answer",
        headers=headers,
        json={"answer_id": answer_id, "title": "   ", "content_md": "Body", "tags": []},
    )
    oversized = client.post(
        f"/api/notebooks/{notebook_id}/memories/from-answer",
        headers=headers,
        json={
            "answer_id": answer_id,
            "title": "Title",
            "content_md": "x" * (MEMORY_CONTENT_MAX_CHARS + 1),
            "tags": [],
        },
    )
    assert blank.status_code == 422
    assert oversized.status_code == 422
    repo = repository()
    candidate = repo.create_memory_candidate(
        notebook_id, _user_id, None, "api-limits", "Candidate", "Body", [],
        "reason", {}, [],
    )
    blank_update = client.patch(
        f"/api/memories/{candidate.id}",
        headers=headers,
        json={"content_md": "   "},
    )
    oversized_review_reason = client.post(
        f"/api/memories/{candidate.id}/confirm",
        headers=headers,
        json={"reason": "r" * (MEMORY_REASON_MAX_CHARS + 1)},
    )
    assert blank_update.status_code == 422
    assert oversized_review_reason.status_code == 422


def test_memory_api_returns_global_stats_and_candidate_review_identity(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    headers, user_id = _register(client, "y00100018")
    notebook_id = client.post(
        "/api/notebooks", headers=headers, json={"name": "Review details"}
    ).json()["id"]
    from app.api.deps import repository

    repo = repository()
    profile = repo.create_agent_profile(user_id, "Codex review", "")
    item = repo.create_memory_candidate(
        notebook_id,
        user_id,
        profile.id,
        "client-safe-1",
        "Candidate",
        "Body",
        [],
        "Review this",
        {"task": "audit"},
        [{"source_id": "missing"}],
    )
    response = client.get("/api/memories?status=candidate", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["owner_total_count"] == 1
    assert body["owner_pending_count"] == 1
    assert body["notebook_options"] == [
        {
            "notebook_id": notebook_id,
            "name": "Review details",
            "memory_count": 1,
            "pending_count": 1,
        }
    ]
    returned = body["items"][0]
    assert returned["id"] == item.id
    assert returned["provenance"]["agent_profile"] == {
        "id": profile.id,
        "name": "Codex review",
    }
    assert returned["provenance"]["client_request_id"] == "client-safe-1"
    assert returned["provenance"]["evidence_refs"][0]["validation"]["status"] == "invalid"
    assert "token" not in json.dumps(returned["provenance"])


def test_replay_after_answer_deleted_returns_existing_memory(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    headers, _user_id = _register(client, "l00100012")
    notebook_id = client.post(
        "/api/notebooks", headers=headers, json={"name": "Idempotent replay"}
    ).json()["id"]

    from app.api.deps import repository

    repo = repository()
    answer_id = _answer(repo, notebook_id)
    payload = {
        "answer_id": answer_id,
        "title": "Saved",
        "content_md": "Body",
        "tags": [],
    }
    first = client.post(
        f"/api/notebooks/{notebook_id}/memories/from-answer",
        headers=headers,
        json=payload,
    )
    assert first.status_code == 201
    with repo._write() as db:
        db.execute("DELETE FROM answers WHERE id=?", (answer_id,))

    replay = client.post(
        f"/api/notebooks/{notebook_id}/memories/from-answer",
        headers=headers,
        json={**payload, "title": "Ignored replay edit"},
    )
    assert replay.status_code == 201
    assert replay.json()["id"] == first.json()["id"]
    assert replay.json()["title"] == "Saved"


def test_answer_idempotency_does_not_cross_notebook_path(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    headers, _user_id = _register(client, "m00100013")
    notebook_a = client.post(
        "/api/notebooks", headers=headers, json={"name": "Notebook A"}
    ).json()["id"]
    notebook_b = client.post(
        "/api/notebooks", headers=headers, json={"name": "Notebook B"}
    ).json()["id"]

    from app.api.deps import repository

    answer_id = _answer(repository(), notebook_a)
    payload = {
        "answer_id": answer_id,
        "title": "Saved in A",
        "content_md": "Body",
        "tags": [],
    }
    first = client.post(
        f"/api/notebooks/{notebook_a}/memories/from-answer",
        headers=headers,
        json=payload,
    )
    assert first.status_code == 201

    crossed = client.post(
        f"/api/notebooks/{notebook_b}/memories/from-answer",
        headers=headers,
        json=payload,
    )
    assert crossed.status_code == 409
    assert crossed.json() != first.json()


def test_revoked_reader_loses_all_memory_read_and_mutation_access(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    owner_headers, _owner_id = _register(client, "n00100014")
    reader_headers, reader_id = _register(client, "o00100015")
    notebook_id = client.post(
        "/api/notebooks", headers=owner_headers, json={"name": "Revoked"}
    ).json()["id"]

    from app.api.deps import repository

    repo = repository()
    repo.add_member(notebook_id, reader_id)
    answer_id = _answer(repo, notebook_id)
    confirmed = client.post(
        f"/api/notebooks/{notebook_id}/memories/from-answer",
        headers=reader_headers,
        json={
            "answer_id": answer_id,
            "title": "Private after revoke",
            "content_md": "Body",
            "tags": [],
        },
    ).json()
    candidate_a = repo.create_memory_candidate(
        notebook_id, reader_id, None, "revoke-a", "A", "Body", [], "review"
    )
    candidate_b = repo.create_memory_candidate(
        notebook_id, reader_id, None, "revoke-b", "B", "Body", [], "review"
    )
    repo.remove_member(notebook_id, reader_id)

    assert client.get("/api/memories", headers=reader_headers).json()["items"] == []
    assert client.get(
        f"/api/notebooks/{notebook_id}/memories", headers=reader_headers
    ).status_code == 404
    assert client.get(
        f"/api/memories/{confirmed['id']}", headers=reader_headers
    ).status_code == 404
    assert client.patch(
        f"/api/memories/{confirmed['id']}",
        headers=reader_headers,
        json={"title": "Must not land"},
    ).status_code == 404
    assert client.post(
        f"/api/memories/{confirmed['id']}/deprecate", headers=reader_headers
    ).status_code == 404
    assert client.post(
        f"/api/memories/{candidate_a.id}/confirm", headers=reader_headers
    ).status_code == 404
    assert client.post(
        f"/api/memories/{candidate_b.id}/reject", headers=reader_headers
    ).status_code == 404
