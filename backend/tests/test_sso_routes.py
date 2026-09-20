from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api import auth_routes, sso_routes
from app.core.config import Settings
from app.domain.auth_provider import AuthProviderDescriptor, ExternalIdentity
from app.services.auth_flow import AuthFlowService
from app.services.sqlite_repository import SQLiteRepository


class Provider:
    def __init__(self):
        self.identity = ExternalIdentity("corp.production", "employee-17", "b87654321", "统一姓名")
        self.calls = []

    def describe(self):
        return AuthProviderDescriptor("corp.auth", "corp.auth.provider", "corp.auth",
            "corp.production", "generation-1", "统一登录", "https://identity.test/authorize", True)

    def authorization_url(self, *, state, redirect_uri, code_challenge):
        return "https://identity.test/authorize?" + urlencode({"state": state,
            "redirect_uri": redirect_uri, "code_challenge": code_challenge})

    def authenticate(self, **kwargs):
        self.calls.append(kwargs)
        return self.identity


@pytest.fixture
def setup(tmp_path, monkeypatch):
    settings = Settings(_env_file=None, database_url=f"sqlite:///{tmp_path}/auth.db",
        storage_dir=str(tmp_path / "storage"), auth_optional=False,
        auth_public_base_url="http://localhost", auth_frontend_base_url="http://localhost:3000",
        event_log_enabled=False, llm_log_enabled=False)
    repo = SQLiteRepository(settings)
    identity = repo._runtime.identity
    provider = Provider()
    flow = AuthFlowService(identity.auth, provider, settings)
    monkeypatch.setattr(sso_routes, "auth_flow", lambda: flow)
    monkeypatch.setattr(auth_routes, "identity_repository", lambda: identity)
    app = FastAPI()
    app.include_router(auth_routes.auth_router, prefix="/api")
    app.include_router(sso_routes.sso_router, prefix="/api")
    client = TestClient(app, base_url="http://localhost", follow_redirects=False)
    yield client, identity, provider, flow
    client.close()
    repo.close()


def _dual(identity):
    identity.auth.set_policy("dual", actor_id="user-local", expected_revision=0,
        plugin_id="corp.auth", provider_id="corp.auth", provider_namespace="corp.production",
        config_generation="generation-1")


def _local_user(client, name="a12345678"):
    response = client.post("/api/auth/register", json={"username": name, "password": "local-password"})
    assert response.status_code == 200, response.text
    return response.json()


def _complete(client, start):
    assert start.status_code == 200, start.text
    params = parse_qs(urlsplit(start.json()["authorization_url"]).query)
    callback = client.get("/api/auth/sso/callback", params={"state": params["state"][0], "code": "provider-code"})
    assert callback.status_code == 303, callback.text
    query = parse_qs(urlsplit(callback.headers["location"]).query)
    assert "code" in query, query
    assert "token" not in query
    return client.post("/api/auth/sso/complete", json={"code": query["code"][0]})


def test_local_default_needs_no_external_plugin(setup):
    client, identity, provider, flow = setup
    caps = client.get("/api/auth/capabilities").json()
    assert caps["mode"] == "local" and caps["local_login"]
    assert not caps["sso_login"]
    assert client.post("/api/auth/sso/start").status_code == 503
    assert provider.calls == []


def test_local_register_and_login_preserve_complete_legacy_user_payload(setup):
    client, identity, provider, flow = setup
    registered = _local_user(client)
    profile = identity.resolve_session(registered["token"]).model_dump(mode="json")
    assert set(registered) == {"token", "user"}
    assert registered["user"] == profile
    assert {"memory_mode", "domain_focus", "ui_mode", "search_profile"} <= registered["user"].keys()
    logged_in = client.post("/api/auth/login", json={"username": "a12345678", "password": "local-password"}).json()
    assert set(logged_in) == {"token", "user"}
    assert logged_in["user"] == profile
    assert provider.calls == []


def test_binding_stage_adds_migration_flag_to_complete_user_payload(setup):
    client, identity, provider, flow = setup
    registered = _local_user(client)
    _dual(identity)
    identity.auth.set_policy("binding_required", actor_id="user-local", expected_revision=1)
    response = client.post("/api/auth/login", json={"username": "a12345678", "password": "local-password"})
    assert response.status_code == 200
    assert response.json()["migration_required"] is True
    assert response.json()["user"] == registered["user"]
    assert identity.resolve_session(response.json()["token"]) is None


def test_local_first_confirmation_keeps_id_and_old_password_name(setup):
    client, identity, provider, flow = setup
    original = _local_user(client)
    _dual(identity)
    headers = {"Authorization": "Bearer " + original["token"]}
    complete = _complete(client, client.post("/api/me/identity-binding/start",
        json={"current_password": "local-password"}, headers=headers))
    assert complete.status_code == 200, complete.text
    preview = complete.json()
    assert preview["status"] == "binding_required"
    assert preview["local_login_name"] == "a12345678"
    assert preview["external_username"] == "b87654321"
    assert identity.auth.identities(original["user"]["id"])["linked"] is False
    confirmed = client.post("/api/me/identity-binding/confirm",
        json={"pending_id": preview["pending_id"]}, headers=headers)
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["user"]["id"] == original["user"]["id"]
    assert confirmed.json()["user"]["username"] == "b87654321"
    assert identity.resolve_session(original["token"]) is None
    old = client.post("/api/auth/login", json={"username": "a12345678", "password": "local-password"})
    assert old.status_code == 200 and old.json()["user"]["id"] == original["user"]["id"]
    assert client.post("/api/auth/login", json={"username": "b87654321", "password": "local-password"}).status_code == 401
    direct = _complete(client, client.post("/api/auth/sso/start"))
    assert direct.status_code == 200, direct.text
    assert direct.json()["user"]["id"] == original["user"]["id"]
    assert provider.calls[0]["code_verifier"]


def test_sso_name_does_not_claim_unlinked_same_name(setup):
    client, identity, provider, flow = setup
    _local_user(client, "b87654321")
    _dual(identity)
    response = _complete(client, client.post("/api/auth/sso/start"))
    assert response.status_code == 409
    assert "尚未关联" in response.json()["detail"]


def test_binding_is_rejected_after_original_session_logout(setup):
    client, identity, provider, flow = setup
    original = _local_user(client)
    _dual(identity)
    headers = {"Authorization": "Bearer " + original["token"]}
    preview = _complete(client, client.post("/api/me/identity-binding/start",
        json={"current_password": "local-password"}, headers=headers)).json()
    assert client.post("/api/auth/logout", headers=headers).status_code == 204
    response = client.post("/api/me/identity-binding/confirm",
        json={"pending_id": preview["pending_id"]}, headers=headers)
    assert response.status_code == 400
    assert not identity.auth.identities(original["user"]["id"])["linked"]


def test_binding_cancel_and_other_browser_cannot_confirm(setup):
    client, identity, provider, flow = setup
    original = _local_user(client)
    _dual(identity)
    headers = {"Authorization": "Bearer " + original["token"]}
    preview = _complete(client, client.post("/api/me/identity-binding/start",
        json={"current_password": "local-password"}, headers=headers)).json()
    saved = dict(client.cookies)
    client.cookies.clear()
    response = client.post("/api/me/identity-binding/confirm",
        json={"pending_id": preview["pending_id"]}, headers=headers)
    assert response.status_code == 400
    client.cookies.update(saved)
    assert client.post("/api/me/identity-binding/cancel",
        json={"pending_id": preview["pending_id"]}, headers=headers).status_code == 204
    assert not identity.auth.identities(original["user"]["id"])["linked"]


def test_browser_origin_and_duplicate_callback_are_rejected(setup):
    client, identity, provider, flow = setup
    _dual(identity)
    assert client.post("/api/auth/sso/start", headers={"Origin": "https://other.test"}).status_code == 403
    response = client.get("/api/auth/sso/callback?state=one&state=two&code=foo")
    assert response.status_code == 303 and "error=" in response.headers["location"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert provider.calls == []


def test_production_origin_and_provider_changes_fail_closed(setup):
    client, identity, provider, flow = setup
    _dual(identity)
    flow.settings.auth_frontend_base_url = "https://different.test"
    assert client.get("/api/auth/capabilities").status_code == 503
    flow.settings.auth_frontend_base_url = "http://localhost:3000"
    flow.settings.auth_optional = True
    assert client.get("/api/auth/capabilities").status_code == 503


def test_logout_cancels_other_tab_login_even_with_old_browser_cookie(setup):
    client, identity, provider, flow = setup
    local = _local_user(client)
    _dual(identity)
    start = client.post("/api/auth/sso/start")
    state = parse_qs(urlsplit(start.json()["authorization_url"]).query)["state"][0]
    old_cookies = dict(client.cookies)
    assert client.post("/api/auth/logout", headers={"Authorization": "Bearer " + local["token"]}).status_code == 204
    client.cookies.update(old_cookies)
    callback = client.get("/api/auth/sso/callback", params={"state": state, "code": "provider-code"})
    assert "error=" in callback.headers["location"]
    assert provider.calls == []


def test_replacement_plugin_cannot_impersonate_selected_provider(setup, monkeypatch):
    from dataclasses import replace

    client, identity, provider, flow = setup
    _dual(identity)
    descriptor = provider.describe()
    monkeypatch.setattr(provider, "describe", lambda: replace(descriptor, plugin_id="other.plugin"))
    assert client.get("/api/auth/capabilities").status_code == 503


def test_configuration_maintenance_keeps_mode_and_supports_restart(setup, monkeypatch):
    from dataclasses import replace

    client, identity, provider, flow = setup
    _dual(identity)
    admin = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()
    headers = {"Authorization": "Bearer " + admin["token"]}
    response = client.patch("/api/admin/auth/provider-configuration", headers=headers,
        json={"expected_revision": 1, "configuration_generation": "generation-2"})
    assert response.status_code == 200, response.text
    assert response.json()["mode"] == "dual"
    assert client.get("/api/auth/capabilities").status_code == 503
    descriptor = provider.describe()
    monkeypatch.setattr(provider, "describe", lambda: replace(descriptor, configuration_generation="generation-2"))
    assert client.get("/api/auth/capabilities").status_code == 200


def test_authorized_replacement_keeps_account_and_exposes_restricted_audit(setup):
    client, identity, provider, flow = setup
    original = _local_user(client)
    _dual(identity)
    headers = {"Authorization": "Bearer " + original["token"]}
    preview = _complete(client, client.post("/api/me/identity-binding/start",
        json={"current_password": "local-password"}, headers=headers)).json()
    linked = client.post("/api/me/identity-binding/confirm",
        json={"pending_id": preview["pending_id"]}, headers=headers).json()
    admin = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()
    admin_headers = {"Authorization": "Bearer " + admin["token"]}
    grant = client.post("/api/admin/auth/grants", headers=admin_headers,
        json={"purpose": "replace", "subject": "employee-18", "target_user_id": original["user"]["id"]})
    assert grant.status_code == 200, grant.text
    provider.identity = ExternalIdentity("corp.production", "employee-18", "c12345678", "新姓名")
    replacement = _complete(client, client.post("/api/auth/sso/grant/start",
        json={"purpose": "replace", "grant_token": grant.json()["grant_token"]})).json()
    assert replacement["target_user_id"] == original["user"]["id"]
    assert replacement["target_username"] == "b87654321"
    assert identity.resolve_session(linked["token"]) is not None
    confirmed = client.post("/api/me/identity-binding/confirm", json={"pending_id": replacement["pending_id"]})
    assert confirmed.status_code == 200, confirmed.text
    assert confirmed.json()["user"]["id"] == original["user"]["id"]
    assert confirmed.json()["user"]["username"] == "c12345678"
    assert identity.resolve_session(linked["token"]) is None
    audit = client.get("/api/admin/auth/audit?limit=2&offset=0", headers=admin_headers)
    assert audit.status_code == 200 and audit.headers["cache-control"] == "no-store"
    assert len(audit.json()["items"]) == 2 and audit.json()["total"] == 4
    next_page = client.get("/api/admin/auth/audit?limit=2&offset=2", headers=admin_headers).json()
    assert len(next_page["items"]) == 2
    assert "grant_completed:replace" in {item["action"] for item in audit.json()["items"] + next_page["items"]}
    assert grant.json()["grant_token"] not in audit.text
    assert client.get("/api/admin/auth/audit").status_code == 401
    user_headers = {"Authorization": "Bearer " + confirmed.json()["token"]}
    assert client.get("/api/admin/auth/audit", headers=user_headers).status_code == 403
    assert client.get("/api/admin/auth/audit?limit=201", headers=admin_headers).status_code == 422


def test_provider_failure_uses_fixed_public_message(setup, monkeypatch):
    client, identity, provider, flow = setup
    monkeypatch.setattr(flow, "capabilities", lambda: (_ for _ in ()).throw(ValueError("secret-provider-response")))
    response = client.get("/api/auth/capabilities")
    assert response.status_code == 409
    assert response.headers["x-user-message"] == "1"
    assert "secret-provider-response" not in response.text


def test_configuration_maintenance_rejects_noncanonical_generation_without_write(setup):
    client, identity, _provider, _flow = setup
    _dual(identity)
    admin = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin"}
    ).json()
    headers = {"Authorization": "Bearer " + admin["token"]}

    rejected = client.patch(
        "/api/admin/auth/provider-configuration",
        headers=headers,
        json={
            "expected_revision": 1,
            "configuration_generation": "Generation-2",
        },
    )
    assert rejected.status_code == 422
    unchanged = identity.auth.get_policy()
    assert unchanged["revision"] == 1
    assert unchanged["config_generation"] == "generation-1"

    accepted = client.patch(
        "/api/admin/auth/provider-configuration",
        headers=headers,
        json={
            "expected_revision": 1,
            "configuration_generation": "generation-2",
        },
    )
    assert accepted.status_code == 200, accepted.text
    updated = identity.auth.get_policy()
    assert updated["revision"] == 2
    assert updated["config_generation"] == "generation-2"
