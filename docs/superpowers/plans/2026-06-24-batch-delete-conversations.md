# 历史对话「按最后活动时间」批量清理 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 历史会话面板新增「按最后活动时间批量清理」——一键删除当前 notebook 下最近 N 天(3/7/30)内无活动的会话及其历史问答。

**Architecture:** 后端新增一个仓储方法 `bulk_delete_conversations`(按 `updated_at < now-N天` + 当前用户 + 当前 notebook 过滤,级联删 answers)与一个 `DELETE /notebooks/{id}/conversations?older_than_days=N` 路由。前端把「将删条数」的纯计算抽到可单测的 `conversation-cleanup.ts`,面板内渲染 3 个预设按钮 → 确认 → 调用接口 → 重载 + toast。基准是**最后活动时间 `updated_at`**(非创建时间),设计依据见 spec。

**Tech Stack:** Python / FastAPI / SQLite(后端)、pytest(后端测试);TypeScript / React / Next.js(前端)、`node --test`(前端 `.test.mjs` 测试)。

**关联 spec:** [docs/superpowers/specs/2026-06-24-batch-delete-conversations-design.md](docs/superpowers/specs/2026-06-24-batch-delete-conversations-design.md)

---

## File Structure

**后端**
- `backend/app/services/sqlite_repository.py` — 改 import(加 `timedelta`)+ 新增 `bulk_delete_conversations` 方法。
- `backend/app/api/routes.py` — 新增 `DELETE /notebooks/{notebook_id}/conversations` 路由。
- `backend/tests/test_conversations.py` — 新增仓储级 + 路由级测试。

**前端**
- `frontend/app/conversation-cleanup.ts` — **新建**:纯函数 `conversationsOlderThan` + 常量 `CLEANUP_PRESETS`。
- `frontend/app/conversation-cleanup.test.mjs` — **新建**:上面纯函数的 `node:test`。
- `frontend/app/page.tsx` — 引入 helper、加 `requestBulkCleanup`/`bulkCleanup` 处理函数、面板内加预设按钮 UI。
- `frontend/app/globals.css` — 新增 `.chat-session-cleanup` 样式。

依赖顺序:Task 1 → 2(路由调用仓储方法)→ 3 → 4(UI 引用 helper)。

---

## Task 1: 后端仓储方法 `bulk_delete_conversations`

**Files:**
- Modify: `backend/app/services/sqlite_repository.py:12`(import)、插入点在 `delete_conversation` 之后(`:6540` 与 `:6542` 之间)
- Test: `backend/tests/test_conversations.py`(在文件末尾追加)

- [ ] **Step 1: 写失败测试**

先把测试文件顶部 import 补上 `_now`(用于构造"今天"的时间戳)。修改 `backend/tests/test_conversations.py:3`:

```python
from app.services.sqlite_repository import SQLiteRepository, _now
```

在 `backend/tests/test_conversations.py` 末尾追加:

```python
def test_bulk_delete_conversations_by_last_activity(repo):
    nb = _seed(repo)
    other = repo.create_notebook(NotebookCreate(name="other"))
    me = repo.current_user().id

    def add_conv(cid, notebook_id, updated_at, *, created_by=None, created_at="2000-01-01T00:00:00"):
        with repo._connect() as db:
            db.execute(
                "INSERT INTO conversations (id, notebook_id, title, created_by, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?)",
                (cid, notebook_id, cid, created_by or me, created_at, updated_at),
            )
            db.execute(
                "INSERT INTO answers (id, notebook_id, question, payload, created_at, conversation_id) "
                "VALUES (?,?,?,?,?,?)",
                (f"ans-{cid}", notebook_id, "q", "{}", updated_at, cid),
            )

    add_conv("conv-old", nb.id, "2000-01-01T00:00:00")                          # 久未活动 -> 删
    add_conv("conv-new", nb.id, _now())                                         # 今天活动 -> 留
    add_conv("conv-revived", nb.id, _now(), created_at="2000-01-01T00:00:00")   # 旧建、近活动 -> 留(看 updated_at 非 created_at)
    add_conv("conv-otnb", other.id, "2000-01-01T00:00:00")                      # 别的 notebook -> 不动
    add_conv("conv-other-user", nb.id, "2000-01-01T00:00:00", created_by="someone-else")  # 别的用户 -> 不动

    deleted = repo.bulk_delete_conversations(nb.id, older_than_days=3)
    assert deleted == 1                                                         # 只命中 conv-old

    survivors = {c.id for c in repo.list_conversations(nb.id)}
    assert survivors == {"conv-new", "conv-revived"}                           # 按 updated_at 判定

    with repo._connect() as db:
        assert db.execute("SELECT count(*) FROM answers WHERE conversation_id='conv-old'").fetchone()[0] == 0   # 级联删
        assert db.execute("SELECT count(*) FROM answers WHERE conversation_id='conv-new'").fetchone()[0] == 1   # 幸存者 answers 保留
        assert db.execute("SELECT count(*) FROM conversations WHERE id='conv-otnb'").fetchone()[0] == 1         # 跨 notebook 隔离
        assert db.execute("SELECT count(*) FROM conversations WHERE id='conv-other-user'").fetchone()[0] == 1   # 跨用户隔离


def test_bulk_delete_conversations_missing_notebook(repo):
    with pytest.raises(KeyError):
        repo.bulk_delete_conversations("nb-bogus", older_than_days=3)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_conversations.py::test_bulk_delete_conversations_by_last_activity tests/test_conversations.py::test_bulk_delete_conversations_missing_notebook -v`
Expected: FAIL,`AttributeError: 'SQLiteRepository' object has no attribute 'bulk_delete_conversations'`

- [ ] **Step 3: 加 `timedelta` 导入**

改 `backend/app/services/sqlite_repository.py:12`:

```python
from datetime import datetime, timedelta
```

- [ ] **Step 4: 实现方法**

在 `backend/app/services/sqlite_repository.py` 的 `delete_conversation`(结束于 `:6540`)之后、`submit_feedback`(`:6542`)之前插入:

```python
    def bulk_delete_conversations(self, notebook_id: str, older_than_days: int) -> int:
        """Delete the current user's conversations in `notebook_id` whose last
        activity (`updated_at`) is strictly older than `older_than_days` days,
        cascading to their answers. Returns the number deleted. Raises KeyError
        if the notebook does not exist."""
        self.get_notebook(notebook_id)
        cutoff = (datetime.now() - timedelta(days=older_than_days)).replace(microsecond=0).isoformat()
        with self._write() as db:
            ids = [
                row["id"]
                for row in db.execute(
                    "SELECT id FROM conversations "
                    "WHERE notebook_id = ? AND created_by = ? AND updated_at < ?",
                    (notebook_id, self.current_user().id, cutoff),
                ).fetchall()
            ]
            for cid in ids:
                db.execute("DELETE FROM answers WHERE conversation_id = ?", (cid,))
            db.executemany("DELETE FROM conversations WHERE id = ?", [(cid,) for cid in ids])
        return len(ids)
```

> 说明:`cutoff` 用 `datetime.now()`(本地、无时区,与 `_now()` [:7156] 同格式 `YYYY-MM-DDTHH:MM:SS`),故 SQL 字符串比较 `updated_at < cutoff` 成立;级联删法复刻 `delete_conversation` [:6535];范围谓词复刻 `list_conversations` [:6510]。

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_conversations.py::test_bulk_delete_conversations_by_last_activity tests/test_conversations.py::test_bulk_delete_conversations_missing_notebook -v`
Expected: PASS(2 passed)

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_conversations.py
git commit -m "feat(conversations): bulk_delete_conversations repo method keyed on last activity" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: 后端路由 `DELETE /notebooks/{id}/conversations`

**Files:**
- Modify: `backend/app/api/routes.py` — 插入点在 `delete_conversation` 路由之后(`:554` 与 `:558` 之间)。`Query` 已在 `:9` 导入,无需改 import。
- Test: `backend/tests/test_conversations.py`(末尾追加)

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_conversations.py` 末尾追加:

```python
def test_bulk_delete_conversations_route(tmp_path, monkeypatch):
    import sqlite3
    from fastapi.testclient import TestClient
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.main import app
    client = TestClient(app)
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    cid = client.post(f"/api/notebooks/{nb}/ask", json={"question": "q"}).json()["conversation_id"]

    # 把这条会话的最后活动改到很久以前(外部连接直接写,请求间无并发写)
    con = sqlite3.connect(tmp_path / "t.db")
    con.execute("UPDATE conversations SET updated_at='2000-01-01T00:00:00' WHERE id=?", (cid,))
    con.commit(); con.close()

    # 非法阈值 -> 422(Query ge=1)
    assert client.delete(f"/api/notebooks/{nb}/conversations", params={"older_than_days": 0}).status_code == 422
    # notebook 不存在 -> 404
    assert client.delete("/api/notebooks/bogus/conversations", params={"older_than_days": 3}).status_code == 404
    # 正常路径 -> 删掉这条老会话
    resp = client.delete(f"/api/notebooks/{nb}/conversations", params={"older_than_days": 3})
    assert resp.status_code == 200 and resp.json()["deleted"] == 1
    assert client.get(f"/api/notebooks/{nb}/conversations").json() == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_conversations.py::test_bulk_delete_conversations_route -v`
Expected: FAIL(路由不存在 → DELETE 该路径返回 405,首个断言期望 422 故失败)

- [ ] **Step 3: 实现路由**

在 `backend/app/api/routes.py` 的 `delete_conversation` 路由(结束于 `:554`)之后插入:

```python
@router.delete("/notebooks/{notebook_id}/conversations")
def bulk_delete_conversations(notebook_id: str, older_than_days: int = Query(..., ge=1)):
    try:
        deleted = repository().bulk_delete_conversations(notebook_id, older_than_days)
        return {"deleted": deleted}
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
```

> 与单条删除 `DELETE /conversations/{id}` 路径不冲突;`Query(..., ge=1)` 拒绝 0/负数(→422)。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_conversations.py::test_bulk_delete_conversations_route -v`
Expected: PASS(1 passed)

- [ ] **Step 5: 跑整个会话测试文件确认无回归**

Run: `cd backend && python -m pytest tests/test_conversations.py -v`
Expected: PASS(全部通过,含既有用例)

- [ ] **Step 6: 提交**

```bash
git add backend/app/api/routes.py backend/tests/test_conversations.py
git commit -m "feat(api): DELETE /notebooks/{id}/conversations bulk-cleanup route" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: 前端纯函数 helper + 单测

**Files:**
- Create: `frontend/app/conversation-cleanup.ts`
- Test: `frontend/app/conversation-cleanup.test.mjs`

- [ ] **Step 1: 写失败测试**

新建 `frontend/app/conversation-cleanup.test.mjs`:

```javascript
import test from "node:test";
import assert from "node:assert/strict";

import { conversationsOlderThan, CLEANUP_PRESETS } from "./conversation-cleanup.ts";

// 固定 "now",使测试与真实时钟/时区无关。
// updated_at 是无时区本地 ISO,被当作本地时间解析 —— 与 NOW(同样本地)同基准。
const NOW = Date.parse("2026-06-24T12:00:00");
const conv = (id, updated_at) => ({ id, updated_at });

test("只挑出最后活动早于 N 天的会话", () => {
  const sessions = [
    conv("old", "2026-06-20T12:00:00"),    // 4 天前 -> 早于 3 天
    conv("fresh", "2026-06-23T12:00:00"),  // 1 天前 -> 3 天内
  ];
  const ids = conversationsOlderThan(sessions, 3, NOW).map((s) => s.id);
  assert.deepEqual(ids, ["old"]);
});

test("恰好 N 天边界保留(严格小于)", () => {
  const exactly3 = conv("edge", "2026-06-21T12:00:00"); // 正好 NOW - 3*24h
  assert.equal(conversationsOlderThan([exactly3], 3, NOW).length, 0);
});

test("预设为 3 / 7 / 30 天", () => {
  assert.deepEqual([...CLEANUP_PRESETS], [3, 7, 30]);
});
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd frontend && node --test app/conversation-cleanup.test.mjs`
Expected: FAIL,`Cannot find module './conversation-cleanup.ts'`

- [ ] **Step 3: 实现 helper**

新建 `frontend/app/conversation-cleanup.ts`:

```typescript
// 历史会话「批量清理」的纯计算 helper。
// 清理以会话的「最后活动时间」(updated_at)为准,而非创建时间:
// 「陈旧」= 已 N 天没有任何新对话。

export const CLEANUP_PRESETS = [3, 7, 30] as const;

const DAY_MS = 86_400_000;

/** 最后活动严格早于 `days` 天的会话。`updated_at` 为无时区本地 ISO,与 Date.now() 同基准。 */
export function conversationsOlderThan<T extends { updated_at: string }>(
  sessions: T[],
  days: number,
  nowMs: number = Date.now(),
): T[] {
  const cutoff = nowMs - days * DAY_MS;
  return sessions.filter((s) => new Date(s.updated_at).getTime() < cutoff);
}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd frontend && node --test app/conversation-cleanup.test.mjs`
Expected: PASS(3 tests passed)

- [ ] **Step 5: 提交**

```bash
git add frontend/app/conversation-cleanup.ts frontend/app/conversation-cleanup.test.mjs
git commit -m "feat(web): conversation-cleanup pure helper + node:test" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: 前端面板 UI 接线 + 样式

**Files:**
- Modify: `frontend/app/page.tsx:30`(import)、`:1705` 之后(处理函数)、`:2446`–`:2447` 之间(UI)
- Modify: `frontend/app/globals.css:978` 之后(样式)

> 本任务无独立单测(核心计算已由 Task 3 覆盖),以 `npm run lint`(tsc)为硬门 + 预览人工确认。

- [ ] **Step 1: 引入 helper**

在 `frontend/app/page.tsx:30`(`./edge-review-queue` 那行)之后加一行:

```typescript
import { conversationsOlderThan, CLEANUP_PRESETS } from "./conversation-cleanup";
```

> 注意:page.tsx 用**无扩展名**导入(TS 解析);Task 3 的 `.test.mjs` 用 `./conversation-cleanup.ts`**带扩展名**(node 解析)。两者都正确。

- [ ] **Step 2: 加处理函数**

在 `frontend/app/page.tsx` 的 `requestDeleteSession`(结束于 `:1705`)之后插入:

```typescript
  function requestBulkCleanup(days: number) {
    const victims = conversationsOlderThan(sessions, days);
    if (victims.length === 0) return;
    setInfoModal({
      title: "批量清理会话",
      message: `将删除 ${victims.length} 条最近 ${days} 天内无活动的会话，对应的历史问答会一起移除。`,
      actions: [
        { label: "取消", action: () => undefined },
        { label: "删除", danger: true, action: () => { bulkCleanup(days, victims).catch(reportError); } },
      ],
    });
  }

  async function bulkCleanup(days: number, victims: ConversationSummary[]) {
    const { deleted } = await api<{ deleted: number }>(
      `/notebooks/${currentNotebookId}/conversations?older_than_days=${days}`,
      { method: "DELETE" },
    );
    if (conversationId && victims.some((s) => s.id === conversationId)) {
      setTurns([]);
      setConversationId(null);
      setPendingQuestion("");
      setPendingMode(DEFAULT_ASK_MODE);
      setPendingTrace([]);
    }
    await loadSessions(currentNotebookId);
    setToast(`已删除 ${deleted} 条会话`);
  }
```

> `victims` 在确认时一次性算好并传入,确保"是否删到当前会话"用的是与预览数完全一致的集合;重置分支复刻 `deleteSession` [:1685]。

- [ ] **Step 3: 加 UI(预设按钮行)**

在 `frontend/app/page.tsx` 面板头 `</div>`(`:2446`)与 `<div className="chat-session-list">`(`:2447`)之间插入:

```tsx
                  {sessions.length > 0 && (
                    <div className="chat-session-cleanup">
                      <span>批量清理</span>
                      {CLEANUP_PRESETS.map((days) => {
                        const n = conversationsOlderThan(sessions, days).length;
                        return (
                          <button
                            key={days}
                            type="button"
                            className="chat-session-cleanup-btn"
                            disabled={n === 0}
                            title={`删除最近 ${days} 天内无活动的会话`}
                            onClick={() => requestBulkCleanup(days)}
                          >
                            {days} 天前{n > 0 ? ` (${n})` : ""}
                          </button>
                        );
                      })}
                    </div>
                  )}
```

- [ ] **Step 4: 加样式**

在 `frontend/app/globals.css:978`(`.chat-session-popover-head small {…}` 块结束)之后、`.chat-session-list {`(`:980`)之前插入:

```css
.chat-session-cleanup {
  display: flex;
  align-items: center;
  flex-wrap: wrap;
  gap: 6px;
  font-size: 12px;
  color: var(--muted);
}

.chat-session-cleanup-btn {
  border: 1px solid #d8dee8;
  border-radius: 999px;
  padding: 2px 10px;
  background: #fff;
  color: #475569;
  font-size: 12px;
  cursor: pointer;
}

.chat-session-cleanup-btn:hover:not(:disabled) {
  border-color: #ef4444;
  color: #ef4444;
}

.chat-session-cleanup-btn:disabled {
  opacity: 0.45;
  cursor: not-allowed;
}
```

- [ ] **Step 5: 类型检查(硬门)**

Run: `cd frontend && npm run lint`
Expected: PASS(`tsc --noEmit` 无错误输出)

- [ ] **Step 6: 预览人工确认**

> 需前端 dev server + 后端(:8000)。按用户的服务启停偏好处理:不自行重启用户服务;若已运行则直接验证。

验证点:
1. 进入 ask 模式,打开会话面板,顶部出现「批量清理 3 天前 / 7 天前 / 30 天前」按钮行,条数与实际相符;无对应会话的档位置灰禁用。
2. 点某档 → 弹确认框,文案显示正确条数 → 点「删除」→ 列表刷新、toast「已删除 N 条会话」。
3. 若删到当前正在查看的会话,回答区被重置为新会话态。

- [ ] **Step 7: 提交**

```bash
git add frontend/app/page.tsx frontend/app/globals.css
git commit -m "feat(web): batch-cleanup buttons in conversation history panel" -m "Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Self-Review(写完即查)

**1. Spec 覆盖**
- 后端 `bulk_delete_conversations`(`updated_at`/当前用户/当前 notebook/级联 answers/返回计数)→ Task 1 ✅
- 后端路由 `DELETE …?older_than_days=N`、N≥1、404 → Task 2 ✅
- 前端 3/7/30 预设 + 每档条数 + 0 禁用 → Task 3(常量/计算)+ Task 4(渲染)✅
- 确认文案「最近 X 天内无活动」、删除→重载→toast、删到当前会话则重置 → Task 4 ✅
- 「按 updated_at 非 created_at」回归保护 → Task 1 `conv-revived` 断言 ✅

**2. 占位扫描**:无 TBD/TODO;每个代码步骤均给出完整代码与确切命令/预期。✅

**3. 类型/命名一致性**:
- 仓储方法名 `bulk_delete_conversations` 在 Task 1(定义)、Task 2(`repository().bulk_delete_conversations(...)`)一致 ✅
- helper `conversationsOlderThan` / 常量 `CLEANUP_PRESETS` 在 Task 3(定义)、Task 4(import 与调用)一致 ✅
- 接口契约 `{ deleted: number }` 在 Task 2(后端返回)与 Task 4(`api<{ deleted: number }>`)一致 ✅
- query 参数名 `older_than_days` 在后端 `Query` 与前端 URL 一致 ✅
