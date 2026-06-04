# Phase C —笔记本分享 + 权限（view/edit） 实现 plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。步骤用 `- [ ]`。spec：`docs/superpowers/specs/2026-06-04-users-sharing-cowork-design.md` §1（访问控制）/§4。**依赖 Phase B 已完成**（用户身份 + `current_user()` 经 `X-User-Id`）。

**Goal:** owner 可把 notebook 分享给其他用户名，选 `view` 或 `edit`；被分享者在自己的列表里看到，按权限读/写；后端硬拦越权。

**Architecture:** 新表 `notebook_shares(notebook_id,user_id,permission)`。`_access_tier(notebook_id,user)` 返回 `owner|edit|view|None`；`_require_access(notebook_id,min)` 在 repo 读/写入口统一校验（读≥view、写≥edit）。`list_notebooks` = 我创建的 ∪ 分享给我的（每条带 `access_tier`）。前端 owner 有分享入口，列表区分「我的/分享给我的」，`view` 隐藏写入口（前端隐 + 后端硬拦双保险）。

**Tech Stack:** 同 Phase B。测试 pytest（全 mock，用 `X-User-Id` 头区分用户）+ 前端 `tsc`。

**Run from:** ROOT on `master`。Gate 同前。

参考阅读：`get_notebook()`（读入口，被大量路径调用）、`update_notebook`/`delete_notebook`/`upload_sources`/`process_source`/`approve_candidate`/`store_kg`/`ask` 等写入口、`list_notebooks`（Phase B 已加 owner 过滤）、`NotebookSummary` schema、`routes.py` 的 `/notebooks/{id}/*` 路由族、前端 notebook 卡片/工作区与写操作按钮。

## 文件结构
- 改 `sqlite_repository.py`——`notebook_shares` 建表、share CRUD、`_access_tier`/`_require_access`、`list_notebooks` 并入 shared、`NotebookSummary.access_tier`。
- 改 `routes.py`——分享 CRUD 路由。
- 改 `schemas.py`——`ShareRequest`、`NotebookShare`、`NotebookSummary.access_tier`。
- 改 `frontend/app/page.tsx`(+`globals.css`)——分享 UI、列表分区、按 tier 显隐写入口。

---

### Task C.1: `notebook_shares` 表 + share CRUD（repo）

**Files:** Modify `sqlite_repository.py`、`schemas.py`；Test `backend/tests/test_sharing.py`(新)。

- [ ] **Step 1: 写失败测试**（fixture 复用 test_users 的 TestClient 模式；helper：登录两个用户拿 id，A 建 notebook）
```python
def test_share_crud(client_two_users):
    c, a, b, nb = client_two_users  # A 拥有 nb
    # A 把 nb 以 view 分享给 b00000002
    r = c.post(f"/api/notebooks/{nb}/shares", json={"username": "b00000002", "permission": "view"},
               headers={"X-User-Id": a})
    assert r.status_code == 200
    shares = c.get(f"/api/notebooks/{nb}/shares", headers={"X-User-Id": a}).json()
    assert len(shares) == 1 and shares[0]["permission"] == "view"
    # 改成 edit
    c.patch(f"/api/notebooks/{nb}/shares/{b}", json={"permission": "edit"}, headers={"X-User-Id": a})
    assert c.get(f"/api/notebooks/{nb}/shares", headers={"X-User-Id": a}).json()[0]["permission"] == "edit"
    # 撤销
    c.delete(f"/api/notebooks/{nb}/shares/{b}", headers={"X-User-Id": a})
    assert c.get(f"/api/notebooks/{nb}/shares", headers={"X-User-Id": a}).json() == []
```
（在测试文件里实现 `client_two_users` fixture：TestClient + 两次 /login + A 建 nb。）

- [ ] **Step 2: 跑确认 FAIL**。

- [ ] **Step 3: 实现**
  - 建表：`CREATE TABLE IF NOT EXISTS notebook_shares (id TEXT PRIMARY KEY, notebook_id TEXT NOT NULL, user_id TEXT NOT NULL, permission TEXT NOT NULL CHECK(permission IN ('view','edit')), created_by TEXT, created_at TEXT NOT NULL, UNIQUE(notebook_id,user_id))`。
  - repo 方法：`share_notebook(notebook_id, username, permission)`（按 username 解析 user_id——若该用户名尚未登录过则按需建用户行/或拒绝，**选择：允许分享给合法格式但未登录的用户名→先 upsert 一个用户行**，便于对方登录即见；非法用户名 `ValueError`）、`list_shares(notebook_id) -> List[NotebookShare]`、`update_share(notebook_id,user_id,permission)`、`unshare(notebook_id,user_id)`。CRUD 前用 `_require_access(notebook_id,'owner')`（仅 owner 可管理分享）。
  - schemas：`ShareRequest{username:str, permission:str}`、`SharePermissionUpdate{permission:str}`、`NotebookShare{id,notebook_id,user_id,username,permission}`。

- [ ] **Step 4: 跑 PASS** + 全量绿。

- [ ] **Step 5: Commit**
```bash
git add backend/app/services/sqlite_repository.py backend/app/models/schemas.py backend/tests/test_sharing.py
git commit -m "feat(share): notebook_shares table + share CRUD (owner only)"
```

### Task C.2: `_access_tier` / `_require_access` + 读写入口硬拦

**Files:** Modify `sqlite_repository.py`、`routes.py`；Test `tests/test_sharing.py`。

- [ ] **Step 1: 写失败测试**
```python
def test_access_enforcement(client_two_users):
    c, a, b, nb = client_two_users
    # 未分享：b 读/写都被拒
    assert c.get(f"/api/notebooks/{nb}", headers={"X-User-Id": b}).status_code in (403, 404)
    c.post(f"/api/notebooks/{nb}/shares", json={"username": "b00000002", "permission": "view"}, headers={"X-User-Id": a})
    # view：能读，不能写（改名/删除）
    assert c.get(f"/api/notebooks/{nb}", headers={"X-User-Id": b}).status_code == 200
    assert c.patch(f"/api/notebooks/{nb}", json={"name": "X"}, headers={"X-User-Id": b}).status_code == 403
    # 升 edit：能写
    c.patch(f"/api/notebooks/{nb}/shares/{b}", json={"permission": "edit"}, headers={"X-User-Id": a})
    assert c.patch(f"/api/notebooks/{nb}", json={"name": "X"}, headers={"X-User-Id": b}).status_code == 200
```

- [ ] **Step 2: 跑确认 FAIL**。

- [ ] **Step 3: 实现**
  - `_access_tier(self, notebook_id, user_id=None) -> str|None`：user 为 owner(created_by)→`'owner'`；否则查 `notebook_shares`：`edit`/`view`；无→`None`。
  - `_require_access(self, notebook_id, min_tier='view')`：`order={'view':1,'edit':2,'owner':3}`；tier=None → `raise KeyError`(→404，避免泄露存在性) 或专用 `PermissionError`；tier<min → `raise PermissionError`(→403)。
  - 在 **读入口** `get_notebook()` 开头 `self._require_access(notebook_id,'view')`；在 **写入口**（`update_notebook`/`delete_notebook`/`upload_sources`/`delete_source`/`approve_candidate`/`reject_candidate`/`update_knowledge`/`merge_knowledge`/`store_kg`/`add_relations`/`ask`(写 answer，但 ask 视为读？——定为 **edit** 不合理；**ask 归 view**，因为问答是读行为，会写 conversation 属当前用户自己。故 ask 用 `'view'`)）开头加相应 `_require_access`（写类 `'edit'`）。逐个列在实现里；以 `get_notebook` 覆盖绝大多数读路径。
  - `routes.py`：全局异常处理——把 repo 抛的 `PermissionError`→403、`KeyError`(notebook)→404。可在 routes 的 try/except 或加 FastAPI exception handler：`@app.exception_handler(PermissionError)`（放 main.py）→ 403。

- [ ] **Step 4: 跑 PASS** + 全量绿（注意：单用户回退下 owner=user-local，自访仍 owner，现有测试不破）。

- [ ] **Step 5: Commit**
```bash
git add backend/app/services/sqlite_repository.py backend/app/api/routes.py backend/app/main.py backend/tests/test_sharing.py
git commit -m "feat(share): access tiers + read>=view / write>=edit enforcement"
```

### Task C.3: `list_notebooks` 并入「分享给我的」+ `access_tier`

**Files:** Modify `sqlite_repository.py`、`schemas.py`；Test `tests/test_sharing.py`。

- [ ] **Step 1: 写失败测试**
```python
def test_list_includes_shared_with_tier(client_two_users):
    c, a, b, nb = client_two_users
    c.post(f"/api/notebooks/{nb}/shares", json={"username": "b00000002", "permission": "edit"}, headers={"X-User-Id": a})
    lb = c.get("/api/notebooks", headers={"X-User-Id": b}).json()
    mine = [n for n in lb if n["id"] == nb]
    assert mine and mine[0]["access_tier"] == "edit"
    la = c.get("/api/notebooks", headers={"X-User-Id": a}).json()
    assert [n for n in la if n["id"] == nb][0]["access_tier"] == "owner"
```

- [ ] **Step 2: 跑确认 FAIL**。

- [ ] **Step 3: 实现**——`NotebookSummary` 加 `access_tier: str = "owner"`。`list_notebooks` 改为：`created_by=me` 的（tier=owner）∪ `notebook_shares.user_id=me` 关联的 notebook（tier=该 permission）；`_notebook_from_row` 时带上 `access_tier`（用 `_access_tier` 或 join 出来）。去重（owner 优先）。

- [ ] **Step 4: 跑 PASS** + 全量绿。

- [ ] **Step 5: Commit**
```bash
git add backend/app/services/sqlite_repository.py backend/app/models/schemas.py backend/tests/test_sharing.py
git commit -m "feat(share): list_notebooks unions shared-with-me + access_tier"
```

### Task C.4: 分享路由

**Files:** Modify `routes.py`；Test `tests/test_sharing.py`（C.1/C.2 已覆盖，补 404/权限边界即可）。

- [ ] **Step 1: 写失败测试**——非 owner 调分享管理 → 403；分享给非法用户名 → 400。

- [ ] **Step 2: 跑确认 FAIL**。

- [ ] **Step 3: 实现**——加路由：`POST /notebooks/{id}/shares`(ShareRequest)、`GET /notebooks/{id}/shares`、`PATCH /notebooks/{id}/shares/{user_id}`(SharePermissionUpdate)、`DELETE /notebooks/{id}/shares/{user_id}`。各自 try repo 方法，`PermissionError`→403、`KeyError`→404、`ValueError`→400。`response_model=List[NotebookShare]` 等。

- [ ] **Step 4: 跑 PASS** + 全量绿。

- [ ] **Step 5: Commit**
```bash
git add backend/app/api/routes.py backend/tests/test_sharing.py
git commit -m "feat(share): notebook share CRUD endpoints"
```

### Task C.5: 前端——分享 UI + 列表分区 + 按 tier 显隐写入口

**Files:** Modify `frontend/app/page.tsx`(+`globals.css`)。Gate `npm run lint`。

- [ ] **Step 1: 类型**——`NotebookSummary` 前端类型加 `access_tier: "owner"|"edit"|"view"`；`NotebookShare = {id;notebook_id;user_id;username;permission}`。
- [ ] **Step 2: 分享入口**——notebook 卡片菜单 / 工作区，仅 `access_tier==="owner"` 显示「分享」。点开弹窗（复用 `.utility-modal`）：输入用户名 + 选 view/edit + 现有分享列表（每行可改权限/撤销）。调 `POST/GET/PATCH/DELETE /notebooks/{id}/shares`。非法用户名就地提示后端 400 detail。
- [ ] **Step 3: 列表分区**——集合页把「我的」（owner）与「分享给我的」（edit/view）分组或加角标显示 `access_tier`（owner/可编辑/只读）。
- [ ] **Step 4: 按 tier 隐写入口**——工作区/来源区：当 `access_tier==="view"` 时隐藏所有写操作入口（上传、删除来源、审核候选、改标题、删除 notebook、分享）。后端已硬拦，这里仅 UX。`edit` 与 `owner` 显示写入口（`owner` 额外有分享/删除 notebook）。
- [ ] **Step 5: 验证 + Commit**——`npm run lint` 0 错误；走查：owner 分享→对方列表出现（角标对）、view 看不到写按钮且后端 403、edit 能写、撤销后失访。
```bash
git add frontend/app/page.tsx frontend/app/globals.css
git commit -m "feat(ui): notebook sharing UI + owned/shared sections + view-mode hides writes"
```

## Self-Review（对照 spec §1/§4）
- 分享 CRUD（owner-only）→ C.1/C.4；access tier 读≥view 写≥edit 双端拦 → C.2/C.5；列表并入 shared + tier → C.3；前端入口/显隐 → C.5。
- 类型/方法名一致：`_access_tier`/`_require_access`/`share_notebook`/`list_shares`/`update_share`/`unshare`、`ShareRequest`/`SharePermissionUpdate`/`NotebookShare`、`access_tier`。
- 非目标：实时协作（Phase D）。本阶段共享后看到更新仍需手动刷新；presence/轮询在 D。
