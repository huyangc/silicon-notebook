from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.deps import USER_MESSAGE_HEADER
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.repositories.identity_errors import AgentTokenInactiveError
from app.services.sqlite_repository import (
    SQLiteRepository,
    reset_request_user,
    set_request_user,
)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'tokens.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


@pytest.fixture
def token_context(repo):
    alice = repo.create_user("a00119001", "pw")
    bob = repo.create_user("b00119002", "pw")
    marker = set_request_user(alice)
    try:
        notebook = repo.create_notebook(NotebookCreate(name="Agent notebook"))
        other = repo.create_notebook(NotebookCreate(name="Other notebook"))
    finally:
        reset_request_user(marker)
    return repo._runtime.memory_service, alice, bob, notebook, other


def _issue(service, owner, profile, notebook, *, scopes=None, expires_at=None):
    return service.issue_agent_token(
        owner.id,
        profile.id,
        scopes or ["memory:read", "memory:read_candidates"],
        notebook.id,
        [notebook.id],
        expires_at,
    )


def test_token_is_hashed_and_raw_value_is_returned_only_at_issue(token_context):
    service, alice, _bob, notebook, _other = token_context
    profile = service.create_agent_profile(alice.id, "Claude Code", "Local agent")

    issued = _issue(service, alice, profile, notebook)
    listed = service.list_agent_tokens(alice.id)

    assert issued.token.startswith(f"snm_{issued.id}.")
    assert listed[0].id == issued.id
    assert not hasattr(listed[0], "token")
    assert "hash" not in listed[0].model_dump()
    with service.store.database.connect() as db:
        row = db.execute(
            "SELECT token_hash FROM agent_access_tokens WHERE id=?", (issued.id,)
        ).fetchone()
    assert row["token_hash"] == hashlib.sha256(issued.token.encode()).hexdigest()
    assert issued.token not in row["token_hash"]


def test_resolve_returns_scoped_principal_and_enforces_notebook_allowlist(token_context):
    service, alice, _bob, notebook, other = token_context
    profile = service.create_agent_profile(alice.id, "Codex", "")
    issued = _issue(service, alice, profile, notebook)

    principal = service.resolve_agent_token(issued.token)

    assert principal is not None
    assert principal.profile_id == profile.id
    assert principal.owner_id == alice.id
    assert principal.default_notebook_id == notebook.id
    assert principal.notebook_ids == [notebook.id]
    assert service.require_agent_access(
        principal, "memory:read_candidates", notebook.id
    ) is None
    with pytest.raises(PermissionError):
        service.require_agent_access(principal, "memory:propose", notebook.id)
    with pytest.raises(PermissionError):
        service.require_agent_access(principal, "memory:read", other.id)


def test_foreign_profile_and_notebook_cannot_be_used_to_issue_token(token_context):
    service, alice, bob, notebook, other = token_context
    alice_profile = service.create_agent_profile(alice.id, "Alice agent", "")
    bob_profile = service.create_agent_profile(bob.id, "Bob agent", "")

    with pytest.raises(KeyError):
        _issue(service, alice, bob_profile, notebook)
    with pytest.raises(PermissionError):
        service.issue_agent_token(
            bob.id,
            bob_profile.id,
            ["memory:read"],
            notebook.id,
            [notebook.id],
            None,
        )
    with pytest.raises(ValueError):
        service.issue_agent_token(
            alice.id,
            alice_profile.id,
            ["memory:read"],
            notebook.id,
            [other.id],
            None,
        )

    service.notebooks.add_member(notebook.id, bob.id)
    bob_token = _issue(service, bob, bob_profile, notebook, scopes=["memory:read"])
    principal = service.resolve_agent_token(bob_token.token)
    assert principal is not None
    service.notebooks.remove_member(notebook.id, bob.id)
    with pytest.raises(PermissionError):
        service.require_agent_access(principal, "memory:read", notebook.id)


def test_expired_revoked_disabled_and_tampered_tokens_do_not_resolve(token_context):
    service, alice, _bob, notebook, _other = token_context
    profile = service.create_agent_profile(alice.id, "Agent", "")
    expired = _issue(
        service,
        alice,
        profile,
        notebook,
        expires_at=(datetime.now(timezone.utc) - timedelta(minutes=1)).replace(microsecond=0).isoformat(),
    )
    active = _issue(service, alice, profile, notebook)

    assert service.resolve_agent_token(expired.token) is None
    assert service.resolve_agent_token(active.token + "x") is None
    stale_principal = service.resolve_agent_token(active.token)
    assert stale_principal is not None
    assert service.revoke_agent_token(alice.id, active.id).revoked_at is not None
    assert service.resolve_agent_token(active.token) is None
    with pytest.raises(PermissionError):
        service.require_agent_access(stale_principal, "memory:read", notebook.id)

    replacement = _issue(service, alice, profile, notebook)
    service.update_agent_profile(profile.id, alice.id, {"status": "revoked"})
    assert service.resolve_agent_token(replacement.token) is None


def test_expiry_requires_offset_and_is_normalized_to_utc(token_context):
    service, alice, _bob, notebook, _other = token_context
    profile = service.create_agent_profile(alice.id, "Timezone", "")

    with pytest.raises(ValueError, match="timezone offset"):
        _issue(service, alice, profile, notebook, expires_at="2030-01-02T03:04:05")

    issued = _issue(
        service,
        alice,
        profile,
        notebook,
        expires_at="2030-01-02T03:04:05+08:00",
    )
    assert issued.expires_at == "2030-01-01T19:04:05Z"
    assert service.list_agent_tokens(alice.id)[0].expires_at == "2030-01-01T19:04:05Z"


def test_rotation_keeps_new_token_usable_after_old_token_is_revoked(token_context):
    service, alice, _bob, notebook, _other = token_context
    profile = service.create_agent_profile(alice.id, "Rotated", "")
    old_token = _issue(service, alice, profile, notebook)
    new_token = _issue(service, alice, profile, notebook)

    service.revoke_agent_token(alice.id, old_token.id)

    assert service.resolve_agent_token(old_token.token) is None
    principal = service.resolve_agent_token(new_token.token)
    assert principal is not None
    assert principal.profile_id == profile.id


def test_live_principal_refresh_uses_token_id_and_rejects_revoked_or_disabled_state(
    token_context,
):
    service, alice, _bob, notebook, _other = token_context
    profile = service.create_agent_profile(alice.id, "Live MCP session", "")
    issued = _issue(service, alice, profile, notebook)

    refreshed = service.refresh_agent_principal(issued.id)
    assert refreshed is not None
    assert refreshed.token_id == issued.id
    assert refreshed.scopes == ["memory:read", "memory:read_candidates"]

    service.revoke_agent_token(alice.id, issued.id)
    assert service.refresh_agent_principal(issued.id) is None

    replacement = _issue(service, alice, profile, notebook)
    service.update_agent_profile(profile.id, alice.id, {"status": "revoked"})
    assert service.refresh_agent_principal(replacement.id) is None


def test_last_used_touch_is_throttled(token_context):
    service, alice, _bob, notebook, _other = token_context
    profile = service.create_agent_profile(alice.id, "Touch", "")
    issued = _issue(service, alice, profile, notebook)

    assert service.resolve_agent_token(issued.token) is not None
    first = service.list_agent_tokens(alice.id)[0].last_used_at
    assert first is not None
    assert service.resolve_agent_token(issued.token) is not None
    second = service.list_agent_tokens(alice.id)[0].last_used_at
    assert second == first


def test_update_access_replaces_scopes_notebooks_and_default_live(token_context):
    service, alice, _bob, notebook, other = token_context
    profile = service.create_agent_profile(alice.id, "Editable", "")
    issued = _issue(
        service, alice, profile, notebook,
        scopes=["memory:read", "memory:read_candidates"],
    )
    with service.store.database.connect() as db:
        original_hash = db.execute(
            "SELECT token_hash FROM agent_access_tokens WHERE id=?", (issued.id,)
        ).fetchone()["token_hash"]

    updated = service.update_agent_token_access(
        alice.id, issued.id, ["memory:propose"], other.id, [other.id], None
    )

    assert updated.id == issued.id
    assert updated.scopes == ["memory:propose"]
    assert updated.default_notebook_id == other.id
    assert updated.notebook_ids == [other.id]
    assert updated.agent_profile_id == profile.id
    assert updated.created_at == issued.created_at

    listed = service.list_agent_tokens(alice.id)[0]
    assert listed.scopes == ["memory:propose"]
    assert listed.default_notebook_id == other.id
    assert listed.notebook_ids == [other.id]
    with service.store.database.connect() as db:
        row = db.execute(
            "SELECT token_hash,agent_profile_id,created_at FROM agent_access_tokens "
            "WHERE id=?",
            (issued.id,),
        ).fetchone()
    assert row["token_hash"] == original_hash
    assert row["agent_profile_id"] == profile.id
    assert row["created_at"] == issued.created_at

    # The original plaintext token still resolves (token_hash untouched) and
    # the live principal carries the new scopes/allowlist immediately.
    principal = service.resolve_agent_token(issued.token)
    assert principal is not None
    assert principal.scopes == ["memory:propose"]
    assert principal.notebook_ids == [other.id]
    assert principal.default_notebook_id == other.id

    # A removed scope/notebook is denied right away ...
    with pytest.raises(PermissionError):
        service.require_agent_access(principal, "memory:read_candidates", other.id)
    with pytest.raises(PermissionError):
        service.require_agent_access(principal, "memory:propose", notebook.id)
    # ... and a newly granted scope/notebook is allowed right away.
    assert service.require_agent_access(principal, "memory:propose", other.id) is None


def test_update_access_expiry_clear_expire_and_restore_follow_issue_rules(
    token_context,
):
    service, alice, _bob, notebook, _other = token_context
    profile = service.create_agent_profile(alice.id, "Expiring", "")
    issued = _issue(
        service,
        alice,
        profile,
        notebook,
        expires_at="2030-01-02T03:04:05+00:00",
    )
    scopes = ["memory:read", "memory:read_candidates"]

    cleared = service.update_agent_token_access(
        alice.id, issued.id, scopes, notebook.id, [notebook.id], None
    )
    assert cleared.expires_at is None
    assert service.resolve_agent_token(issued.token) is not None

    past = (
        datetime.now(timezone.utc) - timedelta(minutes=1)
    ).replace(microsecond=0).isoformat()
    expired = service.update_agent_token_access(
        alice.id, issued.id, scopes, notebook.id, [notebook.id], past
    )
    assert expired.expires_at is not None
    assert service.resolve_agent_token(issued.token) is None

    future = (
        datetime.now(timezone.utc) + timedelta(days=1)
    ).replace(microsecond=0).isoformat()
    restored = service.update_agent_token_access(
        alice.id, issued.id, scopes, notebook.id, [notebook.id], future
    )
    assert restored.expires_at is not None
    assert service.resolve_agent_token(issued.token) is not None


def test_update_access_rejects_revoked_disabled_foreign_and_invalid_input(
    token_context, repo,
):
    service, alice, bob, notebook, other = token_context
    profile = service.create_agent_profile(alice.id, "Guarded", "")
    scopes = ["memory:read"]

    revoked = _issue(service, alice, profile, notebook, scopes=scopes)
    service.revoke_agent_token(alice.id, revoked.id)
    with pytest.raises(AgentTokenInactiveError) as exc_info:
        service.update_agent_token_access(
            alice.id, revoked.id, scopes, notebook.id, [notebook.id], None
        )
    assert exc_info.value.reason == "revoked"

    disabled_profile_token = _issue(service, alice, profile, notebook, scopes=scopes)
    service.update_agent_profile(profile.id, alice.id, {"status": "revoked"})
    with pytest.raises(AgentTokenInactiveError) as exc_info:
        service.update_agent_token_access(
            alice.id, disabled_profile_token.id, scopes, notebook.id,
            [notebook.id], None,
        )
    assert exc_info.value.reason == "profile_disabled"

    active_profile = service.create_agent_profile(alice.id, "Active", "")
    live = _issue(service, alice, active_profile, notebook, scopes=scopes)
    # Bob can read the notebook (so validation passes) but does not own the
    # token's profile, so the store's owner-scoped lookup must still 404.
    service.notebooks.add_member(notebook.id, bob.id)
    try:
        with pytest.raises(KeyError):
            service.update_agent_token_access(
                bob.id, live.id, scopes, notebook.id, [notebook.id], None
            )
    finally:
        service.notebooks.remove_member(notebook.id, bob.id)

    marker = set_request_user(bob)
    try:
        bob_notebook = repo.create_notebook(NotebookCreate(name="Bob private"))
    finally:
        reset_request_user(marker)
    with pytest.raises(PermissionError):
        service.update_agent_token_access(
            alice.id, live.id, scopes, bob_notebook.id, [bob_notebook.id], None
        )
    with pytest.raises(ValueError):
        service.update_agent_token_access(
            alice.id, live.id, ["admin:all"], notebook.id, [notebook.id], None
        )
    with pytest.raises(ValueError):
        service.update_agent_token_access(
            alice.id, live.id, [], notebook.id, [notebook.id], None
        )
    with pytest.raises(ValueError):
        service.update_agent_token_access(
            alice.id, live.id, scopes, notebook.id, [other.id], None
        )

    # None of the rejected attempts mutated the token's stored allowlist.
    unchanged = service.list_agent_tokens(alice.id)
    still_live = next(item for item in unchanged if item.id == live.id)
    assert still_live.scopes == scopes
    assert still_live.notebook_ids == [notebook.id]
    assert still_live.default_notebook_id == notebook.id


def _register(client: TestClient, username: str) -> tuple[dict, str]:
    response = client.post(
        "/api/auth/register", json={"username": username, "password": "pw"}
    )
    assert response.status_code == 200, response.text
    body = response.json()
    return {"Authorization": f"Bearer {body['token']}"}, body["user"]["id"]


def test_profile_and_token_endpoints_are_owner_private(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'token-api.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import create_app

    client = TestClient(create_app())
    alice_headers, _ = _register(client, "c00119003")
    bob_headers, _ = _register(client, "d00119004")
    notebook_id = client.post(
        "/api/notebooks", headers=alice_headers, json={"name": "Agent API"}
    ).json()["id"]

    created = client.post(
        "/api/agent-profiles",
        headers=alice_headers,
        json={"name": "Claude Code", "description": "Local"},
    )
    assert created.status_code == 201, created.text
    profile = created.json()
    assert client.post(
        "/api/agent-profiles", headers=alice_headers, json={"name": "Codex"}
    ).status_code == 201
    assert len(client.get(
        "/api/agent-profiles?offset=0&limit=1", headers=alice_headers
    ).json()) == 1
    assert client.get(
        "/api/agent-profiles?limit=101", headers=alice_headers
    ).status_code == 422
    assert client.get("/api/agent-profiles", headers=bob_headers).json() == []

    issued = client.post(
        f"/api/agent-profiles/{profile['id']}/tokens",
        headers=alice_headers,
        json={
            "agent_profile_id": profile["id"],
            "scopes": ["memory:read", "memory:read_candidates"],
            "default_notebook_id": notebook_id,
            "notebook_ids": [notebook_id],
        },
    )
    assert issued.status_code == 201, issued.text
    raw = issued.json()["token"]
    listed = client.get("/api/agent-tokens", headers=alice_headers)
    assert listed.status_code == 200
    assert listed.json()[0]["id"] == issued.json()["id"]
    assert "token" not in listed.json()[0]
    assert "token_hash" not in listed.text
    assert client.get("/api/agent-tokens", headers=bob_headers).json() == []
    assert client.delete(
        f"/api/agent-tokens/{issued.json()['id']}", headers=bob_headers
    ).status_code == 404
    revoked = client.delete(
        f"/api/agent-tokens/{issued.json()['id']}", headers=alice_headers
    )
    assert revoked.status_code == 200
    assert revoked.json()["revoked_at"] is not None

    disabled = client.patch(
        f"/api/agent-profiles/{profile['id']}",
        headers=alice_headers,
        json={"status": "revoked"},
    )
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "revoked"
    assert raw not in listed.text


def test_token_api_rejects_unknown_scope_and_default_outside_allowlist(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'token-validation.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    from app.main import create_app

    client = TestClient(create_app())
    headers, _ = _register(client, "e00119005")
    notebook_id = client.post(
        "/api/notebooks", headers=headers, json={"name": "Validation"}
    ).json()["id"]
    profile_id = client.post(
        "/api/agent-profiles", headers=headers, json={"name": "Agent"}
    ).json()["id"]

    unknown_scope = client.post(
        f"/api/agent-profiles/{profile_id}/tokens",
        headers=headers,
        json={
            "agent_profile_id": profile_id,
            "scopes": ["admin:all"],
            "default_notebook_id": notebook_id,
            "notebook_ids": [notebook_id],
        },
    )
    assert unknown_scope.status_code == 422
    missing_default = client.post(
        f"/api/agent-profiles/{profile_id}/tokens",
        headers=headers,
        json={
            "agent_profile_id": profile_id,
            "scopes": ["memory:read"],
            "default_notebook_id": notebook_id,
            "notebook_ids": ["another-notebook"],
        },
    )
    assert missing_default.status_code == 422
    invalid_expiry = client.post(
        f"/api/agent-profiles/{profile_id}/tokens",
        headers=headers,
        json={
            "agent_profile_id": profile_id,
            "scopes": ["memory:read"],
            "default_notebook_id": notebook_id,
            "notebook_ids": [notebook_id],
            "expires_at": "not-a-date",
        },
    )
    assert invalid_expiry.status_code == 422
    naive_expiry = client.post(
        f"/api/agent-profiles/{profile_id}/tokens",
        headers=headers,
        json={
            "agent_profile_id": profile_id,
            "scopes": ["memory:read"],
            "default_notebook_id": notebook_id,
            "notebook_ids": [notebook_id],
            "expires_at": "2030-01-02T03:04:05",
        },
    )
    assert naive_expiry.status_code == 422


def test_update_access_endpoint_owner_private_and_error_mapping(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'token-access-api.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import create_app

    client = TestClient(create_app())
    alice_headers, _ = _register(client, "f00119006")
    bob_headers, _ = _register(client, "g00119007")
    notebook_id = client.post(
        "/api/notebooks", headers=alice_headers, json={"name": "Access API"}
    ).json()["id"]
    other_notebook_id = client.post(
        "/api/notebooks", headers=alice_headers, json={"name": "Access API 2"}
    ).json()["id"]
    profile_id = client.post(
        "/api/agent-profiles", headers=alice_headers, json={"name": "Agent"}
    ).json()["id"]
    issued = client.post(
        f"/api/agent-profiles/{profile_id}/tokens",
        headers=alice_headers,
        json={
            "agent_profile_id": profile_id,
            "scopes": ["memory:read"],
            "default_notebook_id": notebook_id,
            "notebook_ids": [notebook_id],
        },
    ).json()
    token_id = issued["id"]

    # owner: 200, whole-object replace takes effect.
    updated = client.put(
        f"/api/agent-tokens/{token_id}/access",
        headers=alice_headers,
        json={
            "scopes": ["memory:read", "memory:propose"],
            "default_notebook_id": other_notebook_id,
            "notebook_ids": [other_notebook_id],
            "expires_at": None,
        },
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert sorted(body["scopes"]) == ["memory:propose", "memory:read"]
    assert body["default_notebook_id"] == other_notebook_id
    assert body["notebook_ids"] == [other_notebook_id]

    # someone else's token: 404, no X-User-Message leak of existence. Bob's
    # payload targets a notebook HE can read so the rejection is proven to
    # come from the store's owner-scoped lookup, not the notebook allowlist
    # check that runs first.
    bob_notebook_id = client.post(
        "/api/notebooks", headers=bob_headers, json={"name": "Bob's own"}
    ).json()["id"]
    foreign = client.put(
        f"/api/agent-tokens/{token_id}/access",
        headers=bob_headers,
        json={
            "scopes": ["memory:read"],
            "default_notebook_id": bob_notebook_id,
            "notebook_ids": [bob_notebook_id],
            "expires_at": None,
        },
    )
    assert foreign.status_code == 404

    # revoked token: 409 with the X-User-Message marker the frontend trusts.
    revoke = client.delete(f"/api/agent-tokens/{token_id}", headers=alice_headers)
    assert revoke.status_code == 200
    revoked_update = client.put(
        f"/api/agent-tokens/{token_id}/access",
        headers=alice_headers,
        json={
            "scopes": ["memory:read"],
            "default_notebook_id": other_notebook_id,
            "notebook_ids": [other_notebook_id],
            "expires_at": None,
        },
    )
    assert revoked_update.status_code == 409
    assert revoked_update.headers.get(USER_MESSAGE_HEADER) == "1"
    assert revoked_update.json()["detail"] == "这个 Token 已撤销，不能再修改权限"

    # A second live token to exercise payload-shape validation.
    live = client.post(
        f"/api/agent-profiles/{profile_id}/tokens",
        headers=alice_headers,
        json={
            "agent_profile_id": profile_id,
            "scopes": ["memory:read"],
            "default_notebook_id": notebook_id,
            "notebook_ids": [notebook_id],
        },
    ).json()

    # missing required field (expires_at omitted entirely): 422.
    missing_field = client.put(
        f"/api/agent-tokens/{live['id']}/access",
        headers=alice_headers,
        json={
            "scopes": ["memory:read"],
            "default_notebook_id": notebook_id,
            "notebook_ids": [notebook_id],
        },
    )
    assert missing_field.status_code == 422

    # extra/unknown field: 422 (extra="forbid").
    extra_field = client.put(
        f"/api/agent-tokens/{live['id']}/access",
        headers=alice_headers,
        json={
            "scopes": ["memory:read"],
            "default_notebook_id": notebook_id,
            "notebook_ids": [notebook_id],
            "expires_at": None,
            "unexpected": "field",
        },
    )
    assert extra_field.status_code == 422
