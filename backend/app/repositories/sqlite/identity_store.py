from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping, Sequence
from uuid import uuid4

from app.core.config import Settings
from app.core.request_context import get_request_user
from app.models.identity import UserProfile
from app.repositories.sqlite.database import SqliteDatabase
from app.services.model_config import (
    MODEL_SERVICE_ROLES,
    STATUS_SERVICE_ROLES,
    ResolvedModelConfig,
    model_config_fingerprint,
    resolve_effective_config,
    system_model_settings,
)


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _session_expiry(days: int = 30) -> str:
    return (datetime.now() + timedelta(days=days)).replace(microsecond=0).isoformat()


def _status_checked_at(value: str) -> str:
    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("checked_at must be an offset-aware ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("checked_at must be an offset-aware ISO timestamp")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _new_user_id() -> str:
    return f"user-{uuid4().hex}"


def _decode_model_settings(value: object) -> dict:
    try:
        parsed = json.loads(value) if value else {}
    except (ValueError, TypeError):
        parsed = {}
    return parsed if isinstance(parsed, dict) else {}


class IdentityStore:
    """SQLite identity, session, and per-user model-settings persistence."""

    def __init__(
        self,
        database: SqliteDatabase,
        settings: Settings,
        model_config_cache: dict[str, dict[str, Any]],
    ) -> None:
        self.database = database
        self.settings = settings
        self.model_config_cache = model_config_cache

    def current_user(self) -> UserProfile:
        ctx_user = get_request_user()
        if ctx_user is not None:
            return ctx_user
        with self.database.connect() as db:
            user = db.execute(
                "SELECT * FROM users WHERE id = ?", ("user-local",)
            ).fetchone()
            profile = db.execute(
                "SELECT * FROM user_profiles WHERE user_id = ?", ("user-local",)
            ).fetchone()
        return self._user_profile(user, profile)

    @staticmethod
    def _user_profile(user, profile) -> UserProfile:
        return UserProfile(
            id=user["id"],
            email=user["email"],
            display_name=user["display_name"],
            role=user["role"],
            username=user["username"] if "username" in user.keys() else "",
            memory_mode=profile["memory_mode"] if profile else "manual",
            domain_focus=json.loads(profile["domain_focus"]) if profile else [],
        )

    def get_user_model_settings(self, user_id: str) -> dict:
        with self.database.connect() as db:
            row = db.execute(
                "SELECT model_settings FROM user_profiles WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        return _decode_model_settings(row["model_settings"] if row else None)

    def set_user_model_settings(self, user_id: str, settings: dict) -> None:
        with self.database.write() as db:
            db.execute(
                "UPDATE user_profiles SET model_settings = ?, updated_at = ? WHERE user_id = ?",
                (json.dumps(settings, ensure_ascii=False), _now(), user_id),
            )
        self.model_config_cache.pop(user_id, None)

    def patch_user_model_settings_atomic(
        self,
        user_id: str,
        patch: Mapping[str, Mapping[str, str | None] | None],
    ) -> dict:
        """Patch settings and invalidate changed effective statuses atomically."""
        system = system_model_settings(self.settings)
        policy = self.settings.user_model_config_policy
        with self.database.write() as db:
            self.database.begin_immediate(db)
            row = db.execute(
                "SELECT model_settings FROM user_profiles WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            stored = _decode_model_settings(row["model_settings"] if row else None)

            before = {
                role: model_config_fingerprint(
                    resolve_effective_config(stored, role, policy, system)
                )
                for role in STATUS_SERVICE_ROLES
            }
            for role in MODEL_SERVICE_ROLES:
                role_patch = patch.get(role)
                if not isinstance(role_patch, Mapping):
                    continue
                service = dict(stored.get(role) or {})
                for field in ("base_url", "api_key", "model"):
                    if field not in role_patch or role_patch[field] is None:
                        continue
                    value = role_patch[field]
                    if value == "":
                        service.pop(field, None)
                    else:
                        service[field] = value
                if service:
                    stored[role] = service
                else:
                    stored.pop(role, None)

            db.execute(
                "UPDATE user_profiles SET model_settings = ?, updated_at = ? WHERE user_id = ?",
                (json.dumps(stored, ensure_ascii=False), _now(), user_id),
            )
            changed = [
                role
                for role in STATUS_SERVICE_ROLES
                if model_config_fingerprint(
                    resolve_effective_config(stored, role, policy, system)
                ) != before[role]
            ]
            if changed:
                placeholders = ", ".join("?" for _ in changed)
                db.execute(
                    "DELETE FROM model_service_status WHERE user_id = ? "
                    f"AND service IN ({placeholders})",
                    (user_id, *changed),
                )
        self.model_config_cache.pop(user_id, None)
        return stored

    def get_model_service_statuses(self, user_id: str) -> dict[str, dict[str, Any]]:
        with self.database.connect() as db:
            rows = db.execute(
                "SELECT service, config_fingerprint, status, latency_ms, code, trigger, checked_at "
                "FROM model_service_status WHERE user_id = ?",
                (user_id,),
            ).fetchall()
        return {
            row["service"]: {
                "config_fingerprint": row["config_fingerprint"],
                "status": row["status"],
                "latency_ms": row["latency_ms"],
                "code": row["code"],
                "trigger": row["trigger"],
                "checked_at": row["checked_at"],
            }
            for row in rows
        }

    def record_model_service_status(
        self,
        user_id: str,
        service: str,
        config_fingerprint: str,
        status: str,
        latency_ms: int,
        code: str,
        trigger: str,
        checked_at: str,
    ) -> None:
        checked_at = _status_checked_at(checked_at)
        with self.database.write() as db:
            self._upsert_model_service_status(
                db, user_id, service, config_fingerprint, status, latency_ms,
                code, trigger, checked_at,
            )

    def record_model_service_status_if_current(
        self,
        user_id: str,
        service: str,
        expected_fingerprint: str,
        status: str,
        latency_ms: int,
        code: str,
        trigger: str,
        checked_at: str,
    ) -> bool:
        """Record status only if the originating effective identity is current."""
        if service not in STATUS_SERVICE_ROLES or not expected_fingerprint:
            return False
        checked_at = _status_checked_at(checked_at)
        with self.database.write() as db:
            self.database.begin_immediate(db)
            row = db.execute(
                "SELECT model_settings FROM user_profiles WHERE user_id = ?",
                (user_id,),
            ).fetchone()
            stored = _decode_model_settings(row["model_settings"] if row else None)
            current = resolve_effective_config(
                stored,
                service,
                self.settings.user_model_config_policy,
                system_model_settings(self.settings),
            )
            if (
                not current.configured
                or model_config_fingerprint(current) != expected_fingerprint
            ):
                return False
            self._upsert_model_service_status(
                db, user_id, service, expected_fingerprint, status, latency_ms,
                code, trigger, checked_at,
            )
        return True

    @staticmethod
    def _upsert_model_service_status(
        db,
        user_id: str,
        service: str,
        config_fingerprint: str,
        status: str,
        latency_ms: int,
        code: str,
        trigger: str,
        checked_at: str,
    ) -> None:
        db.execute(
            "INSERT INTO model_service_status "
            "(user_id, service, config_fingerprint, status, latency_ms, code, trigger, checked_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(user_id, service) DO UPDATE SET "
            "config_fingerprint = excluded.config_fingerprint, "
            "status = excluded.status, latency_ms = excluded.latency_ms, "
            "code = excluded.code, trigger = excluded.trigger, "
            "checked_at = excluded.checked_at "
            "WHERE excluded.checked_at > model_service_status.checked_at "
            "OR (excluded.checked_at = model_service_status.checked_at AND "
            "(CASE WHEN excluded.trigger = 'observed_failure' THEN 3 "
            "      WHEN excluded.status = 'error' THEN 2 ELSE 1 END) > "
            "(CASE WHEN model_service_status.trigger = 'observed_failure' THEN 3 "
            "      WHEN model_service_status.status = 'error' THEN 2 ELSE 1 END))",
            (
                user_id, service, config_fingerprint, status, latency_ms,
                code, trigger, checked_at,
            ),
        )

    def clear_model_service_statuses(
        self, user_id: str, services: Sequence[str] | None = None
    ) -> None:
        if services is not None and not services:
            return
        with self.database.write() as db:
            if services is None:
                db.execute(
                    "DELETE FROM model_service_status WHERE user_id = ?", (user_id,)
                )
                return
            placeholders = ", ".join("?" for _ in services)
            db.execute(
                "DELETE FROM model_service_status WHERE user_id = ? "
                f"AND service IN ({placeholders})",
                (user_id, *services),
            )

    def resolve_model_config(self, user: UserProfile, role: str) -> ResolvedModelConfig:
        return resolve_effective_config(
            self.get_user_model_settings(user.id),
            role,
            self.settings.user_model_config_policy,
            system_model_settings(self.settings),
        )

    def create_user(self, username: str, password: str) -> UserProfile:
        from app.services.auth_utils import hash_password, is_valid_username, normalize_username

        if not is_valid_username(username):
            raise ValueError("invalid username")
        norm = normalize_username(username)
        user_id = _new_user_id()
        now = _now()
        pw_hash, pw_salt, pw_iters = hash_password(password)
        email = f"{norm}@users.silicon-notebook.local"
        with self.database.write() as db:
            exists = db.execute(
                "SELECT 1 FROM users WHERE username = ?", (norm,)
            ).fetchone()
            if exists:
                raise ValueError("username already exists")
            db.execute(
                "INSERT INTO users (id, email, display_name, role, status, username, "
                "password_hash, password_salt, password_iterations, created_at, updated_at) "
                "VALUES (?, ?, ?, 'user', 'active', ?, ?, ?, ?, ?, ?)",
                (user_id, email, norm, norm, pw_hash, pw_salt, pw_iters, now, now),
            )
            db.execute(
                "INSERT INTO user_profiles (id, user_id, memory_mode, domain_focus, created_at, updated_at) "
                "VALUES (?, ?, 'manual', '[]', ?, ?)",
                (f"profile-{user_id}", user_id, now, now),
            )
            user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            profile = db.execute(
                "SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)
            ).fetchone()
            return self._user_profile(user, profile)

    def authenticate_user(self, username: str, password: str) -> UserProfile | None:
        from app.services.auth_utils import normalize_username, verify_password

        norm = normalize_username(username)
        with self.database.connect() as db:
            user = db.execute(
                "SELECT * FROM users WHERE username = ?", (norm,)
            ).fetchone()
            if user is None:
                return None
            if not verify_password(
                password,
                user["password_hash"],
                user["password_salt"],
                user["password_iterations"],
            ):
                return None
            profile = db.execute(
                "SELECT * FROM user_profiles WHERE user_id = ?", (user["id"],)
            ).fetchone()
            return self._user_profile(user, profile)

    def create_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        now = _now()
        with self.database.write() as db:
            db.execute(
                "INSERT INTO auth_sessions (token, user_id, created_at, expires_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (token, user_id, now, _session_expiry(), now),
            )
        return token

    def resolve_session(self, token: str) -> UserProfile | None:
        if not token:
            return None
        now = _now()
        with self.database.connect() as db:
            row = db.execute(
                "SELECT * FROM auth_sessions WHERE token = ?", (token,)
            ).fetchone()
            if row is None:
                return None
            expired = row["expires_at"] <= now
            user = None if expired else db.execute(
                "SELECT * FROM users WHERE id = ?", (row["user_id"],)
            ).fetchone()
            profile = None if user is None else db.execute(
                "SELECT * FROM user_profiles WHERE user_id = ?", (user["id"],)
            ).fetchone()
        if expired:
            with self.database.write() as db:
                db.execute("DELETE FROM auth_sessions WHERE token = ?", (token,))
            return None
        if user is None:
            return None
        touch_before = (
            datetime.now()
            - timedelta(
                seconds=max(1, self.settings.auth_session_touch_interval_seconds)
            )
        ).replace(microsecond=0).isoformat()
        if row["last_seen_at"] <= touch_before:
            with self.database.write() as db:
                db.execute(
                    "UPDATE auth_sessions SET last_seen_at = ?, expires_at = ? "
                    "WHERE token = ? AND last_seen_at = ? AND expires_at > ?",
                    (now, _session_expiry(), token, row["last_seen_at"], now),
                )
        return self._user_profile(user, profile)

    def delete_session(self, token: str) -> None:
        with self.database.write() as db:
            db.execute("DELETE FROM auth_sessions WHERE token = ?", (token,))
