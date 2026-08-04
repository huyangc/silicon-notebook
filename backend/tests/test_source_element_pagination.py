"""Source detail reads bounded element windows, including citation anchors."""
from __future__ import annotations

from fastapi.testclient import TestClient


_NOW = "2026-01-01T00:00:00"


def _client(tmp_path, monkeypatch) -> TestClient:
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'elements-page.db'}")
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


def _register(client: TestClient, username: str) -> dict[str, str]:
    response = client.post(
        "/api/auth/register", json={"username": username, "password": "pw"}
    )
    assert response.status_code == 200, response.text
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _seed_source(repo, notebook_id: str, source_id: str, count: int) -> None:
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,parse_status,created_at,updated_at) "
            "VALUES (?,?,?,?,'extracted','extracted',?,?)",
            (source_id, notebook_id, "Large source", "document", _NOW, _NOW),
        )
        db.executemany(
            "INSERT INTO source_elements "
            "(id,source_id,element_type,location_label,text,metadata,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            [
                (
                    f"element-{index:04d}",
                    source_id,
                    "paragraph",
                    f"Page {index}",
                    f"text {index}",
                    "{}",
                    _NOW,
                )
                for index in range(count)
            ],
        )


def test_scoped_element_page_is_bounded_and_resolves_anchor(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner = _register(client, "a00900001")
    notebook = client.post(
        "/api/notebooks", headers=owner, json={"name": "Paging"}
    ).json()["id"]

    from app.api.deps import repository

    _seed_source(repository(), notebook, "source-large", 85)

    first = client.get(
        f"/api/notebooks/{notebook}/sources/source-large/elements-page",
        headers=owner,
    )
    assert first.status_code == 200, first.text
    assert first.json()["total_count"] == 85
    assert first.json()["offset"] == 0
    assert len(first.json()["items"]) == 40
    assert [item["id"] for item in first.json()["items"][:2]] == [
        "element-0000",
        "element-0001",
    ]

    anchored = client.get(
        f"/api/notebooks/{notebook}/sources/source-large/elements-page",
        headers=owner,
        params={"anchor_element_id": "element-0060"},
    )
    assert anchored.status_code == 200, anchored.text
    assert anchored.json()["offset"] == 40
    assert len(anchored.json()["items"]) == 40
    assert "element-0060" in [item["id"] for item in anchored.json()["items"]]


def test_element_page_keeps_source_read_authorization(tmp_path, monkeypatch):
    client = _client(tmp_path, monkeypatch)
    owner = _register(client, "b00900002")
    stranger = _register(client, "c00900003")
    notebook = client.post(
        "/api/notebooks", headers=owner, json={"name": "Private"}
    ).json()["id"]

    from app.api.deps import repository

    _seed_source(repository(), notebook, "source-private", 1)
    response = client.get(
        "/api/sources/source-private/elements-page", headers=stranger
    )
    assert response.status_code == 404

