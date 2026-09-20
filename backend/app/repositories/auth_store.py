"""Atomic authentication persistence shared by the two SQL adapters.

Every authentication write locks the singleton policy before users and sessions.
The PostgreSQL adapter converts placeholders; SQLite takes BEGIN IMMEDIATE.
No provider/network operation belongs in this transaction boundary.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import secrets
import time

from app.domain.auth_utils import verify_password
from app.domain.auth_policy import (
    AUTH_MODES, AuthStoreError, AUTH_CUTOVER_MIN_ADMINS,
    AUTH_INVENTORY_PAGE_SIZE, AUTH_INVENTORY_PAGE_MAX,
)
from app.domain.auth_provider import (
    AUTH_PROVIDER_SUBJECT_MAX_CHARS, AUTH_PROVIDER_USERNAME_MAX_CHARS,
    AUTH_PROVIDER_DISPLAY_NAME_MAX_CHARS, AUTH_PROVIDER_NAMESPACE_MAX_CHARS,
)
from app.repositories.auth_ports import AuthGrantPreview

AUTH_TOKEN_BYTES = 32
LOCAL_SESSION_SECONDS = 30 * 24 * 60 * 60
_INITIAL_POLICY = dict(id=1,mode="local",revision=0,provider_id="",provider_namespace="",config_generation="",plugin_id="",retired_at=None,updated_by="")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


class AuthStore:
    def __init__(self, database, settings, identity, *, postgres=False):
        self.database = database
        self.settings = settings
        self.identity = identity
        self.postgres = postgres

    def _execute(self, db, sql, params=()):
        return db.execute(sql.replace("?", "%s") if self.postgres else sql, params)

    def _now(self):
        return datetime.now(timezone.utc) if self.postgres else datetime.now().replace(microsecond=0).isoformat()

    def _expires(self, seconds):
        value = datetime.now(timezone.utc) if self.postgres else datetime.now()
        value += timedelta(seconds=seconds)
        return value if self.postgres else value.replace(microsecond=0).isoformat()

    def lock(self, db):
        if not self.postgres and not db.in_transaction:
            self.database.begin_immediate(db)
        self._execute(db,"INSERT INTO auth_policy(id) VALUES(1) ON CONFLICT(id) DO NOTHING")
        sql = "SELECT * FROM auth_policy WHERE id=1" + (" FOR UPDATE" if self.postgres else "")
        return dict(self._execute(db, sql).fetchone())

    def _read_policy(self, db):
        row = self._execute(db,"SELECT * FROM auth_policy WHERE id=1").fetchone()
        return dict(row) if row else dict(_INITIAL_POLICY)

    @contextmanager
    def _write(self):
        with self.database.write() as db:
            yield db, self.lock(db)

    def get_policy(self):
        with self.database.connect() as db:
            return self._read_policy(db)

    def require_local_write(self, db):
        policy = self.lock(db)
        if policy["mode"] not in ("local", "dual") or policy["retired_at"]:
            raise AuthStoreError("local_auth_disabled")
        return policy

    def local_login_allowed(self, db):
        return self.lock(db)["mode"] in ("local", "dual", "binding_required")

    def _user(self, db, user_id):
        return self._execute(db, "SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

    def _profile(self, db, user):
        row = self._execute(db, "SELECT * FROM user_profiles WHERE user_id=?", (user["id"],)).fetchone()
        return self.identity._user_profile(user, row)

    def owner_eligible(self, user_id):
        with self.database.connect() as db:
            return self._execute(
                db,
                "SELECT 1 FROM users u LEFT JOIN auth_policy p ON p.id=1 "
                "LEFT JOIN external_identities e ON e.user_id=u.id "
                "AND e.provider_namespace=p.provider_namespace AND e.status='active' "
                "WHERE u.id=? AND u.status='active' "
                "AND (coalesce(p.mode,'local') IN ('local','dual','binding_required') "
                "OR (u.id<>'user-local' AND e.user_id IS NOT NULL))",
                (user_id,),
            ).fetchone() is not None

    def _session(self, db, token, policy, *, migration_only=False):
        # This one statement is the authorization linearization point. In
        # particular, never re-read a user after returning a boolean decision.
        # The policy argument is retained for callers already holding its lock.
        del policy
        now = self._now()
        return self._execute(
            db,
            "SELECT u.*,s.last_seen_at AS session_last_seen_at,"
            "s.expires_at AS session_expires_at,s.auth_source,"
            "s.absolute_expires_at AS session_absolute_expires_at "
            "FROM auth_sessions s JOIN users u ON u.id=s.user_id "
            "LEFT JOIN auth_policy p ON p.id=1 "
            "LEFT JOIN external_identities e ON e.user_id=u.id "
            "AND e.provider_namespace=s.provider_namespace "
            "AND e.subject=s.external_subject AND e.status='active' "
            "WHERE s.token=? AND u.status='active' AND s.expires_at>? "
            "AND (s.absolute_expires_at IS NULL OR s.absolute_expires_at>?) "
            "AND ((s.auth_source='local' AND p.retired_at IS NULL "
            "AND (coalesce(p.mode,'local') IN ('local','dual') "
            "OR (?=1 AND p.mode='binding_required'))) "
            "OR (s.auth_source='sso' AND e.user_id IS NOT NULL "
            "AND u.id<>'user-local' AND p.mode<>'local' "
            "AND s.provider_namespace=p.provider_namespace))",
            (token,now,now,int(migration_only)),
        ).fetchone()

    def resolve_session(self, token, *, migration_only=False):
        if not token:
            return None
        with self.database.connect() as db:
            user = self._session(db, token, None, migration_only=migration_only)
            profile = self._profile(db, user) if user else None
        if user is None:
            # Expired session cleanup cannot turn a rejected read into an
            # authorization decision, and never deletes a concurrently renewed
            # live session because the expiry predicate is checked at deletion.
            with self.database.write() as db:
                now = self._now()
                self._execute(db,"DELETE FROM auth_sessions WHERE token=? AND (expires_at<=? OR absolute_expires_at<=?)",(token,now,now))
            return None
        touch_before = self._expires(-max(1,self.settings.auth_session_touch_interval_seconds))
        if user["session_last_seen_at"] <= touch_before:
            with self._write() as (db, policy):
                fresh = self._session(db,token,policy,migration_only=migration_only)
                if fresh is None:
                    return None
                now = self._now()
                self._execute(
                    db,"UPDATE users SET last_seen_at=? WHERE id=? AND (last_seen_at IS NULL OR last_seen_at<?)",
                    (now,fresh["id"],now),
                )
                # SSO expires at the original external-authentication deadline.
                expires = fresh["session_expires_at"] if fresh["auth_source"]=="sso" else self._expires(LOCAL_SESSION_SECONDS)
                self._execute(
                    db,"UPDATE auth_sessions SET last_seen_at=?,expires_at=? "
                    "WHERE token=? AND last_seen_at=? AND expires_at>?",
                    (now,expires,token,fresh["session_last_seen_at"],now),
                )
                profile = self._profile(db,fresh)
        return profile

    def insert_local_session(self, db, user_id):
        policy = self.lock(db)
        user = self._user(db, user_id)
        if policy["mode"] not in ("local", "dual", "binding_required") or not user or user["status"] != "active":
            raise AuthStoreError("local_auth_disabled")
        return self._insert_session(db, user_id, "local", LOCAL_SESSION_SECONDS)

    def _insert_session(self, db, user_id, source, seconds, namespace="", subject=""):
        token = secrets.token_urlsafe(AUTH_TOKEN_BYTES)
        now, expires = self._now(), self._expires(seconds)
        absolute = expires if source == "sso" else None
        self._execute(db, "INSERT INTO auth_sessions(token,user_id,created_at,expires_at,last_seen_at,auth_source,absolute_expires_at,provider_namespace,external_subject) VALUES (?,?,?,?,?,?,?,?,?)", (token,user_id,now,expires,now,source,absolute,namespace,subject))
        self._execute(db, "UPDATE users SET last_seen_at=? WHERE id=? AND (last_seen_at IS NULL OR last_seen_at<?)", (now,user_id,now))
        return token

    def check_name(self, db, username, user_id=""):
        row = self._execute(db, "SELECT id FROM users WHERE id<>? AND (lower(username)=lower(?) OR lower(local_login_name)=lower(?))", (user_id, username, username)).fetchone()
        if row:
            raise AuthStoreError("username already exists")

    def _put_transaction(self, db, purpose, proof, payload, ttl_seconds):
        if ttl_seconds <= 0 or not proof:
            raise AuthStoreError("invalid_transaction")
        self._execute(db,"DELETE FROM auth_transactions WHERE expires_at<=?",(int(time.time()),))
        token = secrets.token_urlsafe(AUTH_TOKEN_BYTES)
        self._execute(db, "INSERT INTO auth_transactions(token_digest,purpose,browser_digest,payload,expires_at) VALUES (?,?,?,?,?)", (digest(token),purpose,digest(proof),json.dumps(payload),int(time.time())+ttl_seconds))
        return token

    def _take(self, db, token, proof, purposes):
        row = self._execute(db, "SELECT * FROM auth_transactions WHERE token_digest=?", (digest(token),)).fetchone()
        if not row or row["purpose"] not in purposes or row["expires_at"] <= time.time() or not secrets.compare_digest(row["browser_digest"],digest(proof)):
            raise AuthStoreError("invalid_transaction")
        self._execute(db,"DELETE FROM auth_transactions WHERE token_digest=?",(digest(token),))
        return json.loads(row["payload"])

    def _validate_transaction(self, db, payload, policy):
        if payload["policy_revision"] != policy["revision"] or payload["config_generation"] != policy["config_generation"] or policy["mode"] == "local":
            raise AuthStoreError("stale_transaction")
        if payload["purpose"] == "login" and "identity" in payload:
            # A staged provider result cannot regain authority after a password
            # reset or identity reassignment. Keep this separate from bind's
            # target_user_id, which requires proof of the original local session.
            if "identity_user_id" not in payload or "identity_auth_revision" not in payload:
                raise AuthStoreError("stale_transaction")
            if payload["identity_user_id"] is None:
                raise AuthStoreError("identity_not_linked")
            values = payload["identity"]
            mapping = self._execute(
                db,
                "SELECT user_id,status FROM external_identities "
                "WHERE provider_namespace=? AND subject=?",
                (values["provider_namespace"],values["subject"]),
            ).fetchone()
            user = self._user(db,payload["identity_user_id"])
            if not mapping or mapping["user_id"] != payload["identity_user_id"] or not user or user["auth_revision"] != payload["identity_auth_revision"]:
                raise AuthStoreError("stale_transaction")
            if mapping["status"] != "active" or user["status"] != "active":
                raise AuthStoreError("account_inactive")
        if payload["purpose"] in ("enroll","recover","replace"):
            actor = self._user(db,payload["grant_actor_id"])
            if not actor or actor["role"] != "admin" or actor["status"] != "active" or actor["auth_revision"] != payload["grant_actor_revision"]:
                raise AuthStoreError("grant_revoked")
            if payload["purpose"] in ("recover","replace"):
                target = self._user(db,payload["target_user_id"])
                if not target or target["auth_revision"] != payload["target_auth_revision"]:
                    raise AuthStoreError("grant_revoked")
                if payload["purpose"] == "replace":
                    current = self._active_identity(db, target["id"], policy["provider_namespace"])
                    if target["status"] != "active" or not current or current["subject"] != payload["previous_subject"]:
                        raise AuthStoreError("grant_revoked")
        if payload["purpose"] == "bind":
            if policy["mode"] not in ("dual","binding_required"):
                raise AuthStoreError("binding_disabled")
            user = self._session(db,payload["session_token"],policy,migration_only=True)
            if not user or user["id"] != payload["target_user_id"] or user["auth_revision"] != payload["auth_revision"]:
                raise AuthStoreError("stale_local_proof")

    def issue_grant(self, purpose, subject, *, actor_id, ttl_seconds, target_user_id=""):
        if purpose not in ("enroll","recover","replace") or not isinstance(subject,str) or not subject.strip():
            raise AuthStoreError("invalid_grant")
        if len(subject) > AUTH_PROVIDER_SUBJECT_MAX_CHARS or any(ord(c)<32 or ord(c)==127 for c in subject):
            raise AuthStoreError("invalid_grant")
        with self._write() as (db, policy):
            actor = self._user(db,actor_id)
            if policy["mode"] == "local" or not actor or actor["role"] != "admin" or actor["status"] != "active":
                raise AuthStoreError("admin_required")
            if purpose == "enroll" and target_user_id:
                raise AuthStoreError("invalid_grant")
            if purpose == "recover":
                user = self._user(db,target_user_id)
                existing = self._execute(db,"SELECT 1 FROM external_identities WHERE user_id=?",(target_user_id,)).fetchone()
                if not user or target_user_id == "user-local" or existing:
                    raise AuthStoreError("invalid_recovery_target")
            if purpose == "replace":
                user = self._user(db,target_user_id)
                current = self._active_identity(db,target_user_id,policy["provider_namespace"])
                reserved = self._execute(
                    db,
                    "SELECT user_id FROM external_identities WHERE provider_namespace=? AND subject=?",
                    (policy["provider_namespace"],subject),
                ).fetchone()
                if not user or user["status"] != "active" or target_user_id == "user-local" or not current or current["subject"] == subject:
                    raise AuthStoreError("invalid_replacement_target")
                if reserved and reserved["user_id"] != target_user_id:
                    raise AuthStoreError("identity_conflict")
            payload = {"purpose":purpose,"subject":subject,"target_user_id":target_user_id,"grant_actor_id":actor_id,"grant_actor_revision":actor["auth_revision"],"policy_revision":policy["revision"],"config_generation":policy["config_generation"],"provider_namespace":policy["provider_namespace"]}
            if purpose in ("recover","replace"):
                payload["target_auth_revision"] = user["auth_revision"]
            if purpose == "replace":
                payload["previous_subject"] = current["subject"]
                payload["previous_external_username"] = user["username"]
            payload["grant_reference"] = secrets.token_hex(AUTH_TOKEN_BYTES)
            self._audit_identity(
                db,actor_id,target_user_id,"grant_issued:"+purpose,
                policy["provider_namespace"],subject,payload["grant_reference"],
            )
            return self._put_transaction(db,"grant","admin-grant",payload,ttl_seconds)

    def begin(self, purpose, browser_proof, *, ttl_seconds, session_token="", password="", pkce_verifier="", grant_token=""):
        if purpose not in ("login","bind","enroll","recover","replace"):
            raise AuthStoreError("invalid_purpose")
        with self._write() as (db, policy):
            if policy["mode"] == "local":
                raise AuthStoreError("sso_disabled")
            payload = {"purpose":purpose,"policy_revision":policy["revision"],"config_generation":policy["config_generation"],"provider_namespace":policy["provider_namespace"],"provider_id":policy["provider_id"],"pkce_verifier":pkce_verifier}
            if purpose in ("enroll","recover","replace"):
                grant = self._take(db,grant_token,"admin-grant",("grant",))
                if grant["purpose"] != purpose:
                    raise AuthStoreError("invalid_grant")
                self._validate_transaction(db,grant,policy)
                payload.update(grant)
                self._audit_identity(
                    db,grant["grant_actor_id"],grant["target_user_id"],
                    "grant_started:"+purpose,grant["provider_namespace"],
                    grant["subject"],grant["grant_reference"],
                )
            if purpose == "bind":
                user = self._session(db,session_token,policy,migration_only=True)
                if policy["mode"] not in ("dual","binding_required") or not user or user["id"] == "user-local" or not verify_password(password,user["password_hash"],user["password_salt"],user["password_iterations"]):
                    raise AuthStoreError("local_verification_failed")
                payload.update(target_user_id=user["id"],session_token=session_token,auth_revision=user["auth_revision"])
            return self._put_transaction(db,purpose,browser_proof,payload,ttl_seconds)

    def claim(self, state, browser_proof):
        with self._write() as (db, policy):
            payload = self._take(db,state,browser_proof,("login","bind","enroll","recover","replace"))
            self._validate_transaction(db,payload,policy)
            # An unguessable server-only claim prevents replay of a claimed dict.
            payload["claim"] = self._put_transaction(db,"claim",browser_proof,payload,max(1,int(getattr(self.settings,"auth_transaction_ttl_seconds",600))))
            return payload

    def stage_identity(self, transaction, identity, browser_proof, *, ttl_seconds):
        values = {key:(identity[key] if isinstance(identity,dict) else getattr(identity,key)) for key in ("provider_namespace","subject","username","display_name")}
        if any(not isinstance(value,str) or not value.strip() for value in values.values()):
            raise AuthStoreError("invalid_identity")
        rails = {"provider_namespace":AUTH_PROVIDER_NAMESPACE_MAX_CHARS,"subject":AUTH_PROVIDER_SUBJECT_MAX_CHARS,"username":AUTH_PROVIDER_USERNAME_MAX_CHARS,"display_name":AUTH_PROVIDER_DISPLAY_NAME_MAX_CHARS}
        if any(len(values[key]) > limit or any(ord(c)<32 or ord(c)==127 for c in values[key]) for key,limit in rails.items()):
            raise AuthStoreError("invalid_identity")
        with self._write() as (db, policy):
            payload = self._take(db,transaction["claim"],browser_proof,("claim",))
            self._validate_transaction(db,payload,policy)
            if values["provider_namespace"] != policy["provider_namespace"]:
                raise AuthStoreError("wrong_provider")
            if payload["purpose"] in ("enroll","recover","replace") and values["subject"] != payload["subject"]:
                raise AuthStoreError("grant_identity_mismatch")
            if payload["purpose"] == "login":
                mapping = self._execute(
                    db,
                    "SELECT user_id FROM external_identities "
                    "WHERE provider_namespace=? AND subject=?",
                    (values["provider_namespace"],values["subject"]),
                ).fetchone()
                user = self._user(db,mapping["user_id"]) if mapping else None
                payload["identity_user_id"] = user["id"] if user else None
                payload["identity_auth_revision"] = user["auth_revision"] if user else None
            payload.pop("pkce_verifier",None)
            payload["identity"] = values
            payload["authenticated_at"] = int(time.time())
            return self._put_transaction(db,"handoff",browser_proof,payload,ttl_seconds)

    def inspect_completion(self, token, browser_proof, *, session_seconds):
        with self._write() as (db, policy):
            payload = self._take(db,token,browser_proof,("handoff",))
            self._validate_transaction(db,payload,policy)
            if payload["purpose"] == "bind":
                pending = self._put_transaction(db,"confirmation",browser_proof,payload,self.settings.auth_transaction_ttl_seconds)
                user = self._user(db,payload["target_user_id"])
                return {"status":"binding_required","pending_id":pending,"local_login_name":user["local_login_name"],"external_username":payload["identity"]["username"],"display_name":payload["identity"]["display_name"]}
            if payload["purpose"] in ("enroll","recover","replace"):
                pending = self._put_transaction(db,"confirmation",browser_proof,payload,self.settings.auth_transaction_ttl_seconds)
                target = self._user(db,payload["target_user_id"]) if payload.get("target_user_id") else None
                preview: AuthGrantPreview = {
                    "status":"confirmation_required", "pending_id":pending,
                    "purpose":payload["purpose"],
                    "external_username":payload["identity"]["username"],
                    "display_name":payload["identity"]["display_name"],
                    "previous_external_username":payload.get("previous_external_username"),
                    "target_user_id":target["id"] if target else None,
                    "target_username":target["username"] if target else None,
                }
                return preview
            user, session = self._complete(db,policy,payload,"",session_seconds)
            return {"status":"authenticated","user":user,"token":session}

    def confirm(self, token, browser_proof, *, session_token="", session_seconds):
        with self._write() as (db, policy):
            payload = self._take(db,token,browser_proof,("confirmation",))
            self._validate_transaction(db,payload,policy)
            return self._complete(db,policy,payload,session_token,session_seconds)

    def _complete(self, db, policy, payload, session_token, session_seconds):
        remaining = payload["authenticated_at"] + session_seconds - int(time.time())
        if remaining <= 0:
            raise AuthStoreError("external_auth_expired")
        values = payload["identity"]
        mapping = self._execute(db,"SELECT * FROM external_identities WHERE provider_namespace=? AND subject=?",(values["provider_namespace"],values["subject"])).fetchone()
        target = payload.get("target_user_id")
        if payload["purpose"] == "enroll":
            if mapping:
                raise AuthStoreError("identity_conflict")
            self.check_name(db,values["username"])
            target = "user-" + secrets.token_hex(AUTH_TOKEN_BYTES)
            now = self._now()
            self._execute(db,"INSERT INTO users(id,email,display_name,role,status,username,created_at,updated_at) VALUES (?,?,?,'user','active',?,?,?)",(target,target+"@users.silicon-notebook.local",values["display_name"],values["username"],now,now))
            self._execute(db,"INSERT INTO user_profiles(id,user_id,memory_mode,domain_focus,created_at,updated_at) VALUES (?,?,'manual',?,?,?)",("profile-"+target,target,"[]",now,now))
        elif payload["purpose"] == "recover":
            if mapping or self._execute(db,"SELECT 1 FROM external_identities WHERE user_id=?",(target,)).fetchone():
                raise AuthStoreError("identity_conflict")
            self._execute(db,"UPDATE users SET status='active',auth_revision=auth_revision+1 WHERE id=?",(target,))
            self._execute(db,"DELETE FROM auth_sessions WHERE user_id=?",(target,))
        elif payload["purpose"] == "replace":
            mapping = self._replace_identity(db, policy, payload, mapping)
        elif target:
            if not secrets.compare_digest(session_token,payload["session_token"]):
                raise AuthStoreError("stale_local_proof")
            existing = self._active_identity(db,target,values["provider_namespace"])
            historical = self._execute(db,"SELECT 1 FROM external_identities WHERE user_id=?",(target,)).fetchone()
            if (mapping and mapping["user_id"] != target) or (historical and not existing) or (existing and existing["subject"] != values["subject"]):
                raise AuthStoreError("identity_conflict")
        elif not mapping:
            raise AuthStoreError("identity_not_linked")
        user_id = target or mapping["user_id"]
        user = self._user(db,user_id)
        if not user or user["status"] != "active" or (mapping and mapping["status"] != "active"):
            raise AuthStoreError("account_inactive")
        self.check_name(db,values["username"],user_id)
        if not mapping:
            self._execute(db,"INSERT INTO external_identities(provider_namespace,subject,user_id,status,created_at,updated_at,last_login_at) VALUES (?,?,?,'active',?,?,?)",(values["provider_namespace"],values["subject"],user_id,self._now(),self._now(),self._now() if payload["purpose"]=="login" else None))
        else:
            self._execute(db,"UPDATE external_identities SET updated_at=? WHERE provider_namespace=? AND subject=?",(self._now(),values["provider_namespace"],values["subject"]))
            if payload["purpose"] == "login":
                self._execute(db,"UPDATE external_identities SET last_login_at=? WHERE provider_namespace=? AND subject=?",(self._now(),values["provider_namespace"],values["subject"]))
        self._execute(db,"UPDATE users SET username=?,display_name=?,updated_at=? WHERE id=?",(values["username"],values["display_name"],self._now(),user_id))
        if payload["purpose"] in ("enroll","recover","replace"):
            self._audit_identity(
                db,payload["grant_actor_id"],user_id,"grant_completed:"+payload["purpose"],
                values["provider_namespace"],values["subject"],payload["grant_reference"],
            )
        elif payload["purpose"] == "bind" or user["username"] != values["username"]:
            action = "identity_bound" if payload["purpose"]=="bind" else "identity_renamed"
            self._audit_identity(db,user_id,user_id,action,values["provider_namespace"],values["subject"])
        if payload["purpose"] == "bind":
            self._execute(db,"DELETE FROM auth_sessions WHERE token=?",(session_token,))
        new_token = self._insert_session(db,user_id,"sso",remaining,values["provider_namespace"],values["subject"])
        return self._profile(db,self._user(db,user_id)),new_token

    def _active_identity(self, db, user_id, namespace):
        return self._execute(
            db,
            "SELECT * FROM external_identities "
            "WHERE user_id=? AND provider_namespace=? AND status='active'",
            (user_id,namespace),
        ).fetchone()

    def _replace_identity(self, db, policy, payload, mapping):
        """Replace a grant-fixed mapping while retaining old subject ownership."""
        user_id = payload["target_user_id"]
        namespace = policy["provider_namespace"]
        if mapping and mapping["user_id"] != user_id:
            raise AuthStoreError("identity_conflict")
        self._execute(
            db,
            "UPDATE external_identities SET status='disabled',updated_at=? "
            "WHERE user_id=? AND provider_namespace=? AND status='active'",
            (self._now(),user_id,namespace),
        )
        if mapping:
            self._execute(
                db,
                "UPDATE external_identities SET status='active',updated_at=?,last_login_at=NULL "
                "WHERE provider_namespace=? AND subject=?",
                (self._now(),namespace,payload["subject"]),
            )
            mapping = dict(mapping)
            mapping["status"] = "active"
        self._execute(
            db, "UPDATE users SET auth_revision=auth_revision+1 WHERE id=?", (user_id,)
        )
        self._execute(
            db,
            "DELETE FROM auth_sessions WHERE user_id=? AND auth_source='sso' AND provider_namespace=?",
            (user_id,namespace),
        )
        return mapping

    def cancel(self, pending_id, browser_proof, *, session_token):
        with self._write() as (db, policy):
            payload = self._take(db,pending_id,browser_proof,("confirmation",))
            if payload["purpose"] == "bind" and not secrets.compare_digest(payload.get("session_token",""),session_token):
                raise AuthStoreError("stale_local_proof")

    def cancel_for_session(self, session_token):
        # Invalid session references already fail on every claim/commit. Remove
        # their pending material as well, without relying on JSON SQL dialects.
        with self._write() as (db, policy):
            rows = self._execute(db,"SELECT token_digest,payload FROM auth_transactions").fetchall()
            for row in rows:
                if json.loads(row["payload"]).get("session_token") == session_token:
                    self._execute(db,"DELETE FROM auth_transactions WHERE token_digest=?",(row["token_digest"],))

    def cancel_for_browser(self, browser_proof):
        if not browser_proof:
            return
        with self._write() as (db, policy):
            self._execute(db,"DELETE FROM auth_transactions WHERE browser_digest=?",(digest(browser_proof),))

    def prepare_provider_configuration(
        self, *, expected_revision, actor_id, plugin_id, provider_id,
        provider_namespace, config_generation,
    ):
        with self._write() as (db, policy):
            actor = self._user(db,actor_id)
            if not actor or actor["status"] != "active" or actor["role"] != "admin":
                raise AuthStoreError("admin_required")
            if expected_revision != policy["revision"]:
                raise AuthStoreError("stale_policy")
            if policy["mode"] == "local" or not config_generation:
                raise AuthStoreError("invalid_transition")
            if (plugin_id,provider_id,provider_namespace) != (policy["plugin_id"],policy["provider_id"],policy["provider_namespace"]):
                raise AuthStoreError("identity_source_change_requires_migration")
            self._execute(db,"UPDATE auth_policy SET config_generation=?,revision=revision+1,updated_by=? WHERE id=1",(config_generation,actor_id))
            self._execute(db,"DELETE FROM auth_transactions")
            self._execute(db,"INSERT INTO auth_policy_audit(id,actor_id,previous_mode,mode,revision,created_at) VALUES (?,?,?,?,?,?)",(secrets.token_hex(AUTH_TOKEN_BYTES),actor_id,policy["mode"],policy["mode"],expected_revision+1,self._now()))
        return self.get_policy()

    def identities(self, user_id):
        with self.database.connect() as db:
            policy = self._read_policy(db)
            user = self._user(db,user_id)
            row = self._active_identity(db,user_id,policy["provider_namespace"])
            return {"linked":bool(row and row["status"]=="active"),"local_login_name":user["local_login_name"] if user else None,"external_username":user["username"] if user and row else None,"display_name":user["display_name"] if user else ""}

    def preflight(self):
        with self.database.connect() as db:
            policy = self._read_policy(db)
            counts = self._execute(db,"SELECT count(*) AS active_users,coalesce(sum(CASE WHEN u.id='user-local' OR e.user_id IS NULL OR e.last_login_at IS NULL THEN 1 ELSE 0 END),0) AS unready_users,coalesce(sum(CASE WHEN u.id<>'user-local' AND u.role='admin' AND e.user_id IS NOT NULL AND e.last_login_at IS NOT NULL THEN 1 ELSE 0 END),0) AS ready_admins FROM users u LEFT JOIN external_identities e ON e.user_id=u.id AND e.provider_namespace=? AND e.status='active' WHERE u.status='active'",(policy["provider_namespace"],)).fetchone()
            return {"policy":policy,**dict(counts),"ready":not counts["unready_users"] and counts["ready_admins"]>=AUTH_CUTOVER_MIN_ADMINS}

    def inventory(self, *, offset=0, limit=AUTH_INVENTORY_PAGE_SIZE):
        if offset < 0 or not 0 < limit <= AUTH_INVENTORY_PAGE_MAX:
            raise AuthStoreError("invalid_page")
        with self.database.connect() as db:
            policy = self._read_policy(db)
            return [dict(row) for row in self._execute(db,"SELECT u.id,u.username,u.local_login_name,u.display_name,u.role,u.status,e.provider_namespace,e.subject,e.status AS identity_status,e.last_login_at FROM users u LEFT JOIN external_identities e ON e.user_id=u.id AND e.provider_namespace=? AND e.status='active' ORDER BY u.id LIMIT ? OFFSET ?",(policy["provider_namespace"],limit,offset)).fetchall()]

    def inventory_page(self, *, offset=0, limit=AUTH_INVENTORY_PAGE_SIZE):
        items = self.inventory(offset=offset,limit=limit)
        with self.database.connect() as db:
            total = self._execute(db,"SELECT count(*) AS n FROM users").fetchone()["n"]
        return {"items":items,"total":total,"offset":offset,"limit":limit}

    def _audit_identity(self, db, actor_id, user_id, action, namespace="", subject="", grant_reference=""):
        self._execute(
            db,
            "INSERT INTO auth_identity_audit"
            "(id,actor_id,target_user_id,action,provider_namespace,subject,grant_reference,created_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (secrets.token_hex(AUTH_TOKEN_BYTES),actor_id,user_id,action,namespace,subject,grant_reference,self._now()),
        )

    def audit_page(self, *, offset=0, limit=AUTH_INVENTORY_PAGE_SIZE):
        if offset < 0 or not 0 < limit <= AUTH_INVENTORY_PAGE_MAX:
            raise AuthStoreError("invalid_page")
        with self.database.connect() as db:
            rows = self._execute(
                db,
                "SELECT id,actor_id,target_user_id,action,provider_namespace,subject,grant_reference,created_at "
                "FROM auth_identity_audit ORDER BY created_at DESC,id DESC LIMIT ? OFFSET ?",
                (limit,offset),
            ).fetchall()
            total = self._execute(db,"SELECT count(*) AS n FROM auth_identity_audit").fetchone()["n"]
        return {"items":[dict(row) for row in rows],"total":total,"offset":offset,"limit":limit}

    def set_policy(self, mode, *, actor_id, expected_revision, provider_id="", provider_namespace="", config_generation="", plugin_id="", allow_rollback=False):
        if mode not in AUTH_MODES:
            raise AuthStoreError("invalid_mode")
        with self._write() as (db, policy):
            actor = self._user(db,actor_id)
            if not actor or actor["status"] != "active" or actor["role"] != "admin":
                raise AuthStoreError("admin_required")
            if policy["revision"] != expected_revision:
                raise AuthStoreError("stale_policy")
            old_index,new_index = AUTH_MODES.index(policy["mode"]),AUTH_MODES.index(mode)
            if policy["retired_at"] or mode == "local" and old_index > 0:
                raise AuthStoreError("irreversible_policy")
            if new_index > old_index+1 or (new_index < old_index and (not allow_rollback or new_index < 1)):
                raise AuthStoreError("invalid_transition")
            namespace = provider_namespace or policy["provider_namespace"]
            provider = provider_id or policy["provider_id"]
            generation = config_generation or policy["config_generation"]
            plugin = plugin_id or policy["plugin_id"]
            if mode != "local" and not all((namespace,provider,generation)):
                raise AuthStoreError("provider_required")
            if policy["provider_namespace"] and namespace != policy["provider_namespace"]:
                raise AuthStoreError("identity_source_change_requires_migration")
            if plugin:
                toggle = self._execute(db,"SELECT enabled FROM extension_runtime_toggles WHERE plugin_id=?",(plugin,)).fetchone()
                if toggle and not toggle["enabled"]:
                    raise AuthStoreError("provider_disabled")
            if mode in ("sso_only","retired"):
                missing = self._execute(db,"SELECT u.id FROM users u LEFT JOIN external_identities e ON e.user_id=u.id AND e.status='active' AND e.provider_namespace=? WHERE u.status='active' AND (u.id='user-local' OR e.user_id IS NULL OR e.last_login_at IS NULL)",(namespace,)).fetchall()
                admins = self._execute(db,"SELECT count(*) AS n FROM users u JOIN external_identities e ON e.user_id=u.id WHERE u.status='active' AND u.role='admin' AND u.id<>'user-local' AND e.status='active' AND e.provider_namespace=? AND e.last_login_at IS NOT NULL",(namespace,)).fetchone()["n"]
                if missing or admins < AUTH_CUTOVER_MIN_ADMINS:
                    raise AuthStoreError("migration_incomplete")
            self._execute(db,"UPDATE auth_policy SET mode=?,revision=revision+1,provider_id=?,provider_namespace=?,config_generation=?,plugin_id=?,retired_at=?,updated_by=? WHERE id=1",(mode,provider,namespace,generation,plugin,self._now() if mode=="retired" else None,actor_id))
            self._execute(db,"DELETE FROM auth_transactions")
            if mode in ("sso_only","retired"):
                self._execute(db,"DELETE FROM auth_sessions WHERE auth_source<>'sso'")
            self._execute(db,"INSERT INTO auth_policy_audit(id,actor_id,previous_mode,mode,revision,created_at) VALUES (?,?,?,?,?,?)",(secrets.token_hex(AUTH_TOKEN_BYTES),actor_id,policy["mode"],mode,expected_revision+1,self._now()))
        if mode == "retired":
            self.retirement_cleanup()
        return self.get_policy()

    def retirement_cleanup(self):
        with self._write() as (db, policy):
            if not policy["retired_at"]:
                raise AuthStoreError("retirement_not_started")
            self._execute(db,"UPDATE users SET password_hash='',password_salt='',password_iterations=0,local_login_name=NULL,auth_revision=auth_revision+1 WHERE password_hash<>'' OR password_salt<>'' OR password_iterations<>0 OR local_login_name IS NOT NULL")
            self._execute(db,"DELETE FROM auth_sessions WHERE auth_source<>'sso'")
            self._execute(db,"DELETE FROM auth_transactions")

    def set_account_status(self, user_id, status, *, actor_id):
        if status not in ("active","disabled","archived"):
            raise AuthStoreError("invalid_status")
        with self._write() as (db, policy):
            actor = self._user(db,actor_id)
            if not actor or actor["role"] != "admin" or actor["status"] != "active" or user_id == actor_id:
                raise AuthStoreError("admin_required")
            if not self._user(db,user_id):
                raise AuthStoreError("account_not_found")
            if status == "active" and policy["mode"] in ("sso_only","retired") and not self._execute(db,"SELECT 1 FROM external_identities WHERE user_id=? AND status='active' AND provider_namespace=?",(user_id,policy["provider_namespace"])).fetchone():
                raise AuthStoreError("identity_required")
            self._execute(db,"UPDATE users SET status=?,auth_revision=auth_revision+1 WHERE id=?",(status,user_id))
            self._execute(db,"DELETE FROM auth_sessions WHERE user_id=?",(user_id,))
            self._audit_identity(db,actor_id,user_id,"account_status:"+status)
