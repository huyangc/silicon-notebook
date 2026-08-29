"""GET /admin/extensions + PATCH /api/admin/extensions/{plugin_id}: the admin
runtime-toggle surface.

Two layers, two sources, one response row:

* the loaded topology (id/version/trust/display_name/contributions/
  ui_contributions) is startup-frozen — a plugin the deployment disabled in
  ``EXTENSIONS_CONFIG`` (or never named) never registered and never appears
  here at all. See backend/app/extensions/admin_projection.py.
* runtime_enabled/runtime_updated_by/runtime_updated_at are a second layer,
  read live from the ``extension_runtime_toggles`` table and synthesized onto
  each row by the route itself: ``None`` for every builtin (read-only), and
  ``True, None, None`` (no row = enabled) or the audited row for a loaded
  deployment plugin.

See docs/superpowers/plans/2026-08-29-extension-runtime-toggle.md and
docs/superpowers/plans/2026-08-23-deployment-extensions-backend.md 主 agent
裁决 1.
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
            "runtime_enabled",
            "runtime_updated_by",
            "runtime_updated_at",
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


def test_builtin_rows_never_carry_a_runtime_toggle(client):
    """builtin is read-only: all three runtime fields are ``None``, always."""

    headers = _auth_admin(client)
    body = client.get("/api/admin/extensions", headers=headers).json()
    assert body["extensions"], "the builtin topology must not be empty"
    for row in body["extensions"]:
        assert row["trust"] == "builtin"
        assert row["runtime_enabled"] is None
        assert row["runtime_updated_by"] is None
        assert row["runtime_updated_at"] is None


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


# --------------------------------------------------------------------------
# Runtime toggle: one loaded deployment plugin, GET synthesis + PATCH
# --------------------------------------------------------------------------

_DEPLOYMENT_PLUGIN_ID = "corp.sample"


def _deployment_plugin_client(tmp_path, monkeypatch) -> TestClient:
    """A fresh app with exactly one loaded deployment plugin: ``corp.sample``.

    Reuses ``_SECRET_UI_BODY`` from test_extension_discovery: it declares no
    HTTP router, only a UI contribution, which is all this surface needs — GET
    and PATCH both work off the admin projection's id/trust, never the mount.
    """

    module = _write_plugin_package(tmp_path, body=_SECRET_UI_BODY)
    config = _write_config(
        tmp_path,
        _entry(
            module,
            extra='[extensions."corp.sample".settings]\ntoken = "irrelevant"\n',
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

    return TestClient(create_app())


def _deployment_row(body: dict) -> dict:
    rows = [row for row in body["extensions"] if row["id"] == _DEPLOYMENT_PLUGIN_ID]
    assert len(rows) == 1, body["extensions"]
    return rows[0]


def test_get_deployment_plugin_without_toggle_row_defaults_to_enabled(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    client = _deployment_plugin_client(tmp_path, monkeypatch)
    headers = _auth_admin(client)
    body = client.get("/api/admin/extensions", headers=headers).json()
    row = _deployment_row(body)
    assert row["trust"] == "deployment"
    assert row["runtime_enabled"] is True
    assert row["runtime_updated_by"] is None
    assert row["runtime_updated_at"] is None


def test_patch_requires_admin(tmp_path, monkeypatch, frozen_runtime_reset):
    client = _deployment_plugin_client(tmp_path, monkeypatch)
    headers = _auth(client)
    response = client.patch(
        f"/api/admin/extensions/{_DEPLOYMENT_PLUGIN_ID}",
        json={"enabled": False},
        headers=headers,
    )
    assert response.status_code == 403
    assert response.headers.get("X-User-Message") == "1"


def test_patch_requires_authentication(tmp_path, monkeypatch, frozen_runtime_reset):
    client = _deployment_plugin_client(tmp_path, monkeypatch)
    response = client.patch(
        f"/api/admin/extensions/{_DEPLOYMENT_PLUGIN_ID}", json={"enabled": False}
    )
    assert response.status_code == 401


def test_patch_rejects_an_id_that_was_never_loaded(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    client = _deployment_plugin_client(tmp_path, monkeypatch)
    headers = _auth_admin(client)
    response = client.patch(
        "/api/admin/extensions/corp.never-loaded",
        json={"enabled": False},
        headers=headers,
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "该扩展不存在或不支持运行时开关"
    assert response.headers.get("X-User-Message") == "1"


def test_patch_rejects_a_builtin_id(tmp_path, monkeypatch, frozen_runtime_reset):
    client = _deployment_plugin_client(tmp_path, monkeypatch)
    headers = _auth_admin(client)
    body = client.get("/api/admin/extensions", headers=headers).json()
    builtin_id = next(
        row["id"] for row in body["extensions"] if row["trust"] == "builtin"
    )
    response = client.patch(
        f"/api/admin/extensions/{builtin_id}",
        json={"enabled": False},
        headers=headers,
    )
    assert response.status_code == 404
    assert response.json()["detail"] == "该扩展不存在或不支持运行时开关"


def test_patch_requires_the_enabled_field(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    client = _deployment_plugin_client(tmp_path, monkeypatch)
    headers = _auth_admin(client)
    response = client.patch(
        f"/api/admin/extensions/{_DEPLOYMENT_PLUGIN_ID}", json={}, headers=headers
    )
    assert response.status_code == 422


def test_patch_rejects_an_unknown_field(tmp_path, monkeypatch, frozen_runtime_reset):
    """``AdminExtensionRuntimeUpdate`` is ``extra="forbid"``: an unknown key is
    422, not silently ignored (mirrors ``test_group_kind_cannot_be_changed_after_creation``
    in test_group_routes.py — the repo's standing pattern for pinning a
    ``extra="forbid"`` model down as an explicit test rather than trusting it
    stays that way by accident)."""

    client = _deployment_plugin_client(tmp_path, monkeypatch)
    headers = _auth_admin(client)
    rejected = client.patch(
        f"/api/admin/extensions/{_DEPLOYMENT_PLUGIN_ID}",
        json={"enabled": False, "x": 1},
        headers=headers,
    )
    assert rejected.status_code == 422
    # The valid field alone still works — the 422 above is about the unknown
    # key, not the endpoint being broken.
    body = client.get("/api/admin/extensions", headers=headers).json()
    assert _deployment_row(body)["runtime_enabled"] is True
    assert client.patch(
        f"/api/admin/extensions/{_DEPLOYMENT_PLUGIN_ID}",
        json={"enabled": False},
        headers=headers,
    ).status_code == 200


def test_patch_disable_then_get_reflects_the_audit_row(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    client = _deployment_plugin_client(tmp_path, monkeypatch)
    headers = _auth_admin(client)

    response = client.patch(
        f"/api/admin/extensions/{_DEPLOYMENT_PLUGIN_ID}",
        json={"enabled": False},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    result = response.json()
    assert set(result.keys()) == {
        "plugin_id",
        "runtime_enabled",
        "runtime_updated_by",
        "runtime_updated_at",
    }
    assert result["plugin_id"] == _DEPLOYMENT_PLUGIN_ID
    assert result["runtime_enabled"] is False
    assert result["runtime_updated_by"]  # non-empty: the admin's user id
    assert result["runtime_updated_at"]  # non-empty ISO-ish timestamp

    body = client.get("/api/admin/extensions", headers=headers).json()
    row = _deployment_row(body)
    assert row["runtime_enabled"] is False
    assert row["runtime_updated_by"] == result["runtime_updated_by"]
    assert row["runtime_updated_at"] == result["runtime_updated_at"]


def test_patch_reenable_restores_get(tmp_path, monkeypatch, frozen_runtime_reset):
    client = _deployment_plugin_client(tmp_path, monkeypatch)
    headers = _auth_admin(client)
    client.patch(
        f"/api/admin/extensions/{_DEPLOYMENT_PLUGIN_ID}",
        json={"enabled": False},
        headers=headers,
    )

    response = client.patch(
        f"/api/admin/extensions/{_DEPLOYMENT_PLUGIN_ID}",
        json={"enabled": True},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    assert response.json()["runtime_enabled"] is True

    body = client.get("/api/admin/extensions", headers=headers).json()
    assert _deployment_row(body)["runtime_enabled"] is True


def test_patch_the_same_value_twice_is_idempotent(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    client = _deployment_plugin_client(tmp_path, monkeypatch)
    headers = _auth_admin(client)
    first = client.patch(
        f"/api/admin/extensions/{_DEPLOYMENT_PLUGIN_ID}",
        json={"enabled": False},
        headers=headers,
    )
    second = client.patch(
        f"/api/admin/extensions/{_DEPLOYMENT_PLUGIN_ID}",
        json={"enabled": False},
        headers=headers,
    )
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["runtime_enabled"] is False
    assert second.json()["runtime_enabled"] is False
    body = client.get("/api/admin/extensions", headers=headers).json()
    assert _deployment_row(body)["runtime_enabled"] is False


def test_patch_store_permission_error_maps_to_403_not_500(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    """Defence in depth (plan): the store's own in-transaction actor re-check
    can never actually fire here — the route's own role gate runs first — but
    a ``PermissionError`` out of it must still map to the same 403 copy as
    that role gate, never leak through as a 500."""

    client = _deployment_plugin_client(tmp_path, monkeypatch)
    headers = _auth_admin(client)

    from app.api import admin_routes

    def _raise(*args, **kwargs):
        raise PermissionError("admin role required")

    monkeypatch.setattr(
        admin_routes.extension_toggle_repository(),
        "set_extension_runtime_enabled",
        _raise,
    )
    response = client.patch(
        f"/api/admin/extensions/{_DEPLOYMENT_PLUGIN_ID}",
        json={"enabled": False},
        headers=headers,
    )
    assert response.status_code == 403
    assert response.json()["detail"] == "仅管理员可管理扩展运行时开关"
    assert response.headers.get("X-User-Message") == "1"


def test_patch_survives_a_refresh_failure(
    tmp_path, monkeypatch, frozen_runtime_reset, caplog
):
    """写路径的取舍（见 ``admin_routes.update_admin_extension_runtime`` 文档）：
    刷新回读失败仍返回 2xx——写已经落库，且本进程用零 I/O 的本地快照运算把
    这次写立即应用（codex #635 R4 P1：不许一边报成功一边继续放行该插件），
    只在既有日志器上留一条 warning。"""

    client = _deployment_plugin_client(tmp_path, monkeypatch)
    headers = _auth_admin(client)

    from app.api import admin_routes

    def _boom(store):
        raise RuntimeError("db hiccup")

    monkeypatch.setattr(admin_routes, "refresh_extension_admission", _boom)

    import logging

    with caplog.at_level(
        logging.WARNING, logger="silicon_notebook.extension_admission"
    ):
        response = client.patch(
            f"/api/admin/extensions/{_DEPLOYMENT_PLUGIN_ID}",
            json={"enabled": False},
            headers=headers,
        )
    assert response.status_code == 200, response.text
    assert response.json()["runtime_enabled"] is False
    assert any(
        "extension admission refresh read failed" in record.message
        for record in caplog.records
    )

    # 回读失败不豁免「写进程立即生效」：本地快照运算已把这一位翻过来。
    from app.core.extension_admission import disabled_plugin_ids

    assert _DEPLOYMENT_PLUGIN_ID in disabled_plugin_ids()

    # The write really did land, independent of the broken refresh.
    body = client.get("/api/admin/extensions", headers=headers).json()
    assert _deployment_row(body)["runtime_enabled"] is False


def test_system_extensions_response_is_unchanged(client):
    """The pre-existing /system/extensions surface must not shift shape."""

    headers = _auth(client)
    response = client.get("/api/system/extensions", headers=headers)
    assert response.status_code == 200
    body = response.json()
    assert set(body.keys()) == {"api_version", "extensions"}
    # Non-emptiness first: the loop below is the whole assertion, and it passes
    # vacuously against a surface that returned nothing at all.
    assert body["extensions"], "the builtin UI topology must not be empty"
    for row in body["extensions"]:
        assert set(row.keys()) == {
            "plugin_id",
            "display_name",
            "version",
            "contribution_id",
            "available",
            "unavailable_reason",
        }
