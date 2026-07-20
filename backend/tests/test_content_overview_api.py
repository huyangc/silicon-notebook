from __future__ import annotations

from fastapi.testclient import TestClient


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'content-overview.db'}")
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


def _login_admin(client: TestClient) -> tuple[dict[str, str], str]:
    response = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return {"Authorization": f"Bearer {body['token']}"}, body["user"]["id"]


def _notebook(client: TestClient, headers: dict[str, str], name: str) -> str:
    response = client.post("/api/notebooks", headers=headers, json={"name": name})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _candidate(repo, notebook_id: str, user_id: str, suffix: str):
    return repo.create_memory_candidate(
        notebook_id,
        user_id,
        None,
        f"overview-{suffix}",
        f"Memory {suffix}",
        "Private memory body",
        [],
        "overview fixture",
    )


def test_content_overview_isolates_memory_but_shares_notebook_knowhow(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    owner_headers, owner_id = _register(client, "a00100101")
    reader_headers, reader_id = _register(client, "b00100102")
    notebook_id = _notebook(client, owner_headers, "Shared overview")

    from app.api.deps import repository

    repo = repository()
    repo.add_member(notebook_id, reader_id)
    owner_confirmed = _candidate(repo, notebook_id, owner_id, "owner-confirmed")
    repo.confirm_memory(owner_confirmed.id, owner_id)
    _candidate(repo, notebook_id, owner_id, "owner-candidate")
    _candidate(repo, notebook_id, reader_id, "reader-candidate")
    repo.create_knowhow_table(
        notebook_id,
        "Shared table",
        "",
        [{"name": "Topic", "role": "anchor"}],
    )

    owner_body = client.get(
        f"/api/notebooks/{notebook_id}/analytics/content-overview",
        headers=owner_headers,
    ).json()
    reader_body = client.get(
        f"/api/notebooks/{notebook_id}/analytics/content-overview",
        headers=reader_headers,
    ).json()
    assert owner_body["memory"]["total"] == 2
    assert reader_body["memory"]["total"] == 1
    assert owner_body["knowhow"] == reader_body["knowhow"]


def test_content_overview_returns_zero_sections_for_empty_notebook(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner_headers, _owner_id = _register(client, "c00100103")
    empty_notebook_id = _notebook(client, owner_headers, "Empty overview")

    response = client.get(
        f"/api/notebooks/{empty_notebook_id}/analytics/content-overview",
        headers=owner_headers,
    )
    assert response.status_code == 200
    assert response.json() == {
        "memory": {
            "total": 0,
            "confirmed": 0,
            "candidate": 0,
            "recent": [],
        },
        "knowhow": {
            "table_count": 0,
            "row_count": 0,
            "projection_pending": 0,
            "projection_failed": 0,
            "stale_code_count": 0,
            "recent_tables": [],
        },
    }


def test_content_overview_uses_uniform_not_found_for_stranger_and_missing(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    owner_headers, _owner_id = _register(client, "d00100104")
    stranger_headers, _stranger_id = _register(client, "e00100105")
    notebook_id = _notebook(client, owner_headers, "Private overview")

    stranger = client.get(
        f"/api/notebooks/{notebook_id}/analytics/content-overview",
        headers=stranger_headers,
    )
    missing = client.get(
        "/api/notebooks/missing/analytics/content-overview",
        headers=owner_headers,
    )
    assert stranger.status_code == 404
    assert missing.status_code == 404
    assert stranger.json() == missing.json()


def test_admin_sees_only_admin_owned_memory_in_an_accessible_notebook(
    tmp_path, monkeypatch
):
    client = _client(tmp_path, monkeypatch)
    admin_headers, admin_id = _login_admin(client)
    admin_notebook_id = _notebook(client, admin_headers, "Admin overview")

    from app.api.deps import repository

    admin_memory_id = _candidate(
        repository(), admin_notebook_id, admin_id, "admin-owned"
    ).id
    body = client.get(
        f"/api/notebooks/{admin_notebook_id}/analytics/content-overview",
        headers=admin_headers,
    ).json()
    assert body["memory"]["total"] == 1
    assert [item["id"] for item in body["memory"]["recent"]] == [admin_memory_id]
