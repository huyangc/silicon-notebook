# 历史会话推理标记 + 恢复时默认开启推理按钮 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在历史会话列表卡片上标记「推理会话」，并在恢复推理会话时默认打开「✦ 推理」按钮。

**Architecture:** 以每轮 `AskResponse.reasoning_trace != null` 为「该轮走了推理」的信号，会话级按「最后一轮」判定。后端 `list_conversations` 用相关子查询取会话最后一条 answer 的推理标志，作为 `ConversationSummary.used_reasoning` 返回给前端画标记；前端 `openSession` 用一个纯函数从已加载的 `detail.turns` 最后一轮还原 `reasoningMode`。不改表结构、不做迁移。

**Tech Stack:** 后端 FastAPI + SQLite（`sqlite_repository.py`）、Pydantic schemas、pytest；前端 Next.js 15 / React 19 / TypeScript，测试用 `node --test`（`.mjs` 直接 import `.ts`）。

**设计依据:** [docs/superpowers/specs/2026-06-08-reasoning-session-badge-restore-design.md](../specs/2026-06-08-reasoning-session-badge-restore-design.md)

**对 spec 的一处细化:** spec 里「最后一轮」子查询写的是 `ORDER BY created_at DESC`；因 `_now()` 是秒级精度（`datetime.now().replace(microsecond=0)`），同秒内插入的多条 answer 会在 `created_at` 上并列、排序不确定。实现改用 `ORDER BY a.rowid DESC`：`answers` 是带 rowid 的普通表，rowid 随插入单调递增，且每轮问答按时序逐条插入，故 rowid 序＝问答时序，无并列。语义（取最后一轮）与 spec 完全一致。

---

## File Structure

- `backend/app/models/schemas.py` — `ConversationSummary` 增加 `used_reasoning` 字段。
- `backend/app/services/sqlite_repository.py` — `list_conversations` 增加子查询并填充 `used_reasoning`。
- `backend/tests/test_conversations.py` — 新增「按最后一轮判定 used_reasoning」测试。
- `frontend/app/session-reasoning.ts`（新建）— 纯函数 `lastTurnUsedReasoning(turns)`，单一职责、可被 `node --test` 覆盖。
- `frontend/app/session-reasoning.test.mjs`（新建）— `lastTurnUsedReasoning` 单测。
- `frontend/app/page.tsx` — `ConversationSummary` 类型加字段、`openSession` 还原按钮、历史卡片渲染标记。
- `frontend/app/globals.css` — 新增 `.chat-session-reasoning-badge` 样式。
- `frontend/app/session-reasoning-ui.test.mjs`（新建）— 读取 `page.tsx`/`globals.css` 文本，回归校验接线（沿用 `workspace-layout.test.mjs` 惯例）。

---

## Task 1: 后端 — `ConversationSummary.used_reasoning` + `list_conversations`

**Files:**
- Test: `backend/tests/test_conversations.py`（追加一个测试函数）
- Modify: `backend/app/models/schemas.py:190-195`（`ConversationSummary`）
- Modify: `backend/app/services/sqlite_repository.py:4030-4052`（`list_conversations`）

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_conversations.py` 末尾追加（该文件已有 `repo` fixture、`_seed`、`AskRequest` 导入；新增 `AskResponse, TraceStep` 在函数内导入）：

```python
def test_list_conversations_used_reasoning_last_turn(repo):
    """used_reasoning 反映会话最后一轮是否走了推理（reasoning_trace 非空）。"""
    from app.models.schemas import AskResponse, TraceStep
    nb = _seed(repo)

    def used_reasoning(conv_id):
        return next(c.used_reasoning for c in repo.list_conversations(nb.id) if c.id == conv_id)

    # 1) 单条快速轮（repo.ask 不写 reasoning_trace）→ 最后一轮快速 → False
    r = repo.ask(nb.id, AskRequest(question="q1"))
    cid = r.conversation_id
    assert used_reasoning(cid) is False

    # 2) 追加一条推理轮（带非空 reasoning_trace）→ 最后一轮推理 → True
    repo._save_answer(
        nb.id, "q2",
        AskResponse(conclusion="c", conversation_id=cid,
                    reasoning_trace=[TraceStep(step_type="answer", summary="s")]),
        conversation_id=cid,
    )
    assert used_reasoning(cid) is True

    # 3) 再追加一条快速轮 → 最后一轮又变快速 → False（证明看的是“最后一轮”而非“任意一轮”）
    repo._save_answer(
        nb.id, "q3",
        AskResponse(conclusion="c", conversation_id=cid),
        conversation_id=cid,
    )
    assert used_reasoning(cid) is False

    # 4) 单条推理会话 → True（直接建会话行 + 一条推理 answer，沿用本文件直插风格）
    with repo._connect() as db:
        db.execute(
            "INSERT INTO conversations (id, notebook_id, title, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?)",
            ("conv-r", nb.id, "r", repo.current_user().id, "t", "t"),
        )
    repo._save_answer(
        nb.id, "qr",
        AskResponse(conclusion="c", conversation_id="conv-r",
                    reasoning_trace=[TraceStep(step_type="answer", summary="s")]),
        conversation_id="conv-r",
    )
    assert used_reasoning("conv-r") is True
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd backend && python -m pytest tests/test_conversations.py::test_list_conversations_used_reasoning_last_turn -v`
Expected: FAIL — `AttributeError: 'ConversationSummary' object has no attribute 'used_reasoning'`（字段尚未定义）。
（若仓库用 venv，先 `source .venv/bin/activate` 或用项目既定方式。）

- [ ] **Step 3: 给 `ConversationSummary` 加字段**

`backend/app/models/schemas.py`，把：

```python
class ConversationSummary(BaseModel):
    id: str
    notebook_id: str
    title: str = ""
    updated_at: str = ""
    turn_count: int = 0
```

改为：

```python
class ConversationSummary(BaseModel):
    id: str
    notebook_id: str
    title: str = ""
    updated_at: str = ""
    turn_count: int = 0
    used_reasoning: bool = False
```

- [ ] **Step 4: 再次运行，确认只剩 True 用例失败**

Run: `cd backend && python -m pytest tests/test_conversations.py::test_list_conversations_used_reasoning_last_turn -v`
Expected: FAIL — 字段现在恒为默认 `False`，断言 `used_reasoning(cid) is True`（第 2 步骤数据）处 AssertionError。说明字段已通但逻辑未填。

- [ ] **Step 5: 在 `list_conversations` 填充 `used_reasoning`**

`backend/app/services/sqlite_repository.py` 的 `list_conversations`，把现有 SQL 与构造改为：

```python
        with self._connect() as db:
            rows = db.execute(
                "SELECT c.id, c.notebook_id, c.title, c.updated_at, "
                "(SELECT COUNT(*) FROM answers a WHERE a.conversation_id = c.id) AS turn_count, "
                "(SELECT json_extract(a.payload, '$.reasoning_trace') IS NOT NULL "
                "   FROM answers a WHERE a.conversation_id = c.id "
                "  ORDER BY a.rowid DESC LIMIT 1) AS used_reasoning "
                "FROM conversations c WHERE c.notebook_id = ? AND c.created_by = ? "
                "ORDER BY c.updated_at DESC",
                (notebook_id, self.current_user().id),
            ).fetchall()
        return [
            ConversationSummary(
                id=row["id"],
                notebook_id=row["notebook_id"],
                title=row["title"] or "",
                updated_at=row["updated_at"] or "",
                turn_count=row["turn_count"],
                used_reasoning=bool(row["used_reasoning"]),
            )
            for row in rows
        ]
```

说明：`json_extract` 在键缺失或值为 JSON `null`（含空 trace，因 `reasoning_trace=trace or None`）时返回 NULL → `IS NOT NULL` 得 0；非空数组得 1。无 answer 的空会话子查询返回 NULL → `bool(None)=False`。

- [ ] **Step 6: 运行测试，确认通过**

Run: `cd backend && python -m pytest tests/test_conversations.py -v`
Expected: PASS（新测试通过，且 `test_list_conversations`、路由等既有测试不回归）。

- [ ] **Step 7: 提交**

```bash
git add backend/app/models/schemas.py backend/app/services/sqlite_repository.py backend/tests/test_conversations.py
git commit -m "feat(conversations): list_conversations 回 used_reasoning(看最后一轮)"
```

---

## Task 2: 前端纯函数 `lastTurnUsedReasoning`

**Files:**
- Create: `frontend/app/session-reasoning.ts`
- Test: `frontend/app/session-reasoning.test.mjs`

- [ ] **Step 1: 写失败测试**

创建 `frontend/app/session-reasoning.test.mjs`：

```js
import test from "node:test";
import assert from "node:assert/strict";

import { lastTurnUsedReasoning } from "./session-reasoning.ts";

const fast = { response: {} };
const fastEmptyTrace = { response: { reasoning_trace: [] } };
const reasoning = { response: { reasoning_trace: [{ step_type: "answer", summary: "s" }] } };

test("空会话 → false", () => {
  assert.equal(lastTurnUsedReasoning([]), false);
});

test("最后一轮快速(无 trace) → false", () => {
  assert.equal(lastTurnUsedReasoning([reasoning, fast]), false);
});

test("最后一轮推理(非空 trace) → true", () => {
  assert.equal(lastTurnUsedReasoning([fast, reasoning]), true);
});

test("最后一轮 trace 为空数组 → false", () => {
  assert.equal(lastTurnUsedReasoning([fastEmptyTrace]), false);
});

test("单条推理 → true", () => {
  assert.equal(lastTurnUsedReasoning([reasoning]), true);
});
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd frontend && node --test app/session-reasoning.test.mjs`
Expected: FAIL — 找不到模块 `./session-reasoning.ts`（`ERR_MODULE_NOT_FOUND`）。

- [ ] **Step 3: 写最小实现**

创建 `frontend/app/session-reasoning.ts`：

```ts
// 纯函数：根据会话“最后一轮”是否走了推理，决定恢复会话时推理按钮的默认开关。
// 信号 = 该轮 AskResponse.reasoning_trace 为非空数组（详见 2026-06-08 设计）。
type TurnLike = { response: { reasoning_trace?: unknown[] | null } };

export function lastTurnUsedReasoning(turns: TurnLike[]): boolean {
  const last = turns[turns.length - 1];
  return !!(last?.response?.reasoning_trace && last.response.reasoning_trace.length > 0);
}
```

- [ ] **Step 4: 运行测试，确认通过**

Run: `cd frontend && node --test app/session-reasoning.test.mjs`
Expected: PASS（5 个用例全过）。

- [ ] **Step 5: 提交**

```bash
git add frontend/app/session-reasoning.ts frontend/app/session-reasoning.test.mjs
git commit -m "feat(frontend): lastTurnUsedReasoning 纯函数(看最后一轮)"
```

---

## Task 3: 前端接线 — 卡片标记 + 恢复时还原推理按钮

**Files:**
- Test: `frontend/app/session-reasoning-ui.test.mjs`（新建，文本回归校验）
- Modify: `frontend/app/page.tsx:123`（类型）、`:1529-1539`（`openSession`）、`:2264-2267`（卡片 JSX）、文件顶部 import 区
- Modify: `frontend/app/globals.css`（`.chat-session-card small` 规则附近，约 :1030-1034）

- [ ] **Step 1: 写失败测试**

创建 `frontend/app/session-reasoning-ui.test.mjs`（沿用 `workspace-layout.test.mjs` 的「读源码做文本断言」惯例）：

```js
import test from "node:test";
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const page = await readFile(new URL("./page.tsx", import.meta.url), "utf8");
const css = await readFile(new URL("./globals.css", import.meta.url), "utf8");

test("ConversationSummary 类型带 used_reasoning", () => {
  assert.match(page, /type ConversationSummary = \{[^}]*used_reasoning\?: boolean/);
});

test("import 了纯函数 lastTurnUsedReasoning", () => {
  assert.match(page, /from "\.\/session-reasoning"/);
});

test("openSession 用最后一轮还原推理按钮", () => {
  assert.match(page, /setReasoningMode\(lastTurnUsedReasoning\(detail\.turns\)\)/);
});

test("历史卡片渲染推理标记", () => {
  assert.match(page, /session\.used_reasoning/);
  assert.match(page, /chat-session-reasoning-badge/);
});

test("标记样式已定义", () => {
  assert.match(css, /\.chat-session-reasoning-badge\s*\{/);
});
```

- [ ] **Step 2: 运行测试，确认失败**

Run: `cd frontend && node --test app/session-reasoning-ui.test.mjs`
Expected: FAIL — 上述字符串/样式尚不存在，多个断言报 `AssertionError`。

- [ ] **Step 3: 顶部 import 纯函数**

`frontend/app/page.tsx` 顶部 import 区（紧随现有 `import ... from "./reasoning-trace"` 等本地模块 import 之后）加一行：

```ts
import { lastTurnUsedReasoning } from "./session-reasoning";
```

- [ ] **Step 4: 给 `ConversationSummary` 类型加字段**

`frontend/app/page.tsx:123`，把：

```ts
type ConversationSummary = { id: string; title: string; updated_at: string; turn_count: number };
```

改为：

```ts
type ConversationSummary = { id: string; title: string; updated_at: string; turn_count: number; used_reasoning?: boolean };
```

- [ ] **Step 5: `openSession` 还原推理按钮**

`frontend/app/page.tsx` 的 `openSession`，在 `setTurns(...)` 之后插入一行 `setReasoningMode(...)`：

```ts
  async function openSession(id: string) {
    const detail = await api<ConversationDetail>(`/conversations/${id}`);
    setTurns(detail.turns.map((turn) => ({ question: turn.question, response: turn.response })));
    setReasoningMode(lastTurnUsedReasoning(detail.turns));
    setConversationId(id);
    setPendingQuestion("");
    setPendingReasoning(false);
    setPendingTrace([]);
    setChatMode("ask");
    setSessionPanelOpen(false);
    setRenamingSessionId(null);
  }
```

- [ ] **Step 6: 卡片渲染推理标记**

`frontend/app/page.tsx:2264-2267`，把卡片主体按钮：

```tsx
                            <button className="chat-session-card-main" type="button" onClick={() => openSession(session.id).catch(reportError)}>
                              <span>{session.title || "未命名会话"}</span>
                              <small>{formatRelativeTime(session.updated_at)} · {session.turn_count} 轮</small>
                            </button>
```

改为：

```tsx
                            <button className="chat-session-card-main" type="button" onClick={() => openSession(session.id).catch(reportError)}>
                              <span>{session.title || "未命名会话"}</span>
                              <small>
                                {formatRelativeTime(session.updated_at)} · {session.turn_count} 轮
                                {session.used_reasoning && <span className="chat-session-reasoning-badge">✦ 推理</span>}
                              </small>
                            </button>
```

- [ ] **Step 7: 加标记样式**

`frontend/app/globals.css`，在 `.chat-session-card small,` / `.chat-session-card-main small { ... }` 规则块（约 1030-1034 行）之后插入：

```css
.chat-session-reasoning-badge {
  display: inline-block;
  margin-left: 6px;
  padding: 0 7px;
  border-radius: 999px;
  background: #101820;
  color: #fff;
  font-size: 11px;
  font-weight: 600;
  line-height: 18px;
  vertical-align: middle;
}
```

（深色 pill，呼应「✦ 推理」按钮激活态 `.reasoning-toggle.active` 的 `#101820`，作为静态标记更紧凑。）

- [ ] **Step 8: 运行 UI 文本测试 + 纯函数测试 + 类型检查**

Run: `cd frontend && node --test app/session-reasoning-ui.test.mjs app/session-reasoning.test.mjs && npx tsc --noEmit`
Expected: 两个测试文件全 PASS；`tsc --noEmit` 无错误（`detail.turns` 结构兼容 `TurnLike[]`，`used_reasoning?` 可选不破坏既有构造）。

- [ ] **Step 9: 提交**

```bash
git add frontend/app/page.tsx frontend/app/globals.css frontend/app/session-reasoning-ui.test.mjs
git commit -m "feat(frontend): 历史卡片推理标记 + 恢复会话默认开推理按钮"
```

---

## Task 4: 全量验证（自动化 + 预览）

**Files:** 无改动，仅运行验证。

- [ ] **Step 1: 后端全量测试**

Run: `cd backend && python -m pytest tests/test_conversations.py -v`
Expected: PASS，无回归。

- [ ] **Step 2: 前端全量测试 + 类型检查**

Run: `cd frontend && node --test app/*.test.mjs && npx tsc --noEmit`
Expected: 所有 `.mjs` 测试 PASS；`tsc --noEmit` 无错误。

- [ ] **Step 3: 预览验证（可观察改动，必做）**

用 preview 工具（非 Bash）启动 root master 的前后端服务（按用户「基于 root master 启服务」偏好），制造一个推理会话与一个快速会话后：
1. `preview_start` 起前端；确保后端可用。
2. 打开「会话」弹窗，`preview_snapshot` 确认推理会话卡片上出现「✦ 推理」标记、快速会话卡片无标记。
3. `preview_click` 点开推理会话卡片，`preview_snapshot`/`preview_inspect` 确认输入框旁「✦ 推理」按钮变为激活态（`reasoning-toggle.active`）。
4. 点开快速会话卡片，确认推理按钮回到未激活态。
5. `preview_screenshot` 留存标记 + 激活态两张图作为佐证。

Expected: 标记与按钮还原行为符合「看最后一轮」规则。

- [ ] **Step 4: 收尾提 PR**

按既定开发流程（先与 master 三方合并 → push → `gh pr create --base master`）提交 PR。

---

## Self-Review

- **Spec 覆盖：** ① 历史卡片标记 → Task 1（后端 `used_reasoning`）+ Task 3 Step 6/7（前端标记+样式）；② 恢复默认开按钮 → Task 2（纯函数）+ Task 3 Step 5（`openSession` 接线）；③ 「看最后一轮」规则 → Task 1 子查询（`rowid DESC`）+ Task 2 纯函数，两处一致；④ 测试 → Task 1（后端 4 场景）+ Task 2（纯函数 5 用例）+ Task 3（UI 文本回归）。spec 各项均有对应任务。
- **占位符扫描：** 无 TBD/TODO；每个改动步骤均给出完整代码与确切命令/预期。
- **类型/命名一致：** `used_reasoning`（后端 schema + 前端类型 + SQL 列别名）、`lastTurnUsedReasoning`（`.ts` 定义 / `.test.mjs` 与 `page.tsx` 调用）、`chat-session-reasoning-badge`（JSX className + CSS 选择器 + UI 测试断言）三处命名跨任务一致。
- **细化说明：** 「最后一轮」排序键由 spec 的 `created_at DESC` 改为 `rowid DESC`（理由见顶部），语义不变。

---

## 评审后增补

执行中两阶段 review 引入的增改（均已 TDD + 提交）：
- **Task 1**：`get_conversation` 也填充 `used_reasoning`（从 `turns[-1].response.reasoning_trace` 派生）+ answers 查询加 `ORDER BY created_at ASC, rowid ASC` 确定性次序，修复 `/conversations/{id}` 契约恒返回 false（commit `1846895`）。
- **Task 3**：徽标选择器由 `.chat-session-reasoning-badge` 提升为 `.chat-session-card-main .chat-session-reasoning-badge`，修复被卡片 `span` 规则盖成不可见（commit `7d01ca9`）；`startNewSession` 增加 `setReasoningMode(false)` + 回归断言——用户拍板「新会话回默认推理关」，避免跨会话静默带入推理（commit `32938d1`）。
