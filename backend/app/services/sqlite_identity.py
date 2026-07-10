from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta
from typing import Any, Dict, List
from uuid import uuid4

from app.core.config import Settings
from app.models.schemas import UserProfile
from app.services.model_config import ResolvedModelConfig, resolve_effective_config
from app.core.request_context import (
    _REQUEST_USER, get_request_user, request_user_id, set_request_user, reset_request_user,
)


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _session_expiry(days: int = 30) -> str:
    return (datetime.now() + timedelta(days=days)).replace(microsecond=0).isoformat()


def _new_user_id() -> str:
    return f"user-{uuid4().hex}"


class SQLiteIdentityMixin:
    """SQLite-backed identity/session domain for ``SQLiteRepository``.

    The host supplies ``settings``, ``_connect()``, ``_write()``, and the
    bounded user-model configuration cache.  Keeping this domain in a small
    module makes its write policy and security boundary reviewable without
    changing the repository's public interface or schema.
    """

    settings: Settings
    _user_model_cfg_cache: Dict[str, dict]

    def current_user(self) -> UserProfile:
        ctx_user = _REQUEST_USER.get()
        if ctx_user is not None:
            return ctx_user
        with self._connect() as db:
            user = db.execute("SELECT * FROM users WHERE id = ?", ("user-local",)).fetchone()
            profile = db.execute(
                "SELECT * FROM user_profiles WHERE user_id = ?",
                ("user-local",),
            ).fetchone()
        return self._user_profile(user, profile)

    def _user_profile(self, user, profile) -> UserProfile:
        """Build the shared API profile from users + user_profiles rows."""
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
        """Read server-only model configuration JSON with a process cache."""
        cached = self._user_model_cfg_cache.get(user_id)
        if cached is not None:
            return cached
        with self._connect() as db:
            row = db.execute(
                "SELECT model_settings FROM user_profiles WHERE user_id = ?", (user_id,)
            ).fetchone()
        try:
            parsed = json.loads(row["model_settings"]) if row and row["model_settings"] else {}
        except (ValueError, TypeError):
            parsed = {}
        if not isinstance(parsed, dict):
            parsed = {}
        self._user_model_cfg_cache[user_id] = parsed
        return parsed

    def set_user_model_settings(self, user_id: str, settings: dict) -> None:
        """Replace model settings and invalidate provider-client resolution."""
        with self._write() as db:
            db.execute(
                "UPDATE user_profiles SET model_settings = ?, updated_at = ? WHERE user_id = ?",
                (json.dumps(settings, ensure_ascii=False), _now(), user_id),
            )
        self._user_model_cfg_cache.pop(user_id, None)

    def resolve_model_config(self, user, role: str) -> ResolvedModelConfig:
        return resolve_effective_config(
            self.get_user_model_settings(user.id),
            role,
            self.settings.user_model_config_policy,
        )

    def create_user(self, username: str, password: str) -> UserProfile:
        """Register a normalized, unique non-admin user."""
        from app.services.auth_utils import hash_password, is_valid_username, normalize_username

        if not is_valid_username(username):
            raise ValueError("invalid username")
        norm = normalize_username(username)
        user_id = _new_user_id()
        now = _now()
        pw_hash, pw_salt, pw_iters = hash_password(password)
        email = f"{norm}@users.silicon-notebook.local"
        with self._write() as db:
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

    def authenticate_user(self, username: str, password: str) -> "UserProfile | None":
        from app.services.auth_utils import normalize_username, verify_password

        norm = normalize_username(username)
        with self._connect() as db:
            user = db.execute("SELECT * FROM users WHERE username = ?", (norm,)).fetchone()
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

    def list_user_usage(self) -> List[Dict[str, Any]]:
        """Return admin usage aggregates without per-user N+1 queries."""
        with self._connect() as db:
            users = db.execute(
                "SELECT id, username, display_name, role, created_at "
                "FROM users ORDER BY created_at, id"
            ).fetchall()
            notebooks = {
                row["k"]: row["c"]
                for row in db.execute(
                    "SELECT created_by AS k, COUNT(*) AS c FROM notebooks "
                    "WHERE status != 'copying' GROUP BY created_by"
                ).fetchall()
            }
            sources = {
                row["k"]: row["c"]
                for row in db.execute(
                    "SELECT nb.created_by AS k, COUNT(*) AS c FROM sources s "
                    "JOIN notebooks nb ON nb.id = s.notebook_id GROUP BY nb.created_by"
                ).fetchall()
            }
            conversations = {
                row["k"]: row["c"]
                for row in db.execute(
                    "SELECT created_by AS k, COUNT(*) AS c FROM conversations GROUP BY created_by"
                ).fetchall()
            }
            reports = {
                row["k"]: row["c"]
                for row in db.execute(
                    "SELECT nb.created_by AS k, COUNT(*) AS c FROM reports r "
                    "JOIN notebooks nb ON nb.id = r.notebook_id GROUP BY nb.created_by"
                ).fetchall()
            }
            active = {
                row["k"]: row["m"]
                for row in db.execute(
                    "SELECT created_by AS k, MAX(updated_at) AS m FROM conversations "
                    "GROUP BY created_by"
                ).fetchall()
            }
        return [
            {
                "id": user["id"],
                "username": user["username"] or user["display_name"] or user["id"],
                "role": user["role"],
                "created_at": user["created_at"],
                "notebooks": notebooks.get(user["id"], 0),
                "sources": sources.get(user["id"], 0),
                "conversations": conversations.get(user["id"], 0),
                "reports": reports.get(user["id"], 0),
                "last_active": active.get(user["id"]),
            }
            for user in users
        ]

    def list_user_notebooks(self, user_id: str) -> List[Dict[str, Any]]:
        """Return one user's notebook usage with fixed-count aggregate queries."""
        with self._connect() as db:
            notebooks = db.execute(
                "SELECT id, name, status, created_at, updated_at FROM notebooks "
                "WHERE created_by = ? AND status != 'copying' ORDER BY created_at DESC",
                (user_id,),
            ).fetchall()
            ids = [row["id"] for row in notebooks]
            sources: Dict[str, int] = {}
            conversations: Dict[str, int] = {}
            reports: Dict[str, int] = {}
            if ids:
                placeholders = ",".join("?" * len(ids))
                sources = {
                    row["k"]: row["c"]
                    for row in db.execute(
                        f"SELECT notebook_id AS k, COUNT(*) AS c FROM sources "
                        f"WHERE notebook_id IN ({placeholders}) GROUP BY notebook_id",
                        ids,
                    ).fetchall()
                }
                conversations = {
                    row["k"]: row["c"]
                    for row in db.execute(
                        f"SELECT notebook_id AS k, COUNT(*) AS c FROM conversations "
                        f"WHERE notebook_id IN ({placeholders}) GROUP BY notebook_id",
                        ids,
                    ).fetchall()
                }
                reports = {
                    row["k"]: row["c"]
                    for row in db.execute(
                        f"SELECT notebook_id AS k, COUNT(*) AS c FROM reports "
                        f"WHERE notebook_id IN ({placeholders}) GROUP BY notebook_id",
                        ids,
                    ).fetchall()
                }
        return [
            {
                "id": row["id"],
                "name": row["name"],
                "status": row["status"],
                "created_at": row["created_at"],
                "updated_at": row["updated_at"],
                "sources": sources.get(row["id"], 0),
                "conversations": conversations.get(row["id"], 0),
                "reports": reports.get(row["id"], 0),
            }
            for row in notebooks
        ]

    def create_session(self, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        now = _now()
        with self._write() as db:
            db.execute(
                "INSERT INTO auth_sessions (token, user_id, created_at, expires_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (token, user_id, now, _session_expiry(), now),
            )
        return token

    def resolve_session(self, token: str) -> "UserProfile | None":
        """Resolve sessions read-mostly and throttle sliding-expiry writes."""
        if not token:
            return None
        now = _now()
        with self._connect() as db:
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
            with self._write() as db:
                db.execute("DELETE FROM auth_sessions WHERE token = ?", (token,))
            return None
        if user is None:
            return None
        touch_before = (
            datetime.now()
            - timedelta(seconds=max(1, self.settings.auth_session_touch_interval_seconds))
        ).replace(microsecond=0).isoformat()
        if row["last_seen_at"] <= touch_before:
            with self._write() as db:
                db.execute(
                    "UPDATE auth_sessions SET last_seen_at = ?, expires_at = ? "
                    "WHERE token = ? AND last_seen_at = ? AND expires_at > ?",
                    (now, _session_expiry(), token, row["last_seen_at"], now),
                )
        return self._user_profile(user, profile)

    def delete_session(self, token: str) -> None:
        with self._write() as db:
            db.execute("DELETE FROM auth_sessions WHERE token = ?", (token,))
