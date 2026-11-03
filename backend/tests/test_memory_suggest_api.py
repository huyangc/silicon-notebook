"""HTTP suggestion contracts over the real runtime, without model calls."""
from tests.test_memory_api import _answer, _client, _register
from tests.test_memory_exit_routes import _world


def test_memory_mode_switch_is_self_only_and_refuses_disabled_deployment(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_MEMORY_SUGGEST_ENABLED", "false")
    client = _client(tmp_path, monkeypatch)
    headers, _ = _register(client, "m00100201")
    refused = client.patch("/api/me/memory-mode", headers=headers, json={"memory_mode": "suggest"})
    assert refused.status_code == 409
    assert refused.json()["detail"]["code"] == "memory_suggest_unavailable"
    assert client.get("/api/me", headers=headers).json()["memory_mode"] == "manual"
    assert client.patch("/api/me/memory-mode", json={"memory_mode": "manual"}).status_code == 401
    assert client.patch("/api/me/memory-mode", headers=headers,
                        json={"memory_mode": "auto"}).status_code == 422


def test_memory_mode_switch_returns_updated_caller_and_delete_count(tmp_path, monkeypatch):
    monkeypatch.setenv("AUTO_MEMORY_SUGGEST_ENABLED", "true")
    monkeypatch.setenv("AUTO_MEMORY_SUGGEST_CLARIFIED_ENABLED", "true")
    client = _client(tmp_path, monkeypatch)
    headers, owner = _register(client, "m00100202")
    foreign, _ = _register(client, "m00100203")
    response = client.patch("/api/me/memory-mode", headers=headers, json={"memory_mode": "suggest"})
    assert response.status_code == 200, response.text
    assert response.json()["user"]["id"] == owner
    assert response.json()["user"]["memory_mode"] == "suggest"
    assert response.json()["deleted_suggestion_count"] == 0
    assert client.get("/api/me", headers=foreign).json()["memory_mode"] == "manual"
    response = client.patch("/api/me/memory-mode", headers=headers,
                            json={"memory_mode": "manual", "delete_unadopted": True})
    assert response.status_code == 200
    assert response.json()["user"]["memory_mode"] == "manual"


def test_suggested_filter_and_links_preserve_old_client_links(tmp_path, monkeypatch):
    from app.api.deps import repository

    client = _client(tmp_path, monkeypatch)
    headers, owner = _register(client, "m00100204")
    notebook = client.post("/api/notebooks", headers=headers, json={"name": "Suggestions"}).json()["id"]
    repo = repository()
    answer = _answer(repo, notebook, owner_id=owner)
    created = client.post(f"/api/notebooks/{notebook}/memories/from-answer", headers=headers,
                          json={"answer_id": answer, "title": "A draft", "content_md": "Body"}).json()
    with repo._runtime.database.write() as db:
        db.execute("UPDATE memory_items SET status='candidate', suggested_signal='repeated' WHERE id=?",
                   (created["id"],))
    for path in ("/api/memories", f"/api/notebooks/{notebook}/memories"):
        response = client.get(path, headers=headers, params={"origin": "suggested"})
        assert response.status_code == 200
        assert [m["id"] for m in response.json()["items"]] == [created["id"]]
    response = client.post(f"/api/notebooks/{notebook}/answer-memory-links",
                           headers=headers, json={"answer_ids": [answer]})
    assert response.status_code == 200
    assert response.json() == {"links": {}, "states": {
        answer: {"memory_id": created["id"], "status": "candidate", "suggested": True},
    }}


def test_exit_discloses_suggestions_and_deletes_them_only_on_success(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch)
    with w["repo"]._runtime.database.write() as db:
        db.execute("UPDATE memory_items SET suggested_signal='clarified' WHERE id=?", (w["candidate"],))
    url = f"/api/notebooks/{w['notebook']}/membership"
    assert w["client"].get(url + "/exit-disclosure", headers=w["reader"]).json() == {
        "memory_count": 1, "suggestion_count": 1,
    }
    refused = w["client"].delete(url, headers=w["reader"])
    assert refused.status_code == 409
    assert refused.json()["detail"] == {
        "code": "exit_disclosure_required", "memory_count": 1, "suggestion_count": 1,
    }
    done = w["client"].delete(url, headers=w["reader"], params={"acknowledged_memory_count": 1})
    assert done.status_code == 200
    assert done.json() == {"deleted_memory_count": 1, "deleted_suggestion_count": 1}


def test_closing_deletes_only_callers_open_suggestions_and_reports_count(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch)
    with w["repo"]._runtime.database.write() as db:
        db.execute("UPDATE memory_items SET suggested_signal='clarified' WHERE id=?", (w["candidate"],))
    response = w["client"].patch("/api/me/memory-mode", headers=w["reader"],
                                json={"memory_mode": "manual", "delete_unadopted": True})
    assert response.status_code == 200
    assert response.json()["deleted_suggestion_count"] == 1
    items = w["client"].get("/api/memories", headers=w["reader"]).json()["items"]
    assert [m["id"] for m in items] == [w["confirmed"]]


def test_exit_with_only_suggestions_returns_the_deleted_count(tmp_path, monkeypatch):
    w = _world(tmp_path, monkeypatch)
    w["repo"]._runtime.memory_service.bulk_delete(w["reader_id"], [w["confirmed"]])
    with w["repo"]._runtime.database.write() as db:
        db.execute("UPDATE memory_items SET suggested_signal='clarified' WHERE id=?", (w["candidate"],))
    response = w["client"].delete(
        f"/api/notebooks/{w['notebook']}/membership", headers=w["reader"],
    )
    assert response.status_code == 200
    assert response.json() == {"deleted_memory_count": 0, "deleted_suggestion_count": 1}
