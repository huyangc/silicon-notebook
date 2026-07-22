from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any, Mapping, Sequence
from uuid import uuid4

from psycopg import errors

from app.core.config import Settings
from app.core.request_context import get_request_user
from app.models.identity import UserProfile
from app.repositories.postgres._store_utils import (
    iso_timestamp,
    json_value,
    jsonb,
    placeholders,
    utc_now,
)
from app.repositories.postgres.database import PostgresDatabase
from app.services.model_config import (
    MODEL_SERVICE_ROLES,
    STATUS_SERVICE_ROLES,
    ResolvedModelConfig,
    model_config_fingerprint,
    resolve_effective_config,
    system_model_settings,
)


def _status_checked_at(value: str) -> str:
    from datetime import timezone

    try:
        parsed = datetime.fromisoformat(value)
    except (TypeError, ValueError) as exc:
        raise ValueError("checked_at must be an offset-aware ISO timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("checked_at must be an offset-aware ISO timestamp")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _status_timestamp(value: object) -> str:
    if isinstance(value, datetime):
        return value.isoformat(timespec="microseconds")
    return iso_timestamp(value)


class IdentityStore:
    """PostgreSQL identity, session, and per-user model-settings persistence."""

    def __init__(
        self,
        database: PostgresDatabase,
        settings: Settings,
        model_config_cache: dict[str, object],
    ) -> None:
        self.database = database
        self.settings = settings
        self.model_config_cache = model_config_cache

    def current_user(self) -> UserProfile:
        context_user = get_request_user()
        if context_user is not None:
            return context_user
        with self.database.connect() as connection:
            user = connection.execute(
                "SELECT * FROM users WHERE id=%s", ("user-local",)
            ).fetchone()
            profile = connection.execute(
                "SELECT * FROM user_profiles WHERE user_id=%s", ("user-local",)
            ).fetchone()
        return self._user_profile(user, profile)

    @staticmethod
    def _user_profile(user: object, profile: object) -> UserProfile:
        user_row = user  # type: ignore[assignment]
        profile_row = profile  # type: ignore[assignment]
        return UserProfile(
            id=user_row["id"],
            email=user_row["email"],
            display_name=user_row["display_name"],
            role=user_row["role"],
            username=user_row.get("username", ""),
            memory_mode=profile_row["memory_mode"] if profile_row else "manual",
            domain_focus=json_value(
                profile_row.get("domain_focus") if profile_row else None, []
            ),
        )

    def get_user_model_settings(self, user_id: str) -> dict:
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT model_settings FROM user_profiles WHERE user_id=%s", (user_id,)
            ).fetchone()
        value = json_value(row["model_settings"] if row else None, {})
        return value if isinstance(value, dict) else {}

    def set_user_model_settings(self, user_id: str, settings: dict) -> None:
        with self.database.write() as connection:
            connection.execute(
                "UPDATE user_profiles SET model_settings=%s, updated_at=%s WHERE user_id=%s",
                (jsonb(settings), utc_now(), user_id),
            )
        self.model_config_cache.pop(user_id, None)

    def patch_user_model_settings_atomic(
        self,
        user_id: str,
        patch: Mapping[str, Mapping[str, str | None] | None],
    ) -> dict:
        system = system_model_settings(self.settings)
        policy = self.settings.user_model_config_policy
        with self.database.write() as connection:
            row = connection.execute(
                "SELECT model_settings FROM user_profiles WHERE user_id=%s FOR UPDATE",
                (user_id,),
            ).fetchone()
            stored = json_value(row["model_settings"] if row else None, {})
            if not isinstance(stored, dict):
                stored = {}
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
            connection.execute(
                "UPDATE user_profiles SET model_settings=%s, updated_at=%s WHERE user_id=%s",
                (jsonb(stored), utc_now(), user_id),
            )
            changed = [
                role
                for role in STATUS_SERVICE_ROLES
                if model_config_fingerprint(
                    resolve_effective_config(stored, role, policy, system)
                )
                != before[role]
            ]
            if changed:
                connection.execute(
                    "DELETE FROM model_service_status WHERE user_id=%s "
                    f"AND service IN ({placeholders(changed)})",
                    (user_id, *changed),
                )
        self.model_config_cache.pop(user_id, None)
        return stored

    def get_model_service_statuses(self, user_id: str) -> dict[str, dict[str, Any]]:
        with self.database.connect() as connection:
            rows = connection.execute(
                "SELECT service,config_fingerprint,status,latency_ms,code,trigger,checked_at "
                "FROM model_service_status WHERE user_id=%s",
                (user_id,),
            ).fetchall()
        return {
            row["service"]: {
                "config_fingerprint": row["config_fingerprint"],
                "status": row["status"],
                "latency_ms": row["latency_ms"],
                "code": row["code"],
                "trigger": row["trigger"],
                "checked_at": _status_timestamp(row["checked_at"]),
            }
            for row in rows
        }

    @staticmethod
    def _upsert_model_service_status(
        connection,
        user_id: str,
        service: str,
        config_fingerprint: str,
        status: str,
        latency_ms: int,
        code: str,
        trigger: str,
        checked_at: str,
    ) -> None:
        connection.execute(
            "INSERT INTO model_service_status "
            "(user_id,service,config_fingerprint,status,latency_ms,code,trigger,checked_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(user_id,service) DO UPDATE SET "
            "config_fingerprint=excluded.config_fingerprint,status=excluded.status,"
            "latency_ms=excluded.latency_ms,code=excluded.code,trigger=excluded.trigger,"
            "checked_at=excluded.checked_at "
            "WHERE excluded.checked_at > model_service_status.checked_at OR "
            "(excluded.checked_at = model_service_status.checked_at AND "
            "(CASE WHEN excluded.trigger='observed_failure' THEN 3 "
            "WHEN excluded.status='error' THEN 2 ELSE 1 END) > "
            "(CASE WHEN model_service_status.trigger='observed_failure' THEN 3 "
            "WHEN model_service_status.status='error' THEN 2 ELSE 1 END))",
            (
                user_id,
                service,
                config_fingerprint,
                status,
                latency_ms,
                code,
                trigger,
                checked_at,
            ),
        )

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
        with self.database.write() as connection:
            self._upsert_model_service_status(
                connection,
                user_id,
                service,
                config_fingerprint,
                status,
                latency_ms,
                code,
                trigger,
                checked_at,
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
        if service not in STATUS_SERVICE_ROLES or not expected_fingerprint:
            return False
        checked_at = _status_checked_at(checked_at)
        with self.database.write() as connection:
            row = connection.execute(
                "SELECT model_settings FROM user_profiles WHERE user_id=%s FOR UPDATE",
                (user_id,),
            ).fetchone()
            stored = json_value(row["model_settings"] if row else None, {})
            current = resolve_effective_config(
                stored if isinstance(stored, dict) else {},
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
                connection,
                user_id,
                service,
                expected_fingerprint,
                status,
                latency_ms,
                code,
                trigger,
                checked_at,
            )
        return True

    def clear_model_service_statuses(
        self, user_id: str, services: Sequence[str] | None = None
    ) -> None:
        if services is not None and not services:
            return
        with self.database.write() as connection:
            if services is None:
                connection.execute(
                    "DELETE FROM model_service_status WHERE user_id=%s", (user_id,)
                )
            else:
                connection.execute(
                    "DELETE FROM model_service_status WHERE user_id=%s "
                    f"AND service IN ({placeholders(services)})",
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
        normalized = normalize_username(username)
        user_id = f"user-{uuid4().hex}"
        now = utc_now()
        password_hash, password_salt, iterations = hash_password(password)
        try:
            with self.database.write() as connection:
                user = connection.execute(
                    "INSERT INTO users "
                    "(id,email,display_name,role,status,username,password_hash,password_salt,"
                    "password_iterations,created_at,updated_at) "
                    "VALUES (%s,%s,%s,'user','active',%s,%s,%s,%s,%s,%s) RETURNING *",
                    (
                        user_id,
                        f"{normalized}@users.silicon-notebook.local",
                        normalized,
                        normalized,
                        password_hash,
                        password_salt,
                        iterations,
                        now,
                        now,
                    ),
                ).fetchone()
                profile = connection.execute(
                    "INSERT INTO user_profiles "
                    "(id,user_id,memory_mode,domain_focus,created_at,updated_at) "
                    "VALUES (%s,%s,'manual',%s,%s,%s) RETURNING *",
                    (f"profile-{user_id}", user_id, jsonb([]), now, now),
                ).fetchone()
        except errors.UniqueViolation as exc:
            if exc.diag.constraint_name in {"idx_users_username", "uq_users_email"}:
                raise ValueError("username already exists") from exc
            raise
        return self._user_profile(user, profile)

    def authenticate_user(self, username: str, password: str) -> UserProfile | None:
        from app.services.auth_utils import normalize_username, verify_password

        with self.database.connect() as connection:
            user = connection.execute(
                "SELECT * FROM users WHERE username=%s", (normalize_username(username),)
            ).fetchone()
            if user is None or not verify_password(
                password,
                user["password_hash"],
                user["password_salt"],
                user["password_iterations"],
            ):
                return None
            profile = connection.execute(
                "SELECT * FROM user_profiles WHERE user_id=%s", (user["id"],)
            ).fetchone()
        return self._user_profile(user, profile)

    def create_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        now = utc_now()
        with self.database.write() as connection:
            connection.execute(
                "INSERT INTO auth_sessions(token,user_id,created_at,expires_at,last_seen_at) "
                "VALUES (%s,%s,%s,%s,%s)",
                (token, user_id, now, now + timedelta(days=30), now),
            )
        return token

    def resolve_session(self, token: str) -> UserProfile | None:
        if not token:
            return None
        now = utc_now()
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT * FROM auth_sessions WHERE token=%s", (token,)
            ).fetchone()
            if row is None:
                return None
            if row["expires_at"] <= now:
                expired = True
                user = profile = None
            else:
                expired = False
                user = connection.execute(
                    "SELECT * FROM users WHERE id=%s", (row["user_id"],)
                ).fetchone()
                profile = (
                    connection.execute(
                        "SELECT * FROM user_profiles WHERE user_id=%s", (user["id"],)
                    ).fetchone()
                    if user
                    else None
                )
        if expired:
            with self.database.write() as connection:
                connection.execute("DELETE FROM auth_sessions WHERE token=%s", (token,))
            return None
        if user is None:
            return None
        touch_before = now - timedelta(
            seconds=max(1, self.settings.auth_session_touch_interval_seconds)
        )
        if row["last_seen_at"] <= touch_before:
            with self.database.write() as connection:
                connection.execute(
                    "UPDATE auth_sessions SET last_seen_at=%s,expires_at=%s "
                    "WHERE token=%s AND last_seen_at=%s AND expires_at>%s",
                    (
                        now,
                        now + timedelta(days=30),
                        token,
                        row["last_seen_at"],
                        now,
                    ),
                )
        return self._user_profile(user, profile)

    def delete_session(self, token: str) -> None:
        with self.database.write() as connection:
            connection.execute("DELETE FROM auth_sessions WHERE token=%s", (token,))
