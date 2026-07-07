# 管理员「用户使用总览」+ 界面显示用户名 — 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 给管理员一个「用户使用总览」页(列出所有用户+用量,可下钻日志),入口仅管理员可见;所有展示用户的地方显示用户名而非内部 id。

**Architecture:** 后端加一个只读 admin 端点 `GET /api/admin/users`(廉价 GROUP BY 统计);前端加独立路由 `/admin/usage` 渲染统计表 + 顶栏 admin 专属入口 + 增强 `/dev/logs` 以显示用户名并支持 admin 按用户下钻。不改 id/username 模型,不迁移。

**Tech Stack:** FastAPI + SQLite(后端);Next.js 15 App Router + TypeScript(前端);pytest(后端测试)、`node --test` on `.mjs`(前端纯函数测试)。

## Global Constraints

- **不统一/不迁移 user id**:内部 id 保持 `user-<hex>`/`user-local`;`username` 仅用于显示;下钻传参用内部 `id` 当 `owner`。
- **admin 门控**:后端路由内 `if user.role != "admin": raise HTTPException(status_code=403, detail=...)`(复用现有模式)。
- **不依赖 `DEBUG_LOGS_ENABLED`**:统计端点常驻;仅 `/dev/logs` 的原始日志区受该开关约束。
- **效率优先**:统计只读、固定条数 `GROUP BY`(无按用户 N+1)、无 LLM/embed。
- **前端测试放顶层**:`node --test app/*.test.mjs` 只匹配顶层,故新 `.test.mjs` 必须放 `frontend/app/` 顶层(可 import 嵌套 `.ts`)。
- **前端认证**:所有新/改的 fetch 走 `authHeaders()`(from `app/auth.ts`),不得裸 fetch。
- **弯引号**:中文文案沿用项目风格,勿批量替直引号。
- **提交信息结尾**:`Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。
- **预存失败(非本特性)**:`frontend` 的 `app/answer-citations.test.mjs` 在 master 上即因 `Cannot find package 'react'` 失败(76 pass / 1 fail)。本特性只需保证**新增测试全绿**且不新增失败。

---

### Task 1: 后端仓库 `list_user_usage()` + created_by 索引

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(加方法 `list_user_usage`;迁移块补两个索引)
- Test: `backend/tests/test_admin_users.py`(新建;本任务写 repo 级测试)

**Interfaces:**
- Produces: `SQLiteRepository.list_user_usage() -> list[dict]`,每项 `{"id": str, "username": str, "role": str, "created_at": str, "notebooks": int, "sources": int, "conversations": int, "reports": int, "last_active": str | None}`。

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_admin_users.py`:

```python
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    return SQLiteRepository(Settings())


def _seed(repo):
    now = "2026-07-07T00:00:00"
    with repo._write() as db:
        # 两个用户(notebooks.created_by 是 FK→users(id),必须先建用户)
        for uid, uname in (("u1", "a00000001"), ("u2", "b00000002")):
            db.execute(
                "INSERT INTO users (id,email,display_name,role,status,username,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (uid, f"{uid}@x", uid.upper(), "user", "active", uname, now, now),
            )
        # u1: 2 个正常 notebook + 1 个 copying(应被排除);u2: 0
        for nid, status in (("n1", "ready"), ("n2", "ready"), ("n3", "copying")):
            db.execute(
                "INSERT INTO notebooks (id,name,created_by,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)", (nid, nid, "u1", status, now, now),
            )
        # u1 在 n1 下:2 个 source、1 个 report、1 个 conversation
        for sid in ("s1", "s2"):
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?)", (sid, "n1", sid, "md", now, now),
            )
        db.execute(
            "INSERT INTO reports (id,notebook_id,question,created_at,updated_at) "
            "VALUES (?,?,?,?,?)", ("r1", "n1", "q?", now, now),
        )
        db.execute(
            "INSERT INTO conversations (id,notebook_id,created_by,created_at,updated_at) "
            "VALUES (?,?,?,?,?)", ("c1", "n1", "u1", "2026-07-06T10:00:00", "2026-07-06T12:00:00"),
        )


def test_list_user_usage_counts(repo):
    _seed(repo)
    rows = {r["username"]: r for r in repo.list_user_usage()}
    assert set(rows) == {"a00000001", "b00000002"}
    a = rows["a00000001"]
    assert a["id"] == "u1"
    assert a["role"] == "user"
    assert a["notebooks"] == 2          # copying 被排除
    assert a["sources"] == 2
    assert a["conversations"] == 1
    assert a["reports"] == 1
    assert a["last_active"] == "2026-07-06T12:00:00"
    b = rows["b00000002"]
    assert b["notebooks"] == 0 and b["sources"] == 0
    assert b["conversations"] == 0 and b["reports"] == 0
    assert b["last_active"] is None
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_admin_users.py::test_list_user_usage_counts -q`
Expected: FAIL(`AttributeError: 'SQLiteRepository' object has no attribute 'list_user_usage'`)

- [ ] **Step 3: 实现 `list_user_usage`**

在 `backend/app/services/sqlite_repository.py` 的 `authenticate_user` 方法之后(约 1620 行附近)加入(确保文件顶部已有 `from typing import Any, Dict, List` —— 已存在):

```python
    def list_user_usage(self) -> List[Dict[str, Any]]:
        """All users + per-user usage counts for the admin overview.
        Read-only: a fixed set of GROUP BY aggregations joined in Python by
        user id (no per-user N+1). Missing counts default to 0; last_active is
        the newest conversation updated_at (None if the user has none).
        username 仅用于显示;下钻日志仍用内部 id 当 owner。"""
        with self._connect() as db:
            users = db.execute(
                "SELECT id, username, display_name, role, created_at "
                "FROM users ORDER BY created_at, id").fetchall()
            nb = {r["k"]: r["c"] for r in db.execute(
                "SELECT created_by AS k, COUNT(*) AS c FROM notebooks "
                "WHERE status != 'copying' GROUP BY created_by").fetchall()}
            src = {r["k"]: r["c"] for r in db.execute(
                "SELECT nb.created_by AS k, COUNT(*) AS c FROM sources s "
                "JOIN notebooks nb ON nb.id = s.notebook_id "
                "GROUP BY nb.created_by").fetchall()}
            conv = {r["k"]: r["c"] for r in db.execute(
                "SELECT created_by AS k, COUNT(*) AS c FROM conversations "
                "GROUP BY created_by").fetchall()}
            rep = {r["k"]: r["c"] for r in db.execute(
                "SELECT nb.created_by AS k, COUNT(*) AS c FROM reports r "
                "JOIN notebooks nb ON nb.id = r.notebook_id "
                "GROUP BY nb.created_by").fetchall()}
            active = {r["k"]: r["m"] for r in db.execute(
                "SELECT created_by AS k, MAX(updated_at) AS m FROM conversations "
                "GROUP BY created_by").fetchall()}
        out: List[Dict[str, Any]] = []
        for u in users:
            uid = u["id"]
            out.append({
                "id": uid,
                "username": u["username"] or u["display_name"] or uid,
                "role": u["role"],
                "created_at": u["created_at"],
                "notebooks": nb.get(uid, 0),
                "sources": src.get(uid, 0),
                "conversations": conv.get(uid, 0),
                "reports": rep.get(uid, 0),
                "last_active": active.get(uid),
            })
        return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_admin_users.py::test_list_user_usage_counts -q`
Expected: PASS

- [ ] **Step 5: 补 created_by 索引(效率优先)**

在 `sqlite_repository.py` 的守卫式迁移块(约 1150 行、`idx_users_username` 创建处附近)追加:

```python
            # admin 用户总览:按 created_by 分组统计,补覆盖索引(幂等)。
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_notebooks_created_by "
                "ON notebooks(created_by)")
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_conversations_created_by "
                "ON conversations(created_by)")
```

- [ ] **Step 6: 跑一次相关子集确认无回归**

Run: `cd backend && python -m pytest tests/test_admin_users.py tests/test_user_isolation.py -q`
Expected: PASS(新测试全绿;user_isolation 不受影响)

- [ ] **Step 7: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_admin_users.py
git commit -m "feat(admin): list_user_usage 仓库方法+created_by 索引

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: 后端 `GET /api/admin/users` 端点 + schema

**Files:**
- Modify: `backend/app/models/schemas.py`(加 `AdminUserUsage`)
- Modify: `backend/app/api/routes.py`(加路由 + import)
- Test: `backend/tests/test_admin_users.py`(追加 API 测试)

**Interfaces:**
- Consumes: `repository().list_user_usage()`(Task 1)。
- Produces: `GET /api/admin/users` → `200` `list[AdminUserUsage]`(admin);`403`(非 admin)。

- [ ] **Step 1: 写失败测试(追加到 `test_admin_users.py`)**

```python
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
    token = client.post(
        "/api/auth/login", json={"username": username, "password": "pw"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _auth_admin(client):
    token = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_admin_users_forbidden_for_regular_user(client):
    b = _auth(client, "z00123456")
    assert client.get("/api/admin/users", headers=b).status_code == 403


def test_admin_users_lists_username_and_counts(client):
    admin = _auth_admin(client)
    a = _auth(client, "z00123456")
    client.post("/api/notebooks", json={"name": "A1"}, headers=a)
    client.post("/api/notebooks", json={"name": "A2"}, headers=a)
    resp = client.get("/api/admin/users", headers=admin)
    assert resp.status_code == 200
    rows = {r["username"]: r for r in resp.json()}
    assert "admin" in rows and "z00123456" in rows
    assert rows["z00123456"]["notebooks"] == 2
    assert rows["z00123456"]["role"] == "user"
    # 展示用户名,但内部 id 仍是 user-<hex>(未统一)
    assert rows["z00123456"]["id"].startswith("user-")
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_admin_users.py -k admin_users -q`
Expected: FAIL(404 或 `AdminUserUsage` 未定义)

- [ ] **Step 3: 加 `AdminUserUsage` schema**

在 `backend/app/models/schemas.py` 末尾附近加(文件已 `from typing import Optional` 与 `from pydantic import BaseModel`):

```python
class AdminUserUsage(BaseModel):
    id: str
    username: str
    role: str
    created_at: str
    notebooks: int
    sources: int
    conversations: int
    reports: int
    last_active: Optional[str] = None
```

- [ ] **Step 4: 加路由**

在 `backend/app/api/routes.py` 的 schemas import 中追加 `AdminUserUsage`,并在文件内(与其它 admin 路由如 `promotion-queue` 相邻处)加:

```python
@router.get("/admin/users", response_model=List[AdminUserUsage])
def list_admin_users(user: UserProfile = Depends(get_current_user)) -> List[AdminUserUsage]:
    """管理员用户使用总览:所有用户 + 用量统计。仅 admin。"""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可查看用户总览")
    return [AdminUserUsage(**row) for row in repository().list_user_usage()]
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_admin_users.py -q`
Expected: PASS(3 个测试全绿)

- [ ] **Step 6: 全量后端回归**

Run: `cd backend && python -m pytest -q 2>&1 | tail -3`
Expected: 全绿(仅既有无关跳过)

- [ ] **Step 7: 提交**

```bash
git add backend/app/models/schemas.py backend/app/api/routes.py backend/tests/test_admin_users.py
git commit -m "feat(admin): GET /api/admin/users 端点(admin-only 用户使用总览)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: 前端总览页纯函数 helper + 测试

**Files:**
- Create: `frontend/app/admin/usage/format.ts`
- Test: `frontend/app/admin-usage.test.mjs`(**顶层**,import 嵌套 `.ts`)

**Interfaces:**
- Produces:
  - `canSeeAdminUsage(role: string | undefined): boolean`
  - `formatLastActive(iso: string | null | undefined): string`
  - `logsDrillHref(userId: string): string`

- [ ] **Step 1: 写失败测试**

新建 `frontend/app/admin-usage.test.mjs`:

```js
import test from "node:test";
import assert from "node:assert/strict";

import { canSeeAdminUsage, formatLastActive, logsDrillHref } from "./admin/usage/format.ts";

test("canSeeAdminUsage 仅 admin 为真", () => {
  assert.equal(canSeeAdminUsage("admin"), true);
  assert.equal(canSeeAdminUsage("user"), false);
  assert.equal(canSeeAdminUsage(undefined), false);
});

test("formatLastActive 处理空值与格式", () => {
  assert.equal(formatLastActive(null), "—");
  assert.equal(formatLastActive(undefined), "—");
  assert.equal(formatLastActive("2026-07-06T12:34:56"), "2026-07-06 12:34");
});

test("logsDrillHref 编码 owner", () => {
  assert.equal(logsDrillHref("user-abc123"), "/dev/logs?owner=user-abc123");
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && node --test app/admin-usage.test.mjs`
Expected: FAIL(找不到 `./admin/usage/format.ts`)

- [ ] **Step 3: 实现 helper**

新建 `frontend/app/admin/usage/format.ts`:

```ts
export function canSeeAdminUsage(role: string | undefined): boolean {
  return role === "admin";
}

export function formatLastActive(iso: string | null | undefined): string {
  if (!iso) return "—";
  return iso.replace("T", " ").slice(0, 16);
}

export function logsDrillHref(userId: string): string {
  return `/dev/logs?owner=${encodeURIComponent(userId)}`;
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend && node --test app/admin-usage.test.mjs`
Expected: PASS(3 测试)

- [ ] **Step 5: 提交**

```bash
git add frontend/app/admin/usage/format.ts frontend/app/admin-usage.test.mjs
git commit -m "feat(admin): 用户总览页纯函数 helper + 测试

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: 前端总览页 `/admin/usage` + api

**Files:**
- Create: `frontend/app/admin/usage/api.ts`
- Create: `frontend/app/admin/usage/page.tsx`
- Create: `frontend/app/admin/usage/usage.css`

**Interfaces:**
- Consumes: `format.ts`(Task 3);`API_BASE`/`authHeaders`/`fetchMe`(`app/auth.ts`);`GET /api/admin/users`(Task 2)。
- Produces: 路由 `/admin/usage`;`AdminUserUsage` TS 类型;`fetchAdminUsers(): Promise<AdminUserUsage[]>`。

- [ ] **Step 1: 写 api 客户端**

新建 `frontend/app/admin/usage/api.ts`:

```ts
import { API_BASE, authHeaders } from "../../auth.ts";

export type AdminUserUsage = {
  id: string;
  username: string;
  role: string;
  created_at: string;
  notebooks: number;
  sources: number;
  conversations: number;
  reports: number;
  last_active: string | null;
};

export async function fetchAdminUsers(): Promise<AdminUserUsage[]> {
  const res = await fetch(`${API_BASE}/admin/users`, { headers: authHeaders() });
  if (res.status === 403) throw new Error("forbidden");
  if (!res.ok) throw new Error(`${res.status}`);
  return res.json();
}
```

- [ ] **Step 2: 写页面**

新建 `frontend/app/admin/usage/page.tsx`(client component):

```tsx
"use client";

import { useEffect, useState } from "react";
import { fetchMe } from "../../auth.ts";
import { fetchAdminUsers, type AdminUserUsage } from "./api.ts";
import { formatLastActive, logsDrillHref } from "./format.ts";
import "./usage.css";

type State =
  | { kind: "loading" }
  | { kind: "forbidden" }
  | { kind: "error"; message: string }
  | { kind: "ready"; rows: AdminUserUsage[] };

export default function AdminUsagePage() {
  const [state, setState] = useState<State>({ kind: "loading" });

  useEffect(() => {
    (async () => {
      try {
        const me = await fetchMe();
        if (me.role !== "admin") {
          setState({ kind: "forbidden" });
          return;
        }
        const rows = await fetchAdminUsers();
        setState({ kind: "ready", rows });
      } catch (e) {
        const msg = e instanceof Error ? e.message : String(e);
        setState(msg === "forbidden" ? { kind: "forbidden" } : { kind: "error", message: msg });
      }
    })();
  }, []);

  if (state.kind === "loading") return <main className="usage-page">加载中…</main>;
  if (state.kind === "forbidden")
    return <main className="usage-page usage-empty">无权限:仅管理员可查看用户使用总览。</main>;
  if (state.kind === "error")
    return <main className="usage-page usage-empty">加载失败:{state.message}</main>;

  return (
    <main className="usage-page">
      <h1>用户使用总览</h1>
      <table className="usage-table">
        <thead>
          <tr>
            <th>用户名</th><th>角色</th><th>注册时间</th>
            <th>笔记本</th><th>来源</th><th>对话</th><th>报告</th>
            <th>最近活跃</th><th>日志</th>
          </tr>
        </thead>
        <tbody>
          {state.rows.map((u) => (
            <tr key={u.id}>
              <td>{u.username}</td>
              <td>{u.role === "admin" ? "管理员" : "用户"}</td>
              <td>{formatLastActive(u.created_at)}</td>
              <td>{u.notebooks}</td>
              <td>{u.sources}</td>
              <td>{u.conversations}</td>
              <td>{u.reports}</td>
              <td>{formatLastActive(u.last_active)}</td>
              <td><a href={logsDrillHref(u.id)}>查看日志</a></td>
            </tr>
          ))}
        </tbody>
      </table>
    </main>
  );
}
```

- [ ] **Step 3: 写样式**

新建 `frontend/app/admin/usage/usage.css`:

```css
.usage-page { max-width: 1000px; margin: 0 auto; padding: 24px; }
.usage-page h1 { font-size: 20px; margin-bottom: 16px; }
.usage-empty { color: var(--muted, #666); }
.usage-table { width: 100%; border-collapse: collapse; font-size: 14px; }
.usage-table th, .usage-table td {
  text-align: left; padding: 8px 12px; border-bottom: 1px solid var(--border, #e5e7eb);
  white-space: nowrap;
}
.usage-table th { color: var(--muted, #666); font-weight: 600; }
.usage-table td a { color: var(--accent, #2563eb); text-decoration: none; }
.usage-table td a:hover { text-decoration: underline; }
```

- [ ] **Step 4: 类型检查通过**

Run: `cd frontend && npm run lint`
Expected: tsc 无报错(`tsc --noEmit` 干净)

- [ ] **Step 5: 提交**

```bash
git add frontend/app/admin/usage/api.ts frontend/app/admin/usage/page.tsx frontend/app/admin/usage/usage.css
git commit -m "feat(admin): /admin/usage 用户使用总览页(显示用户名+用量+下钻日志)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: 顶栏 admin 专属入口链接

**Files:**
- Modify: `frontend/app/page.tsx`(顶栏账户区)

**Interfaces:**
- Consumes: `currentUser.role`(page.tsx 已有);`/admin/usage` 路由(Task 4)。

- [ ] **Step 1: 定位插入点**

在 `frontend/app/page.tsx` 顶栏(`<header className="topbar">` 内、渲染 `user-name` 那一簇,约 2890–2900 行)找到显示账户名/管理员徽章处。

- [ ] **Step 2: 加 admin 门控链接**

在账户名相邻处插入(仅 admin 渲染;弯引号沿用项目风格):

```tsx
{currentUser.role === "admin" && (
  <a className="topbar-admin-link" href="/admin/usage" title="用户使用总览">用户总览</a>
)}
```

若顶栏无对应类名,复用既有链接/按钮样式类,保持与「知识图谱/Schema」等控件同排对齐(遵循 UI 对齐规范)。

- [ ] **Step 3: 类型检查通过**

Run: `cd frontend && npm run lint`
Expected: tsc 干净

- [ ] **Step 4: 视觉验证(preview)**

用 preview 工具起前端,分别以 admin / 普通用户登录:admin 顶栏可见「用户总览」链接并跳转到 `/admin/usage`;普通用户顶栏**不**渲染该链接。截图留证。
(注:不自动重启用户已有服务;仅用 preview 的独立实例验证。)

- [ ] **Step 5: 提交**

```bash
git add frontend/app/page.tsx
git commit -m "feat(admin): 顶栏「用户总览」入口(仅管理员可见)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: `/dev/logs` 显示用户名 + admin 按用户下钻

**Files:**
- Create: `frontend/app/dev/logs/owner.ts`
- Test: `frontend/app/dev-logs-owner.test.mjs`(**顶层**)
- Modify: `frontend/app/dev/logs/api.ts`(加 `owner` 透传 + `authHeaders`)
- Modify: `frontend/app/dev/logs/page.tsx`(顶部显示用户名 + admin 用户下拉)

**Interfaces:**
- Consumes: `GET /api/admin/users`(Task 2);`fetchMe`/`authHeaders`(auth.ts)。
- Produces: `usernameForOwner(users: {id: string; username: string}[], owner: string, selfName: string): string`。

- [ ] **Step 1: 写 owner→用户名 helper 的失败测试**

新建 `frontend/app/dev-logs-owner.test.mjs`:

```js
import test from "node:test";
import assert from "node:assert/strict";

import { usernameForOwner } from "./dev/logs/owner.ts";

const users = [
  { id: "user-aaa", username: "a00000001" },
  { id: "user-bbb", username: "b00000002" },
];

test("命中 owner 返回其用户名", () => {
  assert.equal(usernameForOwner(users, "user-bbb", "self"), "b00000002");
});

test("owner 为空返回自身名", () => {
  assert.equal(usernameForOwner(users, "", "myself"), "myself");
});

test("未知 owner 回退 owner 原值", () => {
  assert.equal(usernameForOwner(users, "user-zzz", "self"), "user-zzz");
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && node --test app/dev-logs-owner.test.mjs`
Expected: FAIL(找不到 `./dev/logs/owner.ts`)

- [ ] **Step 3: 实现 helper**

新建 `frontend/app/dev/logs/owner.ts`:

```ts
export function usernameForOwner(
  users: { id: string; username: string }[],
  owner: string,
  selfName: string,
): string {
  if (!owner) return selfName;
  const hit = users.find((u) => u.id === owner);
  return hit ? hit.username : owner;
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend && node --test app/dev-logs-owner.test.mjs`
Expected: PASS(3 测试)

- [ ] **Step 5: `api.ts` 加 `owner` 透传 + 认证头**

改 `frontend/app/dev/logs/api.ts`:(a)顶部 `import { authHeaders } from "../../auth.ts";`;(b)`get()` 里 `fetch(..., { headers: authHeaders() })`;(c)`RecordQuery` 增 `owner?: string`(现有 qs 构造已遍历 params,自动带上);(d)`fetchChannels(owner?: string)` 支持传 owner:

```ts
export function fetchChannels(owner?: string): Promise<ChannelsResponse> {
  const suffix = owner ? `?owner=${encodeURIComponent(owner)}` : "";
  return get<ChannelsResponse>(`/debug/logs${suffix}`);
}
```

`get()` 改为带认证头:

```ts
async function get<T>(path: string): Promise<T> {
  const res = await fetch(`${API_BASE}${path}`, { headers: authHeaders() });
  // …（其余错误处理不变）
}
```

- [ ] **Step 6: `page.tsx` 显示用户名 + admin 下拉**

改 `frontend/app/dev/logs/page.tsx`:
- 挂载时 `fetchMe()` 取当前用户 `{username, role}`;从 `window.location.search` 读 `owner`。
- 顶部渲染「当前查看:**{usernameForOwner(users, owner, me.username)}**」。
- 若 `me.role === "admin"`:`fetchAdminUsers()`(import 自 `../../admin/usage/api.ts`)取用户列表,渲染 `<select>`(option 文本=用户名、value=id),onChange 时把 `?owner=<id>` 写入 URL 并重新拉取 records/channels(带 owner)。
- 非 admin:不渲染下拉、不传 owner(维持「只看自己」)。
- 所有 `fetchRecords`/`fetchChannels` 调用带上当前 `owner`。

- [ ] **Step 7: 类型检查 + helper 测试**

Run: `cd frontend && npm run lint && node --test app/dev-logs-owner.test.mjs`
Expected: tsc 干净 + 测试 PASS

- [ ] **Step 8: 视觉验证(preview)**

以 admin 打开 `/dev/logs`:顶部显示自己的用户名;下拉切换到另一个用户 → URL 带 `?owner=<id>`、顶部显示该用户名、日志区显示其记录(需 `DEBUG_LOGS_ENABLED=true` 才有原始日志内容,否则空态提示)。从 `/admin/usage` 点「查看日志」进入时自动带 owner 并显示对应用户名。

- [ ] **Step 9: 提交**

```bash
git add frontend/app/dev/logs/owner.ts frontend/app/dev-logs-owner.test.mjs frontend/app/dev/logs/api.ts frontend/app/dev/logs/page.tsx
git commit -m "feat(admin): /dev/logs 显示用户名 + admin 按用户下钻(内部仍用 id 当 owner)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## 收尾

- [ ] 全量后端:`cd backend && python -m pytest -q 2>&1 | tail -3`(全绿)
- [ ] 前端顶层测试:`cd frontend && npm test 2>&1 | tail -5`(新增测试全绿;仅既有 `answer-citations.test.mjs` 的 react 预存失败,不新增失败)
- [ ] 前端类型:`cd frontend && npm run lint`(干净)
- [ ] 分支 rebase 到 `origin/master` 保持线性 → push → `gh pr create --base master`(PR 体末尾 `🤖 Generated with [Claude Code](https://claude.com/claude-code)`)。

## Self-Review 记录

- **Spec 覆盖**:`/api/admin/users`(Task 1/2)、总览页+用户名显示(Task 3/4)、admin 专属入口(Task 5)、`/dev/logs` 用户名+下钻(Task 6)、authHeaders 修正(Task 6 Step 5)、403 兜底(Task 4 Step 2)、user-local 显示 admin(由 `list_user_usage` 的 username 回退天然覆盖)。非目标(不迁移)由 Global Constraints 锁定。
- **Placeholder**:无 TBD/TODO;每步含真实代码/命令/预期。
- **类型一致**:`AdminUserUsage`(后端 schema / 前端 api 类型字段一致)、`list_user_usage` 返回键与 schema 字段一致、`usernameForOwner`/`logsDrillHref`/`canSeeAdminUsage` 签名在测试与实现间一致。
