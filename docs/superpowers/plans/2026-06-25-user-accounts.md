# 用户系统（注册 + 密码登录 + owner 隔离 + admin/base） Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把当前硬编码单用户（`user-local`）升级为「自助注册 + 密码登录 + 笔记本按 owner 隔离」，现有 notebook 归升级后的 admin，base 层 KG 由 admin 维护、对普通用户隐藏但问答仍检索。

**Architecture:** 后端 FastAPI + SQLite。新增 `auth_sessions` 表与 `users` 的 username/password 列；`get_current_user` 依赖解析 Bearer token、写 `ContextVar`，`current_user()` 读它（未设回退 admin）；notebook 路由加 `require_notebook_access` 守卫，`list_notebooks` 按 `created_by` 过滤；admin = 原地升级的 `user-local`（id 不变，零数据搬运）。前端单页加登录门 + token 注入 + 401 处理。

**Tech Stack:** FastAPI / pydantic-settings / sqlite3 / `hashlib.pbkdf2_hmac` + `secrets`（无新依赖）；Next.js / React / TypeScript；pytest + `node --test`。

**Spec:** `docs/superpowers/specs/2026-06-25-user-accounts-design.md`

**约定（每个 task 通用）：**
- 后端测试从 `backend/` 跑：`PYBIN='/opt/homebrew/Caskroom/miniconda/base/bin/python'`（或 `$PYTHON_BIN`）；命令形如 `cd backend && $PYBIN -m pytest tests/test_xxx.py -v`（cwd=backend 才能 `import app`）。
- 前端测试 `cd frontend && npm run test`（`node --test app/*.test.mjs`）；类型检查 `npm run lint`（`tsc --noEmit`）；构建 `npm run build`。
- 整体门：`bash scripts/check.sh`。
- 所有工作在 worktree `serene-khayyam-0c204e`（分支 `claude/serene-khayyam-0c204e`）内完成。
- **关键不变量**：admin 的 `users.id` 始终是 `'user-local'`（只改 role / 加 username），现有 `created_by='user-local'` 数据零迁移。

---

## File Structure

**新建：**
- `backend/app/services/auth_utils.py` — 纯函数：用户名正则/归一化、pbkdf2 哈希/校验。
- `backend/app/api/deps.py` — `repository()` 单例（从 routes 迁入）、`get_current_user`（async yield 依赖，设/复位 ContextVar）、`require_notebook_access`。
- `backend/app/api/auth_routes.py` — `auth_router`：`POST /auth/register|login|logout`。
- `backend/tests/conftest.py` — 顶层 conftest：测试进程默认 `auth_optional`。
- `backend/tests/test_auth.py`、`backend/tests/test_user_isolation.py` — 新测试。
- `frontend/app/auth.ts` + `frontend/app/auth.test.mjs` — token 存储、auth API、用户名校验。
- `frontend/app/AuthGate.tsx` — 登录/注册门组件。

**修改：**
- `backend/app/core/config.py` — 加 `admin_password` / `auth_optional`。
- `backend/app/models/schemas.py` — `UserProfile.username`；新增 `AuthRequest` / `AuthResult`。
- `backend/app/services/sqlite_repository.py` — 迁移列 + seed 升级 admin + ContextVar/setters + user/session 方法 + notebook owner 过滤/守卫助手。
- `backend/app/api/routes.py` — `repository` 改从 deps 导入；notebook 路由挂 `require_notebook_access`；子资源 id 路由查 owner；`/tier` 限 admin。
- `backend/app/main.py` — 挂 `auth_router`（公开）+ `router`（router 级 `Depends(get_current_user)`）。
- `frontend/app/page.tsx` — `api()`/`readAskStream()` 注入 token + 401 处理；`currentUser` 态 + 启动校验 + 渲染门 + 用户菜单 + 非 admin 隐藏 base 动作。
- `scripts/check.sh`、`README.md`、`README_zh.md`、`AGENTS.md`、`.env.example`、`fangan_done.md`。

---

## Task 1: 配置项（admin 密码 + auth 可选开关）

**Files:**
- Modify: `backend/app/core/config.py:15-23`
- Test: `backend/tests/test_auth_config.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_auth_config.py
import importlib


def test_auth_settings_defaults(monkeypatch):
    monkeypatch.delenv("SILICON_NOTEBOOK_ADMIN_PASSWORD", raising=False)
    monkeypatch.delenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", raising=False)
    from app.core.config import Settings
    s = Settings()
    assert s.admin_password == "admin"
    assert s.auth_optional is False


def test_auth_settings_env(monkeypatch):
    monkeypatch.setenv("SILICON_NOTEBOOK_ADMIN_PASSWORD", "s3cret")
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "true")
    from app.core.config import Settings
    s = Settings()
    assert s.admin_password == "s3cret"
    assert s.auth_optional is True
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && $PYBIN -m pytest tests/test_auth_config.py -v`
Expected: FAIL（`AttributeError: ... 'admin_password'`）

- [ ] **Step 3: 加配置项**

在 `config.py` 第 23 行 `single_user_name` 字段之后插入：

```python
    # 用户系统：admin 初始密码（每次启动据此重置 admin 密码；改密=改此变量后重启）。
    admin_password: str = Field("admin", env="SILICON_NOTEBOOK_ADMIN_PASSWORD")
    # True 时无 token 的请求回退为 seeded admin（仅本地/测试用）；生产保持 False=强制登录。
    auth_optional: bool = Field(False, env="SILICON_NOTEBOOK_AUTH_OPTIONAL")
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && $PYBIN -m pytest tests/test_auth_config.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/core/config.py backend/tests/test_auth_config.py
git commit -m "feat(auth): add admin_password + auth_optional settings"
```

---

## Task 2: 认证纯函数（用户名正则 + pbkdf2 哈希）

**Files:**
- Create: `backend/app/services/auth_utils.py`
- Test: `backend/tests/test_auth_utils.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_auth_utils.py
from app.services.auth_utils import (
    is_valid_username, normalize_username, hash_password, verify_password,
)


def test_username_regex_accepts_one_or_more_letters_00_six_digits():
    assert is_valid_username("zhang00123456")
    assert is_valid_username("a00000042")
    assert is_valid_username("ABc00999999")


def test_username_regex_rejects_bad_shapes():
    assert not is_valid_username("00123456")        # 缺字母
    assert not is_valid_username("zhang0123456")    # 只有一个 0
    assert not is_valid_username("zhang0012345")    # 5 位数字
    assert not is_valid_username("zhang001234567")  # 7 位数字
    assert not is_valid_username("zh4ng00123456")   # 字母段含数字
    assert not is_valid_username("zhang_00123456")  # 非法字符


def test_normalize_username_lowercases_and_strips():
    assert normalize_username("  ZHang00123456 ") == "zhang00123456"


def test_password_hash_roundtrip():
    h, salt, iters = hash_password("hunter2")
    assert h and salt and iters > 0
    assert verify_password("hunter2", h, salt, iters)
    assert not verify_password("wrong", h, salt, iters)


def test_password_hash_uses_random_salt():
    h1, s1, _ = hash_password("same")
    h2, s2, _ = hash_password("same")
    assert s1 != s2 and h1 != h2
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && $PYBIN -m pytest tests/test_auth_utils.py -v`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 实现**

```python
# backend/app/services/auth_utils.py
"""用户名校验 + 密码哈希（纯标准库，无新依赖）。"""
from __future__ import annotations

import hashlib
import re
import secrets

# 1+ 字母 + 字面 "00" + 6 位数字，如 zhang00123456。
USERNAME_RE = re.compile(r"^[A-Za-z]+00\d{6}$")

_PBKDF2_ITERATIONS = 200_000


def normalize_username(username: str) -> str:
    """去空白 + 转小写（唯一性 / 登录大小写不敏感的归一化键）。"""
    return (username or "").strip().lower()


def is_valid_username(username: str) -> bool:
    return bool(USERNAME_RE.match(normalize_username(username)))


def hash_password(
    password: str, *, salt: str | None = None, iterations: int = _PBKDF2_ITERATIONS
) -> tuple[str, str, int]:
    """返回 (hash_hex, salt_hex, iterations)。salt 缺省随机生成。"""
    salt = salt or secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations)
    return dk.hex(), salt, iterations


def verify_password(password: str, password_hash: str, salt: str, iterations: int) -> bool:
    if not password_hash or not salt or iterations <= 0:
        return False
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations)
    return secrets.compare_digest(dk.hex(), password_hash)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && $PYBIN -m pytest tests/test_auth_utils.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/auth_utils.py backend/tests/test_auth_utils.py
git commit -m "feat(auth): username regex + pbkdf2 password helpers"
```

---

## Task 3: Schema（UserProfile.username + Auth 请求/响应）

**Files:**
- Modify: `backend/app/models/schemas.py:6-12`
- Test: `backend/tests/test_auth_schemas.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_auth_schemas.py
from app.models.schemas import UserProfile, AuthRequest, AuthResult


def test_userprofile_has_username_default():
    u = UserProfile(id="user-local", email="x@y.z", display_name="Admin", role="admin")
    assert u.username == ""


def test_auth_request_and_result():
    req = AuthRequest(username="zhang00123456", password="pw")
    assert req.username == "zhang00123456" and req.password == "pw"
    res = AuthResult(token="tok", user=UserProfile(
        id="u1", email="x@y.z", display_name="z", role="user", username="zhang00123456"))
    assert res.token == "tok" and res.user.username == "zhang00123456"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && $PYBIN -m pytest tests/test_auth_schemas.py -v`
Expected: FAIL（`username` / `AuthRequest` 不存在）

- [ ] **Step 3: 改 schema**

把 `UserProfile`（第 6-12 行）改为：

```python
class UserProfile(BaseModel):
    id: str
    email: str
    display_name: str
    role: str
    username: str = ""
    memory_mode: str = "manual"
    domain_focus: List[str] = Field(default_factory=list)


class AuthRequest(BaseModel):
    username: str
    password: str


class AuthResult(BaseModel):
    token: str
    user: UserProfile
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && $PYBIN -m pytest tests/test_auth_schemas.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/models/schemas.py backend/tests/test_auth_schemas.py
git commit -m "feat(auth): UserProfile.username + AuthRequest/AuthResult schemas"
```

---

## Task 4: DB 迁移（users 新列 + auth_sessions 表 + 唯一索引）

**Files:**
- Modify: `backend/app/services/sqlite_repository.py` — `_migrate()` 列迁移块（在第 671 行 `review_status` 迁移之后、第 672 行注释之前插入）；建表块（在 `users` CREATE 附近，见 Step 3）。
- Test: `backend/tests/test_auth_migration.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_auth_migration.py
import sqlite3
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


def _repo(tmp_path):
    s = Settings(database_url=f"sqlite:///{tmp_path}/t.db")
    return SQLiteRepository(s)


def test_users_table_has_auth_columns(tmp_path):
    repo = _repo(tmp_path)
    with repo._connect() as db:
        cols = {r["name"] for r in db.execute("PRAGMA table_info(users)").fetchall()}
    assert {"username", "password_hash", "password_salt", "password_iterations"} <= cols


def test_auth_sessions_table_exists(tmp_path):
    repo = _repo(tmp_path)
    with repo._connect() as db:
        cols = {r["name"] for r in db.execute("PRAGMA table_info(auth_sessions)").fetchall()}
    assert {"token", "user_id", "created_at", "expires_at", "last_seen_at"} <= cols


def test_username_unique_index(tmp_path):
    repo = _repo(tmp_path)
    with repo._connect() as db:
        idx = {r["name"] for r in db.execute("PRAGMA index_list(users)").fetchall()}
    assert "idx_users_username" in idx
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && $PYBIN -m pytest tests/test_auth_migration.py -v`
Expected: FAIL（列/表不存在）

- [ ] **Step 3: 加建表 + 列迁移**

(a) 在 `_migrate()` 的 `executescript` 内（`users` CREATE 之后的任意位置，紧随其它 CREATE TABLE）追加 auth_sessions 建表：

```sql
                CREATE TABLE IF NOT EXISTS auth_sessions (
                  token TEXT PRIMARY KEY,
                  user_id TEXT NOT NULL REFERENCES users(id) ON DELETE CASCADE,
                  created_at TEXT NOT NULL,
                  expires_at TEXT NOT NULL,
                  last_seen_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_auth_sessions_user ON auth_sessions(user_id);
```

(b) 在第 671 行（`knowledge_relations` 的 `review_status` 迁移块结束）之后、第 672 行 `# Seed the editable object-schema registry` 注释之前，插入 users 列迁移：

```python
            # 用户系统：username + 密码列（守卫式 ALTER 幂等）。
            user_cols = {r["name"] for r in db.execute("PRAGMA table_info(users)").fetchall()}
            if "username" not in user_cols:
                db.execute("ALTER TABLE users ADD COLUMN username TEXT NOT NULL DEFAULT ''")
            if "password_hash" not in user_cols:
                db.execute("ALTER TABLE users ADD COLUMN password_hash TEXT NOT NULL DEFAULT ''")
            if "password_salt" not in user_cols:
                db.execute("ALTER TABLE users ADD COLUMN password_salt TEXT NOT NULL DEFAULT ''")
            if "password_iterations" not in user_cols:
                db.execute("ALTER TABLE users ADD COLUMN password_iterations INTEGER NOT NULL DEFAULT 0")
            # 小写 username 唯一（空串不算冲突：用部分索引排除空串）。
            db.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username "
                "ON users(username) WHERE username != ''"
            )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && $PYBIN -m pytest tests/test_auth_migration.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_auth_migration.py
git commit -m "feat(auth): migrate users auth columns + auth_sessions table"
```

---

## Task 5: Seed 升级 user-local → admin

**Files:**
- Modify: `backend/app/services/sqlite_repository.py` — `_seed()`（第 697-742 行），在 user/profile 的 `INSERT OR IGNORE` 之后追加 admin 升级。
- Test: `backend/tests/test_admin_seed.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_admin_seed.py
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.auth_utils import verify_password


def _repo(tmp_path, password="admin"):
    s = Settings(database_url=f"sqlite:///{tmp_path}/t.db", admin_password=password)
    return SQLiteRepository(s)


def test_seed_upgrades_local_user_to_admin(tmp_path):
    repo = _repo(tmp_path)
    with repo._connect() as db:
        row = db.execute("SELECT * FROM users WHERE id='user-local'").fetchone()
    assert row["role"] == "admin"
    assert row["username"] == "admin"
    assert verify_password(
        "admin", row["password_hash"], row["password_salt"], row["password_iterations"])


def test_seed_admin_password_from_settings(tmp_path):
    repo = _repo(tmp_path, password="s3cret")
    with repo._connect() as db:
        row = db.execute("SELECT * FROM users WHERE id='user-local'").fetchone()
    assert verify_password("s3cret", row["password_hash"], row["password_salt"], row["password_iterations"])


def test_admin_id_stays_user_local(tmp_path):
    """关键不变量：admin 的 id 不变，现有 created_by='user-local' 数据零迁移。"""
    repo = _repo(tmp_path)
    assert repo.current_user().id == "user-local"
    assert repo.current_user().role == "admin"
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && $PYBIN -m pytest tests/test_admin_seed.py -v`
Expected: FAIL（role 仍 'curator'、username 空）

- [ ] **Step 3: 改 `_seed()`**

在 `_seed()` 内 `user_profiles` 的 `INSERT OR IGNORE` 之后（第 730 行 `)` 之后、`from app.services.kg.filters import _norm` 之前）插入：

```python
            # 把内置 user-local 升级为 admin（id 不变=现有 notebook 零迁移）：
            # 每次启动据 settings.admin_password 重置 admin 密码（改密=改环境变量后重启）。
            from app.services.auth_utils import hash_password
            pw_hash, pw_salt, pw_iters = hash_password(self.settings.admin_password)
            db.execute(
                "UPDATE users SET role='admin', username='admin', "
                "password_hash=?, password_salt=?, password_iterations=?, updated_at=? "
                "WHERE id='user-local'",
                (pw_hash, pw_salt, pw_iters, now),
            )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && $PYBIN -m pytest tests/test_admin_seed.py -v`
Expected: PASS

- [ ] **Step 5: 回归既有用户/会话测试**

Run: `cd backend && $PYBIN -m pytest tests/test_conversations.py -v`
Expected: PASS（`current_user().id` 仍 `user-local`，会话归属不变）

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_admin_seed.py
git commit -m "feat(auth): upgrade seeded user-local to admin (id unchanged)"
```

---

## Task 6: ContextVar + current_user 读它

**Files:**
- Modify: `backend/app/services/sqlite_repository.py` — 模块级（第 168 行 `_ASK_MODEL_ERRORS` 之后）加 ContextVar + setters；`current_user()`（第 847 行）改读 ContextVar。
- Test: `backend/tests/test_request_user_ctx.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_request_user_ctx.py
from app.core.config import Settings
from app.services.sqlite_repository import (
    SQLiteRepository, set_request_user, reset_request_user,
)
from app.models.schemas import UserProfile


def _repo(tmp_path):
    return SQLiteRepository(Settings(database_url=f"sqlite:///{tmp_path}/t.db"))


def test_current_user_falls_back_to_admin_when_unset(tmp_path):
    repo = _repo(tmp_path)
    assert repo.current_user().id == "user-local"  # 未设 ContextVar → 回退 admin


def test_current_user_reads_contextvar(tmp_path):
    repo = _repo(tmp_path)
    fake = UserProfile(id="u-zhang", email="z@x", display_name="z", role="user", username="zhang00123456")
    tok = set_request_user(fake)
    try:
        assert repo.current_user().id == "u-zhang"
        assert repo.current_user().username == "zhang00123456"
    finally:
        reset_request_user(tok)
    assert repo.current_user().id == "user-local"  # 复位后回退
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && $PYBIN -m pytest tests/test_request_user_ctx.py -v`
Expected: FAIL（`set_request_user` 不存在）

- [ ] **Step 3: 加 ContextVar + setters**

在第 168 行（`_ASK_MODEL_ERRORS` ContextVar 定义之后）插入：

```python
# 请求级「当前用户」槽（单例仓库不能用实例态；由 get_current_user 依赖设/复位）。
# None = 不在已认证请求上下文（离线脚本/直接测 repository）→ current_user() 回退 admin。
_REQUEST_USER: "contextvars.ContextVar[UserProfile | None]" = contextvars.ContextVar(
    "request_user", default=None)


def set_request_user(user: "UserProfile | None"):
    """设当前请求用户，返回 token 供 reset_request_user 复位。"""
    return _REQUEST_USER.set(user)


def reset_request_user(token) -> None:
    _REQUEST_USER.reset(token)
```

- [ ] **Step 4: 改 `current_user()`（第 847-861 行）**

```python
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
        """从 users + user_profiles 行构造 UserProfile（DRY，多处复用）。"""
        return UserProfile(
            id=user["id"],
            email=user["email"],
            display_name=user["display_name"],
            role=user["role"],
            username=user["username"] if "username" in user.keys() else "",
            memory_mode=profile["memory_mode"] if profile else "manual",
            domain_focus=json.loads(profile["domain_focus"]) if profile else [],
        )
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && $PYBIN -m pytest tests/test_request_user_ctx.py -v`
Expected: PASS

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_request_user_ctx.py
git commit -m "feat(auth): request-user ContextVar; current_user reads it"
```

---

## Task 7: 仓库 user/session 方法

**Files:**
- Modify: `backend/app/services/sqlite_repository.py` — 在 `current_user()` 方法后加方法；模块底部 `_now()` 旁加 `_session_expiry()`。
- Test: `backend/tests/test_user_session_repo.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_user_session_repo.py
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


def _repo(tmp_path):
    return SQLiteRepository(Settings(database_url=f"sqlite:///{tmp_path}/t.db"))


def test_create_user_and_authenticate(tmp_path):
    repo = _repo(tmp_path)
    user = repo.create_user("Zhang00123456", "pw")
    assert user.id != "user-local"
    assert user.username == "zhang00123456"   # 小写归一化
    assert user.role == "user"
    assert repo.authenticate_user("zhang00123456", "pw").id == user.id
    assert repo.authenticate_user("ZHANG00123456", "pw").id == user.id  # 大小写不敏感
    assert repo.authenticate_user("zhang00123456", "wrong") is None
    assert repo.authenticate_user("nobody00111111", "pw") is None


def test_duplicate_username_rejected(tmp_path):
    repo = _repo(tmp_path)
    repo.create_user("zhang00123456", "pw")
    with pytest.raises(ValueError):
        repo.create_user("Zhang00123456", "pw2")


def test_session_lifecycle(tmp_path):
    repo = _repo(tmp_path)
    user = repo.create_user("zhang00123456", "pw")
    token = repo.create_session(user.id)
    assert token
    assert repo.resolve_session(token).id == user.id
    repo.delete_session(token)
    assert repo.resolve_session(token) is None
    assert repo.resolve_session("bogus") is None


def test_create_user_makes_profile(tmp_path):
    repo = _repo(tmp_path)
    user = repo.create_user("zhang00123456", "pw")
    with repo._connect() as db:
        prof = db.execute("SELECT * FROM user_profiles WHERE user_id=?", (user.id,)).fetchone()
    assert prof is not None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && $PYBIN -m pytest tests/test_user_session_repo.py -v`
Expected: FAIL（方法不存在）

- [ ] **Step 3: 实现方法**

在 `current_user()` / `_user_profile()` 之后插入：

```python
    def create_user(self, username: str, password: str) -> UserProfile:
        """注册：归一化 username、唯一校验、pbkdf2 哈希、建 user + profile。
        用户名非法/重复 → ValueError。role 固定 'user'。"""
        from app.services.auth_utils import normalize_username, is_valid_username, hash_password
        norm = normalize_username(username)
        if not is_valid_username(norm):
            raise ValueError("invalid username")
        user_id = f"user-{uuid4().hex[:10]}"
        now = _now()
        pw_hash, pw_salt, pw_iters = hash_password(password)
        email = f"{norm}@users.silicon-notebook.local"
        with self._write() as db:
            exists = db.execute(
                "SELECT 1 FROM users WHERE username = ?", (norm,)).fetchone()
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
            user = db.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
            profile = db.execute("SELECT * FROM user_profiles WHERE user_id=?", (user_id,)).fetchone()
            return self._user_profile(user, profile)

    def authenticate_user(self, username: str, password: str) -> "UserProfile | None":
        from app.services.auth_utils import normalize_username, verify_password
        norm = normalize_username(username)
        with self._connect() as db:
            user = db.execute("SELECT * FROM users WHERE username = ?", (norm,)).fetchone()
            if user is None:
                return None
            if not verify_password(
                password, user["password_hash"], user["password_salt"], user["password_iterations"]):
                return None
            profile = db.execute(
                "SELECT * FROM user_profiles WHERE user_id = ?", (user["id"],)).fetchone()
            return self._user_profile(user, profile)

    def create_session(self, user_id: str) -> str:
        import secrets
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
        """命中且未过期 → 滑动续期并返回 user；否则 None（过期行顺手删除）。"""
        if not token:
            return None
        now = _now()
        with self._write() as db:
            row = db.execute(
                "SELECT * FROM auth_sessions WHERE token = ?", (token,)).fetchone()
            if row is None:
                return None
            if row["expires_at"] <= now:
                db.execute("DELETE FROM auth_sessions WHERE token = ?", (token,))
                return None
            db.execute(
                "UPDATE auth_sessions SET last_seen_at = ?, expires_at = ? WHERE token = ?",
                (now, _session_expiry(), token),
            )
            user = db.execute("SELECT * FROM users WHERE id = ?", (row["user_id"],)).fetchone()
            if user is None:
                return None
            profile = db.execute(
                "SELECT * FROM user_profiles WHERE user_id = ?", (user["id"],)).fetchone()
            return self._user_profile(user, profile)

    def delete_session(self, token: str) -> None:
        with self._write() as db:
            db.execute("DELETE FROM auth_sessions WHERE token = ?", (token,))
```

在模块底部 `_now()` 之后插入：

```python
def _session_expiry(days: int = 30) -> str:
    return (datetime.now() + timedelta(days=days)).replace(microsecond=0).isoformat()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && $PYBIN -m pytest tests/test_user_session_repo.py -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_user_session_repo.py
git commit -m "feat(auth): repository create_user/authenticate/session methods"
```

---

## Task 8: notebook owner 过滤 + 访问助手

**Files:**
- Modify: `backend/app/services/sqlite_repository.py` — `list_notebooks()`（第 863 行）、`create_notebook()`（第 896 行硬编码）、新增 `user_can_access_notebook` / `source_owner` / `conversation_owner` / `answer_owner`。
- Test: `backend/tests/test_notebook_owner_scope.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_notebook_owner_scope.py
from app.core.config import Settings
from app.services.sqlite_repository import (
    SQLiteRepository, set_request_user, reset_request_user,
)
from app.models.schemas import NotebookCreate


def _repo(tmp_path):
    return SQLiteRepository(Settings(database_url=f"sqlite:///{tmp_path}/t.db"))


def test_list_and_create_scoped_to_current_user(tmp_path):
    repo = _repo(tmp_path)
    zhang = repo.create_user("zhang00123456", "pw")
    li = repo.create_user("li00000042", "pw")

    tok = set_request_user(zhang)
    try:
        nb = repo.create_notebook(NotebookCreate(name="zhang nb"))
        names = [n.name for n in repo.list_notebooks()]
    finally:
        reset_request_user(tok)
    assert "zhang nb" in names

    tok = set_request_user(li)
    try:
        assert repo.list_notebooks() == []                      # li 看不到 zhang 的
        assert repo.user_can_access_notebook(nb.id, li.id) is False
        assert repo.user_can_access_notebook(nb.id, zhang.id) is True
    finally:
        reset_request_user(tok)


def test_admin_does_not_see_user_notebooks(tmp_path):
    repo = _repo(tmp_path)
    zhang = repo.create_user("zhang00123456", "pw")
    tok = set_request_user(zhang)
    try:
        repo.create_notebook(NotebookCreate(name="private"))
    finally:
        reset_request_user(tok)
    # admin（ContextVar 未设 → 回退 user-local）看不到 zhang 的私人本
    assert [n.name for n in repo.list_notebooks()] == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && $PYBIN -m pytest tests/test_notebook_owner_scope.py -v`
Expected: FAIL（list 未过滤 / 方法不存在）

- [ ] **Step 3: 改 `list_notebooks()`（第 863-868 行）**

```python
    def list_notebooks(self) -> List[NotebookSummary]:
        owner_id = self.current_user().id
        with self._connect() as db:
            rows = db.execute(
                "SELECT * FROM notebooks WHERE created_by = ? ORDER BY created_at ASC",
                (owner_id,),
            ).fetchall()
            return [self._notebook_from_row(db, row) for row in rows]
```

- [ ] **Step 4: 改 `create_notebook()` 第 896 行**

把硬编码的 `"user-local",` 改为 `self.current_user().id,`。

- [ ] **Step 5: 加访问/owner 助手**（放在 `get_notebook()` 之后）

```python
    def user_can_access_notebook(self, notebook_id: str, user_id: str) -> bool:
        """owner 即可访问；无 admin 全局越权（base 本 owner=admin，故仅 admin 能进）。"""
        with self._connect() as db:
            row = db.execute(
                "SELECT created_by FROM notebooks WHERE id = ?", (notebook_id,)).fetchone()
        return bool(row) and row["created_by"] == user_id

    def source_owner(self, source_id: str) -> "str | None":
        with self._connect() as db:
            row = db.execute(
                "SELECT nb.created_by AS owner FROM sources s "
                "JOIN notebooks nb ON nb.id = s.notebook_id WHERE s.id = ?",
                (source_id,),
            ).fetchone()
        return row["owner"] if row else None

    def conversation_owner(self, conversation_id: str) -> "str | None":
        with self._connect() as db:
            row = db.execute(
                "SELECT nb.created_by AS owner FROM conversations c "
                "JOIN notebooks nb ON nb.id = c.notebook_id WHERE c.id = ?",
                (conversation_id,),
            ).fetchone()
        return row["owner"] if row else None

    def answer_owner(self, answer_id: str) -> "str | None":
        with self._connect() as db:
            row = db.execute(
                "SELECT nb.created_by AS owner FROM answers a "
                "JOIN notebooks nb ON nb.id = a.notebook_id WHERE a.id = ?",
                (answer_id,),
            ).fetchone()
        return row["owner"] if row else None
```

- [ ] **Step 6: 跑测试确认通过**

Run: `cd backend && $PYBIN -m pytest tests/test_notebook_owner_scope.py -v`
Expected: PASS

- [ ] **Step 7: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_notebook_owner_scope.py
git commit -m "feat(auth): scope notebooks by owner + access/owner helpers"
```

---

## Task 9: auth 依赖（deps.py）

**Files:**
- Create: `backend/app/api/deps.py`
- Modify: `backend/app/api/routes.py:90-92`（删除本地 `repository()`，改从 deps 导入）
- Test: `backend/tests/test_auth_deps.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_auth_deps.py
import pytest
from fastapi import HTTPException
from app.api import deps


@pytest.mark.asyncio
async def test_require_notebook_access_404_for_non_owner(tmp_path, monkeypatch):
    pytest.skip("covered end-to-end by test_user_isolation.py")  # 见 Task 11


def test_repository_singleton_importable_from_deps():
    assert deps.repository() is deps.repository()
```

(说明：依赖的 async/yield 行为在 Task 11 经 HTTP 端到端覆盖；此处只验证 deps 可导入、单例稳定。)

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && $PYBIN -m pytest tests/test_auth_deps.py -v`
Expected: FAIL（`app.api.deps` 不存在）

- [ ] **Step 3: 写 deps.py**

```python
# backend/app/api/deps.py
"""请求级依赖：单例仓库 + 当前用户解析 + notebook 访问守卫。"""
from functools import lru_cache
from typing import AsyncIterator

from fastapi import Depends, HTTPException, Request

from app.core.config import get_settings
from app.models.schemas import UserProfile
from app.services.repository import NotebookRepository
from app.services.sqlite_repository import (
    SQLiteRepository, set_request_user, reset_request_user,
)


@lru_cache
def repository() -> NotebookRepository:
    return SQLiteRepository(get_settings())


def _bearer_token(request: Request) -> str:
    header = request.headers.get("Authorization", "")
    if header.lower().startswith("bearer "):
        return header[7:].strip()
    return ""


async def get_current_user(request: Request) -> AsyncIterator[UserProfile]:
    """解析 Bearer token → session → user，写入 ContextVar（请求结束复位）。
    无 token 且 settings.auth_optional → 回退 seeded admin；否则 401。
    注意：必须是 async 依赖——其 ContextVar.set 在请求 task 上下文生效，
    随后被 Starlette 复制进同步路由的 threadpool；同步依赖里 set 不会传播。"""
    settings = get_settings()
    repo = repository()
    token = _bearer_token(request)
    user: "UserProfile | None" = None
    if token:
        user = repo.resolve_session(token)
        if user is None:
            raise HTTPException(status_code=401, detail="invalid or expired session")
    elif settings.auth_optional:
        user = repo.current_user()  # ContextVar 未设 → seeded admin
    else:
        raise HTTPException(status_code=401, detail="authentication required")

    ctx_token = set_request_user(user)
    try:
        yield user
    finally:
        reset_request_user(ctx_token)


async def require_notebook_access(
    notebook_id: str, user: UserProfile = Depends(get_current_user)
) -> str:
    """notebook 子资源守卫：非 owner → 404（不泄露存在性）。"""
    if not repository().user_can_access_notebook(notebook_id, user.id):
        raise HTTPException(status_code=404, detail="Notebook not found")
    return notebook_id
```

- [ ] **Step 4: routes.py 改用 deps.repository**

(a) **删除** `routes.py` 第 90-92 行的本地 `repository()` 定义：

```python
@lru_cache
def repository() -> NotebookRepository:
    return SQLiteRepository(get_settings())
```

(b) 在 routes.py **顶部 import 区**（紧接第 12 行 `from app.core.config import get_settings` 之后）加：

```python
from app.api.deps import repository, require_notebook_access, get_current_user
```

(c) 清理因移走 `repository()` 而可能变成未使用的 import——逐个 `grep` 确认后删除：

Run: `grep -n "lru_cache\|NotebookRepository\|SQLiteRepository" backend/app/api/routes.py`

- 若 `lru_cache` 仅剩第 5 行 import（无其它使用）→ 删第 5 行。
- 若 `NotebookRepository` / `SQLiteRepository` 在 routes.py 已无其它引用 → 从第 66-67 行 import 中删除对应名字（`UploadedSourceFile` 若仍用则保留该行其余部分）。
- `get_settings` 其它函数仍用 → 保留。

> `repository` 现来自 deps，所有调用点 `repository()` 不变；无循环导入（deps 不 import routes）。

- [ ] **Step 5: 跑测试确认通过 + 冒烟导入**

Run: `cd backend && $PYBIN -m pytest tests/test_auth_deps.py -v`
Expected: PASS
Run: `cd backend && $PYBIN -c "import app.main"`
Expected: 无报错（无循环导入）

- [ ] **Step 6: 提交**

```bash
git add backend/app/api/deps.py backend/app/api/routes.py backend/tests/test_auth_deps.py
git commit -m "feat(auth): deps.py with get_current_user + require_notebook_access"
```

---

## Task 10: auth 路由 + main 装配 + 顶层 conftest

**Files:**
- Create: `backend/app/api/auth_routes.py`、`backend/tests/conftest.py`、`backend/tests/test_auth.py`
- Modify: `backend/app/main.py`
- Modify: `backend/app/api/routes.py` — `/me` 改用依赖注入的 user（见 Step 5）

- [ ] **Step 1: 顶层 conftest（先让既有 HTTP 测试在加认证后仍绿）**

```python
# backend/tests/conftest.py
"""测试进程默认开 auth_optional：无 token 的请求回退 seeded admin，
既有 11 个 HTTP 测试无需逐一登录即可继续以 admin 身份跑。"""
import os

os.environ.setdefault("SILICON_NOTEBOOK_AUTH_OPTIONAL", "true")
```

- [ ] **Step 2: 写 auth.py 测试（HTTP 端到端）**

```python
# backend/tests/test_auth.py
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")  # 本套验证真实登录
    from app.core.config import get_settings
    get_settings.cache_clear()
    from app.api import deps
    deps.repository.cache_clear()
    from app.main import create_app
    return TestClient(create_app())


def test_register_returns_token_and_user(client):
    r = client.post("/api/auth/register", json={"username": "Zhang00123456", "password": "pw"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["token"]
    assert body["user"]["username"] == "zhang00123456"
    assert body["user"]["role"] == "user"


def test_register_invalid_username_400(client):
    r = client.post("/api/auth/register", json={"username": "bad", "password": "pw"})
    assert r.status_code == 400


def test_register_empty_password_400(client):
    r = client.post("/api/auth/register", json={"username": "zhang00123456", "password": ""})
    assert r.status_code == 400


def test_register_duplicate_400(client):
    client.post("/api/auth/register", json={"username": "zhang00123456", "password": "pw"})
    r = client.post("/api/auth/register", json={"username": "zhang00123456", "password": "x"})
    assert r.status_code == 400


def test_login_and_me(client):
    client.post("/api/auth/register", json={"username": "zhang00123456", "password": "pw"})
    r = client.post("/api/auth/login", json={"username": "ZHANG00123456", "password": "pw"})
    assert r.status_code == 200
    token = r.json()["token"]
    me = client.get("/api/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["username"] == "zhang00123456"


def test_login_wrong_password_401(client):
    client.post("/api/auth/register", json={"username": "zhang00123456", "password": "pw"})
    r = client.post("/api/auth/login", json={"username": "zhang00123456", "password": "nope"})
    assert r.status_code == 401


def test_me_without_token_401_when_required(client):
    assert client.get("/api/me").status_code == 401


def test_logout_invalidates_token(client):
    client.post("/api/auth/register", json={"username": "zhang00123456", "password": "pw"})
    token = client.post("/api/auth/login", json={"username": "zhang00123456", "password": "pw"}).json()["token"]
    h = {"Authorization": f"Bearer {token}"}
    assert client.post("/api/auth/logout", headers=h).status_code == 204
    assert client.get("/api/me", headers=h).status_code == 401


def test_admin_login_with_seeded_password(client):
    r = client.post("/api/auth/login", json={"username": "admin", "password": "admin"})
    assert r.status_code == 200
    assert r.json()["user"]["role"] == "admin"
```

- [ ] **Step 3: 跑测试确认失败**

Run: `cd backend && $PYBIN -m pytest tests/test_auth.py -v`
Expected: FAIL（`/api/auth/*` 404）

- [ ] **Step 4: 写 auth_routes.py**

```python
# backend/app/api/auth_routes.py
from fastapi import APIRouter, HTTPException, Request

from app.api.deps import repository
from app.models.schemas import AuthRequest, AuthResult
from app.services.auth_utils import is_valid_username

auth_router = APIRouter(prefix="/auth")


@auth_router.post("/register", response_model=AuthResult)
def register(payload: AuthRequest) -> AuthResult:
    if not is_valid_username(payload.username):
        raise HTTPException(status_code=400, detail="用户名须为「字母+00+六位数字」，如 zhang00123456")
    if not (payload.password or "").strip():
        raise HTTPException(status_code=400, detail="密码不能为空")
    try:
        user = repository().create_user(payload.username, payload.password)
    except ValueError as exc:
        detail = "用户名已被占用" if "exists" in str(exc) else "用户名不合法"
        raise HTTPException(status_code=400, detail=detail)
    token = repository().create_session(user.id)
    return AuthResult(token=token, user=user)


@auth_router.post("/login", response_model=AuthResult)
def login(payload: AuthRequest) -> AuthResult:
    user = repository().authenticate_user(payload.username, payload.password)
    if user is None:
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    token = repository().create_session(user.id)
    return AuthResult(token=token, user=user)


@auth_router.post("/logout", status_code=204)
def logout(request: Request) -> None:
    """logout 须拿到原始 token 才能删 session，故直接读 Authorization 头
    （不走 get_current_user，避免 token 已失效时无法登出）。"""
    header = request.headers.get("Authorization", "")
    token = header[7:].strip() if header.lower().startswith("bearer ") else ""
    if token:
        repository().delete_session(token)
    return None
```

- [ ] **Step 5: 改 main.py 装配 + `/me` 用依赖**

`main.py` 顶部加 import：

```python
from fastapi import Depends
from app.api.auth_routes import auth_router
from app.api.deps import get_current_user
```

把第 80-81 行的挂载替换为：

```python
    app.include_router(auth_router, prefix="/api")  # 公开：注册/登录/登出
    app.include_router(
        router, prefix="/api", dependencies=[Depends(get_current_user)]
    )  # 其余全部需登录（router 级依赖：零逐路由遗漏）
    app.include_router(debug_logs_router, prefix="/api")
```

`routes.py` 的 `/me`（第 107-109 行）改为复用依赖解析出的 user（避免再查 ContextVar）：

```python
from app.api.deps import repository, require_notebook_access, get_current_user  # 合并到 Task 9 的导入行


@router.get("/me", response_model=UserProfile)
def me(user: UserProfile = Depends(get_current_user)) -> UserProfile:
    return user
```

> `Depends` 需在 routes.py 导入：把 `from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile` 改为追加 `Depends`。

- [ ] **Step 6: 跑测试确认通过**

Run: `cd backend && $PYBIN -m pytest tests/test_auth.py -v`
Expected: PASS

- [ ] **Step 7: 回归既有 HTTP 测试（验证 conftest auth_optional 兜底）**

Run: `cd backend && $PYBIN -m pytest tests/test_ask_modes_api.py tests/test_unified_kg_api.py tests/test_url_sources_api.py -v`
Expected: PASS（无 token → admin）

- [ ] **Step 8: 提交**

```bash
git add backend/app/api/auth_routes.py backend/app/main.py backend/app/api/routes.py backend/tests/conftest.py backend/tests/test_auth.py
git commit -m "feat(auth): register/login/logout routes; router-level auth; conftest"
```

---

## Task 11: notebook 路由访问守卫 + 子资源 owner + tier 限 admin

**Files:**
- Modify: `backend/app/api/routes.py`
- Test: `backend/tests/test_user_isolation.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_user_isolation.py
import pytest
from fastapi.testclient import TestClient


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    from app.core.config import get_settings
    get_settings.cache_clear()
    from app.api import deps
    deps.repository.cache_clear()
    from app.main import create_app
    return TestClient(create_app())


def _auth(client, username):
    client.post("/api/auth/register", json={"username": username, "password": "pw"})
    token = client.post("/api/auth/login", json={"username": username, "password": "pw"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_user_cannot_see_or_access_others_notebook(client):
    a = _auth(client, "zhang00123456")
    b = _auth(client, "li00000042")
    nb = client.post("/api/notebooks", json={"name": "A's"}, headers=a).json()
    nb_id = nb["id"]
    # B 列表看不到 A 的
    assert client.get("/api/notebooks", headers=b).json() == []
    # B 直接访问 A 的 notebook 及子资源 → 404
    assert client.get(f"/api/notebooks/{nb_id}", headers=b).status_code == 404
    assert client.get(f"/api/notebooks/{nb_id}/sources", headers=b).status_code == 404
    # A 自己能访问
    assert client.get(f"/api/notebooks/{nb_id}", headers=a).status_code == 200


def test_regular_user_cannot_mark_base(client):
    a = _auth(client, "zhang00123456")
    nb_id = client.post("/api/notebooks", json={"name": "x"}, headers=a).json()["id"]
    r = client.post(f"/api/notebooks/{nb_id}/tier", json={"tier": "base"}, headers=a)
    assert r.status_code == 403


def test_admin_can_mark_base(client):
    admin = _auth_admin(client)
    nb_id = client.post("/api/notebooks", json={"name": "ref"}, headers=admin).json()["id"]
    r = client.post(f"/api/notebooks/{nb_id}/tier", json={"tier": "base"}, headers=admin)
    assert r.status_code == 200


def _auth_admin(client):
    token = client.post("/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && $PYBIN -m pytest tests/test_user_isolation.py -v`
Expected: FAIL（B 能看到/访问 A 的；tier 无 admin 校验）

- [ ] **Step 3: 给所有 `/notebooks/{notebook_id}` 路由挂访问守卫**

先列出全部目标路由：

Run: `grep -n '"/notebooks/{notebook_id}' backend/app/api/routes.py`

对**每一条** `@router.<method>("/notebooks/{notebook_id}...")` 装饰器，在其参数里加 `dependencies=[Depends(require_notebook_access)]`。示例（GET sources，第 188 行附近）：

```python
@router.get(
    "/notebooks/{notebook_id}/sources",
    response_model=List[SourceSummary],
    dependencies=[Depends(require_notebook_access)],
)
def list_sources(notebook_id: str) -> List[SourceSummary]:
    ...
```

对已有 `response_model=` 的，把 `dependencies=[...]` 追加进同一个装饰器调用；对无 `response_model` 的（如 DELETE/POST 部分），直接加 `dependencies=[...]`。

> `GET/PATCH/DELETE /notebooks/{notebook_id}`（无子路径）同样要加。`require_notebook_access` 与 `Depends` 已在 Task 9/10 导入。

- [ ] **Step 4: 给子资源 id 路由加 owner 校验**

对 `routes.py` 中以下按子资源 id（非 notebook_id）寻址的路由，在函数体首加 owner 校验。模式：

```python
@router.get("/sources/{source_id}", response_model=SourceDetail)
def get_source(source_id: str, user: UserProfile = Depends(get_current_user)) -> SourceDetail:
    if repository().source_owner(source_id) != user.id:
        raise HTTPException(status_code=404, detail="Source not found")
    try:
        return repository().get_source(source_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Source not found")
```

按此模式处理：`GET /sources/{source_id}`、`DELETE /sources/{source_id}`、`POST /sources/{source_id}/parse`、`GET /sources/{source_id}/elements`（用 `source_owner`）；`GET/PATCH/DELETE /conversations/{conversation_id}`（用 `conversation_owner`）；`POST /answers/{answer_id}/feedback`（用 `answer_owner`）。每个 handler 加 `user: UserProfile = Depends(get_current_user)` 参数 + owner!=user.id → 404。

> `UserProfile` 已在 routes.py 顶部 import。

- [ ] **Step 5: `/tier` 路由限 admin**

找到 `POST /notebooks/{notebook_id}/tier` 的 handler（`grep -n '/tier"' backend/app/api/routes.py`），在函数体首加：

```python
def set_tier(notebook_id: str, payload: SetTierRequest,
             user: UserProfile = Depends(get_current_user)):
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可设置基准库")
    ...
```

（保留其已有的 `dependencies=[Depends(require_notebook_access)]`——admin 也须是该 notebook 的 owner，对 admin 自己的本成立。）

- [ ] **Step 6: 跑测试确认通过**

Run: `cd backend && $PYBIN -m pytest tests/test_user_isolation.py -v`
Expected: PASS

- [ ] **Step 7: 全后端回归**

Run: `cd backend && $PYBIN -m pytest -q`
Expected: 全绿（如个别既有 HTTP 测试因新增 404/owner 失败，核对是否该测试用例本就跨 owner：用 conftest 的 admin 兜底应不跨 owner；若失败属真实回归，修测试或代码）。

- [ ] **Step 8: 提交**

```bash
git add backend/app/api/routes.py backend/tests/test_user_isolation.py
git commit -m "feat(auth): notebook access guard + sub-resource owner checks + admin-only tier"
```

---

## Task 12: 前端 auth 模块（token + API + 用户名校验）

**Files:**
- Create: `frontend/app/auth.ts`、`frontend/app/auth.test.mjs`

- [ ] **Step 1: 写失败测试**

```js
// frontend/app/auth.test.mjs
import { test } from "node:test";
import assert from "node:assert/strict";
import { isValidUsername } from "./auth.ts";

test("username accepts 1+ letters + 00 + 6 digits", () => {
  assert.ok(isValidUsername("zhang00123456"));
  assert.ok(isValidUsername("a00000042"));
  assert.ok(isValidUsername("ABc00999999"));
});

test("username rejects bad shapes", () => {
  assert.ok(!isValidUsername("00123456"));
  assert.ok(!isValidUsername("zhang0123456"));
  assert.ok(!isValidUsername("zhang0012345"));
  assert.ok(!isValidUsername("zh4ng00123456"));
});
```

> 若 `node --test` 不能直接 import `.ts`，本仓库其它 `.test.mjs` 已 import `.ts`（见 `notebook-tier.test.mjs` import `./notebook-tier`）。沿用相同写法：`import { isValidUsername } from "./auth";`（去掉扩展名）。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && node --test app/auth.test.mjs`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 写 auth.ts**

```ts
// frontend/app/auth.ts
export const API_BASE = process.env.NEXT_PUBLIC_API_BASE_URL ?? "http://127.0.0.1:8000/api";
const TOKEN_KEY = "silicon_notebook_token";

export type AuthUser = {
  id: string;
  email: string;
  display_name: string;
  role: string;
  username: string;
};

const USERNAME_RE = /^[A-Za-z]+00\d{6}$/;
export function isValidUsername(username: string): boolean {
  return USERNAME_RE.test((username ?? "").trim().toLowerCase());
}

export function getToken(): string {
  if (typeof window === "undefined") return "";
  return window.localStorage.getItem(TOKEN_KEY) ?? "";
}
export function setToken(token: string): void {
  if (typeof window !== "undefined") window.localStorage.setItem(TOKEN_KEY, token);
}
export function clearToken(): void {
  if (typeof window !== "undefined") window.localStorage.removeItem(TOKEN_KEY);
}
export function authHeaders(): Record<string, string> {
  const t = getToken();
  return t ? { Authorization: `Bearer ${t}` } : {};
}

async function authFetch<T>(path: string, body: unknown): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    let detail = "";
    try { detail = (await res.json())?.detail ?? ""; } catch { /* noop */ }
    throw new Error(typeof detail === "string" && detail ? detail : `${res.status}`);
  }
  return res.json();
}

export async function registerUser(username: string, password: string): Promise<{ token: string; user: AuthUser }> {
  return authFetch("/auth/register", { username, password });
}
export async function loginUser(username: string, password: string): Promise<{ token: string; user: AuthUser }> {
  return authFetch("/auth/login", { username, password });
}
export async function logoutUser(): Promise<void> {
  await fetch(`${API_BASE}/auth/logout`, { method: "POST", headers: authHeaders() }).catch(() => undefined);
  clearToken();
}
export async function fetchMe(): Promise<AuthUser> {
  const res = await fetch(`${API_BASE}/me`, { headers: authHeaders() });
  if (!res.ok) throw new Error(`${res.status}`);
  return res.json();
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend && node --test app/auth.test.mjs`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add frontend/app/auth.ts frontend/app/auth.test.mjs
git commit -m "feat(auth): frontend auth module (token, api, username validation)"
```

---

## Task 13: 前端登录门 + token 注入 + 用户菜单

**Files:**
- Create: `frontend/app/AuthGate.tsx`
- Modify: `frontend/app/page.tsx`（`api()`、`readAskStream()`、`API_BASE`、`currentUser` 态、启动、渲染门、用户菜单、非 admin 隐藏 base 动作）
- Modify: `frontend/app/globals.css`（门样式）

- [ ] **Step 1: 写 AuthGate 组件**

```tsx
// frontend/app/AuthGate.tsx
"use client";
import { FormEvent, useState } from "react";
import { isValidUsername, loginUser, registerUser, setToken, type AuthUser } from "./auth";

export function AuthGate({ onAuthenticated }: { onAuthenticated: (user: AuthUser) => void }) {
  const [mode, setMode] = useState<"login" | "register">("login");
  const [username, setUsername] = useState("");
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);

  const usernameHint = username && !isValidUsername(username)
    ? "用户名须为「字母 + 00 + 六位数字」，如 zhang00123456" : "";

  async function submit(e: FormEvent) {
    e.preventDefault();
    setError("");
    if (mode === "register" && !isValidUsername(username)) {
      setError("用户名须为「字母 + 00 + 六位数字」，如 zhang00123456");
      return;
    }
    if (!password) { setError("请输入密码"); return; }
    setBusy(true);
    try {
      const fn = mode === "login" ? loginUser : registerUser;
      const { token, user } = await fn(username.trim(), password);
      setToken(token);
      onAuthenticated(user);
    } catch (err) {
      setError(err instanceof Error ? err.message : "操作失败");
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="auth-gate">
      <form className="auth-card" onSubmit={submit}>
        <div className="auth-brand">silicon-notebook</div>
        <div className="auth-tabs">
          <button type="button" className={mode === "login" ? "active" : ""}
            onClick={() => { setMode("login"); setError(""); }}>登录</button>
          <button type="button" className={mode === "register" ? "active" : ""}
            onClick={() => { setMode("register"); setError(""); }}>注册</button>
        </div>
        <label className="auth-label">用户名
          <input className="auth-input" value={username} autoFocus
            onChange={(e) => setUsername(e.target.value)} placeholder="zhang00123456" />
        </label>
        {mode === "register" && usernameHint && <div className="auth-hint">{usernameHint}</div>}
        <label className="auth-label">密码
          <input className="auth-input" type="password" value={password}
            onChange={(e) => setPassword(e.target.value)} placeholder="请输入密码" />
        </label>
        {error && <div className="auth-error">{error}</div>}
        <button className="auth-submit" type="submit" disabled={busy}>
          {busy ? "请稍候…" : mode === "login" ? "登录" : "注册并进入"}
        </button>
      </form>
    </div>
  );
}
```

- [ ] **Step 2: page.tsx — `api()` / `readAskStream()` 注入 token + 401 处理**

`page.tsx` 顶部 import 区加：

```tsx
import { authHeaders, clearToken, getToken, fetchMe, logoutUser, type AuthUser } from "./auth";
import { AuthGate } from "./AuthGate";
```

把第 35 行 `const API_BASE = ...` 删除，改从 auth 模块导入（已在上面 import 处补 `API_BASE`）：把上面 import 改成 `import { API_BASE, authHeaders, clearToken, getToken, fetchMe, logoutUser, type AuthUser } from "./auth";`

`api()`（第 475 行 headers 处）改为合并 auth 头，并在 401 时清 token 跳门：

```tsx
  const response = await fetch(`${API_BASE}${path}`, {
    headers: options.body instanceof FormData
      ? { ...authHeaders(), ...(options.headers || {}) }
      : { "Content-Type": "application/json", ...authHeaders(), ...(options.headers || {}) },
    ...options
  });
  // ...（保留 elapsed/requestId/console.debug）
  if (response.status === 401 && getToken()) {
    clearToken();
    if (typeof window !== "undefined") window.location.reload();
  }
```

`readAskStream()`（第 509 行 headers 处）同样合并：

```tsx
    headers: { "Content-Type": "application/json", ...authHeaders() },
```

并在其 `if (!response.ok)` 前加同样的 401 处理三行。

- [ ] **Step 3: page.tsx — currentUser 态 + 启动校验 + 渲染门**

在 `Home()` 顶部 state 区加：

```tsx
  const [currentUser, setCurrentUser] = useState<AuthUser | null>(null);
  const [authChecked, setAuthChecked] = useState(false);
```

把启动 useEffect（第 872-874 行）改为先验 token：

```tsx
  useEffect(() => {
    if (!getToken()) { setAuthChecked(true); return; }
    fetchMe()
      .then((u) => { setCurrentUser(u); return loadNotebookCollection(); })
      .catch(() => { clearToken(); })
      .finally(() => setAuthChecked(true));
  }, []);
```

在最外层 `return`（第 2169 行）之前插入门控：

```tsx
  if (!authChecked) return <div className="auth-gate"><div className="auth-card">加载中…</div></div>;
  if (!currentUser) {
    return <AuthGate onAuthenticated={(u) => {
      setCurrentUser(u);
      setStatusText("");
      loadNotebookCollection().catch(reportError);
    }} />;
  }
```

- [ ] **Step 4: page.tsx — 顶栏用户菜单**

把第 2179 行 `<div className="status">…</div>` 那块替换/追加为带用户名 + 退出：

```tsx
        <div className="topbar-right">
          <div className="status"><span className="status-dot" /><span>{statusText}</span></div>
          <div className="user-menu">
            <span className="user-name">{currentUser.username}{currentUser.role === "admin" ? "（管理员）" : ""}</span>
            <button className="user-logout" onClick={async () => { await logoutUser(); setCurrentUser(null); }}>退出</button>
          </div>
        </div>
```

- [ ] **Step 5: page.tsx — 非 admin 隐藏「设为基准库」**

找到 notebook 动作菜单里渲染 tier 切换（「设为基准库 / 取消基准库」，`grep -n "基准库" frontend/app/page.tsx`）的按钮，在其外层条件加 `currentUser?.role === "admin" &&`：

```tsx
        {currentUser?.role === "admin" && (
          <button className="menu-item" onClick={() => toggleTier(menuNotebook)}>
            {tierLabel(menuNotebook.tier)}
          </button>
        )}
```

（保持原有 onClick / 文案；只加 admin 门控。）

- [ ] **Step 6: globals.css — 门样式**

在 `frontend/app/globals.css` 末尾追加：

```css
.auth-gate { min-height: 100vh; display: grid; place-items: center; background: var(--bg, #0b0d12); }
.auth-card { width: 320px; padding: 28px; border-radius: 14px; background: #161a22; color: #e7ecf3;
  box-shadow: 0 10px 40px rgba(0,0,0,.4); display: flex; flex-direction: column; gap: 12px; }
.auth-brand { font-weight: 700; font-size: 18px; text-align: center; margin-bottom: 4px; }
.auth-tabs { display: flex; gap: 8px; }
.auth-tabs button { flex: 1; padding: 8px; border: 0; border-radius: 8px; background: #232a36; color: #9aa6b5; cursor: pointer; }
.auth-tabs button.active { background: #2f6df6; color: #fff; }
.auth-label { display: flex; flex-direction: column; gap: 4px; font-size: 13px; color: #9aa6b5; }
.auth-input { padding: 9px 10px; border-radius: 8px; border: 1px solid #2a3240; background: #0f131a; color: #e7ecf3; }
.auth-hint { font-size: 12px; color: #e0a042; }
.auth-error { font-size: 13px; color: #ef5350; }
.auth-submit { margin-top: 6px; padding: 10px; border: 0; border-radius: 8px; background: #2f6df6; color: #fff; cursor: pointer; }
.auth-submit:disabled { opacity: .6; cursor: default; }
.topbar-right { display: flex; align-items: center; gap: 16px; }
.user-menu { display: flex; align-items: center; gap: 8px; font-size: 13px; }
.user-name { color: #9aa6b5; }
.user-logout { padding: 4px 10px; border: 1px solid #2a3240; border-radius: 6px; background: transparent; color: #e7ecf3; cursor: pointer; }
```

- [ ] **Step 7: 类型检查 + 测试 + 构建**

Run: `cd frontend && npm run lint`
Expected: tsc 0 error
Run: `cd frontend && npm run test`
Expected: PASS（含 auth.test.mjs 与既有用例）
Run: `cd frontend && npm run build`
Expected: 构建成功

- [ ] **Step 8: 提交**

```bash
git add frontend/app/AuthGate.tsx frontend/app/page.tsx frontend/app/globals.css
git commit -m "feat(auth): login/register gate, token injection, user menu, admin-gated base action"
```

---

## Task 14: 文档同步 + check.sh + PR

**Files:**
- Modify: `scripts/check.sh`、`.env.example`、`README.md`、`README_zh.md`、`AGENTS.md`、`fangan_done.md`

- [ ] **Step 1: check.sh 加新后端文件 py_compile**

在 `scripts/check.sh` 的 `py_compile` 文件清单（`backend/app/main.py` 那段）追加：

```
  "$ROOT_DIR/backend/app/api/deps.py" \
  "$ROOT_DIR/backend/app/api/auth_routes.py" \
  "$ROOT_DIR/backend/app/services/auth_utils.py" \
```

- [ ] **Step 2: .env.example 加变量**

追加：

```
# 用户系统
# admin 初始密码（每次启动据此重置 admin 账号密码；改密=改此值后重启后端）
SILICON_NOTEBOOK_ADMIN_PASSWORD=admin
# True 时无 token 请求回退为 admin（仅本地/测试）；生产留 false=强制登录
SILICON_NOTEBOOK_AUTH_OPTIONAL=false
```

- [ ] **Step 3: README / README_zh / AGENTS 加「用户系统」段**

三处同步加一段（中文版示例，英文版对应）：注册规则（字母+00+六位数字）、密码登录、笔记本按账号隔离、admin（用户名 `admin`，密码取 `SILICON_NOTEBOOK_ADMIN_PASSWORD`）维护 base 层 KG 且对普通用户隐藏但问答仍检索、现有 notebook 归 admin。明确「本地默认 `auth_optional=false` 强制登录」。

- [ ] **Step 4: fangan_done.md 记录**

在合适分节追加：用户系统（自助注册 + 密码登录 + owner 隔离 + admin/base）已实现，列后端（auth_sessions/pbkdf2/ContextVar/router 级守卫）、前端（登录门 + token 注入 + 用户菜单）、测试（test_auth / test_user_isolation）。

- [ ] **Step 5: 全量门**

Run: `bash scripts/check.sh`
Expected: 全绿
Run: `cd backend && $PYBIN -m pytest -q`
Expected: 全绿

- [ ] **Step 6: 提交 + rebase + PR**

```bash
git add scripts/check.sh .env.example README.md README_zh.md AGENTS.md fangan_done.md
git commit -m "docs(auth): user-system docs, env vars, check.sh, fangan_done"
git fetch origin
git rebase origin/master
git push -u origin claude/serene-khayyam-0c204e
gh pr create --base master --title "feat: 用户系统（注册+密码登录+owner 隔离+admin/base）" --body "$(cat <<'EOF'
## 摘要
- 自助注册（用户名「字母+00+六位数字」）+ 密码登录（pbkdf2 + DB session token）
- 笔记本按 owner 隔离；admin（原 user-local 原地升级，id 不变=零迁移）维护 base 层 KG
- base 对普通用户隐藏，但问答仍把 base 当权威参考检索
- 前端登录/注册门 + token 注入 + 401 跳门 + 用户菜单

## 测试
- backend: pytest 全绿（test_auth / test_user_isolation 等）
- frontend: npm run test / lint / build 全绿
- scripts/check.sh 全绿

Spec: docs/superpowers/specs/2026-06-25-user-accounts-design.md
Plan: docs/superpowers/plans/2026-06-25-user-accounts.md

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review 注记（写计划时已核对）

- **Spec 覆盖**：U1 用户名→Task2/12；U2 密码登录/session→Task2/7/10；U3 admin 升级→Task5；U4 base 隐藏（list owner 过滤）+ 问答仍检索（不动 ask 的 `tier='base'` 直查）→Task8/11；U5 角色→Task5/11。认证机制/ContextVar/pbkdf2→Task6/7/9。前端门→Task12/13。测试策略（conftest auth_optional + 隔离专项）→Task10/11。文档→Task14。
- **类型/命名一致**：`set_request_user/reset_request_user/_REQUEST_USER`、`_user_profile`、`create_user/authenticate_user/create_session/resolve_session/delete_session`、`user_can_access_notebook/source_owner/conversation_owner/answer_owner`、`require_notebook_access`、`repository()`（deps）、前端 `isValidUsername/getToken/setToken/clearToken/authHeaders/registerUser/loginUser/logoutUser/fetchMe/AuthUser`、`API_BASE`（auth.ts 导出、page.tsx 导入）——跨 task 命名统一。
- **不变量**：admin id 始终 `user-local`（Task5 测试守住），现有 `created_by='user-local'` 零迁移。
- **gotcha**：`get_current_user` 必须 async（ContextVar set 才能传播进同步路由 threadpool）——已在 deps.py 注释 + Task11 端到端隔离测试兜底。
