from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from fastapi import FastAPI, HTTPException, Request
from fastapi.testclient import TestClient

from app.api import auth_routes, sso_routes, system_routes
from app.api.deps import get_current_user
from app.core.config import Settings
from app.domain.auth_provider import (
    AuthProviderDescriptor,
    AuthProviderError,
    ExternalIdentity,
)
from app.services.auth_flow import AuthFlowService
from app.services.sqlite_repository import SQLiteRepository


class Provider:
    def __init__(self):
        self.identity = ExternalIdentity("corp.production", "employee-17", "W0012345", "统一姓名")
        self.calls = []
        self.available = True
        self.enabled = True
        self.availability_checks = 0

    def describe(self):
        if not self.enabled:
            return None
        return AuthProviderDescriptor("corp.auth", "corp.auth.provider", "corp.auth",
            "corp.production", "generation-1", "统一认证", "https://identity.test/authorize", True)

    def ensure_available(self):
        self.availability_checks += 1
        if not self.available:
            raise AuthProviderError("auth_provider_unavailable")

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
    provider.enabled = False
    flow = AuthFlowService(identity.auth, provider, settings)
    monkeypatch.setattr(sso_routes, "auth_flow", lambda: flow)
    monkeypatch.setattr(auth_routes, "identity_repository", lambda: identity)
    monkeypatch.setattr(system_routes, "identity_repository", lambda: identity)

    async def current_user(request: Request):
        token = request.headers.get("Authorization", "")[7:]
        user = identity.resolve_session(token)
        if user is None:
            raise HTTPException(status_code=401)
        yield user

    app = FastAPI()
    app.include_router(auth_routes.auth_router, prefix="/api")
    app.include_router(sso_routes.sso_router, prefix="/api")
    app.include_router(system_routes.router, prefix="/api")
    app.dependency_overrides[get_current_user] = current_user
    client = TestClient(app, base_url="http://localhost", follow_redirects=False)
    yield client, identity, provider, flow
    client.close()
    repo.close()


def _local_user(client, name="a12345678"):
    response = client.post("/api/auth/register", json={"username": name, "password": "local-password"})
    assert response.status_code == 200, response.text
    return response.json()


def _complete(client, start=None):
    start = start or client.post("/api/auth/sso/start")
    assert start.status_code == 200, start.text
    params = parse_qs(urlsplit(start.json()["authorization_url"]).query)
    callback = client.get("/api/auth/sso/callback", params={"state": params["state"][0], "code": "provider-code"})
    assert callback.status_code == 303, callback.text
    query = parse_qs(urlsplit(callback.headers["location"]).query)
    assert "code" in query, query
    assert "token" not in query
    return client.post("/api/auth/sso/complete", json={"code": query["code"][0]})


def _choice(client):
    response = _complete(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "choice_required", body
    return body


def test_local_login_needs_no_external_plugin(setup):
    client, identity, provider, flow = setup
    caps = client.get("/api/auth/capabilities").json()
    assert caps == {"sso_login": False, "local_login": True, "local_registration": True,
                    "provider_label": "统一认证"}
    assert client.post("/api/auth/sso/start").status_code == 503
    assert provider.calls == []
    assert provider.availability_checks == 0


def test_local_register_and_login_keep_the_complete_user_payload(setup):
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


def test_enabled_provider_hides_local_login_and_refuses_local_credentials(setup):
    client, identity, provider, flow = setup
    local = _local_user(client)
    headers = {"Authorization": "Bearer " + local["token"]}
    provider.enabled = True
    caps = client.get("/api/auth/capabilities").json()
    assert caps == {"sso_login": True, "local_login": False, "local_registration": False,
                    "provider_label": "统一认证"}
    closed = "已启用统一认证，请使用统一认证登录"
    login = client.post("/api/auth/login", json={"username": "a12345678", "password": "local-password"})
    register = client.post("/api/auth/register", json={"username": "b12345678", "password": "pw"})
    for response in (login, register):
        assert response.status_code == 403
        assert response.json()["detail"] == closed
        assert response.headers["x-user-message"] == "1"
    # The local session no longer authenticates while unified auth is on.
    password = client.patch("/api/me/password", headers=headers,
                            json={"old_password": "local-password", "new_password": "x"})
    assert password.status_code == 401
    assert identity.resolve_session(local["token"]) is None
    # Switching the plugin off is the way back to local passwords.
    provider.enabled = False
    assert identity.resolve_session(local["token"]).username == "a12345678"
    assert client.post("/api/auth/login", json={"username": "a12345678",
                       "password": "local-password"}).status_code == 200


def test_password_change_is_refused_for_an_sso_session(setup):
    client, identity, provider, flow = setup
    _local_user(client)
    provider.enabled = True
    provider.identity = ExternalIdentity("corp.production", "employee-17", "a12345678", "统一姓名")
    signed_in = _complete(client).json()
    assert signed_in["status"] == "authenticated"
    response = client.patch("/api/me/password", headers={"Authorization": "Bearer " + signed_in["token"]},
                            json={"old_password": "local-password", "new_password": "x"})
    assert response.status_code == 403
    assert response.json()["detail"] == "已启用统一认证，请使用统一认证登录"


def test_exact_username_signs_in_directly(setup):
    client, identity, provider, flow = setup
    local = _local_user(client)
    provider.enabled = True
    provider.identity = ExternalIdentity("corp.production", "employee-17", "a12345678", "统一姓名")
    response = _complete(client)
    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "authenticated"
    assert body["user"]["id"] == local["user"]["id"]
    assert identity.resolve_session(body["token"]).id == local["user"]["id"]
    assert response.headers["cache-control"] == "no-store"


def test_choice_then_link_moves_the_old_account_onto_the_external_name(setup):
    client, identity, provider, flow = setup
    local = _local_user(client)
    provider.enabled = True
    choice = _choice(client)
    assert choice["external_username"] == "W0012345"
    assert choice["display_name"] == "统一姓名"
    assert set(choice) == {"status", "pending_id", "external_username", "display_name"}
    wrong = client.post("/api/auth/sso/link", json={"pending_id": choice["pending_id"],
                        "login_name": "a12345678", "password": "wrong"})
    unknown = client.post("/api/auth/sso/link", json={"pending_id": choice["pending_id"],
                          "login_name": "z99999999", "password": "local-password"})
    for response in (wrong, unknown):
        assert response.status_code == 409
        assert response.json()["detail"] == "用户名或密码错误"
        assert response.headers["x-user-message"] == "1"
    linked = client.post("/api/auth/sso/link", json={"pending_id": choice["pending_id"],
                         "login_name": "a12345678", "password": "local-password"})
    assert linked.status_code == 200, linked.text
    assert set(linked.json()) == {"token", "user"}
    assert linked.json()["user"]["id"] == local["user"]["id"]
    assert linked.json()["user"]["username"] == "W0012345"
    assert identity.resolve_session(local["token"]) is None
    replay = client.post("/api/auth/sso/link", json={"pending_id": choice["pending_id"],
                         "login_name": "a12345678", "password": "local-password"})
    assert replay.status_code == 409
    assert replay.json()["detail"] == "认证操作已过期或已使用，请重新登录。"


def test_choice_then_create_makes_a_new_passwordless_account(setup):
    client, identity, provider, flow = setup
    _local_user(client)
    provider.enabled = True
    choice = _choice(client)
    created = client.post("/api/auth/sso/create", json={"pending_id": choice["pending_id"]})
    assert created.status_code == 200, created.text
    user = created.json()["user"]
    assert (user["username"], user["display_name"], user["role"]) == ("W0012345", "统一姓名", "user")
    assert identity.resolve_session(created.json()["token"]).id == user["id"]
    # The next login matches the new account directly.
    assert _complete(client).json()["user"]["id"] == user["id"]


def test_stale_choice_and_disabled_account_messages(setup):
    client, identity, provider, flow = setup
    provider.enabled = True
    first = _choice(client)
    second = _choice(client)
    assert client.post("/api/auth/sso/create", json={"pending_id": first["pending_id"]}).status_code == 200
    stale = client.post("/api/auth/sso/create", json={"pending_id": second["pending_id"]})
    assert stale.status_code == 409
    assert stale.json()["detail"] == "认证状态已变化，请重新登录。"
    created = identity.auth._execute
    with identity.database.connect() as db:
        user_id = created(db, "SELECT id FROM users WHERE username='W0012345'").fetchone()["id"]
    identity.auth.set_account_status(user_id, "disabled", actor_id="user-local")
    inactive = _complete(client)
    assert inactive.status_code == 409
    assert inactive.json()["detail"] == "账号已停用，请联系管理员。"


def test_choice_is_bound_to_the_browser_and_cancel_discards_it(setup):
    client, identity, provider, flow = setup
    provider.enabled = True
    choice = _choice(client)
    saved = dict(client.cookies)
    client.cookies.clear()
    no_proof = client.post("/api/auth/sso/create", json={"pending_id": choice["pending_id"]})
    assert no_proof.status_code == 400
    client.cookies.update(saved)
    assert client.post("/api/auth/sso/cancel", json={"pending_id": choice["pending_id"]}).status_code == 204
    gone = client.post("/api/auth/sso/create", json={"pending_id": choice["pending_id"]})
    assert gone.status_code == 409
    with identity.database.connect() as db:
        assert db.execute("SELECT count(*) FROM users WHERE username='W0012345'").fetchone()[0] == 0


@pytest.mark.parametrize("route,payload", [
    ("complete", {"code": "x"}),
    ("link", {"pending_id": "x", "login_name": "a", "password": "b"}),
    ("create", {"pending_id": "x"}),
    ("cancel", {"pending_id": "x"}),
])
def test_choice_routes_reject_foreign_origins(setup, route, payload):
    client, identity, provider, flow = setup
    provider.enabled = True
    response = client.post(f"/api/auth/sso/{route}", json=payload, headers={"Origin": "https://other.test"})
    assert response.status_code == 403


@pytest.mark.parametrize("provider_response", [{"code": "private-code"}, {"error": "private-error"}])
def test_callback_without_browser_proof_redirects_to_sanitized_recovery(setup, provider_response):
    client, identity, provider, flow = setup
    provider.enabled = True
    start = client.post("/api/auth/sso/start")
    state = parse_qs(urlsplit(start.json()["authorization_url"]).query)["state"][0]
    client.cookies.clear()
    response = client.get("/api/auth/sso/callback", params={"state": state, **provider_response})
    assert response.status_code == 303
    assert response.headers["location"] == "http://localhost:3000/auth/sso/callback?error=authentication_failed"
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert provider.calls == []


def test_browser_origin_and_duplicate_callback_are_rejected(setup):
    client, identity, provider, flow = setup
    provider.enabled = True
    assert client.post("/api/auth/sso/start", headers={"Origin": "https://other.test"}).status_code == 403
    response = client.get("/api/auth/sso/callback?state=one&state=two&code=foo")
    assert response.status_code == 303 and "error=" in response.headers["location"]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["referrer-policy"] == "no-referrer"
    assert provider.calls == []


def test_invalid_configuration_fails_closed_instead_of_falling_back(setup):
    client, identity, provider, flow = setup
    provider.enabled = True
    flow.settings.auth_frontend_base_url = "https://different.test"
    assert client.get("/api/auth/capabilities").status_code == 503
    flow.settings.auth_frontend_base_url = "http://localhost:3000"
    flow.settings.auth_optional = True
    assert client.get("/api/auth/capabilities").status_code == 503
    flow.settings.auth_optional = False
    provider.available = False
    assert client.get("/api/auth/capabilities").status_code == 503
    # Local passwords stay refused while the enabled provider is unusable.
    assert client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).status_code == 403


def test_logout_cancels_other_tab_login_even_with_old_browser_cookie(setup):
    client, identity, provider, flow = setup
    local = _local_user(client)
    provider.enabled = True
    start = client.post("/api/auth/sso/start")
    state = parse_qs(urlsplit(start.json()["authorization_url"]).query)["state"][0]
    old_cookies = dict(client.cookies)
    assert client.post("/api/auth/logout", headers={"Authorization": "Bearer " + local["token"]}).status_code == 204
    client.cookies.update(old_cookies)
    callback = client.get("/api/auth/sso/callback", params={"state": state, "code": "provider-code"})
    assert "error=" in callback.headers["location"]
    assert provider.calls == []


def test_provider_failure_uses_fixed_public_message(setup, monkeypatch):
    client, identity, provider, flow = setup
    monkeypatch.setattr(flow, "capabilities", lambda: (_ for _ in ()).throw(ValueError("secret-provider-response")))
    response = client.get("/api/auth/capabilities")
    assert response.status_code == 409
    assert response.headers["x-user-message"] == "1"
    assert "secret-provider-response" not in response.text


def test_production_http_intranet_origin_needs_explicit_opt_in(setup):
    client, identity, provider, flow = setup
    provider.enabled = True
    flow.settings.environment = "production"
    flow.settings.auth_public_base_url = "http://notebook.corp.example"
    flow.settings.auth_frontend_base_url = "http://notebook.corp.example"
    assert client.get("/api/auth/capabilities").status_code == 503
    flow.settings.auth_allow_insecure_http = True
    response = client.get("/api/auth/capabilities")
    assert response.status_code == 200, response.text
    assert response.json()["sso_login"] is True
    # Plain-http origins keep the non-Secure dev cookie; a __Host- cookie would
    # be dropped by the browser and the callback would never see its proof.
    assert flow.cookie_name == "sn-auth-browser-dev" and flow.secure_cookie is False
    start = client.post("/api/auth/sso/start", headers={"Origin": "http://notebook.corp.example"})
    assert start.status_code == 200, start.text
    params = parse_qs(urlsplit(start.json()["authorization_url"]).query)
    assert params["redirect_uri"] == ["http://notebook.corp.example/api/auth/sso/callback"]
    flow.settings.auth_frontend_base_url = "http://other.corp.example"
    assert client.get("/api/auth/capabilities").status_code == 503


def test_public_and_frontend_origins_must_share_a_scheme(setup):
    client, identity, provider, flow = setup
    provider.enabled = True
    flow.settings.auth_allow_insecure_http = True
    flow.settings.auth_public_base_url = "https://notebook.corp.example"
    flow.settings.auth_frontend_base_url = "http://notebook.corp.example"
    # The https public origin issues a Secure __Host- proof the http frontend never gets.
    assert client.get("/api/auth/capabilities").status_code == 503
    assert client.post("/api/auth/sso/start").status_code == 503
    flow.settings.auth_frontend_base_url = ""
    assert client.get("/api/auth/capabilities").status_code == 200
    flow.settings.auth_frontend_base_url = "https://notebook.corp.example"
    assert client.get("/api/auth/capabilities").status_code == 200
