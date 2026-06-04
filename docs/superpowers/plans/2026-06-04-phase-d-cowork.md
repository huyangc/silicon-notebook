# Phase D —近实时协作（轮询 + presence） 实现 plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。步骤用 `- [ ]`。spec：`docs/superpowers/specs/2026-06-04-users-sharing-cowork-design.md` §5，决策 D1（近实时=轮询+presence，无 websocket/CRDT，冲突 last-write-wins）。**依赖 Phase B+C 已完成**（身份 + 分享/权限）。

**Goal:** 同一 `edit` 共享 notebook 下，两人操作，几秒内经轮询互见对方改动（来源/KG/文章），并显示在线协作者。

**Architecture:** `notebook_presence(notebook_id,user_id,last_seen)` 心跳；`notebooks.revision` 单调计数，所有写操作 `_touch_notebook` 自增 + 记一条 `notebook_activity`；`GET /notebooks/{id}/state?since=rev` 返回当前 revision + 在线用户 + 自 since 起的 activity。前端进入 notebook 后定时（~4s）心跳 + 拉 state，revision 变了就重拉受影响区，顶部显示在线协作者。冲突 last-write-wins，刷新即对齐。无 websocket / CRDT / 锁。

**Tech Stack:** 同前。测试 pytest（全 mock，两个 X-User-Id）+ 前端 `tsc`。

**Run from:** ROOT on `master`。Gate 同前。

参考阅读：`notebooks` 建表（加 `revision`）、Phase C 的写入口集合（`update_notebook`/`upload_sources`/`approve_candidate`/`store_kg`/…，在它们里调 `_touch_notebook`）、`_require_access`、前端来源轮询（`page.tsx` 现有 source parse-status 轮询的 `setTimeout`/`useEffect`）、工作区顶部区域。

## 文件结构
- 改 `sqlite_repository.py`——`notebook_presence`/`notebook_activity` 表、`notebooks.revision`、`heartbeat`、`notebook_state`、`_touch_notebook` + 写入口接线。
- 改 `routes.py`——`POST /notebooks/{id}/presence`、`GET /notebooks/{id}/state`。
- 改 `schemas.py`——`PresenceUser`、`NotebookState`、`ActivityItem`。
- 改 `frontend/app/page.tsx`(+`globals.css`)——心跳 + state 轮询 + 刷新 + presence 条。

---

### Task D.1: presence 表 + 心跳 + 在线用户

**Files:** Modify `sqlite_repository.py`、`schemas.py`；Test `backend/tests/test_cowork.py`(新)。

- [ ] **Step 1: 写失败测试**（复用两用户 TestClient helper；A edit-分享给 B）
```python
def test_presence_heartbeat(client_shared_edit):
    c, a, b, nb = client_shared_edit  # A owner, B 有 edit
    c.post(f"/api/notebooks/{nb}/presence", headers={"X-User-Id": a})
    online = c.post(f"/api/notebooks/{nb}/presence", headers={"X-User-Id": b}).json()["online"]
    names = {u["username"] for u in online}
    assert names == {"a00000001", "b00000002"}      # 两人都在线
```

- [ ] **Step 2: 跑确认 FAIL**。

- [ ] **Step 3: 实现**
  - 建表：`CREATE TABLE IF NOT EXISTS notebook_presence (notebook_id TEXT NOT NULL, user_id TEXT NOT NULL, last_seen TEXT NOT NULL, PRIMARY KEY(notebook_id,user_id))`。
  - `heartbeat(self, notebook_id) -> List[PresenceUser]`：`_require_access(notebook_id,'view')`；`INSERT OR REPLACE` 当前用户 last_seen=now；查 last_seen 在最近 ~15s 内的所有 user，join users 取 username，返回 `[PresenceUser{user_id,username}]`。
  - schemas：`class PresenceUser(BaseModel): user_id:str; username:str`。
  - 时间比较：存 ISO，用 `_now()` 与阈值；用 Python 端过滤（读全部该 notebook presence 行，按 `now - last_seen <= 15s` 过滤）以避免 SQL 时间运算依赖。

- [ ] **Step 4: 跑 PASS** + 全量绿。

- [ ] **Step 5: Commit**
```bash
git add backend/app/services/sqlite_repository.py backend/app/models/schemas.py backend/tests/test_cowork.py
git commit -m "feat(cowork): notebook_presence + heartbeat with online users"
```

### Task D.2: `notebooks.revision` + `notebook_activity` + 写操作 `_touch_notebook`

**Files:** Modify `sqlite_repository.py`；Test `tests/test_cowork.py`。

- [ ] **Step 1: 写失败测试**（B（edit）改名后 revision 自增、产生 activity）
```python
def test_write_bumps_revision_and_activity(client_shared_edit):
    c, a, b, nb = client_shared_edit
    rev0 = c.get(f"/api/notebooks/{nb}/state", headers={"X-User-Id": a}).json()["revision"]
    c.patch(f"/api/notebooks/{nb}", json={"name": "改了"}, headers={"X-User-Id": b})
    st = c.get(f"/api/notebooks/{nb}/state", headers={"X-User-Id": a}).json()
    assert st["revision"] > rev0
    assert any(act["user"] == "b00000002" for act in st["activities"])
```

- [ ] **Step 2: 跑确认 FAIL**。

- [ ] **Step 3: 实现**
  - `notebooks` 加列 `revision INTEGER NOT NULL DEFAULT 0`（守卫式 ALTER）。
  - 建表 `notebook_activity(id TEXT PK, notebook_id TEXT, user_id TEXT, action TEXT, target TEXT, created_at TEXT)`。
  - `_touch_notebook(self, db, notebook_id, action, target='')`：`UPDATE notebooks SET revision=revision+1, updated_at=? WHERE id=?`；插入一条 activity（user=current_user().id）。
  - 在 Phase C 标记为「写入口」的方法里调用 `_touch_notebook`（与其 `_require_access('edit')` 配对）：`update_notebook`(action='rename'/'update')、`upload_sources`('add_source')、`delete_source`('delete_source')、`approve_candidate`('approve')、`reject_candidate`('reject')、`update_knowledge`('update_knowledge')、`merge_knowledge`('merge')、`store_kg`('extract')、`add_relations`('relations')。**`ask` 不 bump**（问答属个人读行为，不算协作改动）。注意在同一 `_connect()` 事务内调用，避免连接嵌套——按各方法现有 db 句柄传入。

- [ ] **Step 4: 跑 PASS** + 全量绿。

- [ ] **Step 5: Commit**
```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_cowork.py
git commit -m "feat(cowork): notebooks.revision + activity log; writes touch notebook"
```

### Task D.3: `GET /notebooks/{id}/state` + presence 路由

**Files:** Modify `routes.py`、`sqlite_repository.py`(`notebook_state`)、`schemas.py`；Test `tests/test_cowork.py`。

- [ ] **Step 1: 写失败测试**——`since` 过滤：传上次 revision，只回新 activity；缺访问权 → 403/404。
```python
def test_state_since_filter(client_shared_edit):
    c, a, b, nb = client_shared_edit
    c.patch(f"/api/notebooks/{nb}", json={"name": "一"}, headers={"X-User-Id": b})
    rev1 = c.get(f"/api/notebooks/{nb}/state", headers={"X-User-Id": a}).json()["revision"]
    c.patch(f"/api/notebooks/{nb}", json={"name": "二"}, headers={"X-User-Id": b})
    st = c.get(f"/api/notebooks/{nb}/state?since={rev1}", headers={"X-User-Id": a}).json()
    assert st["revision"] > rev1
    assert all(act for act in st["activities"])         # 只含 since 之后的
    assert len(st["activities"]) == 1
```

- [ ] **Step 2: 跑确认 FAIL**。

- [ ] **Step 3: 实现**
  - `notebook_state(self, notebook_id, since=0) -> NotebookState`：`_require_access(notebook_id,'view')`；读 `notebooks.revision`；在线用户（复用 D.1 过滤逻辑）；activity 取 `WHERE notebook_id=? AND rowid>?`——但 revision 与 activity 行不是同一序。**改用 activity 自增主键 / created_at 排序 + 只回 revision>since 的对应区间**。简化：activity 表加自增 `seq INTEGER`（或用 rowid），`notebook_state` 返回当前 revision，并回「自 since 起新增的 activity」= activity 表里 `seq > since_seq`。**为简单**：让 `revision` 直接等于该 notebook 的 activity 计数（每次 `_touch_notebook` revision = 新 activity 的序号），这样 `since=revision` 即可 `WHERE notebook_id=? AND <activity序> > since`。在 `_touch_notebook` 里让 activity 带一个 per-notebook 递增序号 = 新 revision 值（存进 activity 的一列 `rev INTEGER`）。则 `notebook_state(since)` = `SELECT ... FROM notebook_activity WHERE notebook_id=? AND rev>?`。
  - schemas：`ActivityItem{user:str(username); action:str; target:str; created_at:str}`、`NotebookState{revision:int; online:List[PresenceUser]; activities:List[ActivityItem]}`。
  - 路由：`POST /notebooks/{id}/presence`(→heartbeat，返回 `{online:[...]}` 或直接 `NotebookState` 的 online 部分)、`GET /notebooks/{id}/state?since=0`(→notebook_state)。`PermissionError`→403、`KeyError`→404。

- [ ] **Step 4: 跑 PASS** + 全量绿。

- [ ] **Step 5: Commit**
```bash
git add backend/app/services/sqlite_repository.py backend/app/api/routes.py backend/app/models/schemas.py backend/tests/test_cowork.py
git commit -m "feat(cowork): GET state(since) + POST presence endpoints"
```

### Task D.4: 前端——心跳 + state 轮询 + 刷新 + presence 条

**Files:** Modify `frontend/app/page.tsx`(+`globals.css`)。Gate `npm run lint`。

- [ ] **Step 1: 轮询 effect**——进入 notebook（`currentNotebookId` 非空）时启一个 `setInterval`(~4000ms)：`POST /notebooks/{id}/presence` 拿 `online`；`GET /notebooks/{id}/state?since={lastRev}` 拿 `{revision, online, activities}`。用 `const lastRevRef = useRef(0)` 记上次 revision。组件卸载 / 切 notebook 时 `clearInterval` 并重置 `lastRevRef=0`。
- [ ] **Step 2: revision 变了就刷新**——当 `state.revision > lastRevRef.current`：刷新当前 notebook 的共享态（来源列表、KG（若 KG 视图开着）、文章列表）——调用现有的 `loadSources`/`loadNotebookCollection`/KG 拉取函数；更新 `lastRevRef.current = state.revision`。避免刷新聊天线程（会话是个人态）。
- [ ] **Step 3: presence 条**——工作区顶部显示在线协作者（`online` 里除自己外的 username，头像/标签）；可选「最近操作」浮条显示最新 1–2 条 activity（如「b00000002 添加了来源」）。
- [ ] **Step 4: UI/CSS**——`.presence-bar`/`.presence-chip`/`.activity-toast`，复用设计变量。
- [ ] **Step 5: 验证 + Commit**——`npm run lint` 0 错误；走查：两浏览器两用户同一 edit 笔记本，一方加来源/审核，另一方 ~4s 内来源列表更新且看到 activity；presence 显示双方；view 用户只读也能看到更新；切 notebook 清定时器。
```bash
git add frontend/app/page.tsx frontend/app/globals.css
git commit -m "feat(ui): cowork — presence + state polling refresh of shared notebook"
```

## Self-Review（对照 spec §5 + D1）
- presence 心跳/在线 → D.1/D.4；revision+activity、写操作互见 → D.2/D.3/D.4；轮询刷新共享态、聊天保持个人态 → D.4 Step 2。
- 类型/方法名一致：`heartbeat`/`notebook_state`/`_touch_notebook`、`PresenceUser`/`NotebookState`/`ActivityItem`、`revision`/`since`/`lastRevRef`。
- 非目标（再次确认）：websocket、字符级实时光标、CRDT/OT、编辑锁——均不做。
- 性能注意：state 轮询是只读小查询；presence 行随心跳 upsert，旧行可惰性忽略（>15s 视为离线），无需后台清理。
