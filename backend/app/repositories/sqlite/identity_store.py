from __future__ import annotations

import json
import secrets
from datetime import datetime, timedelta
from uuid import uuid4

from app.core.config import Settings
from app.core.request_context import get_request_user
from app.models.identity import UserProfile
from app.repositories.identity_errors import (
    BuiltinAdminDemotionError,
    BuiltinAdminPasswordError,
    PasswordMismatchError,
    SelfDemotionError,
)
from app.repositories.sqlite.database import SqliteDatabase


def _now() -> str:
    return datetime.now().replace(microsecond=0).isoformat()


def _session_expiry(days: int = 30) -> str:
    return (datetime.now() + timedelta(days=days)).replace(microsecond=0).isoformat()


def _new_user_id() -> str:
    return f"user-{uuid4().hex}"


# app_settings key holding the global default per-notebook visible-document limit.
# Admin overrides the global default by writing this key; absent -> config fallback.
_UPLOAD_LIMIT_DEFAULT_KEY = "upload_document_limit_default"

# 管理员可配置的「每笔记本文档数量上限」允许区间(全局默认与 per-user 覆盖共用)。
# 下限 1(至少能放 1 篇);上限 100000(挡住误配的天文数字/负数/0)。越界由写方法
# raise ValueError,端点翻成中文用户文案。
_DOCUMENT_LIMIT_MIN = 1
_DOCUMENT_LIMIT_MAX = 100000


def _resolve_global_default(raw_value: "str | None", settings) -> int:
    """把 app_settings 里存的原始值解析成全局默认「每笔记本文档数量上限」:合法整数
    直接用,None 或非整数(坏配置)回退 config 的 USER_UPLOAD_DOCUMENT_LIMIT。这是
    app_settings 读取 + config 回退的**单一真源**——IdentityStore 的只读入口/写事务
    复算与 QueryStore 的用量列表都调它,避免两份拷贝日后静默漂移(如某处加了 clamp
    另一处漏改)。纯函数(不碰连接),让批量路径能在自己已开的连接里取值后复用同一语义。"""
    if raw_value is not None:
        try:
            return int(raw_value)
        except (TypeError, ValueError):
            pass
    return int(settings.user_upload_document_limit)


class IdentityStore:
    """SQLite identity and session persistence."""

    def __init__(
        self,
        database: SqliteDatabase,
        settings: Settings,
    ) -> None:
        self.database = database
        self.settings = settings

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
        ui_mode = "auto"
        if profile is not None and "ui_mode" in profile.keys() and profile["ui_mode"]:
            ui_mode = profile["ui_mode"]
        return UserProfile(
            id=user["id"],
            email=user["email"],
            display_name=user["display_name"],
            role=user["role"],
            username=user["username"] if "username" in user.keys() else "",
            memory_mode=profile["memory_mode"] if profile else "manual",
            domain_focus=json.loads(profile["domain_focus"]) if profile else [],
            ui_mode=ui_mode,
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

    def login_with_password(
        self, username: str, password: str
    ) -> "tuple[UserProfile, str] | None":
        """密码登录:验证与建会话在**同一写事务**内完成(codex R1 P1)。拆开的
        authenticate_user + create_session 与改密/重置存在竞态——旧密码在吊销
        DELETE 提交前完成验证、之后再插会话,该会话就带着已作废的密码活下来。
        同一写事务让登录与改密在同一把写锁上串行:登录排在改密前,它插的会话
        会被改密事务的 DELETE 带走;排在后,旧密码直接验证失败。verify 的
        PBKDF2(~30ms)因此进了写锁——登录低频,原子性优先(与改密同一取舍)。"""
        from app.services.auth_utils import normalize_username, verify_password

        norm = normalize_username(username)
        with self.database.write() as db:
            self.database.begin_immediate(db)
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
            token = secrets.token_urlsafe(32)
            now = _now()
            db.execute(
                "INSERT INTO auth_sessions (token, user_id, created_at, expires_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (token, user["id"], now, _session_expiry(), now),
            )
            return (self._user_profile(user, profile), token)

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

    def audit_labels_for_user_ids(self, user_ids) -> dict[str, str]:
        """Resolve at most 512 exact ids, in SQLite-safe chunks of at most 200."""

        ids = list(dict.fromkeys(str(value) for value in user_ids if value))[:512]
        labels: dict[str, str] = {}
        with self.database.connect() as db:
            for offset in range(0, len(ids), 200):
                chunk = ids[offset : offset + 200]
                placeholders = ",".join("?" for _ in chunk)
                rows = db.execute(
                    f"SELECT id, username, display_name FROM users "
                    f"WHERE id IN ({placeholders})",
                    chunk,
                ).fetchall()
                for row in rows:
                    username = str(row["username"] or "").strip()
                    display_name = str(row["display_name"] or "").strip()
                    labels[row["id"]] = username or display_name or row["id"]
        return labels

    def set_user_role(self, actor_id: str, user_id: str, role: str) -> dict[str, str]:
        """Assign a user/admin role with authorization rechecked in the write txn."""
        if role not in {"admin", "user"}:
            raise ValueError("invalid role")
        with self.database.write() as db:
            self.database.begin_immediate(db)
            actor = db.execute(
                "SELECT role FROM users WHERE id = ?", (actor_id,)
            ).fetchone()
            if actor is None or actor["role"] != "admin":
                raise PermissionError("admin role required")
            target = db.execute(
                "SELECT id, username, role FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if target is None:
                raise KeyError(user_id)
            if role == "user" and user_id == "user-local":
                raise BuiltinAdminDemotionError("built-in admin cannot be demoted")
            if role == "user" and user_id == actor_id:
                raise SelfDemotionError("cannot demote the active administrator")
            if target["role"] != role:
                db.execute(
                    "UPDATE users SET role = ?, updated_at = ? WHERE id = ?",
                    (role, _now(), user_id),
                )
            return {
                "id": target["id"],
                "username": target["username"] or target["id"],
                "role": role,
            }

    def set_user_ui_mode(self, user_id: str, ui_mode: str) -> UserProfile:
        """自助设置调用者自己的界面模式偏好("auto"|"advanced")；无 admin 校验(照
        request 里的 user.id 写自己那行)。镜像 create_user 的 profile 写路径:
        user_profiles 没有 user_id 的 UNIQUE 约束,不能用 INSERT ... ON CONFLICT,
        先 UPDATE、rowcount==0(缺 profile 行,理论上不会发生——create_user/_seed
        必建)再补 INSERT,与 set_user_document_limit_override 的「缺行响亮失败」
        不同:这里是自助写自己的偏好,缺行应当自愈而不是 404。"""
        if ui_mode not in {"auto", "advanced"}:
            raise ValueError("invalid ui_mode")
        now = _now()
        with self.database.write() as db:
            user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if user is None:
                raise KeyError(user_id)
            cursor = db.execute(
                "UPDATE user_profiles SET ui_mode = ?, updated_at = ? WHERE user_id = ?",
                (ui_mode, now, user_id),
            )
            if cursor.rowcount == 0:
                db.execute(
                    "INSERT INTO user_profiles "
                    "(id, user_id, memory_mode, domain_focus, ui_mode, created_at, updated_at) "
                    "VALUES (?, ?, 'manual', '[]', ?, ?, ?)",
                    (f"profile-{user_id}", user_id, ui_mode, now, now),
                )
            profile = db.execute(
                "SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)
            ).fetchone()
            return self._user_profile(user, profile)

    def change_user_password(
        self,
        user_id: str,
        old_password: str,
        new_password: str,
        *,
        keep_token: "str | None" = None,
    ) -> None:
        """自助改密:校验调用者自己提供的旧密码后写入新密码,并吊销该用户除
        `keep_token`(默认当前请求所带的会话)之外的全部会话——防止旧密码泄露的
        场景下,攻击者持有的旧会话在改密后继续有效。吊销范围只有浏览器会话
        (auth_sessions);Agent 长期凭据(agent_access_tokens)刻意不动,改密不该
        打断已授权的外部集成。内置管理员(user-local)拒绝:它的密码每次启动都被
        seed 按 settings.admin_password 重写,在线改了也会在下次重启被静默回滚。"""
        from app.services.auth_utils import hash_password, verify_password

        if user_id == "user-local":
            raise BuiltinAdminPasswordError("builtin admin password is env-derived")
        if not (new_password or "").strip():
            raise ValueError("empty password")
        # 新密码哈希不依赖事务内状态,提前算好少持约一半写锁时间;旧密码 verify
        # 必须留在事务内(要读当前行 + 原子的「校验-写入」),这 ~30ms 的 PBKDF2
        # 持锁是刻意取舍——改密低频,原子性优先。
        pw_hash, pw_salt, pw_iters = hash_password(new_password)
        with self.database.write() as db:
            self.database.begin_immediate(db)
            row = db.execute(
                "SELECT * FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if row is None:
                raise KeyError(user_id)
            if not verify_password(
                old_password,
                row["password_hash"],
                row["password_salt"],
                row["password_iterations"],
            ):
                raise PasswordMismatchError("wrong password")
            now = _now()
            db.execute(
                "UPDATE users SET password_hash = ?, password_salt = ?, "
                "password_iterations = ?, updated_at = ? WHERE id = ?",
                (pw_hash, pw_salt, pw_iters, now, user_id),
            )
            db.execute(
                "DELETE FROM auth_sessions WHERE user_id = ? AND token != ?",
                (user_id, keep_token or ""),
            )

    def admin_reset_user_password(
        self, actor_id: str, user_id: str, new_password: str
    ) -> dict[str, str]:
        """管理员重置某用户密码;目标用户的浏览器会话(auth_sessions)全部吊销,
        须用新密码重新登录(Agent 长期凭据 agent_access_tokens 刻意不动)。
        授权在写事务内按 actor 现时角色复检(镜像 set_user_role)。内置管理员
        (user-local)拒绝——理由同 change_user_password。"""
        if user_id == "user-local":
            raise BuiltinAdminPasswordError("builtin admin password is env-derived")
        if not (new_password or "").strip():
            raise ValueError("empty password")
        from app.services.auth_utils import hash_password

        # 与 change_user_password 同理:哈希提前算,少持写锁。
        pw_hash, pw_salt, pw_iters = hash_password(new_password)
        with self.database.write() as db:
            self.database.begin_immediate(db)
            actor = db.execute(
                "SELECT role FROM users WHERE id = ?", (actor_id,)
            ).fetchone()
            if actor is None or actor["role"] != "admin":
                raise PermissionError("admin role required")
            target = db.execute(
                "SELECT id, username FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if target is None:
                raise KeyError(user_id)
            now = _now()
            db.execute(
                "UPDATE users SET password_hash = ?, password_salt = ?, "
                "password_iterations = ?, updated_at = ? WHERE id = ?",
                (pw_hash, pw_salt, pw_iters, now, user_id),
            )
            db.execute(
                "DELETE FROM auth_sessions WHERE user_id = ?", (user_id,)
            )
            return {
                "id": target["id"],
                "username": target["username"] or target["id"],
            }

    # ---- 每笔记本文档数量上限:配额解析 ----
    def _global_default_from(self, db) -> int:
        """从已打开的连接 db 读 app_settings 的全局默认覆盖值,交 _resolve_global_default
        解析(缺省/坏值回退 config)。抽成接收连接的私有 helper,让只读入口
        (global_document_limit_default,自开读连接)与写事务内的复算
        (set_user_document_limit_override 清除覆盖后要算回落值,复用写连接)共用同一次读。"""
        row = db.execute(
            "SELECT value FROM app_settings WHERE key = ?",
            (_UPLOAD_LIMIT_DEFAULT_KEY,),
        ).fetchone()
        return _resolve_global_default(
            row["value"] if row is not None else None, self.settings
        )

    def global_document_limit_default(self) -> int:
        """全局默认「每笔记本文档数量上限」:优先 app_settings 的覆盖值,缺省回退
        config 的 USER_UPLOAD_DOCUMENT_LIMIT(默认 20)。存的值非法(非整数)时也回退默认,
        不让一条坏配置卡死所有上传。"""
        with self.database.connect() as db:
            return self._global_default_from(db)

    def user_document_limit_override(self, user_id: str) -> "int | None":
        """该用户的「每笔记本文档数量上限」覆盖值;NULL 或无 profile 行 → None
        (= 继承全局默认)。"""
        with self.database.connect() as db:
            row = db.execute(
                "SELECT upload_document_limit FROM user_profiles WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None or row["upload_document_limit"] is None:
            return None
        return int(row["upload_document_limit"])

    def effective_document_limit(self, user_id: "str | None") -> int:
        """用户有效上限 = COALESCE(用户覆盖值, 全局默认)。user_id 为 None(笔记本无
        owner)时直接取全局默认。"""
        if user_id is not None:
            override = self.user_document_limit_override(user_id)
            if override is not None:
                return override
        return self.global_document_limit_default()

    def notebook_owner(self, notebook_id: str) -> "tuple[str | None, str | None]":
        """(owner_id, owner_role) —— 文档数量上限的 admin 豁免与「按 owner 配额」解析。
        owner 为 admin 的笔记本不受上限约束;notebook 不存在 → (None, None)。"""
        with self.database.connect() as db:
            row = db.execute(
                "SELECT nb.created_by AS owner_id, u.role AS owner_role "
                "FROM notebooks nb LEFT JOIN users u ON u.id = nb.created_by "
                "WHERE nb.id = ?",
                (notebook_id,),
            ).fetchone()
        if row is None:
            return (None, None)
        return (row["owner_id"], row["owner_role"])

    # ---- 每笔记本文档数量上限:管理员写路径(镜像 set_user_role 的写事务复检) ----
    def set_global_document_limit_default(
        self, actor_id: str, value: int
    ) -> dict[str, int]:
        """管理员设置全局默认「每笔记本文档数量上限」(写 app_settings)。授权在写
        事务内按 actor 现时角色复检(与 set_user_role 一致,避免读到已被降权的旧角色);
        value 范围 1..100000,越界 → ValueError(由端点翻成用户文案)。"""
        if not _DOCUMENT_LIMIT_MIN <= value <= _DOCUMENT_LIMIT_MAX:
            raise ValueError("document limit out of range")
        now = _now()
        with self.database.write() as db:
            self.database.begin_immediate(db)
            actor = db.execute(
                "SELECT role FROM users WHERE id = ?", (actor_id,)
            ).fetchone()
            if actor is None or actor["role"] != "admin":
                raise PermissionError("admin role required")
            db.execute(
                "INSERT INTO app_settings(key, value, updated_at) VALUES(?, ?, ?) "
                "ON CONFLICT(key) DO UPDATE SET value = excluded.value, "
                "updated_at = excluded.updated_at",
                (_UPLOAD_LIMIT_DEFAULT_KEY, str(value), now),
            )
            return {"limit": value}

    def set_user_document_limit_override(
        self, actor_id: str, user_id: str, value: "int | None"
    ) -> dict:
        """管理员给某用户设/清「每笔记本文档数量上限」覆盖值(写
        user_profiles.upload_document_limit;value=None → SQL NULL = 清除覆盖、
        回落全局默认)。授权写事务内复检;value 非空时范围 1..100000;目标用户不存在
        → KeyError(端点翻成 404)。返回改动后的生效上限与是否仍为覆盖值。"""
        if value is not None and not _DOCUMENT_LIMIT_MIN <= value <= _DOCUMENT_LIMIT_MAX:
            raise ValueError("document limit out of range")
        now = _now()
        with self.database.write() as db:
            self.database.begin_immediate(db)
            actor = db.execute(
                "SELECT role FROM users WHERE id = ?", (actor_id,)
            ).fetchone()
            if actor is None or actor["role"] != "admin":
                raise PermissionError("admin role required")
            target = db.execute(
                "SELECT id, username FROM users WHERE id = ?", (user_id,)
            ).fetchone()
            if target is None:
                raise KeyError(user_id)
            # 目标用户已在上面确认存在;create_user/种子必建 user_profiles 行,故
            # UPDATE 正常命中一行(NULL 覆盖即清除)。防御:若 profile 行竟缺失(仅
            # 迁移前遗留库/外部改库可能),rowcount==0 → 响亮失败(端点翻成 404),
            # 胜过静默 no-op 却回 200 谎报「已设置」(下次读又回落默认)。
            cursor = db.execute(
                "UPDATE user_profiles SET upload_document_limit = ?, updated_at = ? "
                "WHERE user_id = ?",
                (value, now, user_id),
            )
            if cursor.rowcount == 0:
                raise KeyError(user_id)
            # 清除覆盖后的生效值 = 全局默认;复用写连接算,与 global_document_limit_default
            # 同一份回退逻辑(单一真源)。
            effective = value if value is not None else self._global_default_from(db)
            return {
                "id": target["id"],
                "username": target["username"] or target["id"],
                "upload_limit": effective,
                "upload_limit_overridden": value is not None,
            }
