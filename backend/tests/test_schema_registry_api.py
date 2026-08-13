from __future__ import annotations

from fastapi.testclient import TestClient


def _register(client: TestClient, username: str) -> tuple[dict[str, str], str]:
    response = client.post(
        "/api/auth/register",
        json={"username": username, "password": "pw123456"},
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return {"Authorization": f"Bearer {body['token']}"}, body["user"]["id"]


def _schema_payload(object_type: str = "lab_recipe") -> dict[str, object]:
    return {
        "object_type": object_type,
        "plural": "lab_recipes",
        "fields": ["name", "steps"],
        "primary": "name",
        "description": "A local lab workflow.",
        "label": "Lab recipe",
        "list_fields": ["steps"],
    }


def test_notebook_schema_api_role_matrix_and_validation(tmp_path, monkeypatch):
    from app.api import deps
    from app.core.config import get_settings
    from app.main import app

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'schemas.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    get_settings.cache_clear()
    deps.repository.cache_clear()
    client = TestClient(app)

    owner_headers, _owner_id = _register(client, "a00002001")
    reader_headers, reader_id = _register(client, "b00002002")
    stranger_headers, _stranger_id = _register(client, "c00002003")
    notebook = client.post(
        "/api/notebooks", json={"name": "schema api"}, headers=owner_headers
    ).json()["id"]
    deps.repository().add_member(notebook, reader_id)
    collection = f"/api/notebooks/{notebook}/object-schemas"

    owner_view = client.get(collection, headers=owner_headers)
    assert owner_view.status_code == 200
    assert owner_view.json()
    assert all(item["can_edit"] is True for item in owner_view.json())

    created = client.post(
        collection, headers=owner_headers, json=_schema_payload()
    )
    assert created.status_code == 200, created.text
    assert created.json()["scope"] == "notebook"
    assert created.json()["can_edit"] is True

    # Invalid input is a request error, while a valid duplicate is a conflict.
    invalid = client.post(
        collection,
        headers=owner_headers,
        json=_schema_payload("Bad-Type"),
    )
    assert invalid.status_code == 400
    assert invalid.headers["X-User-Message"] == "1"
    assert invalid.json()["detail"] == "类型定义无效，请检查类型标识、字段、主字段和列表字段"
    duplicate = client.post(
        collection, headers=owner_headers, json=_schema_payload()
    )
    assert duplicate.status_code == 409
    assert duplicate.headers["X-User-Message"] == "1"
    assert duplicate.json()["detail"] == "当前笔记本已存在同名类型，请换一个类型标识"

    reader_view = client.get(collection, headers=reader_headers)
    assert reader_view.status_code == 200
    assert all(item["can_edit"] is False for item in reader_view.json())
    for method, url, payload in (
        ("POST", collection, _schema_payload("reader_type")),
        ("PATCH", f"{collection}/lab_recipe", {"label": "reader edit"}),
        ("DELETE", f"{collection}/lab_recipe", None),
    ):
        response = client.request(
            method, url, headers=reader_headers, json=payload
        )
        assert response.status_code == 404, (method, response.text)

    assert client.get(collection, headers=stranger_headers).status_code == 404
    assert client.post(
        collection,
        headers=stranger_headers,
        json=_schema_payload("stranger_type"),
    ).status_code == 404

    # The global baseline is readable but remains administrator-only to mutate.
    global_view = client.get("/api/object-schemas", headers=owner_headers)
    assert global_view.status_code == 200
    assert all(item["can_edit"] is False for item in global_view.json())
    for method, url, payload in (
        ("POST", "/api/object-schemas", _schema_payload("global_type")),
        ("PATCH", "/api/object-schemas/concept", {"label": "nope"}),
        ("DELETE", "/api/object-schemas/concept", None),
    ):
        response = client.request(method, url, headers=owner_headers, json=payload)
        assert response.status_code == 403, (method, response.text)
