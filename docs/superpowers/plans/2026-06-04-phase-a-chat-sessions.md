# Phase A —聊天会话管理 实现 plan

> **For agentic workers:** REQUIRED SUB-SKILL: superpowers:subagent-driven-development。步骤用 `- [ ]`。

**Goal:** 同一 notebook 下，当前用户可拥有多个会话（session = conversation），并在它们之间切换、看历史、新建、删除、重命名。

**Architecture:** 后端 conversations/answers 已落地（Phase 3）。本阶段：(1) 给 conversations 加 `created_by` 并按当前用户过滤；(2) 加删除/重命名会话；(3) 前端聊天面板加「会话列表 + 切换」。当前 `current_user()` 仍返回 `user-local`（Phase B 才改为按 `X-User-Id`）——本阶段按 `current_user().id` 归属/过滤，天然向后兼容。

**Tech Stack:** FastAPI routes、SQLite repo、Pydantic schemas、Next.js `frontend/app/page.tsx`。测试：pytest（全 mock）+ 前端 `tsc --noEmit`。

**Run from:** ROOT `/Users/hzf/workspace/silicon_notebook` on `master`。后端 gate：`cd backend && python -m pytest -q`；前端 gate：`cd frontend && npm run lint`。

参考阅读：`_ensure_conversation` / `_conversation_history` / `list_conversations` / `get_conversation`（`backend/app/services/sqlite_repository.py`，Phase 3 加的，搜函数名）；`conversations` 建表（`:299`）；`current_user()`（`:558`）；会话路由（`backend/app/api/routes.py`，搜 `conversations`）；前端多轮线程（`frontend/app/page.tsx`：`turns`/`conversationId`/`runAsk` ~1283、`AnswerView`、"新对话" reset）。

---

### Task A.1: `conversations.created_by` + 按当前用户归属/过滤

**Files:** Modify `backend/app/services/sqlite_repository.py`（建表/守卫 ALTER、`_ensure_conversation`、`list_conversations`）；Test `backend/tests/test_conversations.py`（追加）。

- [ ] **Step 1: 写失败测试**（复用现有 repo fixture：temp DB、注入 FakeEmbedder + FakeLLM）

```python
def test_conversations_scoped_by_current_user(repo):
    nb = _seed(repo)  # 复用本文件已有的 _seed
    r = repo.ask(nb.id, AskRequest(question="q1"))
    # 当前用户能看到自己的会话
    convs = repo.list_conversations(nb.id)
    assert [c.id for c in convs] == [r.conversation_id]
    # 归属字段已写入
    with repo._connect() as db:
        owner = db.execute("SELECT created_by FROM conversations WHERE id=?", (r.conversation_id,)).fetchone()[0]
    assert owner == repo.current_user().id
    # 另一个用户的会话不出现在列表里
    with repo._connect() as db:
        db.execute("INSERT INTO conversations (id, notebook_id, title, created_by, created_at, updated_at) "
                   "VALUES ('conv-other','%s','x','someone-else','t','t')" % nb.id)
    assert all(c.id != "conv-other" for c in repo.list_conversations(nb.id))
```

- [ ] **Step 2: 跑，确认 FAIL**：`cd backend && python -m pytest tests/test_conversations.py::test_conversations_scoped_by_current_user -v`（无 `created_by` 列 / 未过滤）。

- [ ] **Step 3: 实现**
  - 建表 SQL 的 `conversations` 加列 `created_by TEXT DEFAULT ''`；并加守卫式迁移（同 Phase 3 answers 的写法）：
    ```python
    ccols = {r[1] for r in db.execute("PRAGMA table_info(conversations)").fetchall()}
    if "created_by" not in ccols:
        db.execute("ALTER TABLE conversations ADD COLUMN created_by TEXT DEFAULT ''")
    db.execute("UPDATE conversations SET created_by='user-local' WHERE created_by IS NULL OR created_by=''")
    ```
  - `_ensure_conversation`：新建会话时写 `created_by = self.current_user().id`（追加分支不变）。
  - `list_conversations(self, notebook_id)`：`WHERE notebook_id=? AND created_by=?`，参数 `(notebook_id, self.current_user().id)`，仍按 `updated_at DESC`。

- [ ] **Step 4: 跑测试 PASS**，再跑全量 `python -m pytest -q` 无回归（注意 Phase 3 的 `test_conversations` 仍绿；若某测试依赖跨用户列表，按当前用户语义修正）。

- [ ] **Step 5: Commit**
```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_conversations.py
git commit -m "feat(chat): conversations.created_by; list scoped to current user"
```

### Task A.2: 删除 / 重命名会话（repo）

**Files:** Modify `backend/app/services/sqlite_repository.py`；Test `backend/tests/test_conversations.py`（追加）。

- [ ] **Step 1: 写失败测试**

```python
def test_delete_and_rename_conversation(repo):
    nb = _seed(repo)
    r = repo.ask(nb.id, AskRequest(question="q1"))
    repo.rename_conversation(r.conversation_id, "新标题")
    assert repo.get_conversation(r.conversation_id).title == "新标题"
    repo.delete_conversation(r.conversation_id)
    import pytest
    with pytest.raises(KeyError):
        repo.get_conversation(r.conversation_id)         # 会话已删
    with repo._connect() as db:
        n = db.execute("SELECT count(*) FROM answers WHERE conversation_id=?", (r.conversation_id,)).fetchone()[0]
    assert n == 0                                          # 其下 answers 一并删除
```

- [ ] **Step 2: 跑，确认 FAIL**（无 `rename_conversation`/`delete_conversation`）。

- [ ] **Step 3: 实现**
```python
def rename_conversation(self, conversation_id: str, title: str) -> None:
    with self._connect() as db:
        cur = db.execute("UPDATE conversations SET title=?, updated_at=? WHERE id=?",
                         (title, _now(), conversation_id))
        if cur.rowcount == 0:
            raise KeyError(conversation_id)

def delete_conversation(self, conversation_id: str) -> None:
    with self._connect() as db:
        cur = db.execute("DELETE FROM conversations WHERE id=?", (conversation_id,))
        if cur.rowcount == 0:
            raise KeyError(conversation_id)
        db.execute("DELETE FROM answers WHERE conversation_id=?", (conversation_id,))
```

- [ ] **Step 4: 跑 PASS** + 全量绿。

- [ ] **Step 5: Commit**
```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_conversations.py
git commit -m "feat(chat): delete + rename conversation (cascades answers)"
```

### Task A.3: 路由 DELETE / PATCH `/conversations/{id}`

**Files:** Modify `backend/app/api/routes.py`；Test `backend/tests/test_conversations.py`（追加，沿用本文件已有的 TestClient 用法）。

- [ ] **Step 1: 写失败测试**（TestClient：建会话→PATCH 改名→GET 校验→DELETE→GET 404）。参考本文件已有的 `test_conversation_routes` 写法构造 client。

```python
def test_conversation_mutation_routes(client, seeded_notebook):
    # 先 ask 建一个会话，拿 conversation_id
    cid = client.post(f"/api/notebooks/{seeded_notebook}/ask", json={"question": "q"}).json()["conversation_id"]
    assert client.patch(f"/api/conversations/{cid}", json={"title": "T"}).status_code == 200
    assert client.get(f"/api/conversations/{cid}").json()["title"] == "T"
    assert client.delete(f"/api/conversations/{cid}").status_code in (200, 204)
    assert client.get(f"/api/conversations/{cid}").status_code == 404
```
（若现有套件没有共享的 `client`/`seeded_notebook` fixture，按本文件 `test_conversation_routes` 已有的构造方式内联即可。）

- [ ] **Step 2: 跑，确认 FAIL**（路由不存在）。

- [ ] **Step 3: 实现**——在 routes.py 加：
```python
class ConversationRenameRequest(BaseModel):  # 放 schemas.py 或就近；若放 schemas 记得 import
    title: str

@router.patch("/conversations/{conversation_id}")
def rename_conversation(conversation_id: str, payload: ConversationRenameRequest):
    try:
        repository().rename_conversation(conversation_id, payload.title)
        return {"ok": True}
    except KeyError:
        raise HTTPException(status_code=404, detail="Conversation not found")

@router.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str):
    try:
        repository().delete_conversation(conversation_id)
        return {"ok": True}
    except KeyError:
        raise HTTPException(status_code=404, detail="Conversation not found")
```
（`ConversationRenameRequest` 建议放 `app/models/schemas.py` 并在 routes import，保持与其它请求体一致。）

- [ ] **Step 4: 跑 PASS** + 全量绿。

- [ ] **Step 5: Commit**
```bash
git add backend/app/api/routes.py backend/app/models/schemas.py backend/tests/test_conversations.py
git commit -m "feat(chat): PATCH rename + DELETE conversation endpoints"
```

### Task A.4: 前端会话列表 + 切换

**Files:** Modify `frontend/app/page.tsx`（+ `globals.css`）。Gate：`cd frontend && npm run lint`（无 test 框架）。

现状：聊天面板有 `turns`/`conversationId`/`runAsk`，"新对话" 会 reset；后端 `GET /notebooks/{id}/conversations` 返回 `[{id,title,updated_at,turn_count}]`，`GET /conversations/{id}` 返回 `{...,turns:[{answer_id,question,response,created_at}]}`。

- [ ] **Step 1: 状态 + 拉取**——新增 `const [sessions, setSessions] = useState<ConversationSummary[]>([])`（定义 TS 类型 `ConversationSummary = {id;title;updated_at;turn_count}`）。新增 `loadSessions()`：`GET /notebooks/{currentNotebookId}/conversations` → `setSessions`。在 `openNotebook` 成功后调用一次；在 `runAsk` 成功后（拿到 `response.conversation_id`）再调一次，使新会话/turn_count 即时刷新。切 notebook / 回列表时 `setSessions([])`。

- [ ] **Step 2: 切换会话**——`async function openSession(id)`：`GET /conversations/{id}` → 把 `detail.turns.map(t => ({question:t.question, response:t.response}))` 写入 `setTurns`，`setConversationId(id)`，并 `setChatMode("ask")`。空 turns 也允许（显示空线程）。

- [ ] **Step 3: 删除 / 重命名 / 新建**——
  - 删除：`DELETE /conversations/{id}` → `loadSessions()`；若删的是当前 `conversationId`，同时 reset 线程（`setTurns([]); setConversationId(null)`）。
  - 重命名：`PATCH /conversations/{id} {title}`（用 prompt 或就地输入，按既有 UI 风格）→ `loadSessions()`。
  - 新建：复用现有 "新对话" reset（`setTurns([]); setConversationId(null)`）——下一条消息 `runAsk` 自动建新会话，随后 `loadSessions()` 让它出现在列表。

- [ ] **Step 4: UI**——在聊天面板加一个「会话」区（侧栏或顶部下拉，按既有 `chat-*` 风格）：列出 `sessions`（标题/相对时间/turn 数），高亮当前 `conversationId`，每项有切换点击 + 删除/重命名小按钮，顶部有「＋ 新会话」。在 `globals.css` 加少量 `.chat-session*` 类，复用现有设计变量。复用 `AnswerView`/`renderAnswer` 不动。

- [ ] **Step 5: 验证 + Commit**——`cd frontend && npm run lint` 0 错误；推理走查：建 2 个会话→切换各自历史→刷新后仍在→删除当前会话线程重置→新建会话独立→切 notebook 列表重置。
```bash
git add frontend/app/page.tsx frontend/app/globals.css
git commit -m "feat(ui): chat session list — switch/new/delete/rename conversations per notebook"
```

---

## Self-Review（对照 spec §2）
- 多会话 + 切换 + 历史保留 → A.1（归属/过滤）+ A.4（列表/切换/加载历史）。新建/删除/重命名 → A.2/A.3/A.4。按 notebook 取各自会话 → A.4（loadSessions 用 currentNotebookId + reset on switch）。
- 向后兼容：`created_by` 守卫式迁移 + 旧会话回填 `user-local`；`current_user()` 暂为单用户，Phase B 换头后语义不变。
- 无 placeholder：每步给了代码/测试。类型名一致：`rename_conversation`/`delete_conversation`、`loadSessions`/`openSession`、`ConversationSummary`。

## 非目标（本阶段）
多用户/登录（Phase B）、分享（Phase C）、协作（Phase D）。本阶段会话仍归当前（单一）用户。
