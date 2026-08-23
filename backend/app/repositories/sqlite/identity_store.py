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
from app.domain.search_profile import (
    SEARCH_PROFILE_ORIGINS,
    merge_field,
    parse_search_profile,
    serialize_search_profile,
)


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
        # Agentic Memory P3(T6):search_profile 走 parse_search_profile 统一
        # fail-open——列缺失(旧库尚未跑迁移)/NULL/畸形 JSON 全部落到空 profile,
        # 再折成 None(同 NULL 一个展示口径:"没有可展示的偏好")。非空 fields
        # 才把解析后的整份文档挂到 UserProfile 上。
        search_profile = None
        if profile is not None and "search_profile_json" in profile.keys():
            parsed = parse_search_profile(profile["search_profile_json"])
            if parsed["fields"]:
                search_profile = parsed
        return UserProfile(
            id=user["id"],
            email=user["email"],
            display_name=user["display_name"],
            role=user["role"],
            username=user["username"] if "username" in user.keys() else "",
            memory_mode=profile["memory_mode"] if profile else "manual",
            domain_focus=json.loads(profile["domain_focus"]) if profile else [],
            ui_mode=ui_mode,
            search_profile=search_profile,
        )

    def _create_user_in_txn(self, db, username: str, password: str) -> UserProfile:
        """在调用方已打开的写事务内建用户+profile。供 create_user 与
        register_user_with_session 共用,后者要求「建用户+发首个会话」原子。"""
        from app.domain.auth_utils import hash_password, is_valid_username, normalize_username

        if not is_valid_username(username):
            raise ValueError("invalid username")
        norm = normalize_username(username)
        user_id = _new_user_id()
        now = _now()
        pw_hash, pw_salt, pw_iters = hash_password(password)
        email = f"{norm}@users.silicon-notebook.local"
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

    def _insert_session_in_txn(self, db, user_id: str) -> str:
        token = secrets.token_urlsafe(32)
        now = _now()
        db.execute(
            "INSERT INTO auth_sessions (token, user_id, created_at, expires_at, last_seen_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (token, user_id, now, _session_expiry(), now),
        )
        return token

    def create_user(self, username: str, password: str) -> UserProfile:
        with self.database.write() as db:
            return self._create_user_in_txn(db, username, password)

    def register_user_with_session(
        self, username: str, password: str
    ) -> tuple[UserProfile, str]:
        """注册+发首个会话在同一写事务(codex R2 P2):拆开的 create_user +
        create_session 会让管理员重置恰好落在两次提交之间——重置的 DELETE 扫不到
        任何会话,注册随后插入的会话带着已被重置的密码活下来。原子化后重置只能
        排在整个注册之前(目标不存在,404)或之后(会话已在,吊销扫得到)。"""
        with self.database.write() as db:
            profile = self._create_user_in_txn(db, username, password)
            return (profile, self._insert_session_in_txn(db, profile.id))

    def authenticate_user(self, username: str, password: str) -> UserProfile | None:
        from app.domain.auth_utils import normalize_username, verify_password

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
        from app.domain.auth_utils import normalize_username, verify_password

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
            return (self._user_profile(user, profile), self._insert_session_in_txn(db, user["id"]))

    def create_session(self, user_id: str) -> str:
        with self.database.write() as db:
            return self._insert_session_in_txn(db, user_id)

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

    def set_user_search_profile(
        self, user_id: str, fields: "dict", origin: str
    ) -> UserProfile:
        """读-改-写 ``user_profiles.search_profile_json`` 里的 ``fields``
        (Agentic Memory P3, T6)。``origin="user"`` 是自助编辑(PATCH
        /me/search-profile),``origin="job"`` 是 T7 归纳 job 的写入;两者的
        合并规则(job 不覆盖已被用户写过的字段、value=None 清空字段)全部在
        ``search_profile.merge_field`` 里,这里只负责把「读、逐字段合并、
        序列化、写」钉在**同一个** ``write()`` 块内。

        SQLite 的 ``write()`` 本身就是进程内写串行(write_lock),同一进程里
        「用户编辑」与「后台归纳」两个写者天然互斥在这一个块的粒度上,不需要
        像 PostgreSQL 侧那样再显式 ``FOR UPDATE``——但读、合并、写仍必须留在
        同一个块里,拆成两次独立的 ``write()`` 调用会在两次调用之间打开一个
        「两个写者都读到旧文档、后写者整体覆盖前写者刚提交的字段」的
        lost-update 窗口,即使锁本身是串行的。

        缺 profile 行(理论上不会发生——create_user/_seed 必建)时补插一行,
        镜像 ``set_user_ui_mode`` 的自愈语义(自助写自己的偏好不该因为一行
        缺失的种子数据就 404)。目标用户不存在 → KeyError。

        T9 修复轮(P2-6):合并结果与既存 ``search_profile_json`` 逐字相同时
        (最常见的形状——T7 job 每次触发都重新归纳同一个已经写过的
        ``answer_language``,连续两次 job 写入十有八九产出相同的赢家)跳过
        ``UPDATE`` 本身,只回读现有行。这个读本来就发生在同一个块里(上面
        的 ``SELECT``),比较是纯字符串比较,零额外查询;省下的是一次
        ``UPDATE`` 语句本身以及它顺带推进的 ``updated_at``——一个语义不变
        的写入不该让「最后编辑时间」跳动,也不该占用 SQLite 那把进程内
        write_lock 哪怕多一条语句的时间。"""
        if origin not in SEARCH_PROFILE_ORIGINS:
            raise ValueError("invalid search-profile origin")
        now = _now()
        with self.database.write() as db:
            user = db.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
            if user is None:
                raise KeyError(user_id)
            row = db.execute(
                "SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)
            ).fetchone()
            raw = row["search_profile_json"] if row is not None else None
            profile_doc = parse_search_profile(raw)
            for field, value in fields.items():
                profile_doc = merge_field(profile_doc, field, value, origin)
            serialized = serialize_search_profile(profile_doc)
            if row is not None and serialized == raw:
                return self._user_profile(user, row)
            cursor = db.execute(
                "UPDATE user_profiles SET search_profile_json = ?, updated_at = ? "
                "WHERE user_id = ?",
                (serialized, now, user_id),
            )
            if cursor.rowcount == 0:
                db.execute(
                    "INSERT INTO user_profiles "
                    "(id, user_id, memory_mode, domain_focus, search_profile_json, "
                    "created_at, updated_at) VALUES (?, ?, 'manual', '[]', ?, ?, ?)",
                    (f"profile-{user_id}", user_id, serialized, now, now),
                )
            profile = db.execute(
                "SELECT * FROM user_profiles WHERE user_id = ?", (user_id,)
            ).fetchone()
            return self._user_profile(user, profile)

    def get_user_search_profile(self, user_id: str) -> "dict | None":
        """一次主键点读该用户的检索/回答风格偏好**文档**(Agentic Memory P3,
        T8)——不是整个 ``UserProfile``,不 join ``users`` 表:调用方
        (``AskService._search_profile_style_block``/``ReasoningRetriever.run()``)
        只需要喂给 ``render_style_block`` 的这一份 JSON。空 ``user_id``、行不
        存在、列缺失(旧库未跑迁移)与畸形 JSON 全部 fail-open 到 ``None``,
        与 ``_user_profile`` 静默降级的口径一致——见 ``parse_search_profile``
        的 fail-open 矩阵。"""
        if not user_id:
            return None
        with self.database.connect() as db:
            row = db.execute(
                "SELECT search_profile_json FROM user_profiles WHERE user_id = ?",
                (user_id,),
            ).fetchone()
        if row is None or "search_profile_json" not in row.keys():
            return None
        parsed = parse_search_profile(row["search_profile_json"])
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
        `keep_token`(默认当前请求所带的会话)之外的全部会话——防止旧密码泄露的
        场景下,攻击者持有的旧会话在改密后继续有效。吊销范围只有浏览器会话
        (auth_sessions);Agent 长期凭据(agent_access_tokens)刻意不动,改密不该
        打断已授权的外部集成。内置管理员(user-local)拒绝:它的密码每次启动都被
        seed 按 settings.admin_password 重写,在线改了也会在下次重启被静默回滚。"""
        from app.domain.auth_utils import hash_password, verify_password

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
        from app.domain.auth_utils import hash_password

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
