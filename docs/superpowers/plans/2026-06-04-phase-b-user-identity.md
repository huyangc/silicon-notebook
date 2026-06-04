# Phase B —用户身份 + 数据隔离 实现 plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。步骤用 `- [ ]`。spec：`docs/superpowers/specs/2026-06-04-users-sharing-cowork-design.md` §1/§3，决策 D2（用户名 `^[A-Za-z]00[0-9]{6}$`）、D3（仅用户名无密码、`X-User-Id` 头）、D4（存量数据归首登用户）。

**Goal:** 用「一个字母+00+6位数字」的用户名作为身份（无注册无密码），按用户隔离数据；存量单用户数据迁移给首个登录用户。

**Architecture:** `users` 加 `username`（唯一、小写）。登录 = `POST /login {username}` 校验格式→upsert→返回 user。前端存 user id 于 localStorage，每请求带 `X-User-Id`。后端用「中间件 + ContextVar」解析当前用户，`current_user()` 读 ContextVar（无上下文时回退 `user-local`，兼容脚本/测试）。受保护路由缺/错头 → 401（白名单 `/login`、`/health`、`/`、`/docs`、`/openapi.json`）。`list_notebooks` 与写操作按 `created_by` 归属。

**Tech Stack:** FastAPI 中间件、`contextvars`、SQLite repo、Pydantic、Next.js `page.tsx`。测试 pytest（全 mock）+ 前端 `tsc`。

**Run from:** ROOT `/Users/hzf/workspace/silicon_notebook` on `master`。后端 gate `cd backend && python -m pytest -q`；前端 gate `cd frontend && npm run lint`。

参考阅读：`users` 建表 `sqlite_repository.py:159`、`current_user()` `:558`、`list_notebooks()` `:582`、`create_notebook()` `:592`、`repository()` `routes.py:74`、`/me` `routes.py:88`、`main.py` 中间件 `:21`/`add_middleware` `:61`、前端 `api()` `page.tsx:352`、`Home()` 入口、`UserProfile`/`NotebookSummary` schemas。

## 文件结构
- 新建 `backend/app/core/request_context.py`——`current_user_id: ContextVar`。
- 改 `backend/app/main.py`——加用户解析中间件 + 401 守卫。
- 改 `backend/app/services/sqlite_repository.py`——`users.username` 迁移、`validate_username`、`login`、`current_user()` 读 ContextVar、`list_notebooks` 过滤、`create_notebook`/写操作记 `created_by`、存量迁移。
- 改 `backend/app/api/routes.py`——`POST /login`、`/me` 不变（已返回 current_user）。
- 改 `backend/app/models/schemas.py`——`LoginRequest`、`UserProfile` 加 `username`。
- 改 `frontend/app/page.tsx`(+`globals.css`)——登录闸 + `X-User-Id` 头 + 退出。

---

### Task B.1: 用户名校验 + `users.username` + `login` upsert（repo 层）

**Files:** Modify `sqlite_repository.py`、`schemas.py`；Test `backend/tests/test_users.py`(新)。

- [ ] **Step 1: 写失败测试**
```python
# backend/tests/test_users.py
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository, validate_username

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())

def test_validate_username():
    assert validate_username("a00123456")
    assert validate_username("A00000000")
    assert not validate_username("ab00123456")   # 多个字母
    assert not validate_username("a12345678")     # 不是 00 开头
    assert not validate_username("a0012345")      # 7 位
    assert not validate_username("00123456")      # 无字母

def test_login_upsert(repo):
    u1 = repo.login("a00123456")
    assert u1.username == "a00123456" and u1.id
    u2 = repo.login("A00123456")                  # 大小写归一
    assert u2.id == u1.id                          # 同一用户，不重复建
    with pytest.raises(ValueError):
        repo.login("bad")                          # 非法格式抛 ValueError
```

- [ ] **Step 2: 跑确认 FAIL**：`cd backend && python -m pytest tests/test_users.py -v`。

- [ ] **Step 3: 实现**
  - `sqlite_repository.py` 顶部加：
    ```python
    import re
    _USERNAME_RE = re.compile(r"^[A-Za-z]00[0-9]{6}$")
    def validate_username(name: str) -> bool:
        return bool(_USERNAME_RE.match((name or "").strip()))
    ```
  - 建表 SQL `users` 加 `username TEXT`；守卫式迁移 + 唯一索引：
    ```python
    ucols = {r[1] for r in db.execute("PRAGMA table_info(users)").fetchall()}
    if "username" not in ucols:
        db.execute("ALTER TABLE users ADD COLUMN username TEXT")
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username) WHERE username IS NOT NULL")
    ```
  - `login(self, username) -> UserProfile`：校验（不合法 `raise ValueError`），`uname=username.strip().lower()`；查 `SELECT * FROM users WHERE username=?`；存在则返回其 UserProfile；否则建 `id=f"user-{uuid4().hex[:10]}"`、`username=uname`、`display_name=username`、`role='curator'`、`email=uname+'@local'`(满足 NOT NULL UNIQUE)，插入 + 建空 user_profiles 行，返回 UserProfile。复用现有 `_user_profile_from(...)` 或 `current_user()` 的组装逻辑（抽一个 `_load_user(db, user_id)` helper 供两处用）。
  - `UserProfile` schema 加 `username: str = ""`。

- [ ] **Step 4: 跑 PASS** + 全量绿。

- [ ] **Step 5: Commit**
```bash
git add backend/app/services/sqlite_repository.py backend/app/models/schemas.py backend/tests/test_users.py
git commit -m "feat(users): username validation + users.username + login upsert"
```

### Task B.2: 当前用户经 `X-User-Id`（ContextVar + 中间件）+ `POST /login` + 401 守卫

**Files:** New `backend/app/core/request_context.py`；Modify `main.py`、`sqlite_repository.py`(`current_user`)、`routes.py`、`schemas.py`；Test `tests/test_users.py`。

- [ ] **Step 1: 写失败测试**（TestClient）
```python
def test_login_and_header_identity(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import app
    c = TestClient(app)
    assert c.post("/api/login", json={"username": "bad"}).status_code == 400      # 非法
    uid = c.post("/api/login", json={"username": "a00123456"}).json()["id"]
    assert c.get("/api/notebooks").status_code == 401                              # 无头 → 401
    me = c.get("/api/me", headers={"X-User-Id": uid})
    assert me.status_code == 200 and me.json()["username"] == "a00123456"
    assert c.get("/api/notebooks", headers={"X-User-Id": uid}).status_code == 200  # 有头 → 放行
```

- [ ] **Step 2: 跑确认 FAIL**。

- [ ] **Step 3: 实现**
  - `request_context.py`：
    ```python
    from contextvars import ContextVar
    current_user_id: ContextVar[str | None] = ContextVar("current_user_id", default=None)
    ```
  - `main.py` 加中间件（在现有 http 中间件附近）：白名单前缀 `("/api/login","/api/health","/","/docs","/openapi.json")`；从 `request.headers.get("X-User-Id")` 取；非白名单且缺失/未知用户 → `JSONResponse(status_code=401)`；否则 `token = current_user_id.set(uid)`，`finally: current_user_id.reset(token)`。校验"已知用户"= repo 查得到该 id（用 `repository().user_exists(uid)` 或直接查；缺失→401）。
  - `sqlite_repository.py`：`current_user()` 改为读 `current_user_id.get()`；为空则回退 `"user-local"`（兼容无上下文的脚本/repo 测试）。按该 id 加载（找不到也回退 user-local 的 profile，避免崩）。加 `user_exists(self, uid) -> bool`。
  - `routes.py`：`POST /api/login`（body `LoginRequest{username}`）→ `repository().login(username)`；`ValueError` → `HTTPException(400, "用户名需为：一个字母+00+6位数字，如 a00123456")`。`/me` 不变。
  - `schemas.py`：`class LoginRequest(BaseModel): username: str`。

- [ ] **Step 4: 跑 PASS** + 全量绿。**注意**：现有大量 TestClient 测试不带 `X-User-Id` 会变 401 → 给它们统一加头，或让中间件在「数据库里只有 user-local 一个用户（尚未引入真实用户）」时放行回退 user-local。**采用后者更省**：中间件若 `X-User-Id` 缺失，但系统尚无任何真实用户（只有 user-local），则放行并设 ContextVar=user-local；一旦存在真实用户则强制要求头。这样现有测试与单用户模式不破。把这条写进中间件逻辑并加测试覆盖。

- [ ] **Step 5: Commit**
```bash
git add backend/app/core/request_context.py backend/app/main.py backend/app/services/sqlite_repository.py backend/app/api/routes.py backend/app/models/schemas.py backend/tests/test_users.py
git commit -m "feat(users): X-User-Id middleware + POST /login + 401 guard (single-user fallback)"
```

### Task B.3: 按用户归属——`list_notebooks` 过滤 + 写操作记 `created_by`

**Files:** Modify `sqlite_repository.py`；Test `tests/test_users.py`。

- [ ] **Step 1: 写失败测试**（两个用户各建 notebook，互不可见）
```python
def test_notebooks_scoped_by_owner(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import app
    c = TestClient(app)
    a = c.post("/api/login", json={"username": "a00000001"}).json()["id"]
    b = c.post("/api/login", json={"username": "b00000002"}).json()["id"]
    c.post("/api/notebooks", json={"name": "A的"}, headers={"X-User-Id": a})
    la = c.get("/api/notebooks", headers={"X-User-Id": a}).json()
    lb = c.get("/api/notebooks", headers={"X-User-Id": b}).json()
    assert any(n["name"] == "A的" for n in la) and all(n["name"] != "A的" for n in lb)
```

- [ ] **Step 2: 跑确认 FAIL**（当前 list_notebooks 不过滤）。

- [ ] **Step 3: 实现**——`list_notebooks` 加 `WHERE created_by = ?`（`self.current_user().id`），保持排序。`create_notebook` 写 `created_by = self.current_user().id`（建表已有该列；确认 INSERT 带上）。

- [ ] **Step 4: 跑 PASS** + 全量绿（注意单用户回退：现有测试在「仅 user-local」时不带头仍能看到 user-local 的 notebook）。

- [ ] **Step 5: Commit**
```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_users.py
git commit -m "feat(users): list_notebooks scoped to owner; create records created_by"
```

### Task B.4: 存量数据迁移（首登用户继承 user-local）

**Files:** Modify `sqlite_repository.py`（`login` 内或专用 `_migrate_seed_data`）；Test `tests/test_users.py`。

- [ ] **Step 1: 写失败测试**
```python
def test_first_login_inherits_seed_data(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import app
    c = TestClient(app)
    # 模拟存量：user-local 拥有一个 notebook（无头时回退 user-local）
    c.post("/api/notebooks", json={"name": "存量"})
    uid = c.post("/api/login", json={"username": "a00000001"}).json()["id"]
    got = c.get("/api/notebooks", headers={"X-User-Id": uid}).json()
    assert any(n["name"] == "存量" for n in got)        # 首登用户继承了存量
    # 二次登录的新用户不再继承（迁移只发生一次）
    uid2 = c.post("/api/login", json={"username": "b00000002"}).json()["id"]
    assert all(n["name"] != "存量" for n in c.get("/api/notebooks", headers={"X-User-Id": uid2}).json())
```

- [ ] **Step 2: 跑确认 FAIL**。

- [ ] **Step 3: 实现**——`login` 内：当本次是「创建一个**新真实用户**」且当前不存在其它真实用户（除 user-local 外 users 表无 username 非空行）时，触发一次性迁移：把 `notebooks/conversations`（以及其它带 owner 的表，至少这两类 + `knowledge_objects.owner` 若按 user 归属）中 `created_by='user-local'`（或 owner 空/user-local）的行改为该新用户 id。用一个标记防重复（例如建一个 `meta(key,value)` 行 `seed_migrated=<uid>`，或判断「已存在任一真实用户」即跳过）。迁移幂等 + `events.jsonl` 记日志。

- [ ] **Step 4: 跑 PASS** + 全量绿。

- [ ] **Step 5: Commit**
```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_users.py
git commit -m "feat(users): one-time migration of seed (user-local) data to first real user"
```

### Task B.5: 前端登录闸 + `X-User-Id` 头 + 退出

**Files:** Modify `frontend/app/page.tsx`(+`globals.css`)。Gate `npm run lint`。

- [ ] **Step 1: `api()` 带头**——在 `api()`（`page.tsx:352`）的 headers 里注入 `X-User-Id`：读 `localStorage.getItem("snb.userId")`，存在则加 `"X-User-Id": uid`。FormData 分支也要带（在那一支 headers 里加）。

- [ ] **Step 2: 登录闸**——`Home()` 顶部新增 `const [userId, setUserId] = useState<string|null>(null)`，初值从 `localStorage.getItem("snb.userId")`。若无 `userId` → 渲染登录页（一个输入框 + 提交），不渲染主应用。登录页：输入用户名，前端先用 `/^[A-Za-z]00[0-9]{6}$/` 校验，不过提示「用户名需为：一个字母+00+6位数字，如 a00123456」；通过则 `POST /api/login {username}` → 成功存 `localStorage.setItem("snb.userId", res.id)` + `setUserId(res.id)`；后端 400 时显示其 detail。

- [ ] **Step 3: 退出 + 显示**——右上角（集合页顶栏）显示当前用户名（登录返回的 username，可一并存 localStorage `snb.username`）+「退出」按钮：清 `snb.userId`/`snb.username` + `setUserId(null)`（回到登录页）。

- [ ] **Step 4: UI/CSS**——登录页简洁卡片（复用现有设计变量/`.utility-modal`/卡片风格）；用户名+退出用既有顶栏样式。在 `globals.css` 加少量 `.login-*` 类。

- [ ] **Step 5: 验证 + Commit**——`cd frontend && npm run lint` 0 错误；走查：非法用户名就地提示、登录后进入主应用且 `api()` 带头、退出回登录页、刷新保持登录（localStorage）。
```bash
git add frontend/app/page.tsx frontend/app/globals.css
git commit -m "feat(ui): username login gate + X-User-Id header + logout"
```

## Self-Review（对照 spec §1/§3 + D2/D3/D4）
- 用户名校验 D2 → B.1/B.5；无密码信任制 D3 → B.2（头）；存量迁移 D4 → B.4；数据隔离 → B.3。
- 单用户回退保证现有测试/单用户模式不破（B.2 Step 4）。类型/方法名一致：`validate_username`/`login`/`user_exists`/`current_user`/`current_user_id`、`LoginRequest`、`snb.userId`。
- 非目标：分享（Phase C）、协作（Phase D）。本阶段 `list_notebooks` 仅「我创建的」，Phase C 再并上「分享给我的」。
