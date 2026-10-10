from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app.api.deps import USER_MESSAGE_HEADER
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.domain.agent_tools import OWNER_ONLY_TIERS_MESSAGE, AgentAccessDenied
from app.models.identity import AgentTokenAccess
from app.repositories.identity_errors import (
    AgentOwnerOnlyTierError,
    AgentTokenAccessConflictError,
    AgentTokenInactiveError,
    AgentTokenSecretUnavailableError,
)
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
        scopes or ["read"],
        notebook.id,
        [notebook.id],
        expires_at,
    )


def test_token_is_hashed_and_the_list_never_carries_the_raw_value(token_context):
    service, alice, _bob, notebook, _other = token_context
    profile = service.create_agent_profile(alice.id, "Claude Code", "Local agent")

    issued = _issue(service, alice, profile, notebook)
    listed = service.list_agent_tokens(alice.id)

    assert issued.token.startswith(f"snm_{issued.id}.")
    assert listed[0].id == issued.id
    assert not hasattr(listed[0], "token")
    assert "hash" not in listed[0].model_dump()
    assert issued.token not in listed[0].model_dump_json()
    assert listed[0].copyable is True
    with service.store.database.connect() as db:
        row = db.execute(
            "SELECT token_hash,token_plain FROM agent_access_tokens WHERE id=?",
            (issued.id,),
        ).fetchone()
    assert row["token_hash"] == hashlib.sha256(issued.token.encode()).hexdigest()
    assert issued.token not in row["token_hash"]
    # The plaintext is kept for re-copying, but never travels with the auth row.
    assert row["token_plain"] == issued.token
    assert "token_plain" not in service.store.agent_token_auth_row(issued.id)


def test_owner_disabled_after_agent_auth_is_rechecked_for_live_session(token_context, repo):
    service, alice, _bob, notebook, _other = token_context
    profile = service.create_agent_profile(alice.id, "Owner eligibility", "")
    issued = _issue(service, alice, profile, notebook)
    principal = service.resolve_agent_token(issued.token)
    assert principal is not None
    repo._runtime.identity.auth.set_account_status(alice.id, "disabled", actor_id="user-local")
    assert service.resolve_agent_token(issued.token) is None
    assert service.refresh_agent_principal(principal.token_id) is None
    with pytest.raises(PermissionError):
        service.require_agent_access(principal, "memory:read", notebook.id)


def test_agent_owner_eligibility_ignores_the_unified_auth_switch(token_context, repo):
    """Only users.status decides whether a token owner is eligible: turning
    unified authentication on changes how people sign in, not their tokens."""
    from tests.auth_store_contract import Host

    service, alice, _bob, notebook, _other = token_context
    profile = service.create_agent_profile(alice.id, "Switch eligibility", "")
    issued = _issue(service, alice, profile, notebook)
    principal = service.resolve_agent_token(issued.token)
    repo._runtime.identity.auth.use_provider(Host())
    assert service.resolve_agent_token(issued.token) is not None
    service.require_agent_access(principal, "memory:read", notebook.id)


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
            ["read"],
            notebook.id,
            [notebook.id],
            None,
        )
    with pytest.raises(ValueError):
        service.issue_agent_token(
            alice.id,
            alice_profile.id,
            ["read"],
            notebook.id,
            [other.id],
            None,
        )

    service.notebooks.add_member(notebook.id, bob.id)
    bob_token = _issue(service, bob, bob_profile, notebook, scopes=["read"])
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
    assert refreshed.scopes == ["read"]

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
        scopes=["read"],
    )
    with service.store.database.connect() as db:
        original_hash = db.execute(
            "SELECT token_hash FROM agent_access_tokens WHERE id=?", (issued.id,)
        ).fetchone()["token_hash"]
    # An already-connected Agent holds the principal it authenticated with
    # before the edit; authorization must follow the live row, not this copy.
    stale_principal = service.resolve_agent_token(issued.token)
    assert stale_principal is not None

    updated = service.update_agent_token_access(
        alice.id, issued.id, ["contribute"], other.id, [other.id], None
    )

    assert updated.id == issued.id
    assert updated.scopes == ["contribute"]
    assert updated.default_notebook_id == other.id
    assert updated.notebook_ids == [other.id]
    assert updated.agent_profile_id == profile.id
    assert updated.created_at == issued.created_at

    listed = service.list_agent_tokens(alice.id)[0]
    assert listed.scopes == ["contribute"]
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
    assert principal.scopes == ["contribute"]
    assert principal.notebook_ids == [other.id]
    assert principal.default_notebook_id == other.id

    # A removed scope/notebook is denied right away ...
    with pytest.raises(PermissionError):
        service.require_agent_access(principal, "memory:read_candidates", other.id)
    with pytest.raises(PermissionError):
        service.require_agent_access(principal, "memory:propose", notebook.id)
    # ... and a newly granted scope/notebook is allowed right away.
    assert service.require_agent_access(principal, "memory:propose", other.id) is None
    # The same holds for the pre-edit principal an MCP session still carries.
    with pytest.raises(PermissionError):
        service.require_agent_access(stale_principal, "memory:read", notebook.id)
    assert service.require_agent_access(stale_principal, "memory:propose", other.id) is None


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
    scopes = ["read"]

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


def test_update_access_expected_snapshot_refuses_a_stale_editor(token_context):
    service, alice, _bob, notebook, other = token_context
    profile = service.create_agent_profile(alice.id, "Two tabs", "")
    issued = _issue(
        service, alice, profile, notebook,
        scopes=["read", "delete"],
        expires_at="2030-01-02T03:04:05+00:00",
    )
    # Both editors open from the same stored configuration (a client may
    # echo the expiry in any offset and list members in any order).
    opened = AgentTokenAccess(
        scopes=["delete", "read"],
        default_notebook_id=notebook.id,
        notebook_ids=[notebook.id],
        expires_at="2030-01-02T11:04:05+08:00",
    )

    # Tab B narrows the token.
    narrowed = service.update_agent_token_access(
        alice.id, issued.id, ["read"], notebook.id, [notebook.id],
        "2030-01-02T03:04:05+00:00", opened,
    )
    assert narrowed.scopes == ["read"]

    # Tab A, still holding the old snapshot, only meant to add a notebook; it
    # must not silently restore delete.
    with pytest.raises(AgentTokenAccessConflictError):
        service.update_agent_token_access(
            alice.id, issued.id, ["read", "delete"], notebook.id,
            [notebook.id, other.id], "2030-01-02T03:04:05+00:00", opened,
        )
    listed = service.list_agent_tokens(alice.id)[0]
    assert listed.scopes == ["read"]
    assert listed.notebook_ids == [notebook.id]

    # Without a precondition the replace is last-writer-wins by contract.
    service.update_agent_token_access(
        alice.id, issued.id, ["read"], notebook.id, [notebook.id, other.id],
        None,
    )
    assert sorted(service.list_agent_tokens(alice.id)[0].notebook_ids) == sorted(
        [notebook.id, other.id]
    )


class _EditAfterFirstRead:
    """Connection proxy that commits a token access edit right after the
    first statement of an auth read, i.e. between the two statements a
    split read would issue."""

    def __init__(self, connection, edit):
        self._connection = connection
        self._edit = edit
        self.fired = False

    def __enter__(self):
        self._connection.__enter__()
        return self

    def __exit__(self, *exc):
        return self._connection.__exit__(*exc)

    def execute(self, *args, **kwargs):
        cursor = self._connection.execute(*args, **kwargs)
        if not self.fired:
            self.fired = True
            self._edit()
        return cursor

    def __getattr__(self, name):
        return getattr(self._connection, name)


def test_auth_row_reads_one_access_snapshot_across_a_concurrent_edit(
    token_context, monkeypatch,
):
    service, alice, _bob, notebook, other = token_context
    store = service.store
    profile = service.create_agent_profile(alice.id, "Interleaved", "")
    issued = _issue(service, alice, profile, notebook, scopes=["delete"])
    old = (["delete"], [notebook.id])
    new = (["read"], sorted([notebook.id, other.id]))

    reader = _EditAfterFirstRead(
        store.database.connect(),
        lambda: store.update_agent_token_access(
            issued.id, alice.id, new[0], notebook.id, new[1], None
        ),
    )
    monkeypatch.setattr(store.database, "connect", lambda: reader)
    row = store.agent_token_auth_row(issued.id)
    monkeypatch.undo()

    assert reader.fired
    seen = (json.loads(row["scopes_json"]), sorted(row["notebook_ids"]))
    # Never "old write scope + new allowlist": the read is all old or all new.
    assert seen in (old, new), seen
    # The edit itself did commit, and the next read sees all of it.
    fresh = store.agent_token_auth_row(issued.id)
    assert (json.loads(fresh["scopes_json"]), sorted(fresh["notebook_ids"])) == new


def test_empty_expiry_string_means_no_expiry_on_issue_and_update(token_context):
    service, alice, _bob, notebook, _other = token_context
    profile = service.create_agent_profile(alice.id, "Blank expiry", "")
    issued = service.issue_agent_token(
        alice.id, profile.id, ["read"], notebook.id, [notebook.id], ""
    )
    assert issued.expires_at is None
    updated = service.update_agent_token_access(
        alice.id, issued.id, ["read"], notebook.id, [notebook.id], ""
    )
    assert updated.expires_at is None
    assert service.list_agent_tokens(alice.id)[0].expires_at is None


def test_update_access_rejects_revoked_disabled_foreign_and_invalid_input(
    token_context, repo,
):
    service, alice, bob, notebook, other = token_context
    profile = service.create_agent_profile(alice.id, "Guarded", "")
    scopes = ["read"]

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
            "scopes": ["read"],
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
            "scopes": ["read"],
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
            "scopes": ["read"],
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
            "scopes": ["read"],
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
            "scopes": ["read"],
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
            "scopes": ["read", "contribute"],
            "default_notebook_id": other_notebook_id,
            "notebook_ids": [other_notebook_id],
            "expires_at": None,
        },
    )
    assert updated.status_code == 200, updated.text
    body = updated.json()
    assert sorted(body["scopes"]) == ["contribute", "read"]
    assert body["default_notebook_id"] == other_notebook_id
    assert body["notebook_ids"] == [other_notebook_id]

    # someone else's token: 404 with the same message a missing token gets, so
    # existence is not revealed. Bob's payload targets a notebook HE can read
    # so the rejection is proven to come from the store's owner-scoped lookup,
    # not the notebook allowlist check that runs first.
    bob_notebook_id = client.post(
        "/api/notebooks", headers=bob_headers, json={"name": "Bob's own"}
    ).json()["id"]
    foreign = client.put(
        f"/api/agent-tokens/{token_id}/access",
        headers=bob_headers,
        json={
            "scopes": ["read"],
            "default_notebook_id": bob_notebook_id,
            "notebook_ids": [bob_notebook_id],
            "expires_at": None,
        },
    )
    assert foreign.status_code == 404
    assert foreign.json()["detail"] == "没有找到这个 Token，可能已被删除"
    missing = client.put(
        "/api/agent-tokens/token-does-not-exist/access",
        headers=bob_headers,
        json={
            "scopes": ["read"],
            "default_notebook_id": bob_notebook_id,
            "notebook_ids": [bob_notebook_id],
            "expires_at": None,
        },
    )
    assert missing.status_code == 404
    assert missing.json() == foreign.json()

    # revoked token: 409 with the X-User-Message marker the frontend trusts.
    revoke = client.delete(f"/api/agent-tokens/{token_id}", headers=alice_headers)
    assert revoke.status_code == 200
    revoked_update = client.put(
        f"/api/agent-tokens/{token_id}/access",
        headers=alice_headers,
        json={
            "scopes": ["read"],
            "default_notebook_id": other_notebook_id,
            "notebook_ids": [other_notebook_id],
            "expires_at": None,
        },
    )
    assert revoked_update.status_code == 409
    assert revoked_update.headers.get(USER_MESSAGE_HEADER) == "1"
    assert revoked_update.json()["detail"] == "这个 Token 已撤销，不能再修改权限"

    # A second live token to exercise the precondition and payload shape.
    live = client.post(
        f"/api/agent-profiles/{profile_id}/tokens",
        headers=alice_headers,
        json={
            "agent_profile_id": profile_id,
            "scopes": ["read"],
            "default_notebook_id": notebook_id,
            "notebook_ids": [notebook_id],
        },
    ).json()

    # a stale expected snapshot: 409 with its own user message.
    stale = client.put(
        f"/api/agent-tokens/{live['id']}/access",
        headers=alice_headers,
        json={
            "scopes": ["read", "contribute"],
            "default_notebook_id": notebook_id,
            "notebook_ids": [notebook_id],
            "expires_at": None,
            "expected": {
                "scopes": ["read", "contribute"],
                "default_notebook_id": notebook_id,
                "notebook_ids": [notebook_id],
                "expires_at": None,
            },
        },
    )
    assert stale.status_code == 409
    assert stale.headers.get(USER_MESSAGE_HEADER) == "1"
    assert stale.json()["detail"] == "这个 Token 的权限刚在别处被修改过，请取消后重新打开再改"
    fresh = client.put(
        f"/api/agent-tokens/{live['id']}/access",
        headers=alice_headers,
        json={
            "scopes": ["read", "contribute"],
            "default_notebook_id": notebook_id,
            "notebook_ids": [notebook_id],
            "expires_at": None,
            "expected": {
                "scopes": ["read"],
                "default_notebook_id": notebook_id,
                "notebook_ids": [notebook_id],
                "expires_at": None,
            },
        },
    )
    assert fresh.status_code == 200, fresh.text

    # an allowlisted notebook the owner cannot read: 422 with a user message.
    unreadable = client.put(
        f"/api/agent-tokens/{live['id']}/access",
        headers=alice_headers,
        json={
            "scopes": ["read"],
            "default_notebook_id": notebook_id,
            "notebook_ids": [notebook_id, bob_notebook_id],
            "expires_at": None,
        },
    )
    assert unreadable.status_code == 422
    assert unreadable.headers.get(USER_MESSAGE_HEADER) == "1"
    assert unreadable.json()["detail"] == "白名单里有你已无权访问的笔记本，请取消勾选后再保存"

    # a token whose Profile is disabled: its own 409 message.
    disabled_profile_id = client.post(
        "/api/agent-profiles", headers=alice_headers, json={"name": "Retired"}
    ).json()["id"]
    disabled_token = client.post(
        f"/api/agent-profiles/{disabled_profile_id}/tokens",
        headers=alice_headers,
        json={
            "agent_profile_id": disabled_profile_id,
            "scopes": ["read"],
            "default_notebook_id": notebook_id,
            "notebook_ids": [notebook_id],
        },
    ).json()
    assert client.patch(
        f"/api/agent-profiles/{disabled_profile_id}",
        headers=alice_headers,
        json={"status": "revoked"},
    ).status_code == 200
    disabled_update = client.put(
        f"/api/agent-tokens/{disabled_token['id']}/access",
        headers=alice_headers,
        json={
            "scopes": ["read", "contribute"],
            "default_notebook_id": notebook_id,
            "notebook_ids": [notebook_id],
            "expires_at": None,
        },
    )
    assert disabled_update.status_code == 409
    assert disabled_update.headers.get(USER_MESSAGE_HEADER) == "1"
    assert disabled_update.json()["detail"] == "所属 Agent Profile 已停用，这个 Token 已失效"

    # missing required field (expires_at omitted entirely): 422.
    missing_field = client.put(
        f"/api/agent-tokens/{live['id']}/access",
        headers=alice_headers,
        json={
            "scopes": ["read"],
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
            "scopes": ["read"],
            "default_notebook_id": notebook_id,
            "notebook_ids": [notebook_id],
            "expires_at": None,
            "unexpected": "field",
        },
    )
    assert extra_field.status_code == 422


# ---------------------------------------------------------------------------
# Five tiers, structured refusals, re-copyable tokens, 401 reasons.
# ---------------------------------------------------------------------------


def test_capabilities_map_to_tiers_and_refusals_name_the_reason(token_context):
    service, alice, _bob, notebook, other = token_context
    profile = service.create_agent_profile(alice.id, "Tiers", "")
    issued = _issue(service, alice, profile, notebook, scopes=["read"])
    principal = service.resolve_agent_token(issued.token)

    # ``read`` covers knowledge, the owner's Memory and its candidates.
    for capability in ("knowledge:read", "memory:read", "memory:read_candidates",
                       "agent_profile:read"):
        assert service.require_agent_access(principal, capability, notebook.id) is None

    with pytest.raises(AgentAccessDenied) as missing:
        service.require_agent_access(principal, "ask:execute", notebook.id)
    assert (missing.value.reason, missing.value.tier) == ("scope_missing", "ask")
    assert str(missing.value) == "此凭证缺少「问答」权限，请在 Agent 接入页为它勾选后重试"
    assert notebook.id not in str(missing.value)

    with pytest.raises(AgentAccessDenied) as outside:
        service.require_agent_access(principal, "knowledge:read", other.id)
    assert outside.value.reason == "notebook_not_allowed"
    assert outside.value.notebook_id == other.id

    # Still a PermissionError: every caller that maps a deny to 404 keeps doing so.
    assert isinstance(outside.value, PermissionError)

    # A capability nobody declared is a programming error, not a silent deny.
    with pytest.raises(ValueError, match="unknown Agent capability"):
        service.require_agent_access(principal, "read", notebook.id)

    service.revoke_agent_token(alice.id, issued.id)
    with pytest.raises(AgentAccessDenied) as inactive:
        service.require_agent_access(principal, "knowledge:read", notebook.id)
    assert inactive.value.reason == "inactive"


def test_unreadable_allowlisted_notebook_is_its_own_reason(token_context):
    service, alice, bob, notebook, _other = token_context
    profile = service.create_agent_profile(bob.id, "Member", "")
    service.notebooks.add_member(notebook.id, bob.id)
    issued = _issue(service, bob, profile, notebook, scopes=["read"])
    principal = service.resolve_agent_token(issued.token)
    service.notebooks.remove_member(notebook.id, bob.id)
    with pytest.raises(AgentAccessDenied) as denied:
        service.require_agent_access(principal, "knowledge:read", notebook.id)
    assert denied.value.reason == "notebook_unreadable"


def test_legacy_capability_strings_are_no_longer_issuable(token_context):
    service, alice, _bob, notebook, _other = token_context
    profile = service.create_agent_profile(alice.id, "Legacy", "")
    with pytest.raises(ValueError, match="unsupported agent scopes"):
        _issue(service, alice, profile, notebook, scopes=["knowledge:read"])


def test_manage_and_delete_need_an_owned_notebook_in_the_allowlist(token_context):
    service, alice, bob, notebook, _other = token_context
    service.notebooks.add_member(notebook.id, bob.id)
    profile = service.create_agent_profile(bob.id, "Member writer", "")
    for tier in ("manage", "delete"):
        with pytest.raises(AgentOwnerOnlyTierError) as refused:
            _issue(service, bob, profile, notebook, scopes=["read", tier])
        assert str(refused.value) == OWNER_ONLY_TIERS_MESSAGE
    member_token = _issue(service, bob, profile, notebook, scopes=["read"])
    with pytest.raises(AgentOwnerOnlyTierError):
        service.update_agent_token_access(
            bob.id, member_token.id, ["read", "manage"], notebook.id,
            [notebook.id], None,
        )
    # The owner may hold them on the notebook she owns.
    owner_profile = service.create_agent_profile(alice.id, "Owner writer", "")
    owner_token = _issue(
        service, alice, owner_profile, notebook, scopes=["manage", "delete"]
    )
    assert owner_token.scopes == ["manage", "delete"]


def test_secret_is_owner_only_cleared_on_revoke_and_absent_for_legacy(token_context):
    service, alice, bob, notebook, _other = token_context
    profile = service.create_agent_profile(alice.id, "Copy again", "")
    issued = _issue(service, alice, profile, notebook)

    assert service.agent_token_secret(alice.id, issued.id) == issued.token
    with pytest.raises(KeyError):
        service.agent_token_secret(bob.id, issued.id)
    with pytest.raises(KeyError):
        service.agent_token_secret(alice.id, "token-missing")

    # A token issued before plaintext storage (token_plain NULL).
    legacy = _issue(service, alice, profile, notebook)
    with service.store.database.write() as db:
        db.execute(
            "UPDATE agent_access_tokens SET token_plain=NULL WHERE id=?", (legacy.id,)
        )
    with pytest.raises(AgentTokenSecretUnavailableError):
        service.agent_token_secret(alice.id, legacy.id)
    listed = {item.id: item for item in service.list_agent_tokens(alice.id)}
    assert listed[legacy.id].copyable is False
    assert listed[issued.id].copyable is True
    # Authentication compares the hash only: the legacy token still works.
    assert service.resolve_agent_token(legacy.token) is not None

    revoked = service.revoke_agent_token(alice.id, issued.id)
    assert revoked.copyable is False
    with pytest.raises(AgentTokenInactiveError):
        service.agent_token_secret(alice.id, issued.id)
    with service.store.database.connect() as db:
        assert db.execute(
            "SELECT token_plain FROM agent_access_tokens WHERE id=?", (issued.id,)
        ).fetchone()[0] is None


def test_resolve_status_names_why_a_matching_token_fails(token_context, repo):
    service, alice, _bob, notebook, _other = token_context
    profile = service.create_agent_profile(alice.id, "Statuses", "")
    good = _issue(service, alice, profile, notebook)
    principal, reason = service.resolve_agent_token_status(good.token)
    assert principal is not None and reason == ""

    # Malformed, unknown and tampered tokens are one indistinguishable answer.
    assert service.resolve_agent_token_status("nonsense") == (None, "token_invalid")
    assert service.resolve_agent_token_status(
        "snm_token-missing.secret"
    ) == (None, "token_invalid")
    assert service.resolve_agent_token_status(good.token + "x") == (None, "token_invalid")

    expired = _issue(
        service, alice, profile, notebook,
        expires_at=(datetime.now(timezone.utc) - timedelta(minutes=1))
        .replace(microsecond=0).isoformat(),
    )
    assert service.resolve_agent_token_status(expired.token) == (None, "token_expired")

    revoked = _issue(service, alice, profile, notebook)
    service.revoke_agent_token(alice.id, revoked.id)
    assert service.resolve_agent_token_status(revoked.token) == (None, "token_revoked")

    service.update_agent_profile(profile.id, alice.id, {"status": "revoked"})
    assert service.resolve_agent_token_status(good.token) == (None, "profile_disabled")

    other_profile = service.create_agent_profile(alice.id, "Eligibility", "")
    eligible = _issue(service, alice, other_profile, notebook)
    repo._runtime.identity.auth.set_account_status(
        alice.id, "disabled", actor_id="user-local"
    )
    assert service.resolve_agent_token_status(eligible.token) == (
        None, "owner_ineligible"
    )


def test_secret_endpoint_and_owner_only_tier_errors_are_user_messages(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'token-secret.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import create_app

    client = TestClient(create_app())
    alice_headers, _ = _register(client, "h00119008")
    bob_headers, bob_id = _register(client, "i00119009")
    notebook_id = client.post(
        "/api/notebooks", headers=alice_headers, json={"name": "Secret API"}
    ).json()["id"]
    profile_id = client.post(
        "/api/agent-profiles", headers=alice_headers, json={"name": "Agent"}
    ).json()["id"]
    issued = client.post(
        f"/api/agent-profiles/{profile_id}/tokens",
        headers=alice_headers,
        json={
            "agent_profile_id": profile_id,
            "scopes": ["read", "ask"],
            "default_notebook_id": notebook_id,
            "notebook_ids": [notebook_id],
        },
    ).json()
    listed = client.get("/api/agent-tokens", headers=alice_headers).json()
    assert listed[0]["copyable"] is True
    assert issued["token"] not in json.dumps(listed)

    secret = client.get(
        f"/api/agent-tokens/{issued['id']}/secret", headers=alice_headers
    )
    assert secret.status_code == 200
    assert secret.json() == {"token": issued["token"]}
    assert secret.headers["cache-control"] == "no-store"

    foreign = client.get(f"/api/agent-tokens/{issued['id']}/secret", headers=bob_headers)
    missing = client.get("/api/agent-tokens/token-nope/secret", headers=bob_headers)
    assert foreign.status_code == missing.status_code == 404
    assert foreign.json() == missing.json()

    client.delete(f"/api/agent-tokens/{issued['id']}", headers=alice_headers)
    revoked = client.get(
        f"/api/agent-tokens/{issued['id']}/secret", headers=alice_headers
    )
    assert revoked.status_code == 409
    assert revoked.headers.get(USER_MESSAGE_HEADER) == "1"
    assert revoked.json()["detail"] == "已撤销的 token 不能再复制"

    legacy = client.post(
        f"/api/agent-profiles/{profile_id}/tokens",
        headers=alice_headers,
        json={
            "agent_profile_id": profile_id,
            "scopes": ["read"],
            "default_notebook_id": notebook_id,
            "notebook_ids": [notebook_id],
        },
    ).json()
    from app.api.deps import repository

    with repository()._runtime.memory_service.store.database.write() as db:
        db.execute(
            "UPDATE agent_access_tokens SET token_plain=NULL WHERE id=?",
            (legacy["id"],),
        )
    old = client.get(f"/api/agent-tokens/{legacy['id']}/secret", headers=alice_headers)
    assert old.status_code == 409
    assert old.headers.get(USER_MESSAGE_HEADER) == "1"
    assert old.json()["detail"] == "这个 token 签发于旧版本，无法再次复制，如需请重新签发"

    # manage/delete without an owned notebook: 422 the page can show as is,
    # on both issue and edit.
    owner_only = OWNER_ONLY_TIERS_MESSAGE
    repository()._runtime.memory_service.notebooks.add_member(notebook_id, bob_id)
    bob_profile = client.post(
        "/api/agent-profiles", headers=bob_headers, json={"name": "Bob agent"}
    ).json()["id"]
    refused = client.post(
        f"/api/agent-profiles/{bob_profile}/tokens",
        headers=bob_headers,
        json={
            "agent_profile_id": bob_profile,
            "scopes": ["read", "manage"],
            "default_notebook_id": notebook_id,
            "notebook_ids": [notebook_id],
        },
    )
    assert refused.status_code == 422
    assert refused.headers.get(USER_MESSAGE_HEADER) == "1"
    assert refused.json()["detail"] == owner_only
    bob_token = client.post(
        f"/api/agent-profiles/{bob_profile}/tokens",
        headers=bob_headers,
        json={
            "agent_profile_id": bob_profile,
            "scopes": ["read"],
            "default_notebook_id": notebook_id,
            "notebook_ids": [notebook_id],
        },
    ).json()
    edit = client.put(
        f"/api/agent-tokens/{bob_token['id']}/access",
        headers=bob_headers,
        json={
            "scopes": ["read", "delete"],
            "default_notebook_id": notebook_id,
            "notebook_ids": [notebook_id],
            "expires_at": None,
        },
    )
    assert edit.status_code == 422
    assert edit.headers.get(USER_MESSAGE_HEADER) == "1"
    assert edit.json()["detail"] == owner_only


def test_a_wrong_secret_never_learns_why_a_real_token_id_is_dead(token_context):
    """The specific 401 reasons are only for a caller holding the real token.
    A wrong secret on a revoked, expired or disabled-profile token id gets the
    same ``token_invalid`` as an id that never existed, so nobody can probe
    which token ids exist or what state they are in."""
    service, alice, _bob, notebook, _other = token_context
    profile = service.create_agent_profile(alice.id, "Probe target", "")
    revoked = _issue(service, alice, profile, notebook)
    service.revoke_agent_token(alice.id, revoked.id)
    expired = _issue(
        service, alice, profile, notebook,
        expires_at=(datetime.now(timezone.utc) - timedelta(minutes=1))
        .replace(microsecond=0).isoformat(),
    )
    retired = service.create_agent_profile(alice.id, "Retired", "")
    disabled = _issue(service, alice, retired, notebook)
    service.update_agent_profile(retired.id, alice.id, {"status": "revoked"})

    # The real tokens do get their reasons ...
    assert service.resolve_agent_token_status(revoked.token)[1] == "token_revoked"
    assert service.resolve_agent_token_status(expired.token)[1] == "token_expired"
    assert service.resolve_agent_token_status(disabled.token)[1] == "profile_disabled"
    # ... a wrong secret on the same ids does not.
    for issued in (revoked, expired, disabled):
        assert service.resolve_agent_token_status(
            f"snm_{issued.id}.wrong"
        ) == (None, "token_invalid"), issued.id
        assert service.resolve_agent_token_status(
            issued.token[:-1] + ("A" if issued.token[-1] != "A" else "B")
        ) == (None, "token_invalid"), issued.id
