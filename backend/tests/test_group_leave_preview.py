"""Leaving a group: what the leaver stops seeing, on SQLite.

The scenarios live in ``tests/group_leave_preview_cases.py`` and run unchanged
on PostgreSQL through ``tests/postgres/test_group_leave_preview_pg.py``. This
file adds the HTTP contract of ``GET /api/groups/{id}/membership/leave-preview``.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from tests.group_leave_preview_cases import CASES, build_world
from tests.memory_purge_cases import EMBED_DIM


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'leave-preview.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", str(EMBED_DIM))
    repository = SQLiteRepository(Settings())
    try:
        yield repository
    finally:
        repository.close()


@pytest.mark.parametrize("case", sorted(CASES))
def test_group_leave_preview(repo, case):
    CASES[case](build_world(repo, postgres=False))


# ------------------------------------------------------------------ HTTP


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'leave-preview-http.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import create_app

    return TestClient(create_app())


def _register(client: TestClient, username: str) -> tuple[dict, str]:
    response = client.post(
        "/api/auth/register", json={"username": username, "password": "pw"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return {"Authorization": f"Bearer {body['token']}"}, body["user"]["id"]


@pytest.fixture
def http(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner, owner_id = _register(client, "o00220001")
    leaver, leaver_id = _register(client, "l00220002")
    stranger, _ = _register(client, "s00220003")
    group = client.post(
        "/api/groups", headers=owner, json={"name": "退出预览", "kind": "project"}
    ).json()["id"]
    assert client.put(
        f"/api/groups/{group}/members/{leaver_id}", headers=owner,
        json={"role": "member"},
    ).status_code == 200
    notebook = client.post(
        "/api/notebooks", headers=owner, json={"name": "组里的库"}
    ).json()["id"]
    assert client.post(
        f"/api/notebooks/{notebook}/grants", headers=owner,
        json={"principal_type": "group", "principal_id": group, "role": "viewer"},
    ).status_code == 200
    from app.api.deps import repository

    service = repository()._runtime.memory_service
    first = service.create_candidate(
        notebook, leaver_id, None, "leave-1", "One", "Body one.", [], "r", {}, [],
    )
    service.confirm(first.id, leaver_id, {"extract_kg": False})
    service.create_candidate(
        notebook, leaver_id, None, "leave-2", "Two", "Body two.", [], "r", {}, [],
    )
    return {
        "client": client, "owner": owner, "owner_id": owner_id, "leaver": leaver,
        "leaver_id": leaver_id, "stranger": stranger, "group": group,
        "notebook": notebook,
    }


def _url(group: str) -> str:
    return f"/api/groups/{group}/membership/leave-preview"


def test_preview_reports_notebooks_and_counts_of_every_status(http):
    response = http["client"].get(_url(http["group"]), headers=http["leaver"])
    assert response.status_code == 200, response.text
    assert response.json() == {
        "notebook_count": 1,
        "memory_count": 2,
        "notebooks": [
            {
                "notebook_id": http["notebook"], "name": "组里的库", "memory_count": 2,
                "pending_delete": False, "delete_after": None,
            }
        ],
    }


def test_preview_with_nothing_to_hide_is_an_honest_zero(http):
    # The owner owns the notebook: leaving would hide nothing of theirs.
    response = http["client"].get(_url(http["group"]), headers=http["owner"])
    assert response.status_code == 200
    assert response.json() == {"notebook_count": 0, "memory_count": 0, "notebooks": []}


def test_preview_is_only_for_the_callers_own_membership(http):
    client = http["client"]
    # Not a member: the same 404 as an unknown group, never someone else's numbers.
    assert client.get(_url(http["group"]), headers=http["stranger"]).status_code == 404
    assert client.get(_url("grp-missing"), headers=http["leaver"]).status_code == 404
    # A system admin who is not a member gets no bypass (as for the leave).
    from app.api.deps import repository

    _, admin_id = _register(client, "a00220004")
    with repository()._runtime.database.write() as db:
        db.execute("UPDATE users SET role='admin' WHERE id=?", (admin_id,))
    admin = {
        "Authorization": "Bearer "
        + client.post(
            "/api/auth/login", json={"username": "a00220004", "password": "pw"}
        ).json()["token"]
    }
    assert client.get("/api/me", headers=admin).json()["role"] == "admin"
    assert client.get(_url(http["group"]), headers=admin).status_code == 404
    # Unauthenticated.
    assert client.get(_url(http["group"])).status_code == 401


def test_leaving_keeps_the_memory_and_the_preview_matched_it(http):
    client = http["client"]
    before = client.get(_url(http["group"]), headers=http["leaver"]).json()
    assert client.delete(
        f"/api/groups/{http['group']}/membership", headers=http["leaver"]
    ).status_code == 204
    # The preview's notebook is gone for the leaver ...
    assert client.get(
        f"/api/notebooks/{http['notebook']}", headers=http["leaver"]
    ).status_code == 404
    listed = client.get("/api/memories", headers=http["leaver"]).json()
    assert listed["total_count"] == 0
    # ... and its Memory is still there: rejoining shows the same two again.
    assert client.put(
        f"/api/groups/{http['group']}/members/{http['leaver_id']}",
        headers=http["owner"], json={"role": "member"},
    ).status_code == 200
    again = client.get("/api/memories", headers=http["leaver"]).json()
    assert again["total_count"] == before["memory_count"] == 2
    assert client.get(_url(http["group"]), headers=http["leaver"]).json() == before


def test_the_export_offered_before_leaving_does_not_claim_leaving_deletes(http):
    """The leave panel's export is the notebook export; its header keeps the
    shared-notebook exit warning and says leaving a group deletes nothing."""
    client = http["client"]
    preview = client.get(_url(http["group"]), headers=http["leaver"]).json()
    [notebook] = preview["notebooks"]
    response = client.get(
        f"/api/notebooks/{notebook['notebook_id']}/memories/export", headers=http["leaver"]
    )
    assert response.status_code == 200
    head = response.text.split("\n## ", 1)[0]
    assert "退出这本共享笔记本时，这里列出的全部记忆都会被永久删除" in head
    assert "退出群组不会删除它们" in head
