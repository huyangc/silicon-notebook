from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from typing import Any
from uuid import uuid4

from psycopg import errors

from app.core.config import Settings
from app.core.request_context import get_request_user
from app.models.identity import UserProfile
from app.repositories.identity_errors import (
    BuiltinAdminDemotionError,
    SelfDemotionError,
)
from app.repositories.postgres._store_utils import (
    iso_timestamp,
    json_value,
    jsonb,
    placeholders,
    utc_now,
)
from app.repositories.postgres.database import PostgresDatabase
_UPLOAD_LIMIT_DEFAULT_KEY = "upload_document_limit_default"
_DOCUMENT_LIMIT_MIN = 1
_DOCUMENT_LIMIT_MAX = 100000


def _resolve_global_default(raw_value: "str | None", settings: Settings) -> int:
    if raw_value is not None:
        try:
            return int(raw_value)
        except (TypeError, ValueError):
            pass
    return int(settings.user_upload_document_limit)


class IdentityStore:
    """PostgreSQL identity, session, and per-user model-settings persistence."""

    def __init__(
        self,
        database: PostgresDatabase,
        settings: Settings,
    ) -> None:
        self.database = database
        self.settings = settings

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
        ui_mode = (profile_row.get("ui_mode") if profile_row else None) or "auto"
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
            ui_mode=ui_mode,
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

    def audit_labels_for_user_ids(self, user_ids) -> dict[str, str]:
        """PostgreSQL parity for bounded legacy audit-id resolution."""

        ids = list(dict.fromkeys(str(value) for value in user_ids if value))[:512]
        labels: dict[str, str] = {}
        with self.database.connect() as connection:
            for offset in range(0, len(ids), 200):
                chunk = ids[offset : offset + 200]
                rows = connection.execute(
                    "SELECT id, username, display_name FROM users WHERE id = ANY(%s)",
                    (chunk,),
                ).fetchall()
                for row in rows:
                    username = str(row["username"] or "").strip()
                    display_name = str(row["display_name"] or "").strip()
                    labels[row["id"]] = username or display_name or row["id"]
        return labels

    def set_user_role(self, actor_id: str, user_id: str, role: str) -> dict[str, str]:
        if role not in {"admin", "user"}:
            raise ValueError("invalid role")
        with self.database.write() as db:
            actor = db.execute(
                "SELECT role FROM users WHERE id=%s FOR UPDATE", (actor_id,)
            ).fetchone()
            if actor is None or actor["role"] != "admin":
                raise PermissionError("admin role required")
            target = db.execute(
                "SELECT id,username,role FROM users WHERE id=%s FOR UPDATE",
                (user_id,),
            ).fetchone()
            if target is None:
                raise KeyError(user_id)
            if role == "user" and user_id == "user-local":
                raise BuiltinAdminDemotionError("built-in admin cannot be demoted")
            if role == "user" and user_id == actor_id:
                raise SelfDemotionError("cannot demote the active administrator")
            if target["role"] != role:
                db.execute(
                    "UPDATE users SET role=%s,updated_at=%s WHERE id=%s",
                    (role, utc_now(), user_id),
                )
            return {
                "id": target["id"],
                "username": target["username"] or target["id"],
                "role": role,
            }

    def set_user_ui_mode(self, user_id: str, ui_mode: str) -> UserProfile:
        """自助设置调用者自己的界面模式偏好("auto"|"advanced")；无 admin 校验。
        user_profiles 的 id 是确定性派生的 f"profile-{user_id}"(全部创建路径一致，见
        bundle.py 注册流程与本文件的 create 分支)且是主键，所以可以直接对 id 做
        INSERT ... ON CONFLICT DO UPDATE 原子 upsert，不必先 UPDATE 再按 rowcount==0
        探测着补 INSERT——那种两步写法在并发下有一条竞态窗口:两个请求都 UPDATE 到
        rowcount==0 后各自尝试 INSERT，后者撞主键失败。SQLite 侧因为有全局写锁把
        整个方法串行化，不存在这条竞态，因此保留原实现不变。"""
        if ui_mode not in {"auto", "advanced"}:
            raise ValueError("invalid ui_mode")
        now = utc_now()
        with self.database.write() as db:
            user = db.execute("SELECT * FROM users WHERE id=%s", (user_id,)).fetchone()
            if user is None:
                raise KeyError(user_id)
            profile = db.execute(
                "INSERT INTO user_profiles "
                "(id,user_id,memory_mode,domain_focus,ui_mode,created_at,updated_at) "
                "VALUES (%s,%s,'manual',%s,%s,%s,%s) "
                "ON CONFLICT (id) DO UPDATE SET ui_mode=EXCLUDED.ui_mode,updated_at=EXCLUDED.updated_at "
                "RETURNING *",
                (f"profile-{user_id}", user_id, jsonb([]), ui_mode, now, now),
            ).fetchone()
            return self._user_profile(user, profile)

    def _global_default_from(self, db) -> int:
        row = db.execute(
            "SELECT value FROM app_settings WHERE key=%s",
            (_UPLOAD_LIMIT_DEFAULT_KEY,),
        ).fetchone()
        return _resolve_global_default(
            row["value"] if row is not None else None, self.settings
        )

    def global_document_limit_default(self) -> int:
        with self.database.connect() as db:
            return self._global_default_from(db)

    def user_document_limit_override(self, user_id: str) -> "int | None":
        with self.database.connect() as db:
            row = db.execute(
                "SELECT upload_document_limit FROM user_profiles WHERE user_id=%s",
                (user_id,),
            ).fetchone()
        if row is None or row["upload_document_limit"] is None:
            return None
        return int(row["upload_document_limit"])

    def effective_document_limit(self, user_id: "str | None") -> int:
        if user_id is not None:
            override = self.user_document_limit_override(user_id)
            if override is not None:
                return override
        return self.global_document_limit_default()

    def notebook_owner(self, notebook_id: str) -> "tuple[str | None, str | None]":
        with self.database.connect() as db:
            row = db.execute(
                "SELECT nb.created_by AS owner_id,u.role AS owner_role "
                "FROM notebooks nb LEFT JOIN users u ON u.id=nb.created_by "
                "WHERE nb.id=%s",
                (notebook_id,),
            ).fetchone()
        if row is None:
            return (None, None)
        return (row["owner_id"], row["owner_role"])

    def set_global_document_limit_default(
        self, actor_id: str, value: int
    ) -> dict[str, int]:
        if not _DOCUMENT_LIMIT_MIN <= value <= _DOCUMENT_LIMIT_MAX:
            raise ValueError("document limit out of range")
        with self.database.write() as db:
            actor = db.execute(
                "SELECT role FROM users WHERE id=%s FOR UPDATE", (actor_id,)
            ).fetchone()
            if actor is None or actor["role"] != "admin":
                raise PermissionError("admin role required")
            db.execute(
                "INSERT INTO app_settings(key,value,updated_at) VALUES(%s,%s,%s) "
                "ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                "updated_at=excluded.updated_at",
                (_UPLOAD_LIMIT_DEFAULT_KEY, str(value), utc_now()),
            )
        return {"limit": value}

    def set_user_document_limit_override(
        self, actor_id: str, user_id: str, value: "int | None"
    ) -> dict:
        if value is not None and not _DOCUMENT_LIMIT_MIN <= value <= _DOCUMENT_LIMIT_MAX:
            raise ValueError("document limit out of range")
        with self.database.write() as db:
            actor = db.execute(
                "SELECT role FROM users WHERE id=%s FOR UPDATE", (actor_id,)
            ).fetchone()
            if actor is None or actor["role"] != "admin":
                raise PermissionError("admin role required")
            target = db.execute(
                "SELECT id,username FROM users WHERE id=%s FOR UPDATE", (user_id,)
            ).fetchone()
            if target is None:
                raise KeyError(user_id)
            cursor = db.execute(
                "UPDATE user_profiles SET upload_document_limit=%s,updated_at=%s "
                "WHERE user_id=%s",
                (value, utc_now(), user_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(user_id)
            effective = value if value is not None else self._global_default_from(db)
            return {
                "id": target["id"],
                "username": target["username"] or target["id"],
                "upload_limit": effective,
                "upload_limit_overridden": value is not None,
            }
