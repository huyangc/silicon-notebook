# 用户总览「当前在线」指示 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** admin「用户使用总览」表格里,每个用户名前显示一个在线状态点(绿=当前在线/空心灰=离线),口径为「此刻持有通知中心实时流连接」。

**Architecture:** 复用进程内单例 `pending_bus._conns`(已维护「谁开着实时流」)作为在线信号,零 DB。后端 `GET /api/admin/users` 首屏带 `is_online`;新增零 DB 的 `GET /api/admin/online` 供前端每 15s 轻量轮询刷新在线集合,不重跑用量聚合。

**Tech Stack:** Python / FastAPI / pytest(后端);Next.js / React / TypeScript(前端);既有 `SQLiteRepository`、`PendingBus`。

## Global Constraints

- **单进程假设**:`pending_bus._conns` 只对本进程权威(当前部署单进程)。多 worker 下为本进程视图 —— 文档已注明,本次不做跨进程汇聚。
- **效率一等约束**:在线读为 O(连接数) 内存操作、零 DB;周期轮询只打零 DB 的 `/api/admin/online`;不新增表、不新增 schema 迁移、不重跑 `list_user_usage` 的 5 个 GROUP BY。
- **`PendingBus.online_user_ids()` 仅事件循环线程调用**,与 `register`/`unregister` 同线程,保持 PendingBus「loop-only、免锁」不变量,**不加锁**。
- **文案中文、标识符英文**;中文文案里的弯引号 `「」""` 合法,勿批量替换直引号。
- **UI 对齐/精致**:状态点与用户名基线对齐,明暗主题都清晰。
- **交付**:分支 rebase 到 `master` 保持线性 → push → `gh pr create --base master`(合并按钮为 Rebase and merge)。后端从 `backend/` 目录跑 pytest。

---

### Task 1: `PendingBus.online_user_ids()` 在线集合访问器

**Files:**
- Modify: `backend/app/services/pending_bus.py`(在 `unregister` 后新增方法,约 :47 后)
- Test: `backend/tests/test_pending_bus.py`(追加)

**Interfaces:**
- Consumes: 既有 `PendingBus.register(user_id) -> asyncio.Queue`、`unregister(user_id, q)`、内部 `self._conns: dict[str, set[asyncio.Queue]]`。
- Produces: `PendingBus.online_user_ids() -> set[str]` —— 返回当前持有 ≥1 条连接的 `user_id` 集合。

- [ ] **Step 1: 追加失败测试**

在 `backend/tests/test_pending_bus.py` 末尾追加(文件顶部已 `from app.services.pending_bus import ...` 视情况补 import;若已有 `PendingBus` import 则复用):

```python
def test_online_user_ids_reflects_register_unregister():
    from app.services.pending_bus import PendingBus
    bus = PendingBus()
    assert bus.online_user_ids() == set()
    q1 = bus.register("user-aaa")
    bus.register("user-bbb")
    assert bus.online_user_ids() == {"user-aaa", "user-bbb"}
    # 同一 user 第二条连接不改变成员集合
    q1b = bus.register("user-aaa")
    assert bus.online_user_ids() == {"user-aaa", "user-bbb"}
    # 断开 user-aaa 的一条,仍在线(还有一条)
    bus.unregister("user-aaa", q1)
    assert "user-aaa" in bus.online_user_ids()
    # 断开最后一条 → 下线
    bus.unregister("user-aaa", q1b)
    assert bus.online_user_ids() == {"user-bbb"}
    # 返回的是快照,修改它不影响内部状态
    snap = bus.online_user_ids()
    snap.add("user-zzz")
    assert "user-zzz" not in bus.online_user_ids()
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_pending_bus.py::test_online_user_ids_reflects_register_unregister -v`
Expected: FAIL —— `AttributeError: 'PendingBus' object has no attribute 'online_user_ids'`

- [ ] **Step 3: 实现方法**

在 `backend/app/services/pending_bus.py` 的 `unregister`(:42-47)之后插入:

```python
    def online_user_ids(self) -> set[str]:
        """当前持有 ≥1 条连接(实时流)的 user_id 集合 = 在线用户。
        仅事件循环线程调用:与 register/unregister 同线程,免锁快照。"""
        return set(self._conns.keys())
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_pending_bus.py -v`
Expected: PASS(新测试 + 原有测试全绿)

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/pending_bus.py backend/tests/test_pending_bus.py
git commit -m "feat(pending-bus): online_user_ids() 在线用户集合访问器"
```

---

### Task 2: 后端 —— `is_online` 字段 + `/admin/users` 接线 + `/admin/online` 端点

**Files:**
- Modify: `backend/app/models/schemas.py:698-707`(`AdminUserUsage` 加字段)
- Modify: `backend/app/api/routes.py:1371-1376`(`list_admin_users` 改 async + 填 `is_online`)
- Modify: `backend/app/api/routes.py:1384` 后(新增 `list_online_users` 端点)
- Test: `backend/tests/test_admin_users.py`(追加)

**Interfaces:**
- Consumes: Task 1 的 `pending_bus.online_user_ids() -> set[str]`;既有 `repository().list_user_usage()`、`get_current_user`、模块内已 import 的 `asyncio` / `pending_bus`。
- Produces:
  - `AdminUserUsage.is_online: bool`(默认 `False`)。
  - `GET /api/admin/users` 响应每行含 `is_online`。
  - `GET /api/admin/online`(admin-only)→ `{"online_ids": list[str]}`(已排序)。

- [ ] **Step 1: 追加失败测试**

在 `backend/tests/test_admin_users.py` 末尾追加(复用文件内既有 `client` / `_auth` / `_auth_admin` fixture):

```python
def test_admin_users_is_online_reflects_pending_bus(client):
    from app.services.pending_bus import pending_bus
    admin = _auth_admin(client)
    _auth(client, "z00123456")
    rows = {r["username"]: r for r in client.get("/api/admin/users", headers=admin).json()}
    uid = rows["z00123456"]["id"]
    assert rows["z00123456"]["is_online"] is False        # 未连接 → 离线
    q = pending_bus.register(uid)
    try:
        rows2 = {r["username"]: r for r in client.get("/api/admin/users", headers=admin).json()}
        assert rows2["z00123456"]["is_online"] is True     # 有连接 → 在线
    finally:
        pending_bus.unregister(uid, q)
    rows3 = {r["username"]: r for r in client.get("/api/admin/users", headers=admin).json()}
    assert rows3["z00123456"]["is_online"] is False        # 断开 → 离线


def test_admin_online_endpoint_lists_connected(client):
    from app.services.pending_bus import pending_bus
    admin = _auth_admin(client)
    _auth(client, "z00123456")
    uid = {r["username"]: r for r in client.get("/api/admin/users", headers=admin).json()}["z00123456"]["id"]
    q = pending_bus.register(uid)
    try:
        data = client.get("/api/admin/online", headers=admin).json()
        assert uid in data["online_ids"]
    finally:
        pending_bus.unregister(uid, q)


def test_admin_online_forbidden_for_regular_user(client):
    b = _auth(client, "z00123456")
    assert client.get("/api/admin/online", headers=b).status_code == 403
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_admin_users.py -k "is_online or admin_online" -v`
Expected: FAIL —— `is_online` 未在响应中(KeyError)/ `/api/admin/online` 404。

- [ ] **Step 3a: 加 schema 字段**

`backend/app/models/schemas.py`,把 `AdminUserUsage`(:698-707)改为:

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
    is_online: bool = False
```

- [ ] **Step 3b: `list_admin_users` 改 async 并填 `is_online`**

`backend/app/api/routes.py`,把 `list_admin_users`(:1371-1376)整体替换为:

```python
@router.get("/admin/users", response_model=List[AdminUserUsage])
async def list_admin_users(user: UserProfile = Depends(get_current_user)) -> List[AdminUserUsage]:
    """管理员用户使用总览:所有用户 + 用量统计 + 当前在线。仅 admin。
    重的用量聚合放线程池,回 loop 线程读 pending_bus(免锁快照)。"""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可查看用户总览")
    loop = asyncio.get_running_loop()
    rows = await loop.run_in_executor(None, repository().list_user_usage)
    online = pending_bus.online_user_ids()
    return [AdminUserUsage(**row, is_online=row["id"] in online) for row in rows]
```

- [ ] **Step 3c: 新增 `/admin/online` 端点**

`backend/app/api/routes.py`,在 `list_admin_user_notebooks`(:1379-1384)之后、`# --- 待确认中心` 注释(:1387)之前插入:

```python
@router.get("/admin/online")
async def list_online_users(user: UserProfile = Depends(get_current_user)) -> dict:
    """当前在线用户 id 集合(持有实时流连接)。仅 admin,纯读内存零 DB。"""
    if user.role != "admin":
        raise HTTPException(status_code=403, detail="仅管理员可查看在线状态")
    return {"online_ids": sorted(pending_bus.online_user_ids())}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_admin_users.py -v`
Expected: PASS(新增 3 个 + 原有全绿;原 `test_admin_users_lists_username_and_counts` 不受影响)

- [ ] **Step 5: 提交**

```bash
git add backend/app/models/schemas.py backend/app/api/routes.py backend/tests/test_admin_users.py
git commit -m "feat(admin): 用户总览带 is_online + 新增 /admin/online 在线集合端点"
```

---

### Task 3: 前端 —— 在线状态点 + 15s 轻量轮询

**Files:**
- Modify: `frontend/app/admin/usage/api.ts`(type 加 `is_online` + 新增 `fetchOnlineIds`)
- Modify: `frontend/app/admin/usage/page.tsx`(状态点渲染 + 轮询 effect)
- Modify: `frontend/app/admin/usage/usage.css`(状态点样式)

**Interfaces:**
- Consumes: Task 2 的 `GET /api/admin/users`(每行含 `is_online`)与 `GET /api/admin/online`(`{"online_ids": string[]}`);既有 `API_BASE` / `authHeaders`。
- Produces: 前端 `AdminUserUsage.is_online: boolean`、`fetchOnlineIds(): Promise<string[]>`、用户名前状态点 + 定时刷新。

- [ ] **Step 1: 扩 api.ts 类型与 fetch**

`frontend/app/admin/usage/api.ts`,在 `AdminUserUsage` type 的 `last_active` 后加一行,并在文件末尾追加 `fetchOnlineIds`:

```typescript
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
  is_online: boolean;
};

export async function fetchAdminUsers(): Promise<AdminUserUsage[]> {
  const res = await fetch(`${API_BASE}/admin/users`, { headers: authHeaders() });
  if (res.status === 403) throw new Error("forbidden");
  if (!res.ok) throw new Error(`${res.status}`);
  return res.json();
}

export async function fetchOnlineIds(): Promise<string[]> {
  const res = await fetch(`${API_BASE}/admin/online`, { headers: authHeaders() });
  if (res.status === 403) throw new Error("forbidden");
  if (!res.ok) throw new Error(`${res.status}`);
  const data = (await res.json()) as { online_ids: string[] };
  return data.online_ids;
}
```

- [ ] **Step 2: page.tsx —— import、state、seed、轮询、渲染**

`frontend/app/admin/usage/page.tsx`:

(a) import(:6)改为带上 `fetchOnlineIds`:

```typescript
import { fetchAdminUsers, fetchOnlineIds, type AdminUserUsage } from "./api.ts";
```

(b) 组件内 state(在 `nbCache` state 之后、:22 后)加:

```typescript
  const [onlineIds, setOnlineIds] = useState<Set<string>>(new Set());
```

(c) 首屏 effect 里,把 `setState({ kind: "ready", rows });`(:33)替换为两行(用首屏 `is_online` 种下初值,首帧即正确):

```typescript
        setState({ kind: "ready", rows });
        setOnlineIds(new Set(rows.filter((r) => r.is_online).map((r) => r.id)));
```

(d) 在首屏 `useEffect(..., [])`(:24-39)之后新增轮询 effect:

```typescript
  useEffect(() => {
    if (state.kind !== "ready") return;
    const timer = setInterval(async () => {
      try {
        setOnlineIds(new Set(await fetchOnlineIds()));
      } catch {
        // 忽略瞬时失败,下个周期重试
      }
    }, 15000);
    return () => clearInterval(timer);
  }, [state.kind]);
```

(e) 用户名单元格(:93 `<td>{u.username}</td>`)替换为状态点 + 用户名:

```jsx
                  <td>
                    <span
                      className={`usage-dot ${onlineIds.has(u.id) ? "usage-dot-online" : "usage-dot-offline"}`}
                      aria-label={onlineIds.has(u.id) ? "在线" : "离线"}
                      title={onlineIds.has(u.id) ? "在线" : "离线"}
                    />
                    {u.username}
                  </td>
```

- [ ] **Step 3: usage.css —— 状态点样式**

`frontend/app/admin/usage/usage.css` 末尾追加(在线用既有 `--green` 主题变量;离线用空心灰环,跨明暗主题都清晰):

```css
.usage-dot {
  display: inline-block; width: 8px; height: 8px; border-radius: 50%;
  margin-right: 8px; vertical-align: middle; box-sizing: border-box;
}
.usage-dot-online { background: var(--green, #177a55); }
.usage-dot-offline { background: transparent; border: 1.5px solid var(--muted, #9ca3af); }
```

- [ ] **Step 4: 类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无错误(尤其 `is_online`、`Set<string>`、`fetchOnlineIds` 类型一致)

- [ ] **Step 5: 预览视觉验证**

用 preview 工具:`preview_start`(前端 dev server)→ 以 admin 登录 → 打开 `/admin/usage` → `preview_screenshot`。
Expected(证据):表格用户名前出现状态点 —— 当前登录/开着 App 的用户显示**绿点**,其余显示**空心灰环**,点与用户名基线对齐。必要时 `preview_resize` 看窄屏不错位。

- [ ] **Step 6: 提交**

```bash
git add frontend/app/admin/usage/api.ts frontend/app/admin/usage/page.tsx frontend/app/admin/usage/usage.css
git commit -m "feat(admin-usage): 用户名前在线状态点 + 15s 轮询 /admin/online"
```

---

## 交付收尾(全部任务后)

- [ ] `cd backend && python -m pytest tests/test_pending_bus.py tests/test_admin_users.py -v` 全绿。
- [ ] `cd frontend && npx tsc --noEmit` 干净。
- [ ] `git fetch origin && git rebase origin/master`(保持线性);冲突则解决后 `git rebase --continue`。
- [ ] `git push -u origin claude/user-overview-online-status-7e39bb`
- [ ] `gh pr create --base master`(标题:`feat(admin): 用户总览显示当前在线状态`;正文附 `/admin/usage` 绿点/灰环截图)。

## Self-Review 记录(对照 spec)

- **在线口径 = `pending_bus._conns`**:Task 1 访问器 + Task 2 接线。✓
- **首屏正确 + 周期刷新**:Task 2 `/admin/users` 带 `is_online`(种子);Task 3 `/admin/online` 每 15s 轮询。✓
- **零 DB / 不重跑聚合 / 免锁 loop-only**:Task 1 注释约束;Task 2 `/admin/online` 纯读内存,`/admin/users` 聚合走线程池、在线读回 loop 线程。✓
- **显示 = 用户名前圆点(不新增列)**:Task 3 Step 2e + Step 3。✓
- **测试**:`_conns` 有该 user → `is_online=true`;`/admin/online` 非 admin 403。Task 1/2 覆盖。✓
- **caveat(单进程 / admin 自身页面不挂流 / 重启短暂离线)**:spec 已记录,属接受行为,无对应 task —— 有意为之。
- **YAGNI**:未动「最近活跃」列、未引入 `auth_sessions` 口径、无 presence 表/迁移。✓
