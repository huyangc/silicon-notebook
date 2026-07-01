# 来源/知识库列表真·分页 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`).

**Goal:** 把来源面板与知识库标签从「加载更多」/全量渲染改为页码分页(共享 Pagination 组件),顺带修知识库大库卡顿。

**Architecture:** 新建共享 `Pagination` 组件;来源仅前端页码化(后端已分页);知识库后端补分页(status 服务端过滤 + total)+ 前端页码化。分页后每页 ~50 条,DOM/KaTeX 被页大小天然限住。

**Tech Stack:** Next.js/React, FastAPI, SQLite。

参考 spec:`.claude/worktrees/list-pagination/docs/superpowers/specs/2026-06-30-list-pagination-design.md`。基于最新 master。

## 并行分组
- **P-parallel(不同文件,可并发)**:Task 1(`frontend/app/Pagination.tsx` 组件)与 Task 2(知识库后端分页,`backend/`)。
- **串行**:Task 3(前端接线,改 `page.tsx`,依赖 1+2)→ Task 4(tsc/视觉/全量回归)。
- 执行:并发派 Task 1 + Task 2;回来后 Task 3 → Task 4。

## File Structure
- `frontend/app/Pagination.tsx`(新)— 共享页码控件。
- `backend/app/models/schemas.py`(改)— `PaginatedKnowledge`。
- `backend/app/api/routes.py`(改)— `/knowledge` 加 status/offset/limit,返回 `PaginatedKnowledge`。
- `backend/app/services/sqlite_repository.py` + `backend/app/services/repository.py`(改)— `list_knowledge` 加分页/status/total。
- `frontend/app/page.tsx`(改)— 来源 + 知识库页码化。
- 测试:`backend/tests/test_knowledge_pagination.py`(新);前端 `frontend/app/*.test.mjs`(Pagination)。

---

## Task 1: 共享 `Pagination` 组件 [P-parallel]

**Files:** Create `frontend/app/Pagination.tsx`; Test `frontend/app/pagination.test.mjs`.

- [ ] **Step 1: 失败测试** — `frontend/app/pagination.test.mjs`(纯逻辑函数单测,遵现有 `*.test.mjs` `node --test` 风格;组件本身用一个纯函数 `pageMeta` 承载可测逻辑):
```js
import { test } from "node:test";
import assert from "node:assert";
import { pageMeta } from "./pagination-logic.mjs";

test("pageMeta computes last page + clamps", () => {
  assert.deepEqual(pageMeta({ page: 0, pageSize: 50, total: 120 }),
    { lastPage: 2, canPrev: false, canNext: true, from: 1, to: 50 });
  assert.deepEqual(pageMeta({ page: 2, pageSize: 50, total: 120 }),
    { lastPage: 2, canPrev: true, canNext: false, from: 101, to: 120 });
  assert.equal(pageMeta({ page: 9, pageSize: 50, total: 120 }).lastPage, 2); // over-range tolerated by caller clamp
});
```
Run: `cd frontend && node --test app/pagination.test.mjs` → FAIL (module missing).

- [ ] **Step 2: 纯逻辑单一来源 `frontend/app/pagination-logic.mjs`**(测试与组件共用,避免重复):
```js
export function pageMeta({ page, pageSize, total }) {
  const lastPage = Math.max(0, Math.ceil(total / pageSize) - 1);
  const from = total === 0 ? 0 : page * pageSize + 1;
  const to = Math.min(total, (page + 1) * pageSize);
  return { lastPage, canPrev: page > 0, canNext: page < lastPage, from, to };
}
export const clampPage = (p, lastPage) => Math.max(0, Math.min(lastPage, p));
```
配套类型声明 `frontend/app/pagination-logic.d.ts`(让 tsx 导入时 tsc 干净):
```ts
export function pageMeta(a: { page: number; pageSize: number; total: number }):
  { lastPage: number; canPrev: boolean; canNext: boolean; from: number; to: number };
export function clampPage(p: number, lastPage: number): number;
```

- [ ] **Step 3: 组件 `frontend/app/Pagination.tsx`**(从上面的单一来源 import,不重复逻辑):
```tsx
"use client";
import { useState } from "react";
import { pageMeta, clampPage } from "./pagination-logic.mjs";

export function Pagination({ page, pageSize, total, onPage, busy }: {
  page: number; pageSize: number; total: number; onPage: (p: number) => void; busy?: boolean;
}) {
  const { lastPage, canPrev, canNext, from, to } = pageMeta({ page, pageSize, total });
  const [jump, setJump] = useState("");
  if (lastPage === 0) return null;              // 单页不显控件
  const go = (p: number) => onPage(clampPage(p, lastPage));
  const submitJump = () => {
    const n = parseInt(jump, 10);
    if (!Number.isNaN(n)) go(n - 1);            // 用户输入 1-indexed
    setJump("");
  };
  return (
    <div className="pagination">
      <span className="pagination-info">{from}–{to} / {total}</span>
      <button className="sort-button" disabled={busy || !canPrev} onClick={() => go(page - 1)}>上一页</button>
      <span className="pagination-page">第 {page + 1} / {lastPage + 1} 页</span>
      <button className="sort-button" disabled={busy || !canNext} onClick={() => go(page + 1)}>下一页</button>
      <input
        className="pagination-jump" type="number" min={1} max={lastPage + 1}
        value={jump} placeholder="跳页" disabled={busy}
        onChange={(e) => setJump(e.target.value)}
        onKeyDown={(e) => { if (e.key === "Enter") submitJump(); }}
        onBlur={submitJump}
      />
    </div>
  );
}
```

- [ ] **Step 4: CSS** — `frontend/app/globals.css` 加:
```css
.pagination { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; padding: 8px 2px; }
.pagination-info { font-size: 12px; color: var(--muted, #667085); margin-right: auto; }
.pagination-page { font-size: 13px; color: var(--text, #20242a); }
.pagination-jump { width: 68px; border: 1px solid #d9dfeb; border-radius: 8px; padding: 5px 8px; font-size: 13px; }
```

- [ ] **Step 5:** `cd frontend && node --test app/pagination.test.mjs` → PASS。（若 lint 有 tsc,组件类型需干净——Task 4 会整体 tsc。）
- [ ] **Step 6: 提交** — `git add frontend/app/Pagination.tsx frontend/app/pagination-logic.mjs frontend/app/pagination-logic.d.ts frontend/app/pagination.test.mjs frontend/app/globals.css && git commit -m "feat(ui): 共享 Pagination 页码组件(上一页/第X-Y页/下一页+跳页)"`

---

## Task 2: 知识库后端分页 [P-parallel]

**Files:** Modify `schemas.py`, `routes.py`, `sqlite_repository.py`, `repository.py`; Test `backend/tests/test_knowledge_pagination.py`.

**先读**:`PaginatedSources`(schemas.py:67,照抄形状)、`list_knowledge` 三处(routes.py:390、sqlite_repository.py:2708、repository.py:98 抽象基类)、`list_sources_paginated`(sqlite_repository.py:1553,COUNT+LIMIT/OFFSET 范式)、`_knowledge_objects`(status 过滤参数)。

- [ ] **Step 1: 失败测试** — `backend/tests/test_knowledge_pagination.py`(fixture 仿 `test_unified_kg_repository`):存 >50 个 concept(如 60 个),断言:
```python
def test_list_knowledge_paginated(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [{"local_id":f"c{i}","object_type":"concept","payload":{"name":f"c{i}","section_path":""},"evidence":[]} for i in range(60)], [])
    p0 = repo.list_knowledge(nb.id, "concept", status=None, offset=0, limit=50)
    assert p0.total_count == 60 and len(p0.items) == 50 and p0.offset == 0
    p1 = repo.list_knowledge(nb.id, "concept", status=None, offset=50, limit=50)
    assert len(p1.items) == 10
    # 越界 offset → 空
    assert len(repo.list_knowledge(nb.id, "concept", status=None, offset=999, limit=50).items) == 0
```
Run → FAIL(签名/返回不符)。

- [ ] **Step 2: `PaginatedKnowledge`(schemas.py)** — 照 `PaginatedSources`:
```python
class PaginatedKnowledge(BaseModel):
    items: List[KnowledgeRecord]
    total_count: int
    offset: int
    limit: int
```

- [ ] **Step 3: `list_knowledge` 改分页(sqlite_repository.py + repository.py 抽象)** — 新签名 `list_knowledge(self, notebook_id, object_type, status=None, offset=0, limit=50) -> PaginatedKnowledge`。实现:COUNT(notebook_id, object_type, status 过滤,复用 `_knowledge_objects` 的 status 语义)一次 + 取一页(status 过滤 + `LIMIT ? OFFSET ?`);组装 `PaginatedKnowledge(items=[_knowledge_record(...)], total_count=cnt, offset=offset, limit=limit)`。`status=None` 表示全部(与现行为一致)。更新 `repository.py:98` 抽象签名。
- [ ] **Step 4: route** — `routes.py:389`:加 `status: Optional[str] = Query(None)`、`offset: int = Query(0, ge=0)`、`limit: int = Query(50, ge=1, le=200)`;`response_model=PaginatedKnowledge`;`return repository().list_knowledge(notebook_id, object_type, status=status, offset=offset, limit=limit)`。
- [ ] **Step 5:** `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_knowledge_pagination.py -q` → PASS;再跑相关回归 `pytest tests/ -q -m "not slow" -k "knowledge or unified or kg" -p no:cacheprovider | tail -3`(确认没破坏其它 list_knowledge 调用)。
- [ ] **Step 6: 提交** — `git add backend/app/models/schemas.py backend/app/api/routes.py backend/app/services/sqlite_repository.py backend/app/services/repository.py backend/tests/test_knowledge_pagination.py && git commit -m "feat(kg): /knowledge 服务端分页(status 过滤+total)+ PaginatedKnowledge"`

---

## Task 3: 前端接线 —— 来源 + 知识库页码化 [串行,依赖 1+2]

**Files:** Modify `frontend/app/page.tsx`; Test 前端.

**先读**:`loadSourcesPage`(1427)、「加载更多」按钮(2702)、`sources`/`sourcesTotal` 状态(817)、`loadKnowledge`(1885)、`knowledge`/`knowledgeStatusFilter` 状态(866/868)、`KnowledgeBrowser`(4162,含 `statuses`/`filtered` 客户端过滤 4209/4210、status `<select>`、`onKind`)、`PaginatedSources`/`KnowledgeRecord` 类型。

- [ ] **Step 1: 来源页码化**
  - 加状态 `const [sourcesPage, setSourcesPage] = useState(0);`。
  - `loadSourcesPage(nb, {page=0, q})`:`offset = page*SOURCES_PAGE_SIZE`;`setSources(page.items)`(**替换**);`setSourcesTotal(total_count)`;`setSourcesPage(page)`。所有旧调用(reset/搜索/新增后)传 `page:0`。
  - 渲染:把「加载更多」按钮块(2702–2710)**替换**为 `<Pagination page={sourcesPage} pageSize={SOURCES_PAGE_SIZE} total={sourcesTotal} onPage={(p) => loadSourcesPage(currentNotebookId, { page: p, q: sourceQuery }).catch(reportError)} />`（import `Pagination`）。
- [ ] **Step 2: 知识库页码化**
  - 加状态 `const [knowledgeTotal, setKnowledgeTotal] = useState<Record<string, number>>({});` 和 `const [knowledgePage, setKnowledgePage] = useState<Record<string, number>>({});`。
  - `loadKnowledge(kind, { status, page=0 })`:fetch `/knowledge?type=${kind}&status=${status==='all'?'':status}&offset=${page*50}&limit=50` → `PaginatedKnowledge`;`setKnowledge(prev=>({...prev,[kind]:items}))`、`setKnowledgeTotal(prev=>({...prev,[kind]:total_count}))`、`setKnowledgePage(prev=>({...prev,[kind]:page}))`。
  - `KnowledgeBrowser`:删客户端 `filtered`(直接渲染 `items`);status `<select>` 的 options 用**固定集**（`["all","active","approved","proposed","disabled","deprecated"]` — 实现前核对 KnowledgeRecord.status 取值集），`onChange` 触发上层 `loadKnowledge(kind,{status:newStatus,page:0})`;切 kind(`onKind`)→ `loadKnowledge(kind,{status,page:0})`;底部渲染 `<Pagination page={knowledgePage[kind]??0} pageSize={50} total={knowledgeTotal[kind]??0} onPage={(p)=>loadKnowledge(kind,{status,page:p})} />`。tab 计数仍来自 `/knowledge-types`(该类型全量,与分页 total=当前 status 口径不同——保持 tab=类型总量、分页=当前过滤量)。
- [ ] **Step 3: tsc + 前端测试** — 见 Task 4(整体)。此步先本地 `npm run lint` 确认无类型错。
- [ ] **Step 4: 提交** — `git add frontend/app/page.tsx && git commit -m "feat(ui): 来源+知识库改页码分页(删加载更多/客户端全量过滤),接 Pagination"`

---

## Task 4: tsc + 前端测试 + 视觉 + 全量回归

**Files:** 前端测试;全量。

- [ ] **Step 1:** 软链 node_modules,`cd frontend && npm run lint`(tsc 干净)+ `npm test`(现有 + Pagination 测试全过);`rm frontend/node_modules`。
- [ ] **Step 2: 视觉验证** — show_widget/preview 还原两列表底部的 `Pagination`(对齐精致,按 [[ui-polish-bar]]);给用户截图。
- [ ] **Step 3: 后端全量** — `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q -m "not slow" -p no:cacheprovider | tail -3` → all pass。
- [ ] **Step 4: 提交(若有测试文件新增)** — 相应 commit。

---

## 收尾
- [ ] rebase 到 origin/master 线性 → push → `gh pr create --base master`。PR 说明:页码分页替换加载更多(来源+知识库)、知识库后端分页 + status 服务端过滤修卡顿、共享 Pagination 组件、视觉验证。
