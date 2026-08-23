"""GET /admin/extensions: sanitized, admin-only view of the loaded extension topology.

Field set is frozen at 6 keys per extension (id/version/trust/display_name/
contributions/ui_contributions) — no ``enabled``: the registry topology is
startup-frozen, so a plugin the deployment disabled never registered and
never appears here. See
docs/superpowers/plans/2026-08-23-deployment-extensions-backend.md 主 agent
裁决 1 and backend/app/extensions/admin_projection.py.
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from tests.test_extension_discovery import (
    _SECRET_UI_BODY,
    _entry,
    _plugin_import_isolation,  # noqa: F401 -- autouse pytest fixture, resolved by name
    _write_config,
    _write_plugin_package,
    frozen_runtime_reset,  # noqa: F401 -- pytest fixture, resolved by name
)


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.api import deps

    deps.repository.cache_clear()
    from app.main import create_app

    return TestClient(create_app())


def _auth(client: TestClient, username: str = "z00998877") -> dict[str, str]:
    client.post("/api/auth/register", json={"username": username, "password": "pw"})
    response = client.post(
        "/api/auth/login", json={"username": username, "password": "pw"}
    )
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _auth_admin(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin"}
    )
    return {"Authorization": f"Bearer {response.json()['token']}"}


def test_admin_extensions_requires_authentication(client):
    assert client.get("/api/admin/extensions").status_code == 401


def test_admin_extensions_rejects_non_admin(client):
    headers = _auth(client)
    response = client.get("/api/admin/extensions", headers=headers)
    assert response.status_code == 403
    assert response.headers.get("X-User-Message") == "1"


def test_admin_extensions_lists_builtin_topology_for_admin(client):
    headers = _auth_admin(client)
    response = client.get("/api/admin/extensions", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert body["api_version"] == "1"
    # Hard-coded, mirroring test_extension_discovery.py's own golden count:
    # the point of this assertion is to fail loudly if an unset
    # EXTENSIONS_CONFIG stops producing the exact shipped topology.
    assert len(body["extensions"]) == 10
    assert all(row["trust"] == "builtin" for row in body["extensions"])
    ids = [row["id"] for row in body["extensions"]]
    assert ids == sorted(ids)


def test_admin_extensions_response_is_a_closed_field_whitelist(client):
    headers = _auth_admin(client)
    body = client.get("/api/admin/extensions", headers=headers).json()
    assert set(body.keys()) == {"api_version", "extensions"}
    assert body["extensions"], "the builtin topology must not be empty"
    saw_contribution = False
    saw_ui_contribution = False
    for row in body["extensions"]:
        assert set(row.keys()) == {
            "id",
            "version",
            "trust",
            "display_name",
            "contributions",
            "ui_contributions",
        }
        for contribution in row["contributions"]:
            assert set(contribution.keys()) == {"id", "point", "kind"}
            saw_contribution = True
        for ui in row["ui_contributions"]:
            assert set(ui.keys()) == {"id", "slot", "capability"}
            saw_ui_contribution = True
    # Both nested shapes must actually be exercised by the builtin topology,
    # otherwise these two inner assertions would pass vacuously.
    assert saw_contribution
    assert saw_ui_contribution


def test_admin_extensions_never_leaks_module_paths_or_settings(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    module = _write_plugin_package(tmp_path, body=_SECRET_UI_BODY)
    config = _write_config(
        tmp_path,
        _entry(
            module,
            extra='[extensions."corp.sample".settings]\ntoken = "TOPSECRET"\n',
        ),
    )
    monkeypatch.setenv("EXTENSIONS_CONFIG", config)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    from app.core.config import get_settings

    get_settings.cache_clear()
    from app.extensions.bootstrap import default_extension_runtime

    default_extension_runtime.cache_clear()
    from app.api import deps

    deps.repository.cache_clear()
    from app.main import create_app

    plugin_client = TestClient(create_app())

    headers = _auth_admin(plugin_client)
    response = plugin_client.get("/api/admin/extensions", headers=headers)
    assert response.status_code == 200
    ids = [row["id"] for row in response.json()["extensions"]]
    assert "corp.sample" in ids
    assert "TOPSECRET" not in response.text
    assert str(module) not in response.text
    assert module.stem not in response.text


def test_system_extensions_response_is_unchanged(client):
    """The pre-existing /system/extensions surface must not shift shape."""

    headers = _auth(client)
    response = client.get("/api/system/extensions", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"api_version", "extensions"}
    for row in body["extensions"]:
        assert set(row.keys()) == {
            "plugin_id",
            "display_name",
            "version",
            "contribution_id",
            "available",
            "unavailable_reason",
        }
