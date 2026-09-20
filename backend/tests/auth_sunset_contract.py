"""Behavioral contract executed against both real SQL adapters."""
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
import threading

import pytest

from app.domain.auth_policy import AuthStoreError
from app.domain.auth_provider import ExternalIdentity


PROOF = "independent-browser-proof"


def dual(identity):
    return identity.auth.set_policy(
        "dual", actor_id="user-local", expected_revision=0,
        provider_id="test.provider", provider_namespace="test:tenant",
        config_generation="generation-1",
    )


def pending(identity, user, local_token, *, subject="stable-1", username="CorpUID"):
    auth = identity.auth
    state = auth.begin("bind", PROOF, ttl_seconds=600, session_token=local_token, password="pw")
    claimed = auth.claim(state, PROOF)
    code = auth.stage_identity(
        claimed, ExternalIdentity("test:tenant",subject,username,"Test User"),
        PROOF, ttl_seconds=600,
    )
    result = auth.inspect_completion(code, PROOF, session_seconds=600)
    assert result["status"] == "binding_required"
    assert result["local_login_name"] == user.username
    return result["pending_id"]


def bind(identity, user, token, **kwargs):
    pending_id = pending(identity,user,token,**kwargs)
    return identity.auth.confirm(pending_id,PROOF,session_token=token,session_seconds=600)


def login_sso(identity, subject, username):
    auth = identity.auth
    state = auth.begin("login",PROOF,ttl_seconds=600)
    code = auth.stage_identity(auth.claim(state,PROOF),ExternalIdentity("test:tenant",subject,username,"Test User"),PROOF,ttl_seconds=600)
    return auth.inspect_completion(code,PROOF,session_seconds=600)


def prepare_cutover(identity):
    first, first_local = identity.register_user_with_session("z00000001","pw")
    second, second_local = identity.register_user_with_session("z00000002","pw")
    identity.set_user_role("user-local",first.id,"admin")
    identity.set_user_role("user-local",second.id,"admin")
    dual(identity)
    bind(identity,first,first_local,subject="first",username="first-admin")
    bind(identity,second,second_local,subject="second",username="second-admin")
    assert not identity.auth.preflight()["ready"]
    login_sso(identity,"first","first-admin")
    login_sso(identity,"second","second-admin")
    identity.auth.set_account_status("user-local","disabled",actor_id=first.id)
    assert identity.auth.preflight()["ready"]
    identity.auth.set_policy("binding_required",actor_id=first.id,expected_revision=1)
    return first,second


def replacement_pending(identity, user_id, subject, username, *, proof=PROOF):
    auth = identity.auth
    grant = auth.issue_grant("replace",subject,actor_id="user-local",target_user_id=user_id,ttl_seconds=600)
    state = auth.begin("replace",proof,ttl_seconds=600,grant_token=grant)
    code = auth.stage_identity(auth.claim(state,proof),ExternalIdentity("test:tenant",subject,username,"Replacement Name"),proof,ttl_seconds=600)
    return auth.inspect_completion(code,proof,session_seconds=600)


class AuthSunsetContract:
    def test_replacement_grant_accepts_only_the_fixed_namespace_and_subject(self, identity):
        user, local = identity.register_user_with_session("z00000001","pw")
        dual(identity)
        bind(identity,user,local,username="old-uid")
        auth = identity.auth
        grant = auth.issue_grant("replace","new-subject",actor_id="user-local",target_user_id=user.id,ttl_seconds=600)
        state = auth.begin("replace",PROOF,ttl_seconds=600,grant_token=grant)
        claimed = auth.claim(state,PROOF)
        with pytest.raises(AuthStoreError,match="wrong_provider"):
            auth.stage_identity(claimed,ExternalIdentity("other:tenant","new-subject","new-uid","Name"),PROOF,ttl_seconds=600)
        with pytest.raises(AuthStoreError,match="grant_identity_mismatch"):
            auth.stage_identity(claimed,ExternalIdentity("test:tenant","other-subject","new-uid","Name"),PROOF,ttl_seconds=600)
        assert auth.identities(user.id)["external_username"] == "old-uid"

    def test_replacement_retains_original_id_role_and_profile_and_rejects_old_sso(self, identity):
        user, local = identity.register_user_with_session("z00000001","pw")
        identity.set_user_role("user-local",user.id,"admin")
        identity.set_user_ui_mode(user.id,"advanced")
        dual(identity)
        _, old_sso = bind(identity,user,local,username="old-uid")
        preview = replacement_pending(identity,user.id,"replacement-subject","replacement-uid")
        assert preview["purpose"] == "replace"
        assert preview["previous_external_username"] == "old-uid"
        assert preview["target_user_id"] == user.id
        assert preview["target_username"] == "old-uid"
        replaced, fresh = identity.auth.confirm(preview["pending_id"],PROOF,session_seconds=600)
        assert (replaced.id,replaced.role,replaced.ui_mode) == (user.id,"admin","advanced")
        assert replaced.username == "replacement-uid"
        assert identity.resolve_session(old_sso) is None
        assert identity.resolve_session(fresh).id == user.id
        with identity.database.connect() as db:
            rows = identity.auth._execute(db,"SELECT subject,status FROM external_identities WHERE user_id=?",(user.id,)).fetchall()
        assert {row["subject"]:row["status"] for row in rows} == {"stable-1":"disabled","replacement-subject":"active"}
        with pytest.raises(AuthStoreError,match="account_inactive"):
            login_sso(identity,"stable-1","old-uid")
        with pytest.raises(AuthStoreError,match="invalid_transaction"):
            identity.auth.confirm(preview["pending_id"],PROOF,session_seconds=600)
        assert any(row["action"]=="grant_completed:replace" for row in identity.auth.audit_page()["items"])

    def test_only_explicit_replacement_can_reactivate_an_own_reserved_subject(self, identity):
        user, local = identity.register_user_with_session("z00000001","pw")
        dual(identity)
        bind(identity,user,local,username="old-uid")
        first = replacement_pending(identity,user.id,"second-subject","second-uid")
        _, second_session = identity.auth.confirm(first["pending_id"],PROOF,session_seconds=600)
        with pytest.raises(AuthStoreError,match="account_inactive"):
            login_sso(identity,"stable-1","old-uid")
        second = replacement_pending(identity,user.id,"stable-1","old-uid")
        restored, restored_session = identity.auth.confirm(second["pending_id"],PROOF,session_seconds=600)
        assert restored.id == user.id
        assert identity.resolve_session(second_session) is None
        assert identity.resolve_session(restored_session)
        assert len([row for row in identity.auth.inventory() if row["id"]==user.id]) == 1
        assert identity.auth.identities(user.id)["external_username"] == "old-uid"

    def test_disabled_subject_reservation_cannot_be_transferred_to_another_user(self, identity):
        first, first_local = identity.register_user_with_session("z00000001","pw")
        second, second_local = identity.register_user_with_session("z00000002","pw")
        dual(identity)
        bind(identity,first,first_local,username="first-uid")
        bind(identity,second,second_local,subject="second-subject",username="second-uid")
        preview = replacement_pending(identity,first.id,"third-subject","third-uid")
        identity.auth.confirm(preview["pending_id"],PROOF,session_seconds=600)
        with pytest.raises(AuthStoreError,match="identity_conflict"):
            identity.auth.issue_grant("replace","stable-1",actor_id="user-local",target_user_id=second.id,ttl_seconds=600)
        assert identity.auth.identities(second.id)["external_username"] == "second-uid"

    def test_replacement_name_collision_rolls_back_old_identity_and_session(self, identity):
        user, local = identity.register_user_with_session("z00000001","pw")
        identity.create_user("z00000002","pw")
        dual(identity)
        _, old_session = bind(identity,user,local,username="old-uid")
        preview = replacement_pending(identity,user.id,"new-subject","z00000002")
        with pytest.raises(AuthStoreError,match="username already exists"):
            identity.auth.confirm(preview["pending_id"],PROOF,session_seconds=600)
        assert identity.resolve_session(old_session).username == "old-uid"
        with identity.database.connect() as db:
            rows = identity.auth._execute(db,"SELECT subject,status FROM external_identities WHERE user_id=?",(user.id,)).fetchall()
        assert [(row["subject"],row["status"]) for row in rows] == [("stable-1","active")]
        assert not any(row["action"]=="grant_completed:replace" for row in identity.auth.audit_page()["items"])

    def test_concurrent_replacements_resolve_to_one_new_subject(self, identity):
        user, local = identity.register_user_with_session("z00000001","pw")
        dual(identity)
        bind(identity,user,local,username="old-uid")
        first = replacement_pending(identity,user.id,"first-new-subject","first-new-uid",proof="first-browser")
        second = replacement_pending(identity,user.id,"second-new-subject","second-new-uid",proof="second-browser")
        barrier = threading.Barrier(2)
        def complete(preview, proof):
            barrier.wait()
            try:
                return identity.auth.confirm(preview["pending_id"],proof,session_seconds=600)[1]
            except AuthStoreError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            futures = [pool.submit(complete,first,"first-browser"),pool.submit(complete,second,"second-browser")]
            results = [future.result() for future in futures]
        assert sum(result is not None for result in results) == 1
        with identity.database.connect() as db:
            rows = identity.auth._execute(db,"SELECT subject,status FROM external_identities WHERE user_id=?",(user.id,)).fetchall()
        assert len(rows) == 2
        assert sum(row["status"]=="active" for row in rows) == 1

    def test_session_projection_keeps_authority_from_one_validated_snapshot(self, identity, monkeypatch):
        user, local = identity.register_user_with_session("z00000001","pw")
        original_profile = identity.auth._profile
        changed = False
        def promote_after_validation(db, validated_user):
            nonlocal changed
            if not changed:
                changed = True
                identity.set_user_role("user-local",user.id,"admin")
            return original_profile(db,validated_user)
        monkeypatch.setattr(identity.auth,"_profile",promote_after_validation)
        # The resolver may linearize before this concurrent promotion, but it
        # cannot return newly read authority that was absent from its snapshot.
        assert identity.resolve_session(local).role == "user"
        assert identity.resolve_session(local).role == "admin"

    def test_revocation_during_throttled_touch_is_rechecked(self, identity, monkeypatch):
        user, local = identity.register_user_with_session("z00000001","pw")
        auth = identity.auth
        with auth._write() as (db, policy):
            auth._execute(db,"UPDATE auth_sessions SET last_seen_at=? WHERE token=?",(auth._expires(-3600),local))
        original_profile = auth._profile
        revoked = False
        def revoke_after_validation(db, validated_user):
            nonlocal revoked
            if not revoked:
                revoked = True
                identity.delete_session(local)
            return original_profile(db,validated_user)
        monkeypatch.setattr(auth,"_profile",revoke_after_validation)
        assert identity.resolve_session(local) is None

    def test_sso_touch_preserves_absolute_and_sliding_expiry(self, identity):
        user, local = identity.register_user_with_session("z00000001","pw")
        dual(identity)
        _, sso = bind(identity,user,local)
        auth = identity.auth
        with auth._write() as (db, policy):
            auth._execute(db,"UPDATE auth_sessions SET last_seen_at=? WHERE token=?",(auth._expires(-3600),sso))
            before = auth._execute(db,"SELECT expires_at,absolute_expires_at FROM auth_sessions WHERE token=?",(sso,)).fetchone()
        assert identity.resolve_session(sso)
        with identity.database.connect() as db:
            after = auth._execute(db,"SELECT expires_at,absolute_expires_at FROM auth_sessions WHERE token=?",(sso,)).fetchone()
        assert dict(before) == dict(after)

    def test_s2_agent_owner_remains_eligible_before_cutover(self, identity):
        user, _ = identity.register_user_with_session("z00000001","pw")
        dual(identity)
        identity.auth.set_policy("binding_required",actor_id="user-local",expected_revision=1)
        assert identity.auth.owner_eligible(user.id)

    def test_s3_activation_requires_current_namespace_mapping(self, identity):
        old_user = identity.create_user("z00000003","pw")
        identity.auth.set_account_status(old_user.id,"disabled",actor_id="user-local")
        first, _ = prepare_cutover(identity)
        identity.auth.set_policy("sso_only",actor_id=first.id,expected_revision=2)
        auth = identity.auth
        with auth._write() as (db, policy):
            auth._execute(db,"INSERT INTO external_identities(provider_namespace,subject,user_id,status,created_at,updated_at) VALUES (?,?,?,'active',?,?)",("old:namespace","old-subject",old_user.id,auth._now(),auth._now()))
        with pytest.raises(AuthStoreError,match="identity_required"):
            auth.set_account_status(old_user.id,"active",actor_id=first.id)
        assert not auth.owner_eligible(old_user.id)

    def test_identity_audit_survives_consumption_without_credentials(self, identity):
        import json
        user, local = identity.register_user_with_session("z00000001","pw")
        dual(identity)
        bind(identity,user,local)
        grant = identity.auth.issue_grant("enroll","audit-subject",actor_id="user-local",ttl_seconds=600)
        state = identity.auth.begin("enroll",PROOF,ttl_seconds=600,grant_token=grant)
        code = identity.auth.stage_identity(identity.auth.claim(state,PROOF),ExternalIdentity("test:tenant","audit-subject","audit-user","Audit User"),PROOF,ttl_seconds=600)
        preview = identity.auth.inspect_completion(code,PROOF,session_seconds=600)
        created, session = identity.auth.confirm(preview["pending_id"],PROOF,session_seconds=600)
        audit = identity.auth.audit_page(offset=0,limit=100)
        actions = {row["action"] for row in audit["items"]}
        assert {"identity_bound","grant_issued:enroll","grant_started:enroll","grant_completed:enroll"} <= actions
        grant_rows = [row for row in audit["items"] if row["grant_reference"]]
        assert len({row["grant_reference"] for row in grant_rows}) == 1
        assert next(row for row in grant_rows if row["action"]=="grant_completed:enroll")["target_user_id"] == created.id
        serialized = json.dumps(audit,default=str)
        assert all(secret not in serialized for secret in (local,grant,state,code,session,PROOF))

    def test_browser_logout_cancels_unbound_sso_flow(self, identity):
        dual(identity)
        state = identity.auth.begin("login",PROOF,ttl_seconds=600)
        identity.auth.cancel_for_browser(PROOF)
        with pytest.raises(AuthStoreError,match="invalid_transaction"):
            identity.auth.claim(state,PROOF)

    def test_provider_generation_rotation_keeps_retirement_and_existing_sso(self, identity):
        first, _ = prepare_cutover(identity)
        identity.auth.set_policy("sso_only",actor_id=first.id,expected_revision=2)
        identity.auth.set_policy("retired",actor_id=first.id,expected_revision=3)
        sso = login_sso(identity,"first","first-admin")["token"]
        state = identity.auth.begin("login",PROOF,ttl_seconds=600)
        old = identity.auth.get_policy()
        updated = identity.auth.prepare_provider_configuration(
            actor_id=first.id,expected_revision=4,plugin_id=old["plugin_id"],
            provider_id=old["provider_id"],provider_namespace=old["provider_namespace"],
            config_generation="generation-2",
        )
        assert updated["mode"] == "retired"
        assert updated["retired_at"] == old["retired_at"]
        assert updated["config_generation"] == "generation-2"
        assert identity.resolve_session(sso).id == first.id
        assert identity.login_with_password("z00000001","pw") is None
        with pytest.raises(AuthStoreError,match="invalid_transaction"):
            identity.auth.claim(state,PROOF)
        with pytest.raises(AuthStoreError,match="identity_source_change"):
            identity.auth.prepare_provider_configuration(
                actor_id=first.id,expected_revision=5,plugin_id=old["plugin_id"],
                provider_id=old["provider_id"],provider_namespace="different-tenant",
                config_generation="generation-3",
            )

    def test_s3_cutover_and_controlled_rollback_never_revive_old_session(self, identity):
        first, _ = prepare_cutover(identity)
        _, local = identity.login_with_password("z00000001","pw")
        identity.auth.set_policy("sso_only",actor_id=first.id,expected_revision=2)
        assert identity.auth.resolve_session(local,migration_only=True) is None
        assert identity.login_with_password("z00000001","pw") is None
        with pytest.raises(AuthStoreError,match="invalid_transition"):
            identity.auth.set_policy("dual",actor_id=first.id,expected_revision=3)
        identity.auth.set_policy("dual",actor_id=first.id,expected_revision=3,allow_rollback=True)
        assert identity.resolve_session(local) is None
        assert identity.login_with_password("z00000001","pw")[0].username == "first-admin"

    def test_retirement_is_irreversible_and_cleanup_is_idempotent(self, identity):
        first, _ = prepare_cutover(identity)
        identity.auth.set_policy("sso_only",actor_id=first.id,expected_revision=2)
        identity.auth.set_policy("retired",actor_id=first.id,expected_revision=3)
        identity.auth.retirement_cleanup()
        with identity.database.connect() as db:
            rows = identity.auth._execute(db,"SELECT password_hash,password_salt,password_iterations,local_login_name FROM users").fetchall()
        assert all(row["password_hash"]=="" and row["password_salt"]=="" and row["password_iterations"]==0 and row["local_login_name"] is None for row in rows)
        for mode in ("dual","binding_required","sso_only"):
            with pytest.raises(AuthStoreError,match="irreversible_policy"):
                identity.auth.set_policy(mode,actor_id=first.id,expected_revision=4,allow_rollback=True)
        assert login_sso(identity,"first","first-admin")["user"].id == first.id

    def test_policy_change_invalidates_inflight_confirmation(self, identity):
        user, local = identity.register_user_with_session("z00000001","pw")
        dual(identity)
        token = pending(identity,user,local)
        identity.auth.set_policy("binding_required",actor_id="user-local",expected_revision=1)
        with pytest.raises(AuthStoreError,match="invalid_transaction"):
            identity.auth.confirm(token,PROOF,session_token=local,session_seconds=600)
        assert not identity.auth.identities(user.id)["linked"]

    def test_concurrent_reset_and_bind_cannot_escape_revocation(self, identity):
        user, local = identity.register_user_with_session("z00000001","pw")
        dual(identity)
        token = pending(identity,user,local)
        barrier = threading.Barrier(2)
        def complete():
            barrier.wait()
            try:
                return identity.auth.confirm(token,PROOF,session_token=local,session_seconds=600)[1]
            except AuthStoreError:
                return None
        def reset():
            barrier.wait()
            identity.admin_reset_user_password("user-local",user.id,"changed")
        with ThreadPoolExecutor(max_workers=2) as pool:
            login_future = pool.submit(complete)
            reset_future = pool.submit(reset)
            sso = login_future.result()
            reset_future.result()
        assert not sso or identity.resolve_session(sso) is None
        assert identity.login_with_password("z00000001","pw") is None

    def test_recovery_grant_preserves_old_id_and_requires_bound_subject(self, identity):
        user, _ = identity.register_user_with_session("z00000001","pw")
        dual(identity)
        identity.auth.set_account_status(user.id,"disabled",actor_id="user-local")
        grant = identity.auth.issue_grant("recover","restored-subject",actor_id="user-local",target_user_id=user.id,ttl_seconds=600)
        state = identity.auth.begin("recover",PROOF,ttl_seconds=600,grant_token=grant)
        claimed = identity.auth.claim(state,PROOF)
        with pytest.raises(AuthStoreError,match="grant_identity_mismatch"):
            identity.auth.stage_identity(claimed,ExternalIdentity("test:tenant","wrong-subject","restored-user","Restored"),PROOF,ttl_seconds=600)
        code = identity.auth.stage_identity(claimed,ExternalIdentity("test:tenant","restored-subject","restored-user","Restored"),PROOF,ttl_seconds=600)
        preview = identity.auth.inspect_completion(code,PROOF,session_seconds=600)
        assert preview["target_user_id"] == user.id
        assert preview["target_username"] == user.username
        restored, token = identity.auth.confirm(preview["pending_id"],PROOF,session_seconds=600)
        assert restored.id == user.id
        assert identity.resolve_session(token).id == user.id

    def test_binding_preserves_owner_and_local_login_name(self, identity):
        user, local = identity.register_user_with_session("z00000001","pw")
        dual(identity)
        renamed, sso = bind(identity,user,local)
        assert renamed.id == user.id
        assert renamed.username == "CorpUID"
        assert identity.resolve_session(local) is None
        assert identity.resolve_session(sso).id == user.id
        assert identity.login_with_password("z00000001","pw")[0].id == user.id
        assert identity.login_with_password("CorpUID","pw") is None
        assert identity.auth.identities(user.id)["local_login_name"] == "z00000001"

    def test_claim_proof_and_replay_are_rejected(self, identity):
        dual(identity)
        state = identity.auth.begin("login",PROOF,ttl_seconds=600)
        with pytest.raises(AuthStoreError,match="invalid_transaction"):
            identity.auth.claim(state,"another-browser")
        identity.auth.claim(state,PROOF)
        with pytest.raises(AuthStoreError,match="invalid_transaction"):
            identity.auth.claim(state,PROOF)

    def test_collision_reserves_old_credential_name_after_rename(self, identity):
        first, first_token = identity.register_user_with_session("z00000001","pw")
        second, second_token = identity.register_user_with_session("z00000002","pw")
        dual(identity)
        bind(identity,first,first_token,username="external-first")
        token = pending(identity,second,second_token,subject="stable-2",username="z00000001")
        with pytest.raises(AuthStoreError,match="username already exists"):
            identity.auth.confirm(token,PROOF,session_token=second_token,session_seconds=600)
        assert not identity.auth.identities(second.id)["linked"]
        assert identity.resolve_session(second_token).username == "z00000002"
        with pytest.raises(ValueError,match="exists"):
            identity.create_user("z00000001","pw")

    def test_reset_even_with_kept_session_invalidates_binding_proof(self, identity):
        user, local = identity.register_user_with_session("z00000001","pw")
        dual(identity)
        token = pending(identity,user,local)
        identity.change_user_password(user.id,"pw","new-password",keep_token=local)
        assert identity.resolve_session(local)
        with pytest.raises(AuthStoreError,match="stale_local_proof"):
            identity.auth.confirm(token,PROOF,session_token=local,session_seconds=600)
        assert not identity.auth.identities(user.id)["linked"]

    def test_s2_password_sessions_are_only_migration_credentials(self, identity):
        user, local = identity.register_user_with_session("z00000001","pw")
        dual(identity)
        identity.auth.set_policy("binding_required",actor_id="user-local",expected_revision=1)
        assert identity.resolve_session(local) is None
        assert identity.auth.resolve_session(local,migration_only=True).id == user.id
        _, restricted = identity.login_with_password("z00000001","pw")
        assert identity.resolve_session(restricted) is None
        assert identity.auth.resolve_session(restricted,migration_only=True)
        with pytest.raises(AuthStoreError,match="local_auth_disabled"):
            identity.create_user("z00000002","pw")
        with pytest.raises(AuthStoreError,match="local_auth_disabled"):
            identity.admin_reset_user_password("user-local",user.id,"new")
        _, sso = bind(identity,user,restricted)
        assert identity.resolve_session(sso)

    def test_unknown_external_identity_never_matches_local_name(self, identity):
        user, _ = identity.register_user_with_session("z00000001","pw")
        dual(identity)
        state = identity.auth.begin("login",PROOF,ttl_seconds=600)
        code = identity.auth.stage_identity(identity.auth.claim(state,PROOF),ExternalIdentity("test:tenant","unbound",user.username,"Name"),PROOF,ttl_seconds=600)
        with pytest.raises(AuthStoreError,match="identity_not_linked"):
            identity.auth.inspect_completion(code,PROOF,session_seconds=600)
        assert not identity.auth.identities(user.id)["linked"]

    def test_concurrent_confirmation_commits_one_identity_and_session(self, identity):
        user, local = identity.register_user_with_session("z00000001","pw")
        dual(identity)
        token = pending(identity,user,local)
        barrier = threading.Barrier(2)
        def confirm():
            barrier.wait()
            try:
                return identity.auth.confirm(token,PROOF,session_token=local,session_seconds=600)[1]
            except AuthStoreError:
                return None
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _:confirm(),range(2)))
        assert len([result for result in results if result]) == 1
        assert identity.auth.identities(user.id)["linked"]

    def test_logout_between_callback_and_confirmation_rejects(self, identity):
        user, local = identity.register_user_with_session("z00000001","pw")
        dual(identity)
        token = pending(identity,user,local)
        identity.delete_session(local)
        with pytest.raises(AuthStoreError):
            identity.auth.confirm(token,PROOF,session_token=local,session_seconds=600)

    def test_sso_absolute_expiry_survives_sliding_session_expiry(self, identity):
        user, local = identity.register_user_with_session("z00000001","pw")
        dual(identity)
        _, sso = bind(identity,user,local)
        auth = identity.auth
        with auth._write() as (db, policy):
            auth._execute(db,"UPDATE auth_sessions SET absolute_expires_at=? WHERE token=?",(auth._expires(-1),sso))
        assert identity.resolve_session(sso) is None

    def test_external_rename_uses_subject_and_preserves_old_local_name(self, identity):
        user, local = identity.register_user_with_session("z00000001","pw")
        dual(identity)
        bind(identity,user,local)
        state = identity.auth.begin("login",PROOF,ttl_seconds=600)
        code = identity.auth.stage_identity(identity.auth.claim(state,PROOF),ExternalIdentity("test:tenant","stable-1","NewUID","Updated Name"),PROOF,ttl_seconds=600)
        result = identity.auth.inspect_completion(code,PROOF,session_seconds=600)
        assert result["user"].id == user.id
        assert result["user"].username == "NewUID"
        assert result["user"].display_name == "Updated Name"
        assert identity.authenticate_user("z00000001","pw").id == user.id

    def test_administrator_grant_creates_passwordless_user_only_after_confirmation(self, identity):
        dual(identity)
        grant = identity.auth.issue_grant("enroll","new-subject",actor_id="user-local",ttl_seconds=600)
        state = identity.auth.begin("enroll",PROOF,ttl_seconds=600,grant_token=grant)
        code = identity.auth.stage_identity(identity.auth.claim(state,PROOF),ExternalIdentity("test:tenant","new-subject","new-user","New User"),PROOF,ttl_seconds=600)
        result = identity.auth.inspect_completion(code,PROOF,session_seconds=600)
        assert result["status"] == "confirmation_required"
        user, sso = identity.auth.confirm(result["pending_id"],PROOF,session_seconds=600)
        assert user.role == "user"
        assert identity.resolve_session(sso).id == user.id
        assert identity.auth.identities(user.id)["local_login_name"] is None
