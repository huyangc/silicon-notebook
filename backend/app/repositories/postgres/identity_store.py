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
    BuiltinAdminPasswordError,
    PasswordMismatchError,
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
from app.domain.search_profile import (
    SEARCH_PROFILE_ORIGINS,
    merge_field,
    parse_search_profile,
    serialize_search_profile,
)
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
        # Agentic Memory P3(T6):同 SQLite 侧,parse_search_profile 统一
        # fail-open,非空 fields 才挂到 UserProfile 上(NULL/畸形文档都折成
        # None,与"从未设置"同一展示口径)。
        parsed_search_profile = parse_search_profile(
            profile_row.get("search_profile_json") if profile_row else None
        )
        search_profile = parsed_search_profile if parsed_search_profile["fields"] else None
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
            search_profile=search_profile,
        )

    def _create_user_in_txn(self, connection, username: str, password: str):
        """在调用方已打开的写事务内建用户+profile,返回 (user_row, profile_row)。
        供 create_user 与 register_user_with_session 共用。UniqueViolation 的
        翻译留在公开方法(异常在 execute 时抛出,穿过本 helper 上抛)。"""
        from app.domain.auth_utils import hash_password, is_valid_username, normalize_username

        if not is_valid_username(username):
            raise ValueError("invalid username")
        normalized = normalize_username(username)
        user_id = f"user-{uuid4().hex}"
        now = utc_now()
        password_hash, password_salt, iterations = hash_password(password)
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
        return user, profile

    def _insert_session_in_txn(self, connection, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        now = utc_now()
        connection.execute(
            "INSERT INTO auth_sessions(token,user_id,created_at,expires_at,last_seen_at) "
            "VALUES (%s,%s,%s,%s,%s)",
            (token, user_id, now, now + timedelta(days=30), now),
        )
        return token

    def create_user(self, username: str, password: str) -> UserProfile:
        try:
            with self.database.write() as connection:
                user, profile = self._create_user_in_txn(connection, username, password)
        except errors.UniqueViolation as exc:
            if exc.diag.constraint_name in {"idx_users_username", "uq_users_email"}:
                raise ValueError("username already exists") from exc
            raise
        return self._user_profile(user, profile)

    def register_user_with_session(
        self, username: str, password: str
    ) -> tuple[UserProfile, str]:
        """注册+发首个会话在同一写事务(codex R2 P2):拆开的 create_user +
        create_session 会让管理员重置恰好落在两次提交之间——重置的 DELETE 扫不到
        任何会话,注册随后插入的会话带着已被重置的密码活下来。语义与 SQLite 侧
        逐字一致。"""
        try:
            with self.database.write() as connection:
                user, profile = self._create_user_in_txn(connection, username, password)
                token = self._insert_session_in_txn(connection, user["id"])
        except errors.UniqueViolation as exc:
            if exc.diag.constraint_name in {"idx_users_username", "uq_users_email"}:
                raise ValueError("username already exists") from exc
            raise
        return (self._user_profile(user, profile), token)

    def authenticate_user(self, username: str, password: str) -> UserProfile | None:
        from app.domain.auth_utils import normalize_username, verify_password

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

    def login_with_password(
        self, username: str, password: str
    ) -> "tuple[UserProfile, str] | None":
        """密码登录:验证与建会话在同一写事务内、对 users 行 FOR UPDATE(codex
        R1 P1)。改密/重置同样先 FOR UPDATE 该行,两者因此在行锁上串行——登录
        排在改密前,插的会话会被改密的 DELETE 带走;排在后,旧密码直接失败。
        语义与 SQLite 侧逐字一致。"""
        from app.domain.auth_utils import normalize_username, verify_password

        with self.database.write() as db:
            user = db.execute(
                "SELECT * FROM users WHERE username=%s FOR UPDATE",
                (normalize_username(username),),
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
                "SELECT * FROM user_profiles WHERE user_id=%s", (user["id"],)
            ).fetchone()
            return (self._user_profile(user, profile), self._insert_session_in_txn(db, user["id"]))

    def create_session(self, user_id: str) -> str:
        with self.database.write() as connection:
            return self._insert_session_in_txn(connection, user_id)

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

    def _lock_actor_and_target(self, db, actor_id: str, user_id: str):
        """管理员对目标用户的写路径统一按 id 序一次锁齐 actor/target 两行
        (codex R2/R3):逐行 FOR UPDATE 的「先 actor 后 target」在两个管理员
        互相操作、或同一管理员的两条不同 admin 路径并发时,会以相反顺序等待
        对方持有的行而死锁。ORDER BY id 让所有 admin 变更(角色/文档上限/
        密码重置)共用同一加锁顺序;actor==target 时 IN 去重命中同一行。
        actor 缺失或非 admin → PermissionError;返回 (actor_row, target_row|None),
        目标缺失由调用方按各自语义处理(通常 KeyError → 404)。"""
        rows = db.execute(
            "SELECT id,username,role FROM users WHERE id IN (%s,%s) "
            "ORDER BY id FOR UPDATE",
            (actor_id, user_id),
        ).fetchall()
        by_id = {row["id"]: row for row in rows}
        actor = by_id.get(actor_id)
        if actor is None or actor["role"] != "admin":
            raise PermissionError("admin role required")
        return actor, by_id.get(user_id)

    def set_user_role(self, actor_id: str, user_id: str, role: str) -> dict[str, str]:
        if role not in {"admin", "user"}:
            raise ValueError("invalid role")
        with self.database.write() as db:
            _actor, target = self._lock_actor_and_target(db, actor_id, user_id)
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
        ⚠不能只按派生 id 做 upsert:种子管理员的 profile 行是 `profile-local`
        而非 f"profile-{user_id}" 形状(seed 路径早于派生约定)，按 id 冲突会给同一个
        user_id 插出第二行,后续 `WHERE user_id=%s` 的读取无确定性排序,偏好看起来
        会"回退"(codex R1 P1)。因此先按 user_id UPDATE(覆盖种子行与派生行两种形状)，
        缺行(理论上不会发生——create_user/_seed 必建)再补 INSERT;补插保留
        ON CONFLICT (id) DO UPDATE,吞掉「两个并发请求都 UPDATE 到 0 行后同时补插」
        的竞态。SQLite 侧本就按 user_id UPDATE 且有全局写锁串行化，保持不变。"""
        if ui_mode not in {"auto", "advanced"}:
            raise ValueError("invalid ui_mode")
        now = utc_now()
        with self.database.write() as db:
            user = db.execute("SELECT * FROM users WHERE id=%s", (user_id,)).fetchone()
            if user is None:
                raise KeyError(user_id)
            profile = db.execute(
                "UPDATE user_profiles SET ui_mode=%s,updated_at=%s "
                "WHERE user_id=%s RETURNING *",
                (ui_mode, now, user_id),
            ).fetchone()
            if profile is None:
                profile = db.execute(
                    "INSERT INTO user_profiles "
                    "(id,user_id,memory_mode,domain_focus,ui_mode,created_at,updated_at) "
                    "VALUES (%s,%s,'manual',%s,%s,%s,%s) "
                    "ON CONFLICT (id) DO UPDATE SET ui_mode=EXCLUDED.ui_mode,updated_at=EXCLUDED.updated_at "
                    "RETURNING *",
                    (f"profile-{user_id}", user_id, jsonb([]), ui_mode, now, now),
                ).fetchone()
            return self._user_profile(user, profile)

    def set_user_search_profile(
        self, user_id: str, fields: "dict", origin: str
    ) -> UserProfile:
        """读-改-写 ``user_profiles.search_profile_json`` 里的 ``fields``
        (Agentic Memory P3, T6)。规则(job 不覆盖用户已写字段、value=None
        清空字段)全部在 ``search_profile.merge_field`` 里,这里只负责把
        「读、逐字段合并、序列化、写」钉在**同一个** ``write()`` 事务内。

        ⚠ 与 SQLite 侧的关键差异:SQLite 靠进程级 write_lock 天然把「用户
        编辑」与「后台归纳」两个写者串行化到同一个 ``write()`` 块的粒度;
        PostgreSQL 每个连接互相独立、没有这道进程锁,必须显式 ``SELECT ...
        FOR UPDATE`` 锁住这一行,否则两个并发事务会各自读到同一份旧文档、
        后提交的一方整体覆盖前一方刚合并进去的字段(lost-update)。

        缺 profile 行时的 upsert 形状镜像 ``set_user_ui_mode`` 那条同款
        codex R1 P1 教训:先按 ``user_id`` 尝试 UPDATE(能覆盖种子行与派生
        id 行两种形状),缺行才补 INSERT 且保留 ``ON CONFLICT (id) DO
        UPDATE`` 吞掉两个并发请求都 UPDATE 到 0 行后同时补插的竞态——但
        这条竞态本身只发生在 profile 行整个不存在的边界情形;一旦行存在,
        ``FOR UPDATE`` 才是本方法真正的并发保证。目标用户不存在 →
        KeyError。

        T9 修复轮(P2-6,镜像 SQLite 侧同名方法):合并结果与既存
        ``search_profile_json`` 逐字相同时跳过 ``UPDATE`` 本身,直接复用
        ``FOR UPDATE`` 已经锁定/读到的那一行——比较是纯字符串比较,不额外
        查询;省下的是一次 ``UPDATE`` 语句(以及它顺带推进的
        ``updated_at``)。行锁本身仍然照常持有到事务结束,这条优化只省写,
        不省锁。"""
        if origin not in SEARCH_PROFILE_ORIGINS:
            raise ValueError("invalid search-profile origin")
        now = utc_now()
        with self.database.write() as db:
            # codex #535 R7 P2:profile 行还不存在时,对它的 FOR UPDATE 锁不到
            # 任何东西——两个并发首写各自对空文档 merge,后提交的 ON CONFLICT 会
            # 整份覆盖先提交的字段。先锁**父 users 行**(合法用户恒存在)把缺行
            # 情形也串行化;行已存在时多这把锁无害(同一事务内的另一行锁)。
            user = db.execute(
                "SELECT * FROM users WHERE id=%s FOR UPDATE", (user_id,)
            ).fetchone()
            if user is None:
                raise KeyError(user_id)
            row = db.execute(
                "SELECT * FROM user_profiles WHERE user_id=%s FOR UPDATE", (user_id,)
            ).fetchone()
            raw = row.get("search_profile_json") if row is not None else None
            profile_doc = parse_search_profile(raw)
            for field, value in fields.items():
                profile_doc = merge_field(profile_doc, field, value, origin)
            serialized = serialize_search_profile(profile_doc)
            if row is not None and serialized == raw:
                return self._user_profile(user, row)
            if row is not None:
                profile = db.execute(
                    "UPDATE user_profiles SET search_profile_json=%s,updated_at=%s "
                    "WHERE user_id=%s RETURNING *",
                    (serialized, now, user_id),
                ).fetchone()
            else:
                profile = db.execute(
                    "INSERT INTO user_profiles "
                    "(id,user_id,memory_mode,domain_focus,search_profile_json,"
                    "created_at,updated_at) "
                    "VALUES (%s,%s,'manual',%s,%s,%s,%s) "
                    "ON CONFLICT (id) DO UPDATE SET "
                    "search_profile_json=EXCLUDED.search_profile_json,"
                    "updated_at=EXCLUDED.updated_at "
                    "RETURNING *",
                    (f"profile-{user_id}", user_id, jsonb([]), serialized, now, now),
                ).fetchone()
            return self._user_profile(user, profile)

    def get_user_search_profile(self, user_id: str) -> "dict | None":
        """一次主键点读该用户的检索/回答风格偏好**文档**(Agentic Memory P3,
        T8)——镜像 SQLite 侧同名方法:不是整个 ``UserProfile``,不 join
        ``users`` 表,只读 ``search_profile_json`` 一列供
        ``render_style_block`` 消费。空 ``user_id``、行不存在、列缺失(旧库
        未跑迁移)与畸形 JSON 全部 fail-open 到 ``None``。只读,不需要
        ``FOR UPDATE``(没有读-改-写)。"""
        if not user_id:
            return None
        with self.database.connect() as connection:
            row = connection.execute(
                "SELECT search_profile_json FROM user_profiles WHERE user_id=%s",
                (user_id,),
            ).fetchone()
        if row is None:
            return None
        parsed = parse_search_profile(row.get("search_profile_json"))
        return parsed if parsed["fields"] else None

    def change_user_password(
        self,
        user_id: str,
        old_password: str,
        new_password: str,
        *,
        keep_token: "str | None" = None,
    ) -> None:
        """自助改密:校验调用者自己提供的旧密码后写入新密码,并吊销该用户除
        `keep_token`(默认当前请求所带的会话)之外的全部会话。吊销范围只有浏览器
        会话(auth_sessions),Agent 长期凭据刻意不动;内置管理员(user-local)拒绝
        ——它的密码每次启动都被 seed 重写,语义与 SQLite 侧逐字一致。"""
        from app.domain.auth_utils import hash_password, verify_password

        if user_id == "user-local":
            raise BuiltinAdminPasswordError("builtin admin password is env-derived")
        if not (new_password or "").strip():
            raise ValueError("empty password")
        # 新密码哈希不依赖事务内状态,提前算好缩短持锁时间(镜像 SQLite 侧)。
        pw_hash, pw_salt, pw_iters = hash_password(new_password)
        with self.database.write() as db:
            row = db.execute(
                "SELECT * FROM users WHERE id=%s FOR UPDATE", (user_id,)
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
            db.execute(
                "UPDATE users SET password_hash=%s,password_salt=%s,"
                "password_iterations=%s,updated_at=%s WHERE id=%s",
                (pw_hash, pw_salt, pw_iters, utc_now(), user_id),
            )
            db.execute(
                "DELETE FROM auth_sessions WHERE user_id=%s AND token!=%s",
                (user_id, keep_token or ""),
            )

    def admin_reset_user_password(
        self, actor_id: str, user_id: str, new_password: str
    ) -> dict[str, str]:
        """管理员重置某用户密码;目标用户的浏览器会话(auth_sessions)全部吊销,
        须用新密码重新登录(Agent 长期凭据刻意不动)。授权在写事务内按 actor
        现时角色复检(镜像 set_user_role);内置管理员(user-local)拒绝。"""
        if user_id == "user-local":
            raise BuiltinAdminPasswordError("builtin admin password is env-derived")
        if not (new_password or "").strip():
            raise ValueError("empty password")
        from app.domain.auth_utils import hash_password

        # 镜像 change_user_password:哈希提前算,缩短持锁时间。
        pw_hash, pw_salt, pw_iters = hash_password(new_password)
        with self.database.write() as db:
            _actor, target = self._lock_actor_and_target(db, actor_id, user_id)
            if target is None:
                raise KeyError(user_id)
            db.execute(
                "UPDATE users SET password_hash=%s,password_salt=%s,"
                "password_iterations=%s,updated_at=%s WHERE id=%s",
                (pw_hash, pw_salt, pw_iters, utc_now(), user_id),
            )
            db.execute("DELETE FROM auth_sessions WHERE user_id=%s", (user_id,))
            return {
                "id": target["id"],
                "username": target["username"] or target["id"],
            }

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
            _actor, target = self._lock_actor_and_target(db, actor_id, user_id)
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
