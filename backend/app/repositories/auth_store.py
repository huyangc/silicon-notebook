"""Atomic authentication persistence shared by the two SQL adapters.

Every authentication write takes one store-wide lock before it reads users or
sessions: SQLite's BEGIN IMMEDIATE, PostgreSQL's transaction-scoped advisory
lock. Whether unified authentication is on is not stored anywhere: it is the
attached provider host's answer (``describe()`` is not None), read per call,
so switching the provider plugin off is the way back to local passwords.
The PostgreSQL adapter converts placeholders. No provider/network operation
belongs in this transaction boundary.
"""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
import secrets
import time

from app.domain.auth_utils import PASSWORD_HASH_ITERATIONS, normalize_username, verify_password
from app.domain.auth_provider import (
    AUTH_PROVIDER_SUBJECT_MAX_CHARS, AUTH_PROVIDER_USERNAME_MAX_CHARS,
    AUTH_PROVIDER_DISPLAY_NAME_MAX_CHARS, AUTH_PROVIDER_NAMESPACE_MAX_CHARS,
    AuthProviderHostPort,
)
from app.repositories.identity_errors import AuthStoreError

AUTH_TOKEN_BYTES = 32
LOCAL_SESSION_SECONDS = 30 * 24 * 60 * 60
_DUMMY_PASSWORD = ("0" * 64, "00" * 16, PASSWORD_HASH_ITERATIONS)
# One advisory key serializes every PostgreSQL authentication write.
_AUTH_LOCK_KEY = "silicon-notebook.auth"
_BUILTIN_USER_ID = "user-local"


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _has_control(value: str) -> bool:
    return any(ord(c) < 32 or ord(c) == 127 for c in value)


def valid_account_username(value) -> bool:
    """One rule for every name an account can be given outside registration:
    a provider username about to become (or match) an account name, and an
    administrator's rename. Non-empty, no surrounding whitespace, no control
    characters, within the provider username rail."""
    return (
        isinstance(value, str) and bool(value) and value == value.strip()
        and len(value) <= AUTH_PROVIDER_USERNAME_MAX_CHARS and not _has_control(value)
    )


class AuthStore:
    def __init__(self, database, settings, identity, *, postgres=False):
        self.database = database
        self.settings = settings
        self.identity = identity
        self.postgres = postgres
        self._provider_host: AuthProviderHostPort | None = None

    def use_provider(self, host: AuthProviderHostPort) -> None:
        """Attach the provider host whose enabled state turns unified auth on."""
        self._provider_host = host

    def sso_namespace(self) -> str:
        """The enabled provider's namespace, or "" while local login applies."""
        host = self._provider_host
        descriptor = host.describe() if host is not None else None
        return descriptor.provider_namespace if descriptor is not None else ""

    def _execute(self, db, sql, params=()):
        return db.execute(sql.replace("?", "%s") if self.postgres else sql, params)

    def _now(self):
        return datetime.now(timezone.utc) if self.postgres else datetime.now().replace(microsecond=0).isoformat()

    def _expires(self, seconds):
        value = datetime.now(timezone.utc) if self.postgres else datetime.now()
        value += timedelta(seconds=seconds)
        return value if self.postgres else value.replace(microsecond=0).isoformat()

    def lock(self, db):
        if self.postgres:
            self._execute(db, "SELECT pg_advisory_xact_lock(hashtextextended(?,0))", (_AUTH_LOCK_KEY,))
        elif not db.in_transaction:
            self.database.begin_immediate(db)

    @contextmanager
    def _write(self):
        with self.database.write() as db:
            self.lock(db)
            yield db

    def require_local_write(self, db):
        """Lock, then refuse a local credential write while unified auth is on."""
        self.lock(db)
        if self.sso_namespace():
            raise AuthStoreError("local_auth_disabled")

    def _user(self, db, user_id):
        return self._execute(db, "SELECT * FROM users WHERE id=?", (user_id,)).fetchone()

    def _profile(self, db, user):
        row = self._execute(db, "SELECT * FROM user_profiles WHERE user_id=?", (user["id"],)).fetchone()
        return self.identity._user_profile(user, row)

    def local_account(self, db, login_name, *, for_update=False):
        """The one account a local login name names (case-insensitive)."""
        sql = "SELECT * FROM users WHERE lower(username)=?" + (" FOR UPDATE" if for_update and self.postgres else "")
        return self._execute(db, sql, (normalize_username(login_name),)).fetchone()

    def owner_eligible(self, user_id):
        with self.database.connect() as db:
            return self._execute(
                db, "SELECT 1 FROM users WHERE id=? AND status='active'", (user_id,),
            ).fetchone() is not None

    def _session(self, db, token):
        # This one statement is the authorization linearization point. In
        # particular, never re-read a user after returning a boolean decision.
        # A local session counts only while unified auth is off; an SSO session
        # only while its provider namespace is the enabled one.
        now = self._now()
        namespace = self.sso_namespace()
        return self._execute(
            db,
            "SELECT u.*,s.last_seen_at AS session_last_seen_at,"
            "s.expires_at AS session_expires_at,s.auth_source,"
            "s.absolute_expires_at AS session_absolute_expires_at "
            "FROM auth_sessions s JOIN users u ON u.id=s.user_id "
            "WHERE s.token=? AND u.status='active' AND s.expires_at>? "
            "AND (s.absolute_expires_at IS NULL OR s.absolute_expires_at>?) "
            "AND ((s.auth_source='local' AND ?='') "
            "OR (s.auth_source='sso' AND ?<>'' AND s.provider_namespace=? "
            "AND u.id<>'user-local'))",
            (token, now, now, namespace, namespace, namespace),
        ).fetchone()

    def resolve_session(self, token):
        if not token:
            return None
        with self.database.connect() as db:
            user = self._session(db, token)
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
            with self._write() as db:
                fresh = self._session(db,token)
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
        self.require_local_write(db)
        user = self._user(db, user_id)
        if not user or user["status"] != "active":
            raise AuthStoreError("local_auth_disabled")
        return self._insert_session(db, user_id, "local", LOCAL_SESSION_SECONDS)

    def _insert_session(self, db, user_id, source, seconds, namespace="", subject=""):
        token = secrets.token_urlsafe(AUTH_TOKEN_BYTES)
        now = self._now()
        expires = self._expires(seconds)
        absolute = expires if source == "sso" else None
        self._execute(db, "INSERT INTO auth_sessions(token,user_id,created_at,expires_at,last_seen_at,auth_source,absolute_expires_at,provider_namespace,external_subject) VALUES (?,?,?,?,?,?,?,?,?)", (token,user_id,now,expires,now,source,absolute,namespace,subject))
        self._execute(db, "UPDATE users SET last_seen_at=? WHERE id=? AND (last_seen_at IS NULL OR last_seen_at<?)", (now,user_id,now))
        return token

    def check_name(self, db, username, user_id="", *, email=None):
        """Refuse a name some other account holds in any letter case. A new
        account also needs its placeholder email free: an administrator rename
        keeps the email minted from the old name, so registering that old name
        again must read as "name taken", not as a unique-constraint crash."""
        sql = "SELECT id FROM users WHERE id<>? AND (lower(username)=lower(?)"
        params = [user_id, username]
        if email is not None:
            sql += " OR email=?"
            params.append(email)
        if self._execute(db, sql + ")", tuple(params)).fetchone():
            raise AuthStoreError("username already exists")

    def has_sso_admin(self):
        """Whether an active administrator other than the built-in one exists:
        the built-in account cannot sign in through unified authentication."""
        with self.database.connect() as db:
            return self._execute(
                db, "SELECT 1 FROM users WHERE status='active' AND role='admin' AND id<>? LIMIT 1",
                (_BUILTIN_USER_ID,),
            ).fetchone() is not None

    def _put_transaction(self, db, purpose, proof, payload, ttl_seconds):
        if ttl_seconds <= 0 or not proof:
            raise AuthStoreError("invalid_transaction")
        self._execute(db,"DELETE FROM auth_transactions WHERE expires_at<=?",(int(time.time()),))
        token = secrets.token_urlsafe(AUTH_TOKEN_BYTES)
        self._execute(db, "INSERT INTO auth_transactions(token_digest,purpose,browser_digest,payload,expires_at) VALUES (?,?,?,?,?)", (digest(token),purpose,digest(proof),json.dumps(payload),int(time.time())+ttl_seconds))
        return token

    def _take(self, db, token, proof, purpose, *, consume=True):
        row = self._execute(db, "SELECT * FROM auth_transactions WHERE token_digest=?", (digest(token),)).fetchone()
        if not row or row["purpose"] != purpose or row["expires_at"] <= time.time() or not secrets.compare_digest(row["browser_digest"],digest(proof)):
            raise AuthStoreError("invalid_transaction")
        if consume:
            self._execute(db,"DELETE FROM auth_transactions WHERE token_digest=?",(digest(token),))
        return json.loads(row["payload"])

    def _require_namespace(self, payload):
        namespace = self.sso_namespace()
        if not namespace:
            raise AuthStoreError("sso_disabled")
        if payload.get("provider_namespace") != namespace:
            raise AuthStoreError("stale_transaction")
        return namespace

    def begin(self, browser_proof, *, ttl_seconds, pkce_verifier=""):
        with self._write() as db:
            namespace = self.sso_namespace()
            if not namespace:
                raise AuthStoreError("sso_disabled")
            payload = {"provider_namespace":namespace,"pkce_verifier":pkce_verifier}
            return self._put_transaction(db,"login",browser_proof,payload,ttl_seconds)

    def claim(self, state, browser_proof):
        with self._write() as db:
            payload = self._take(db,state,browser_proof,"login")
            self._require_namespace(payload)
            # An unguessable server-only claim prevents replay of a claimed dict.
            payload["claim"] = self._put_transaction(db,"claim",browser_proof,payload,max(1,int(self.settings.auth_transaction_ttl_seconds)))
            return payload

    def stage_identity(self, transaction, identity, browser_proof, *, ttl_seconds):
        values = {key:(identity[key] if isinstance(identity,dict) else getattr(identity,key)) for key in ("provider_namespace","subject","username","display_name")}
        if any(not isinstance(value,str) or not value.strip() for value in values.values()):
            raise AuthStoreError("invalid_identity")
        rails = {"provider_namespace":AUTH_PROVIDER_NAMESPACE_MAX_CHARS,"subject":AUTH_PROVIDER_SUBJECT_MAX_CHARS,"username":AUTH_PROVIDER_USERNAME_MAX_CHARS,"display_name":AUTH_PROVIDER_DISPLAY_NAME_MAX_CHARS}
        if any(len(values[key]) > limit or _has_control(values[key]) for key,limit in rails.items()):
            raise AuthStoreError("invalid_identity")
        # The provider username becomes an account name: the administrator
        # rename rule applies to it too.
        if not valid_account_username(values["username"]):
            raise AuthStoreError("invalid_identity")
        with self._write() as db:
            payload = self._take(db,transaction["claim"],browser_proof,"claim")
            namespace = self._require_namespace(payload)
            if values["provider_namespace"] != namespace:
                raise AuthStoreError("wrong_provider")
            handoff = {"provider_namespace":namespace,"identity":values,"authenticated_at":int(time.time())}
            return self._put_transaction(db,"handoff",browser_proof,handoff,ttl_seconds)

    def _remaining(self, payload, session_seconds):
        remaining = payload["authenticated_at"] + session_seconds - int(time.time())
        if remaining <= 0:
            raise AuthStoreError("external_auth_expired")
        return remaining

    def _sso_session(self, db, user_id, payload, session_seconds):
        values = payload["identity"]
        return self._insert_session(
            db, user_id, "sso", self._remaining(payload, session_seconds),
            values["provider_namespace"], values["subject"],
        )

    def complete(self, code, browser_proof, *, session_seconds):
        """Sign in the account whose username is exactly the external one.

        No such account (the built-in one never counts) stages a one-time
        choice: link an existing account by its password, or create one.
        """
        with self._write() as db:
            payload = self._take(db,code,browser_proof,"handoff")
            self._require_namespace(payload)
            self._remaining(payload, session_seconds)
            values = payload["identity"]
            user = self._execute(
                db,"SELECT * FROM users WHERE username=? AND id<>?",
                (values["username"],_BUILTIN_USER_ID),
            ).fetchone()
            if user is None:
                pending = self._put_transaction(db,"choice",browser_proof,payload,self.settings.auth_transaction_ttl_seconds)
                return {"status":"choice_required","pending_id":pending,
                        "external_username":values["username"],"display_name":values["display_name"]}
            if user["status"] != "active":
                raise AuthStoreError("account_inactive")
            # The first unified sign-in marks the account as claimed.
            self._execute(
                db,"UPDATE users SET sso_linked_at=? WHERE id=? AND sso_linked_at IS NULL",
                (self._now(),user["id"]),
            )
            token = self._sso_session(db, user["id"], payload, session_seconds)
            return {"status":"authenticated","user":self._profile(db,user),"token":token}

    def _require_name_free(self, db, username, user_id=""):
        """A real account holding the exact name now makes the choice stale.
        The built-in account is a lasting conflict; another account holding a
        case variant is one the person can resolve by linking that account.
        idx_users_username_lower keeps the holder to at most one row."""
        holder = self._execute(
            db,"SELECT id,username FROM users WHERE id<>? AND lower(username)=lower(?)",(user_id,username),
        ).fetchone()
        if holder is None:
            return
        if holder["id"] == _BUILTIN_USER_ID:
            raise AuthStoreError("username_conflict")
        raise AuthStoreError("stale_transaction" if holder["username"] == username else "username_case_conflict")

    def link(self, pending_id, browser_proof, login_name, password, *, session_seconds):
        """Rename a password-verified existing account to the external name.

        A failed verification leaves the choice in place so the person can
        retry; an unknown name and a wrong password are one rejection.
        """
        with self._write() as db:
            payload = self._take(db,pending_id,browser_proof,"choice",consume=False)
            self._require_namespace(payload)
            values = payload["identity"]
            old = self.local_account(db, login_name, for_update=True)
            stored = (old["password_hash"],old["password_salt"],old["password_iterations"]) if old and old["password_hash"] else _DUMMY_PASSWORD
            # Same PBKDF2 cost either way, so timing does not reveal the name.
            if not verify_password(password,*stored) or not old or not old["password_hash"]:
                raise AuthStoreError("link_verification_failed")
            if old["id"] == _BUILTIN_USER_ID:
                raise AuthStoreError("link_target_invalid")
            # Only after the password check, so a wrong guess never learns
            # whether the account was already claimed by a unified login.
            if old["sso_linked_at"] is not None:
                raise AuthStoreError("link_target_linked")
            if old["status"] != "active":
                raise AuthStoreError("account_inactive")
            self._require_name_free(db, values["username"], old["id"])
            self._remaining(payload, session_seconds)
            self._take(db,pending_id,browser_proof,"choice")
            now = self._now()
            self._execute(
                db,"UPDATE users SET username=?,display_name=?,sso_linked_at=?,"
                "auth_revision=auth_revision+1,updated_at=? WHERE id=?",
                (values["username"],values["display_name"],now,now,old["id"]),
            )
            self._execute(db,"DELETE FROM auth_sessions WHERE user_id=?",(old["id"],))
            self._audit(db,old["id"],old["id"],"sso_linked",values["provider_namespace"],values["subject"])
            token = self._sso_session(db, old["id"], payload, session_seconds)
            return self._profile(db,self._user(db,old["id"])),token

    def create(self, pending_id, browser_proof, *, session_seconds):
        """Create a passwordless ordinary account named after the external one."""
        with self._write() as db:
            payload = self._take(db,pending_id,browser_proof,"choice")
            self._require_namespace(payload)
            values = payload["identity"]
            self._remaining(payload, session_seconds)
            self._require_name_free(db, values["username"])
            user_id = "user-" + secrets.token_hex(AUTH_TOKEN_BYTES)
            now = self._now()
            self._execute(db,"INSERT INTO users(id,email,display_name,role,status,username,sso_linked_at,created_at,updated_at) VALUES (?,?,?,'user','active',?,?,?,?)",(user_id,user_id+"@users.silicon-notebook.local",values["display_name"],values["username"],now,now,now))
            self._execute(db,"INSERT INTO user_profiles(id,user_id,memory_mode,domain_focus,created_at,updated_at) VALUES (?,?,'manual',?,?,?)",("profile-"+user_id,user_id,"[]",now,now))
            self._audit(db,user_id,user_id,"sso_created",values["provider_namespace"],values["subject"])
            token = self._sso_session(db, user_id, payload, session_seconds)
            return self._profile(db,self._user(db,user_id)),token

    def cancel(self, pending_id, browser_proof):
        with self._write() as db:
            self._execute(
                db,"DELETE FROM auth_transactions WHERE token_digest=? AND purpose='choice' AND browser_digest=?",
                (digest(pending_id),digest(browser_proof)),
            )

    def cancel_for_browser(self, browser_proof):
        if not browser_proof:
            return
        with self._write() as db:
            self._execute(db,"DELETE FROM auth_transactions WHERE browser_digest=?",(digest(browser_proof),))

    def _audit(self, db, actor_id, user_id, action, namespace="", subject=""):
        self._execute(
            db,
            "INSERT INTO auth_identity_audit"
            "(id,actor_id,target_user_id,action,provider_namespace,subject,created_at) "
            "VALUES (?,?,?,?,?,?,?)",
            (secrets.token_hex(AUTH_TOKEN_BYTES),actor_id,user_id,action,namespace,subject,self._now()),
        )

    def _admin_target(self, db, actor_id, user_id):
        """Lock actor and target in id order; the actor must be an active admin."""
        rows = self._execute(
            db,"SELECT * FROM users WHERE id IN (?,?) ORDER BY id"+(" FOR UPDATE" if self.postgres else ""),
            (actor_id,user_id),
        ).fetchall()
        by_id = {row["id"]:row for row in rows}
        actor = by_id.get(actor_id)
        if actor is None or actor["role"] != "admin" or actor["status"] != "active":
            raise AuthStoreError("admin_required")
        target = by_id.get(user_id)
        if target is None:
            raise AuthStoreError("account_not_found")
        if user_id == actor_id:
            raise AuthStoreError("self_forbidden")
        if user_id == _BUILTIN_USER_ID:
            raise AuthStoreError("builtin_account")
        return target

    def _revoke(self, db, user_id, column, value):
        self._execute(db,f"UPDATE users SET {column}=?,auth_revision=auth_revision+1,updated_at=? WHERE id=?",(value,self._now(),user_id))
        self._execute(db,"DELETE FROM auth_sessions WHERE user_id=?",(user_id,))

    def set_account_status(self, user_id, status, *, actor_id):
        if status not in ("active","disabled"):
            raise AuthStoreError("invalid_status")
        with self._write() as db:
            target = self._admin_target(db, actor_id, user_id)
            self._revoke(db, user_id, "status", status)
            self._audit(db,actor_id,user_id,"account_status:"+status)
            return {"id":user_id,"username":target["username"],"status":status}

    def set_username(self, user_id, username, *, actor_id):
        if not valid_account_username(username):
            raise AuthStoreError("invalid_username")
        with self._write() as db:
            target = self._admin_target(db, actor_id, user_id)
            try:
                self.check_name(db, username, user_id)
            except AuthStoreError:
                raise AuthStoreError("username_conflict") from None
            self._revoke(db, user_id, "username", username)
            self._audit(db,actor_id,user_id,"username_changed")
            return {"id":user_id,"username":username,"status":target["status"]}
