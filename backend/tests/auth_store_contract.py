"""Unified-authentication store contract, executed against both SQL adapters.

An external login signs in the account whose username equals the provider
username exactly; otherwise the person links an existing account by its
password or creates a new one. Unified authentication is on exactly while the
attached provider host describes a provider.
"""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

import pytest

from app.domain.auth_provider import AuthProviderDescriptor, ExternalIdentity
from app.domain.auth_utils import hash_password
from app.repositories.identity_errors import AuthStoreError


PROOF = "independent-browser-proof"
NAMESPACE = "test:tenant"


class Host:
    """A provider host whose describe() follows a switch, like the plugin's."""

    def __init__(self, namespace=NAMESPACE):
        self.enabled = True
        self.namespace = namespace

    def describe(self):
        if not self.enabled:
            return None
        return AuthProviderDescriptor(
            "test.auth", "test.auth.provider", "test", self.namespace, "generation-1",
            "统一认证", "https://identity.test/authorize", True,
        )


def sso_on(identity, host=None):
    host = host or Host()
    identity.auth.use_provider(host)
    return host


def handoff(identity, username, *, subject="subject-1", display_name="统一姓名", proof=PROOF):
    auth = identity.auth
    state = auth.begin(proof, ttl_seconds=600)
    return auth.stage_identity(
        auth.claim(state, proof),
        ExternalIdentity(NAMESPACE, subject, username, display_name), proof, ttl_seconds=600,
    )


def complete(identity, username, **kwargs):
    return identity.auth.complete(handoff(identity, username, **kwargs), kwargs.get("proof", PROOF), session_seconds=600)


def choice(identity, username, **kwargs):
    result = complete(identity, username, **kwargs)
    assert result["status"] == "choice_required", result
    return result["pending_id"]


def row(identity, sql, params=()):
    with identity.database.connect() as db:
        found = identity.auth._execute(db, sql, params).fetchone()
    return dict(found) if found else None


def rows(identity, sql, params=()):
    with identity.database.connect() as db:
        return [dict(item) for item in identity.auth._execute(db, sql, params).fetchall()]


def execute(identity, sql, params=()):
    with identity.auth._write() as db:
        identity.auth._execute(db, sql, params)


def set_builtin_password(identity, password="builtin-password"):
    password_hash, salt, iterations = hash_password(password)
    execute(
        identity,
        "UPDATE users SET password_hash=?,password_salt=?,password_iterations=? WHERE id='user-local'",
        (password_hash, salt, iterations),
    )


class AuthStoreContract:
    # --- direct sign-in by exact username ----------------------------------

    def test_exact_username_signs_in_the_existing_account(self, identity):
        user, local = identity.register_user_with_session("a12345678", "pw")
        sso_on(identity)
        result = complete(identity, "a12345678")
        assert result["status"] == "authenticated"
        assert result["user"].id == user.id
        session = row(identity, "SELECT * FROM auth_sessions WHERE token=?", (result["token"],))
        assert (session["auth_source"], session["provider_namespace"], session["external_subject"]) == (
            "sso", NAMESPACE, "subject-1",
        )
        assert identity.resolve_session(result["token"]).id == user.id

    def test_username_match_is_case_sensitive(self, identity):
        identity.register_user_with_session("a12345678", "pw")
        sso_on(identity)
        result = complete(identity, "A12345678")
        assert result["status"] == "choice_required"
        assert result["external_username"] == "A12345678"
        assert result["display_name"] == "统一姓名"

    def test_builtin_account_is_never_matched(self, identity):
        sso_on(identity)
        pending = choice(identity, "admin")
        # Nor can the name be taken over by a new account.
        with pytest.raises(AuthStoreError, match="username_conflict"):
            identity.auth.create(pending, PROOF, session_seconds=600)

    def test_disabled_account_is_refused(self, identity):
        user, _ = identity.register_user_with_session("a12345678", "pw")
        identity.auth.set_account_status(user.id, "disabled", actor_id="user-local")
        sso_on(identity)
        with pytest.raises(AuthStoreError, match="account_inactive"):
            complete(identity, "a12345678")

    # --- link an existing account --------------------------------------------

    def test_link_renames_the_old_account_and_replaces_its_sessions(self, identity):
        user, local = identity.register_user_with_session("a12345678", "pw")
        before = row(identity, "SELECT auth_revision FROM users WHERE id=?", (user.id,))["auth_revision"]
        sso_on(identity)
        pending = choice(identity, "W0012345", subject="employee-7", display_name="王五")
        linked, token = identity.auth.link(pending, PROOF, "a12345678", "pw", session_seconds=600)
        assert (linked.id, linked.username, linked.display_name) == (user.id, "W0012345", "王五")
        after = row(identity, "SELECT auth_revision FROM users WHERE id=?", (user.id,))
        assert after["auth_revision"] == before + 1
        assert row(identity, "SELECT 1 FROM auth_sessions WHERE token=?", (local,)) is None
        assert identity.resolve_session(token).id == user.id
        audit = rows(identity, "SELECT * FROM auth_identity_audit WHERE action='sso_linked'")
        assert [(a["target_user_id"], a["provider_namespace"], a["subject"]) for a in audit] == [
            (user.id, NAMESPACE, "employee-7"),
        ]
        assert all(secret not in json.dumps(audit, default=str) for secret in (pending, token, local, PROOF))
        with pytest.raises(AuthStoreError, match="invalid_transaction"):
            identity.auth.link(pending, PROOF, "a12345678", "pw", session_seconds=600)
        # The next external login matches the renamed account directly.
        assert complete(identity, "W0012345", subject="employee-7")["user"].id == user.id

    def test_link_fixes_a_case_variant_of_the_same_account(self, identity):
        user, _ = identity.register_user_with_session("a12345678", "pw")
        sso_on(identity)
        pending = choice(identity, "A12345678")
        linked, _ = identity.auth.link(pending, PROOF, "a12345678", "pw", session_seconds=600)
        assert (linked.id, linked.username) == (user.id, "A12345678")

    def test_wrong_password_and_unknown_name_are_one_rejection_and_keep_the_choice(self, identity, monkeypatch):
        import app.repositories.auth_store as store_module

        identity.register_user_with_session("a12345678", "pw")
        sso_on(identity)
        pending = choice(identity, "W0012345")
        checks = []
        real_verify = store_module.verify_password
        monkeypatch.setattr(store_module, "verify_password", lambda *args: checks.append(args[1:]) or real_verify(*args))
        errors = []
        for login_name, password in (("a12345678", "wrong"), ("z99999999", "pw")):
            with pytest.raises(AuthStoreError) as caught:
                identity.auth.link(pending, PROOF, login_name, password, session_seconds=600)
            errors.append(str(caught.value))
        assert errors == ["link_verification_failed", "link_verification_failed"]
        # The unknown name still spends a full password check.
        assert checks[1] == store_module._DUMMY_PASSWORD
        linked, _ = identity.auth.link(pending, PROOF, "a12345678", "pw", session_seconds=600)
        assert linked.username == "W0012345"

    def test_link_refuses_passwordless_builtin_and_disabled_accounts(self, identity):
        host = sso_on(identity)
        created, _ = identity.auth.create(choice(identity, "P0000001"), PROOF, session_seconds=600)
        pending = choice(identity, "W0012345")
        with pytest.raises(AuthStoreError, match="link_verification_failed"):
            identity.auth.link(pending, PROOF, created.username, "", session_seconds=600)
        set_builtin_password(identity)
        with pytest.raises(AuthStoreError, match="link_target_invalid"):
            identity.auth.link(pending, PROOF, "admin", "builtin-password", session_seconds=600)
        host.enabled = False
        old, _ = identity.register_user_with_session("a12345678", "pw")
        host.enabled = True
        identity.auth.set_account_status(old.id, "disabled", actor_id="user-local")
        with pytest.raises(AuthStoreError, match="account_inactive"):
            identity.auth.link(pending, PROOF, "a12345678", "pw", session_seconds=600)
        assert row(identity, "SELECT username FROM users WHERE id=?", (old.id,))["username"] == "a12345678"

    def test_a_claimed_account_cannot_be_linked_by_another_unified_login(self, identity):
        user, _ = identity.register_user_with_session("a12345678", "pw")
        sso_on(identity)
        assert row(identity, "SELECT sso_linked_at FROM users WHERE id=?", (user.id,))["sso_linked_at"] is None
        pending = choice(identity, "W0012345", subject="employee-7")
        identity.auth.link(pending, PROOF, "a12345678", "pw", session_seconds=600)
        assert row(identity, "SELECT sso_linked_at FROM users WHERE id=?", (user.id,))["sso_linked_at"] is not None
        other = choice(identity, "W0099999", subject="employee-8")
        # A wrong password never learns whether the account was claimed.
        with pytest.raises(AuthStoreError, match="link_verification_failed"):
            identity.auth.link(other, PROOF, "W0012345", "wrong", session_seconds=600)
        with pytest.raises(AuthStoreError, match="link_target_linked"):
            identity.auth.link(other, PROOF, "W0012345", "pw", session_seconds=600)
        assert row(identity, "SELECT username FROM users WHERE id=?", (user.id,))["username"] == "W0012345"
        # The administrator rename keeps the mark.
        identity.auth.set_username(user.id, "W0012346", actor_id="user-local")
        with pytest.raises(AuthStoreError, match="link_target_linked"):
            identity.auth.link(other, PROOF, "W0012346", "pw", session_seconds=600)

    def test_a_reset_password_does_not_make_a_created_account_linkable(self, identity):
        sso_on(identity)
        created, _ = identity.auth.create(choice(identity, "P0000001"), PROOF, session_seconds=600)
        assert row(identity, "SELECT sso_linked_at FROM users WHERE id=?", (created.id,))["sso_linked_at"] is not None
        identity.admin_reset_user_password("user-local", created.id, "reset-pw")
        pending = choice(identity, "W0012345", subject="employee-8")
        with pytest.raises(AuthStoreError, match="link_target_linked"):
            identity.auth.link(pending, PROOF, "P0000001", "reset-pw", session_seconds=600)

    def test_direct_sign_in_marks_the_account_once(self, identity):
        user, _ = identity.register_user_with_session("a12345678", "pw")
        sso_on(identity)
        complete(identity, "a12345678")
        first = row(identity, "SELECT sso_linked_at FROM users WHERE id=?", (user.id,))["sso_linked_at"]
        assert first is not None
        complete(identity, "a12345678")
        assert row(identity, "SELECT sso_linked_at FROM users WHERE id=?", (user.id,))["sso_linked_at"] == first

    def test_an_administrator_corrects_a_mistaken_new_account(self, identity):
        """The documented order: disable the mistaken account and give it
        another name, then give the old account the employee number; the next
        unified login signs in the old account directly."""
        host = sso_on(identity)
        host.enabled = False
        old, _ = identity.register_user_with_session("a12345678", "pw")
        host.enabled = True
        mistaken, _ = identity.auth.create(choice(identity, "W0012345"), PROOF, session_seconds=600)
        identity.auth.set_account_status(mistaken.id, "disabled", actor_id="user-local")
        identity.auth.set_username(mistaken.id, "W0012345-mistaken", actor_id="user-local")
        identity.auth.set_username(old.id, "W0012345", actor_id="user-local")
        assert row(identity, "SELECT sso_linked_at FROM users WHERE id=?", (old.id,))["sso_linked_at"] is None
        result = complete(identity, "W0012345")
        assert (result["status"], result["user"].id) == ("authenticated", old.id)
        assert row(identity, "SELECT sso_linked_at FROM users WHERE id=?", (old.id,))["sso_linked_at"] is not None

    def test_provider_username_follows_the_account_name_rule(self, identity):
        sso_on(identity)
        for bad in (" W0012345", "W0012345 ", "W00\x0712345", "W" * 513):
            with pytest.raises(AuthStoreError, match="invalid_identity"):
                handoff(identity, bad)

    def test_registration_refuses_a_name_whose_placeholder_email_is_held(self, identity):
        user, _ = identity.register_user_with_session("a12345678", "pw")
        identity.auth.set_username(user.id, "W0012345", actor_id="user-local")
        with pytest.raises(ValueError, match="username already exists"):
            identity.register_user_with_session("a12345678", "pw")

    def test_sso_admin_presence_ignores_the_builtin_administrator(self, identity):
        assert not identity.auth.has_sso_admin()
        user, _ = identity.register_user_with_session("a12345678", "pw")
        identity.set_user_role("user-local", user.id, "admin")
        assert identity.auth.has_sso_admin()
        identity.auth.set_account_status(user.id, "disabled", actor_id="user-local")
        assert not identity.auth.has_sso_admin()

    def test_link_and_create_are_stale_once_the_name_exists(self, identity):
        host = sso_on(identity)
        host.enabled = False
        old, _ = identity.register_user_with_session("a12345678", "pw")
        host.enabled = True
        first = choice(identity, "W0012345")
        second = choice(identity, "W0012345", proof="other-browser")
        winner, _ = identity.auth.create(first, PROOF, session_seconds=600)
        with pytest.raises(AuthStoreError, match="stale_transaction"):
            identity.auth.link(second, "other-browser", "a12345678", "pw", session_seconds=600)
        with pytest.raises(AuthStoreError, match="stale_transaction"):
            identity.auth.create(second, "other-browser", session_seconds=600)
        assert row(identity, "SELECT username FROM users WHERE id=?", (old.id,))["username"] == "a12345678"
        assert row(identity, "SELECT count(*) AS n FROM users WHERE username='W0012345'")["n"] == 1
        assert winner.username == "W0012345"

    # --- create a new account ------------------------------------------------

    def test_create_makes_a_passwordless_ordinary_user(self, identity):
        sso_on(identity)
        pending = choice(identity, "W0012345", subject="employee-7", display_name="王五")
        created, token = identity.auth.create(pending, PROOF, session_seconds=600)
        user = row(identity, "SELECT * FROM users WHERE id=?", (created.id,))
        assert (user["username"], user["display_name"], user["role"], user["status"]) == (
            "W0012345", "王五", "user", "active",
        )
        assert (user["password_hash"], user["password_salt"]) == ("", "")
        assert identity.resolve_session(token).id == created.id
        assert row(identity, "SELECT 1 FROM user_profiles WHERE user_id=?", (created.id,))
        assert [a["action"] for a in rows(identity, "SELECT action FROM auth_identity_audit WHERE target_user_id=?", (created.id,))] == ["sso_created"]
        with pytest.raises(AuthStoreError, match="invalid_transaction"):
            identity.auth.create(pending, PROOF, session_seconds=600)

    def test_create_refuses_a_case_variant_of_an_existing_name(self, identity):
        host = sso_on(identity)
        host.enabled = False
        identity.register_user_with_session("a12345678", "pw")
        host.enabled = True
        pending = choice(identity, "A12345678")
        with pytest.raises(AuthStoreError, match="username_case_conflict"):
            identity.auth.create(pending, PROOF, session_seconds=600)

    def test_concurrent_choices_commit_one_account(self, identity):
        sso_on(identity)
        pending = choice(identity, "W0012345")
        barrier = threading.Barrier(2)

        def create():
            barrier.wait()
            try:
                return identity.auth.create(pending, PROOF, session_seconds=600)[1]
            except AuthStoreError:
                return None

        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: create(), range(2)))
        assert len([result for result in results if result]) == 1
        assert row(identity, "SELECT count(*) AS n FROM users WHERE username='W0012345'")["n"] == 1

    # --- pending transactions ------------------------------------------------

    def test_pending_is_bound_to_the_browser_one_time_and_expiring(self, identity):
        sso_on(identity)
        pending = choice(identity, "W0012345")
        with pytest.raises(AuthStoreError, match="invalid_transaction"):
            identity.auth.create(pending, "another-browser", session_seconds=600)
        execute(identity, "UPDATE auth_transactions SET expires_at=1 WHERE purpose='choice'")
        with pytest.raises(AuthStoreError, match="invalid_transaction"):
            identity.auth.create(pending, PROOF, session_seconds=600)
        code = handoff(identity, "W0012345")
        identity.auth.complete(code, PROOF, session_seconds=600)
        with pytest.raises(AuthStoreError, match="invalid_transaction"):
            identity.auth.complete(code, PROOF, session_seconds=600)

    def test_claim_proof_and_replay_are_rejected(self, identity):
        sso_on(identity)
        state = identity.auth.begin(PROOF, ttl_seconds=600)
        with pytest.raises(AuthStoreError, match="invalid_transaction"):
            identity.auth.claim(state, "another-browser")
        claimed = identity.auth.claim(state, PROOF)
        with pytest.raises(AuthStoreError, match="invalid_transaction"):
            identity.auth.claim(state, PROOF)
        with pytest.raises(AuthStoreError, match="wrong_provider"):
            identity.auth.stage_identity(claimed, ExternalIdentity("other:tenant", "s", "u", "n"), PROOF, ttl_seconds=600)

    def test_cancel_and_logout_discard_pending_material(self, identity):
        sso_on(identity)
        pending = choice(identity, "W0012345")
        identity.auth.cancel(pending, "another-browser")
        identity.auth.cancel(pending, PROOF)
        with pytest.raises(AuthStoreError, match="invalid_transaction"):
            identity.auth.create(pending, PROOF, session_seconds=600)
        state = identity.auth.begin(PROOF, ttl_seconds=600)
        identity.auth.cancel_for_browser(PROOF)
        with pytest.raises(AuthStoreError, match="invalid_transaction"):
            identity.auth.claim(state, PROOF)

    def test_provider_change_or_switch_off_invalidates_staged_material(self, identity):
        host = sso_on(identity)
        pending = choice(identity, "W0012345")
        host.namespace = "other:tenant"
        with pytest.raises(AuthStoreError, match="stale_transaction"):
            identity.auth.create(pending, PROOF, session_seconds=600)
        host.namespace = NAMESPACE
        host.enabled = False
        with pytest.raises(AuthStoreError, match="sso_disabled"):
            identity.auth.create(pending, PROOF, session_seconds=600)
        with pytest.raises(AuthStoreError, match="sso_disabled"):
            identity.auth.begin(PROOF, ttl_seconds=600)

    def test_external_authentication_deadline_bounds_the_choice(self, identity):
        sso_on(identity)
        pending = choice(identity, "W0012345")
        with pytest.raises(AuthStoreError, match="external_auth_expired"):
            identity.auth.create(pending, PROOF, session_seconds=0)

    # --- sessions follow the switch ------------------------------------------

    def test_local_and_sso_sessions_follow_the_provider_switch(self, identity):
        user, local = identity.register_user_with_session("a12345678", "pw")
        host = sso_on(identity)
        assert identity.resolve_session(local) is None
        sso = complete(identity, "a12345678")["token"]
        assert identity.resolve_session(sso).id == user.id
        host.enabled = False
        assert identity.resolve_session(local).id == user.id
        assert identity.resolve_session(sso) is None
        host.enabled = True
        host.namespace = "other:tenant"
        assert identity.resolve_session(sso) is None

    def test_sso_touch_preserves_absolute_and_sliding_expiry(self, identity):
        identity.register_user_with_session("a12345678", "pw")
        sso_on(identity)
        sso = complete(identity, "a12345678")["token"]
        auth = identity.auth
        execute(identity, "UPDATE auth_sessions SET last_seen_at=? WHERE token=?", (auth._expires(-3600), sso))
        before = row(identity, "SELECT expires_at,absolute_expires_at FROM auth_sessions WHERE token=?", (sso,))
        assert identity.resolve_session(sso)
        after = row(identity, "SELECT expires_at,absolute_expires_at FROM auth_sessions WHERE token=?", (sso,))
        assert before == after
        execute(identity, "UPDATE auth_sessions SET absolute_expires_at=? WHERE token=?", (auth._expires(-1), sso))
        assert identity.resolve_session(sso) is None

    def test_session_projection_keeps_authority_from_one_validated_snapshot(self, identity, monkeypatch):
        user, local = identity.register_user_with_session("z00000001", "pw")
        original_profile = identity.auth._profile
        changed = False

        def promote_after_validation(db, validated_user):
            nonlocal changed
            if not changed:
                changed = True
                identity.set_user_role("user-local", user.id, "admin")
            return original_profile(db, validated_user)

        monkeypatch.setattr(identity.auth, "_profile", promote_after_validation)
        assert identity.resolve_session(local).role == "user"
        assert identity.resolve_session(local).role == "admin"

    def test_revocation_during_throttled_touch_is_rechecked(self, identity, monkeypatch):
        user, local = identity.register_user_with_session("z00000001", "pw")
        auth = identity.auth
        execute(identity, "UPDATE auth_sessions SET last_seen_at=? WHERE token=?", (auth._expires(-3600), local))
        original_profile = auth._profile
        revoked = False

        def revoke_after_validation(db, validated_user):
            nonlocal revoked
            if not revoked:
                revoked = True
                identity.delete_session(local)
            return original_profile(db, validated_user)

        monkeypatch.setattr(auth, "_profile", revoke_after_validation)
        assert identity.resolve_session(local) is None

    # --- local credentials while unified auth is on --------------------------

    def test_local_credentials_are_refused_while_unified_auth_is_on(self, identity):
        user, _ = identity.register_user_with_session("a12345678", "pw")
        host = sso_on(identity)
        for attempt in (
            lambda: identity.login_with_password("a12345678", "pw"),
            lambda: identity.register_user_with_session("b12345678", "pw"),
            lambda: identity.change_user_password(user.id, "pw", "new-pw"),
        ):
            with pytest.raises(AuthStoreError, match="local_auth_disabled"):
                attempt()
        # An administrator can still reset a forgotten password for linking.
        identity.admin_reset_user_password("user-local", user.id, "reset-pw")
        pending = choice(identity, "W0012345")
        assert identity.auth.link(pending, PROOF, "a12345678", "reset-pw", session_seconds=600)[0].id == user.id
        host.enabled = False
        logged_in, token = identity.login_with_password("w0012345", "reset-pw")
        assert logged_in.id == user.id and identity.resolve_session(token).id == user.id

    def test_owner_eligibility_follows_only_account_status(self, identity):
        user, _ = identity.register_user_with_session("a12345678", "pw")
        sso_on(identity)
        assert identity.auth.owner_eligible(user.id)
        identity.auth.set_account_status(user.id, "disabled", actor_id="user-local")
        assert not identity.auth.owner_eligible(user.id)

    # --- administrator account controls --------------------------------------

    def test_account_status_revokes_sessions_and_is_audited(self, identity):
        user, local = identity.register_user_with_session("a12345678", "pw")
        before = row(identity, "SELECT auth_revision FROM users WHERE id=?", (user.id,))["auth_revision"]
        result = identity.auth.set_account_status(user.id, "disabled", actor_id="user-local")
        assert result == {"id": user.id, "username": "a12345678", "status": "disabled"}
        assert identity.resolve_session(local) is None
        assert row(identity, "SELECT count(*) AS n FROM auth_sessions WHERE user_id=?", (user.id,))["n"] == 0
        assert row(identity, "SELECT auth_revision FROM users WHERE id=?", (user.id,))["auth_revision"] == before + 1
        assert identity.login_with_password("a12345678", "pw") is None
        identity.auth.set_account_status(user.id, "active", actor_id="user-local")
        assert identity.login_with_password("a12345678", "pw")[0].id == user.id
        actions = [a["action"] for a in rows(identity, "SELECT action,created_at FROM auth_identity_audit WHERE target_user_id=? ORDER BY created_at,id", (user.id,))]
        assert sorted(actions) == ["account_status:active", "account_status:disabled"]

    def test_username_change_is_unique_case_insensitively_and_revokes_sessions(self, identity):
        user, local = identity.register_user_with_session("a12345678", "pw")
        other, _ = identity.register_user_with_session("b12345678", "pw")
        with pytest.raises(AuthStoreError, match="username_conflict"):
            identity.auth.set_username(user.id, "B12345678", actor_id="user-local")
        with pytest.raises(AuthStoreError, match="username_conflict"):
            identity.auth.set_username(user.id, "ADMIN", actor_id="user-local")
        for bad in ("", " W0012345", "W00\x0712345"):
            with pytest.raises(AuthStoreError, match="invalid_username"):
                identity.auth.set_username(user.id, bad, actor_id="user-local")
        assert identity.resolve_session(local).id == user.id
        result = identity.auth.set_username(user.id, "W0012345", actor_id="user-local")
        assert result == {"id": user.id, "username": "W0012345", "status": "active"}
        assert identity.resolve_session(local) is None
        assert [a["action"] for a in rows(identity, "SELECT action FROM auth_identity_audit WHERE target_user_id=?", (user.id,))] == ["username_changed"]
        sso_on(identity)
        assert complete(identity, "W0012345")["user"].id == user.id
        assert other.id != user.id

    @pytest.mark.parametrize("operation", ["status", "username"])
    def test_account_controls_require_an_active_admin_and_spare_self_and_builtin(self, identity, operation):
        actor, _ = identity.register_user_with_session("a12345678", "pw")
        target, _ = identity.register_user_with_session("b12345678", "pw")

        def mutate(user_id, actor_id):
            if operation == "status":
                return identity.auth.set_account_status(user_id, "disabled", actor_id=actor_id)
            return identity.auth.set_username(user_id, "W0012345", actor_id=actor_id)

        with pytest.raises(AuthStoreError, match="admin_required"):
            mutate(target.id, actor.id)
        identity.set_user_role("user-local", actor.id, "admin")
        with pytest.raises(AuthStoreError, match="self_forbidden"):
            mutate(actor.id, actor.id)
        with pytest.raises(AuthStoreError, match="builtin_account"):
            mutate("user-local", actor.id)
        with pytest.raises(AuthStoreError, match="account_not_found"):
            mutate("user-missing", actor.id)
        identity.auth.set_account_status(actor.id, "disabled", actor_id="user-local")
        with pytest.raises(AuthStoreError, match="admin_required"):
            mutate(target.id, actor.id)
        identity.auth.set_account_status(actor.id, "active", actor_id="user-local")
        mutate(target.id, actor.id)

    @pytest.mark.parametrize("operation", ["role", "password", "global_limit", "user_limit"])
    def test_admin_mutations_recheck_status_after_request_authentication(self, identity, operation):
        actor, session = identity.register_user_with_session("z00000001", "pw")
        target = identity.create_user("z00000002", "pw")
        identity.set_user_role("user-local", actor.id, "admin")
        authenticated_actor = identity.resolve_session(session)
        assert authenticated_actor.role == "admin"

        def mutate(second=False):
            actor_id = authenticated_actor.id
            if operation == "role":
                return identity.set_user_role(actor_id, target.id, "user" if second else "admin")
            if operation == "password":
                return identity.admin_reset_user_password(actor_id, target.id, "second-password" if second else "first-password")
            if operation == "global_limit":
                return identity.set_global_document_limit_default(actor_id, 300 if second else 200)
            return identity.set_user_document_limit_override(actor_id, target.id, 300 if second else 200)

        def snapshot():
            found = row(identity, "SELECT role,password_hash,password_salt,password_iterations,auth_revision FROM users WHERE id=?", (target.id,))
            return found, identity.global_document_limit_default(), identity.user_document_limit_override(target.id)

        mutate()
        before = snapshot()
        target_session = identity.create_session(target.id)
        identity.auth.set_account_status(actor.id, "disabled", actor_id="user-local")
        assert identity.resolve_session(session) is None
        with pytest.raises(PermissionError, match="admin role required"):
            mutate(second=True)
        assert snapshot() == before
        assert identity.resolve_session(target_session).id == target.id
        identity.auth.set_account_status(actor.id, "active", actor_id="user-local")
        mutate(second=True)
        assert snapshot() != before
