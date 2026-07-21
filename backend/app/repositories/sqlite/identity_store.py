from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from app.core.config import Settings
from app.core.request_context import get_request_user
from app.models.schemas import UserProfile
from app.repositories.sqlite.database import SqliteDatabase
from app.services.model_config import (
    ResolvedModelConfig,
    resolve_effective_config,
    system_model_settings,
)


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _session_expiry(days: int = 30) -> str:
    return (datetime.now() + timedelta(days=days)).replace(microsecond=0).isoformat()


def _new_user_id() -> str:
    return f"user-{uuid4().hex}"


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
        cached = self.model_config_cache.get(user_id)
        if cached is not None:
            return cached
        with self.database.connect() as db:
            row = db.execute(
                "SELECT model_settings FROM user_profiles WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        try:
            parsed = json.loads(row["model_settings"]) if row and row["model_settings"] else {}
        except (ValueError, TypeError):
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        self.model_config_cache[user_id] = parsed
        return parsed

    def set_user_model_settings(self, user_id: str, settings: dict) -> None:
        with self.database.write() as db:
            db.execute(
                "UPDATE user_profiles SET model_settings = ?, updated_at = ? WHERE user_id = ?",
                (json.dumps(settings, ensure_ascii=False), _now(), user_id),
            )
        self.model_config_cache.pop(user_id, None)

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
