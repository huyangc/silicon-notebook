# PR A：Notebook 进出体验 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 打开 notebook 时自动落在最近一次对话上，并给工作区一个明显的返回入口（返回按钮 + 浏览器返回键 + 刷新恢复）。

**Architecture:** 纯前端。后端零改动——新对话本来就是第一次提问时由 `ensure_conversation` 隐式创建的，会话列表 SQL 已按 `updated_at DESC` 排序，`openNotebook` 早已把列表拉到手却主动丢弃。本 PR 把丢弃改成恢复，并补上 `#notebook=<id>` 这个 hash「只写不读」的既有缺口。

**Tech Stack:** Next.js App Router（客户端单组件 SPA）、TypeScript、lucide-react、`node --test` + 源码文本断言。

规格：`docs/superpowers/specs/2026-07-17-notebook-ux-and-transfer-design.md` Part A。

## Global Constraints

- **不改后端。** 本 PR 一行 Python 都不碰。
- **不动 `parseMemoryHash` 的现有返回契约**（`memory-navigation.test.mjs:12-21` 锁着它）。新增 `parseWorkspaceHash`，不改老的。
- **不动 `openNotebookMemory` 的签名。** `memory-navigation.test.mjs:46` 断言 `/function openNotebookMemory\(notebookId: string\)/`。
- **不把全局顶栏在工作区里放出来**（规格「明确不做」）。
- **不加第 4 个 `sessionSignal={memorySessionAbortRef.current.signal}`**：`answer-memory.test.mjs:73` 把出现次数写死为 3。
- **中文文案是契约。** 既有 class 名与中文文案被逐字断言锁定，不得顺手改名。
- **弯引号是有意的。** `page.tsx` 中文文案里的 `“”` 是合法 JSX 文本，不要替换成直引号。
- 测试命令：`cd frontend && npm test`（`node --test $(find app -name '*.test.mjs' -type f -print)`）。
- 类型检查：`cd frontend && npm run lint`（`tsc --noEmit`）。
- **本 worktree 没有 `node_modules`。** 跑测试/类型检查须在主 checkout（`/Users/hzf/workspace/silicon_notebook`）进行，或先在主 checkout 验证再把改动迁回。

---

### Task 1: hash 语法扩展

`#notebook=<id>` 目前只写不读——`openNotebook:1953` 写它，但挂载还原只调 `parseMemoryHash`，而它对不带 `tab=memory` 的形式返回 `null`（`memory-model.ts:138-141`）。本任务把这条 hash 的读侧补齐。

`notebookHash` / `parseWorkspaceHash` 放进 `memory-model.ts`，紧挨着 `memoryHash` / `parseMemoryHash`：这两条 hash 共用同一套语法（`#notebook=<id>` 与 `#notebook=<id>&tab=memory` 只差一个 `tab` 参数），拆到两个文件必然漂移。

**Files:**
- Modify: `frontend/app/memory-model.ts:128-142`（在 `parseMemoryHash` 后追加）
- Test: `frontend/app/memory-navigation.test.mjs`（在既有 hash 测试后追加）

**Interfaces:**
- Produces:
  - `notebookHash(notebookId: string): string` — 返回 `#notebook=<encoded id>`
  - `parseWorkspaceHash(hash: string): { notebookId: string } | null` — 只认「有 `notebook` 且**没有** `tab=memory`」的形式；其余一律 `null`

- [ ] **Step 1: 写失败的测试**

追加到 `frontend/app/memory-navigation.test.mjs`（放在 `test("memory count deep-link targets the notebook memory tab", ...)` 之后）：

```js
test("workspace hash round-trips a bare notebook deep-link", () => {
  assert.equal(notebookHash("nb-1"), "#notebook=nb-1");
  assert.deepEqual(parseWorkspaceHash("#notebook=nb-1"), { notebookId: "nb-1" });
});

test("workspace hash encodes ids that need escaping", () => {
  assert.equal(notebookHash("nb//1?x"), "#notebook=nb%2F%2F1%3Fx");
  assert.deepEqual(parseWorkspaceHash(notebookHash("nb//1?x")), { notebookId: "nb//1?x" });
});

test("workspace hash yields to the memory tab and ignores unrelated hashes", () => {
  // 带 tab=memory 的归 parseMemoryHash 管,workspace 解析器必须让路,
  // 否则挂载时两个分支会抢同一条 hash。
  assert.equal(parseWorkspaceHash("#notebook=nb-1&tab=memory"), null);
  assert.equal(parseWorkspaceHash("#memory"), null);
  assert.equal(parseWorkspaceHash(""), null);
  assert.equal(parseWorkspaceHash("#"), null);
  assert.equal(parseWorkspaceHash("#notebook="), null);
});

test("the two hash parsers stay mutually exclusive", () => {
  for (const hash of ["#notebook=nb-1", "#notebook=nb-1&tab=memory", "#memory", "", "#zzz"]) {
    const both = parseMemoryHash(hash) !== null && parseWorkspaceHash(hash) !== null;
    assert.equal(both, false, `${hash} 同时被两个解析器认领`);
  }
});
```

同时把 import 行（`memory-navigation.test.mjs:5`）改成：

```js
import { memoryHash, notebookHash, parseMemoryHash, parseWorkspaceHash } from "./memory-model.ts";
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
cd /Users/hzf/workspace/silicon_notebook/frontend && node --test app/memory-navigation.test.mjs
```

预期：FAIL，`notebookHash is not a function` / `parseWorkspaceHash is not a function`。

- [ ] **Step 3: 写最小实现**

在 `frontend/app/memory-model.ts` 的 `parseMemoryHash`（:134-142）之后追加：

```ts
// 与 memoryHash/parseMemoryHash 同住一个文件是有意的:两条 hash 共用同一套语法
// (`#notebook=<id>` 与 `#notebook=<id>&tab=memory` 只差一个 tab 参数),拆开必然漂移。
export function notebookHash(notebookId: string): string {
  return `#notebook=${encodeURIComponent(notebookId)}`;
}

// 只认「有 notebook 且没有 tab=memory」的裸工作区 hash。带 tab=memory 的归
// parseMemoryHash 管——两个解析器必须互斥,否则挂载时会抢同一条 hash。
export function parseWorkspaceHash(hash: string): { notebookId: string } | null {
  const params = new URLSearchParams(hash.replace(/^#/, ""));
  const notebookId = params.get("notebook");
  if (!notebookId || params.get("tab") === "memory") return null;
  return { notebookId };
}
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd /Users/hzf/workspace/silicon_notebook/frontend && node --test app/memory-navigation.test.mjs && npm run lint
```

预期：全部 PASS，`tsc --noEmit` 无输出。

- [ ] **Step 5: 提交**

```bash
git add frontend/app/memory-model.ts frontend/app/memory-navigation.test.mjs
git commit -m "feat(frontend): 补齐 #notebook hash 的读侧解析(此前只写不读)"
```

---

### Task 2: 打开 notebook 恢复最近对话

`openNotebook` 在 `page.tsx:1951` 已经 `await loadSessions(...)` 把按 `updated_at DESC` 排好的列表拉到手，却在 `:1935` 主动 `setConversationId(null)` 一条都不选。本任务把它改成恢复第一条。

**为什么必须抽内核**：不能直接在 `openNotebook` 里调 `openSession`——`openSession:2375` 第一行就是 `++workspaceEpochRef.current`，而 `openNotebook:1952/:1924` 在其后还要拿自己的 `workspaceEpoch` 做校验，直接串起来 `openNotebook` 会自己撞自己的守卫并提前 `return false`。

**Files:**
- Modify: `frontend/app/page.tsx:2361-2372`（`loadSessions` 返回列表）
- Modify: `frontend/app/page.tsx:2374-2410`（`openSession` 抽出 `applySessionDetail`）
- Modify: `frontend/app/page.tsx:1949-1955`（`openNotebook` 恢复）
- Test: `frontend/app/workspace-layout.test.mjs`（追加源码文本断言）

**Interfaces:**
- Consumes: 无（不依赖 Task 1）
- Produces:
  - `loadSessions(notebookId?, expectedWorkspaceEpoch?): Promise<ConversationSummary[] | null>` — 从 `void` 改为返回列表；epoch/notebook 失配或无 id 时返回 `null`
  - `applySessionDetail(id: string, expectedWorkspaceEpoch: number): Promise<void>` — 拉 `/conversations/{id}` 并灌进 state；**不碰 epoch**，由调用方负责

- [ ] **Step 1: 写失败的测试**

追加到 `frontend/app/workspace-layout.test.mjs` 末尾（**追加到文件尾部**，不要插在中间——插入会移动行号，而本文件多个测试靠 `indexOf` 切片定位）：

```js
test("opening a notebook restores its most recent conversation instead of a blank one", () => {
  // loadSessions 必须把列表交回给调用方,否则 openNotebook 无从知道该恢复哪条。
  assert.match(page, /async function loadSessions\([\s\S]*?\): Promise<ConversationSummary\[\] \| null> \{/);

  // 会话详情的灌入内核必须独立于 openSession 存在,且自己不碰 epoch——
  // openSession 第一行就 ++workspaceEpochRef.current,openNotebook 复用它会自撞守卫。
  const applyStart = page.indexOf("async function applySessionDetail(");
  assert.ok(applyStart > -1, "applySessionDetail 必须存在");
  const applyEnd = page.indexOf("\n  }\n", applyStart);
  const applyBody = page.slice(applyStart, applyEnd);
  assert.equal(applyBody.includes("++workspaceEpochRef.current"), false);
  assert.equal(applyBody.includes("workspaceEpochRef.current +="), false);
  assert.ok(applyBody.includes("setConversationId(id);"));
  assert.ok(applyBody.includes("detail.active_job"), "在途 job 重连必须留在内核里,恢复时才能接上");

  // openSession 只负责推进 epoch + 清场,详情灌入委派给内核(零重复)。
  const openSessionStart = page.indexOf("async function openSession(id: string)");
  const openSessionEnd = page.indexOf("\n  }\n", openSessionStart);
  const openSessionBody = page.slice(openSessionStart, openSessionEnd);
  assert.ok(openSessionBody.includes("++workspaceEpochRef.current"));
  assert.match(openSessionBody, /await applySessionDetail\(id, workspaceEpoch\)/);
  assert.equal(openSessionBody.includes("api<ConversationDetail>"), false, "详情请求不该在 openSession 里重复");

  // openNotebook 用自己的 epoch 恢复最近一条,不新开 epoch。
  const openNotebookStart = page.indexOf("async function openNotebook(notebookId: string");
  const openNotebookEnd = page.indexOf("\n  }\n", openNotebookStart);
  const openNotebookBody = page.slice(openNotebookStart, openNotebookEnd);
  assert.match(openNotebookBody, /const sessionList = await loadSessions\(notebookId, workspaceEpoch\);/);
  assert.match(openNotebookBody, /await applySessionDetail\(sessionList\[0\]\.id, workspaceEpoch\)/);
  assert.equal(openNotebookBody.includes("await openSession("), false, "复用 openSession 会自撞 epoch 守卫");
});
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
cd /Users/hzf/workspace/silicon_notebook/frontend && node --test app/workspace-layout.test.mjs
```

预期：FAIL，`applySessionDetail 必须存在`。

- [ ] **Step 3: 写实现**

**3a.** `page.tsx:2361-2372` 的 `loadSessions` 整体替换为：

```tsx
  async function loadSessions(
    notebookId: string | null = currentNotebookId,
    expectedWorkspaceEpoch = workspaceEpochRef.current,
  ): Promise<ConversationSummary[] | null> {
    if (!notebookId) return null;
    const list = await api<ConversationSummary[]>(`/notebooks/${notebookId}/conversations`);
    if (
      activeNotebookIdRef.current !== notebookId
      || workspaceEpochRef.current !== expectedWorkspaceEpoch
    ) return null;
    setSessions(list);
    return list;
  }
```

**3b.** `page.tsx:2374-2410` 的 `openSession` 整体替换为下面两个函数（内核在前）：

```tsx
  // 会话详情 → state 的灌入内核。刻意不碰 workspaceEpochRef:调用方各自持有
  // 自己的 epoch(openSession 新推一个、openNotebook 沿用自己的),内核只做校验。
  // 这是 openNotebook 能复用它而不自撞守卫的唯一原因。
  async function applySessionDetail(id: string, expectedWorkspaceEpoch: number) {
    const detail = await api<ConversationDetail>(`/conversations/${id}`);
    if (workspaceEpochRef.current !== expectedWorkspaceEpoch) return;
    setTurns(detail.turns.map((turn) => ({ question: turn.question, response: turn.response })));
    setAskMode(modeFromTurn(detail.turns[detail.turns.length - 1]));
    setConversationId(id);
    setPendingQuestion("");
    setPendingMode(DEFAULT_ASK_MODE);
    setPendingTrace([]);
    setChatMode("ask");
    setSessionPanelOpen(false);
    setRenamingSessionId(null);
    const active = detail.active_job;
    if (active) {
      // 把在途 turn 渲染成「生成中」并接回实时轨迹(仿正在 ask 的 UI)。
      setPendingQuestion(active.question);
      setPendingMode(modeFromTurn({ response: { mode: active.mode } }));
      setPendingTrace(active.trace ?? []);
      setAsking(true);
      askJobIdRef.current = active.job_id;                  // 「停止」可作用于重连的 job
      askNotebookIdRef.current = activeNotebookIdRef.current;
      reconnectConvIdRef.current = id;
      setReconnectJob({ jobId: active.job_id, seen: (active.trace ?? []).length });
    } else {
      setReconnectJob(null);
      setAsking(false);
      askJobIdRef.current = null;
      askNotebookIdRef.current = null;
    }
  }

  async function openSession(id: string) {
    const workspaceEpoch = ++workspaceEpochRef.current;
    askRunEpochRef.current += 1;
    memoryLinksAbortRef.current?.abort();
    memoryLinksAbortRef.current = null;
    setAsking(false);
    setReconnectJob(null);
    setMemoryAnswerId(null);
    await applySessionDetail(id, workspaceEpoch);
  }
```

**3c.** `page.tsx:1949-1955`（`openNotebook` 尾部，`setSessions([]);` 起）替换为：

```tsx
    setSessions([]);
    pollCountRef.current = 0;
    const sessionList = await loadSessions(notebookId, workspaceEpoch);
    if (workspaceEpochRef.current !== workspaceEpoch) return false;
    // 落在最近一条对话(列表已按 updated_at DESC 排序)而非空白新会话。
    // 沿用本次 openNotebook 自己的 epoch:openSession 会新推一个 epoch,
    // 那会让下面的守卫立刻失配。零对话的库自然跳过,维持新会话现状。
    if (sessionList && sessionList.length > 0) {
      await applySessionDetail(sessionList[0].id, workspaceEpoch);
      if (workspaceEpochRef.current !== workspaceEpoch) return false;
    }
    window.history.replaceState(null, "", `#notebook=${encodeURIComponent(notebookId)}`);
    window.scrollTo(0, 0);
    return true;
  }
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd /Users/hzf/workspace/silicon_notebook/frontend && npm test && npm run lint
```

预期：全部 PASS。**特别确认 `workspace-layout.test.mjs` 的 "switching or leaving a notebook clears any pending ask-job reconnect" 仍然通过**——它断言 `openNotebook` 体内含 `setReconnectJob(null);`（:1917/:1938 两处都还在，未被本任务删除）。

- [ ] **Step 5: 真机验证**

```bash
cd /Users/hzf/workspace/silicon_notebook && ls .local/*.db
```

用 preview_start 起前端 dev server，然后：打开一个有历史对话的 notebook → 断言落在最近一条对话（有 turns，不是空白欢迎页）；点「新对话」→ 空白；打开一个零对话的库 → 空白欢迎页。**记录打开耗时**（`GET /conversations/{id}` 返回全量 turns，长会话可能拖慢打开——规格 A1「已知代价」要求实测）。

- [ ] **Step 6: 提交**

```bash
git add frontend/app/page.tsx frontend/app/workspace-layout.test.mjs
git commit -m "feat(frontend): 打开 notebook 落在最近一次对话而非空白新会话"
```

---

### Task 3: history 三件套（pushState + popstate + 挂载恢复）

让浏览器返回键和刷新都能用。

**签名策略（重要）**：只给 `openNotebook` 加第二个参数，**`openNotebookMemory` 的签名保持不变**——`memory-navigation.test.mjs:46` 断言 `/function openNotebookMemory\(notebookId: string\)/`。`openNotebookMemory` 内部给 `openNotebook` 传 `"none"` 并保留自己的 `replaceState`，于是 memory 深链的 history 行为与今天逐字一致（今天就是两次 replaceState 净得一个条目）。

**必须同步修的守卫**：`workspace-layout.test.mjs:117` 用
`page.indexOf("async function openNotebook(notebookId: string)")` **按字面签名定位**。加了第二个参数后 `string)` 这个子串不复存在，`indexOf` 返回 -1，`assert.ok(openNotebookStart > -1)` 失败。修法是把定位串的右括号去掉（更健壮，且守卫意图——「openNotebook 体内清 reconnect」——完全不变）。

**Files:**
- Modify: `frontend/app/page.tsx:1907`（`openNotebook` 签名 + history 写入）
- Modify: `frontend/app/page.tsx:1958-1962`（`openNotebookMemory` 传 `"none"`）
- Modify: `frontend/app/page.tsx:1328-1349`（挂载恢复补 workspace 分支）
- Modify: `frontend/app/page.tsx`（新增 popstate useEffect）
- Modify: `frontend/app/workspace-layout.test.mjs:117`（定位串）
- Test: `frontend/app/workspace-layout.test.mjs`（追加）

**Interfaces:**
- Consumes: `notebookHash` / `parseWorkspaceHash`（Task 1）
- Produces: `openNotebook(notebookId: string, history: "push" | "none" = "push"): Promise<boolean>`

- [ ] **Step 1: 先修被签名变更打破的守卫定位串**

`frontend/app/workspace-layout.test.mjs:117` 改为（去掉右括号）：

```js
  const openNotebookStart = page.indexOf("async function openNotebook(notebookId: string");
```

- [ ] **Step 2: 写失败的测试**

追加到 `frontend/app/workspace-layout.test.mjs` **末尾**：

```js
test("entering a notebook pushes history so the browser back button leaves it", () => {
  assert.match(page, /async function openNotebook\(\s*notebookId: string,\s*history: "push" \| "none" = "push",?\s*\): Promise<boolean>/);

  const openNotebookStart = page.indexOf("async function openNotebook(notebookId: string");
  const openNotebookEnd = page.indexOf("\n  }\n", openNotebookStart);
  const openNotebookBody = page.slice(openNotebookStart, openNotebookEnd);
  assert.match(openNotebookBody, /if \(history === "push"\) \{\s*window\.history\.pushState\(null, "", notebookHash\(notebookId\)\);/);
  assert.equal(openNotebookBody.includes("window.history.replaceState"), false, "进 notebook 改用 pushState");

  // 默认必须是 push,否则所有既有调用点(卡片/列表/新建/分享拷贝)都不进历史栈。
  // 既有调用点一律不带第二参,靠默认值——它们的字面形式被 workspace-layout.test.mjs
  // 的 "shared notebook transitions" 断言锁着,不许改。
  assert.match(page, /await openNotebook\(String\(created\.id\)\)/);
  assert.match(page, /await openNotebook\(String\(joined\.id\)\)/);
});

test("openNotebookMemory keeps its signature and its own history write", () => {
  // memory-navigation.test.mjs:46 按字面签名锁死了它,不能加参数。
  assert.match(page, /async function openNotebookMemory\(notebookId: string\) \{/);
  const start = page.indexOf("async function openNotebookMemory(notebookId: string)");
  const end = page.indexOf("\n  }\n", start);
  const body = page.slice(start, end);
  assert.match(body, /await openNotebook\(notebookId, "none"\)/);
  assert.match(body, /window\.history\.replaceState\(null, "", memoryHash\(notebookId\)\)/);
});

test("browser back and refresh both restore the notebook workspace", () => {
  // popstate:hash 变了要跟着切视图,且不能再写 history(浏览器已经改过 URL 了)。
  assert.match(page, /window\.addEventListener\("popstate", onPopState\)/);
  assert.match(page, /window\.removeEventListener\("popstate", onPopState\)/);

  const popStart = page.indexOf("function onPopState()");
  const popEnd = page.indexOf("\n    }\n", popStart);
  const popBody = page.slice(popStart, popEnd);
  assert.ok(popBody.includes('openNotebook(workspace.notebookId, "none")'));
  assert.ok(popBody.includes("showCollection();"));

  // 挂载:#notebook=<id>(不带 tab=memory)要能还原到工作区。
  const mountStart = page.indexOf("const target = parseMemoryHash(window.location.hash);");
  const mountEnd = page.indexOf("\n      })\n      .catch(() => { clearToken(); })", mountStart);
  const mountBlock = page.slice(mountStart, mountEnd);
  assert.ok(mountBlock.includes("parseWorkspaceHash(window.location.hash)"));
  assert.ok(mountBlock.includes('openNotebook(workspace.notebookId, "none")'));
});
```

- [ ] **Step 3: 跑测试确认它失败**

```bash
cd /Users/hzf/workspace/silicon_notebook/frontend && node --test app/workspace-layout.test.mjs
```

预期：FAIL，新增的三个测试全红（旧的 "switching or leaving a notebook clears..." 应因 Step 1 已修而仍然 PASS）。

- [ ] **Step 4: 写实现**

**4a.** 两处 import，都按既有的字母序插入。

`page.tsx:4` 是单行的 lucide 扁平 import，`ArrowLeft` 排在 `BarChart3` 前（Task 4 要用，一并加）：

```tsx
import { ArrowLeft, BarChart3, Bookmark, Check, ChevronDown, ChevronRight, Database, Edit3, ExternalLink, FileText, GitMerge, LayoutDashboard, LayoutGrid, List as ListIcon, LogOut, MessageSquareText, Network, PanelLeftClose, PanelLeftOpen, PanelRightClose, Plus, Settings, Share2, Sparkles, Square, Table2, Trash2, Upload, User, X } from "lucide-react";
```

`page.tsx:12-17` 是多行的 memory-model import 块，整块替换：

```tsx
import {
  answerIdBatches,
  collectSavedAnswerFlags,
  memoryHash,
  notebookHash,
  parseMemoryHash,
  parseWorkspaceHash,
} from "./memory-model";
```

**4b.** `page.tsx:1907` 的签名 + `:1953` 的 history 写入：

```tsx
  async function openNotebook(
    notebookId: string,
    history: "push" | "none" = "push",
  ): Promise<boolean> {
```

尾部（Task 2 已改过的那段）里的 `window.history.replaceState(...)` 一行替换为：

```tsx
    // "none" = 挂载还原 / popstate:浏览器已经把 URL 摆对了,再写一次只会多一个
    // 死条目(用户按返回没反应)。默认 "push" 让返回键能退出 notebook。
    if (history === "push") {
      window.history.pushState(null, "", notebookHash(notebookId));
    }
```

**4c.** `page.tsx:1958-1962` 的 `openNotebookMemory`：

```tsx
  async function openNotebookMemory(notebookId: string) {
    // 传 "none" 让 openNotebook 别写 history,自己下面这次 replaceState 独占写入——
    // 与本函数改动前的净效果逐字一致(旧代码是 replace 再 replace)。
    if (!await openNotebook(notebookId, "none")) return;
    setChatMode("memory");
    window.history.replaceState(null, "", memoryHash(notebookId));
  }
```

**4d.** `page.tsx:1335-1345` 的挂载还原，`else if` 之后补 `else` 分支：

```tsx
        const target = parseMemoryHash(window.location.hash);
        if (target?.scope === "global") {
          setOuterView("memory");
        } else if (target?.scope === "notebook" && target.notebookId) {
          try {
            await openNotebookMemory(target.notebookId);
          } catch {
            showCollection();
            setToast("Memory 深链接不可用或已失效");
          }
        } else {
          // 裸 #notebook=<id>:刷新回到笔记本(此前这条 hash 只写不读,刷新必回集合页)。
          const workspace = parseWorkspaceHash(window.location.hash);
          if (workspace) {
            try {
              await openNotebook(workspace.notebookId, "none");
            } catch {
              showCollection();
              setToast("笔记本链接不可用或已失效");
            }
          }
        }
```

**4e.** 新增 popstate useEffect。放在挂载还原 useEffect（`:1328-1349`）**之后**：

```tsx
  // 浏览器返回/前进:hash 是唯一的真相源,读它切视图。一律传 "none"——
  // 浏览器已经改过 URL,任何再写都会污染历史栈。
  useEffect(() => {
    if (!authChecked) return;
    function onPopState() {
      const hash = window.location.hash;
      const memory = parseMemoryHash(hash);
      if (memory?.scope === "global") {
        showGlobalMemory();
        return;
      }
      if (memory?.scope === "notebook" && memory.notebookId) {
        openNotebookMemory(memory.notebookId).catch(reportError);
        return;
      }
      const workspace = parseWorkspaceHash(hash);
      if (workspace) {
        openNotebook(workspace.notebookId, "none").catch(reportError);
        return;
      }
      // showCollection 自己的 replaceState 写的就是当前 URL(无 hash),是个 no-op。
      showCollection();
    }
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [authChecked]);
```

- [ ] **Step 5: 跑测试确认通过**

```bash
cd /Users/hzf/workspace/silicon_notebook/frontend && npm test && npm run lint
```

预期：全部 PASS。**特别确认 `memory-navigation.test.mjs` 全绿**——它锁着 `openNotebookMemory` 的签名和挂载还原块的 try/catch 结构。

- [ ] **Step 6: 真机验证（这一步不能省）**

history 行为无法靠源码断言证明。用 preview_start 起前端，逐条验证：

1. 集合页 → 打开 notebook A → 按浏览器返回 → **回到集合页**
2. 在 A 里刷新（F5）→ **仍在 A，且落在最近对话**（Task 2 与本任务在此汇合）
3. A → 点返回按钮 → 集合页 → 打开 A → 返回 → 集合页（不出现「按一次没反应」的死条目）
4. 从铃铛点 memory 深链 → 视图正确，且返回键行为与改动前一致
5. `?share=` 分享链路不受影响

- [ ] **Step 7: 提交**

```bash
git add frontend/app/page.tsx frontend/app/workspace-layout.test.mjs
git commit -m "feat(frontend): 浏览器返回键与刷新都能回到笔记本"
```

---

### Task 4: 明显的返回按钮

工作区左上角目前是 `<button className="notebook-home" onClick={showCollection}>SN</button>`（`page.tsx:3375`），一个 46×46 的深色方块，内容就是裸文字 `SN`，是整个工作区唯一的返回口。换成带箭头 + 文字的控件。

文案用 `← 返回主页`，与仓库已有词汇一致（`components/PageHeader.tsx:6` 的 `<a className="page-header-back" href="/">← 返回主页</a>`，那个 `/` 就是集合页）。箭头改用 lucide `ArrowLeft`，不再用字面 `←`。

**注意 `onClick`**：现在写的是 `onClick={showCollection}`，等于把 `MouseEvent` 当第一个实参传给 `showCollection`。今天 `showCollection()` 无参所以无害，但别延续这个写法——改成 `onClick={() => showCollection()}`。

**Files:**
- Modify: `frontend/app/page.tsx:3375`
- Modify: `frontend/app/globals.css:648-657`（`.notebook-home`）
- Test: `frontend/app/workspace-layout.test.mjs`（追加）

**Interfaces:**
- Consumes: `ArrowLeft`（Task 3 Step 4a 已加进 lucide import）
- Produces: 无

- [ ] **Step 1: 写失败的测试**

追加到 `frontend/app/workspace-layout.test.mjs` **末尾**：

```js
test("the workspace exit is a labelled back control, not a bare brand mark", () => {
  // 裸 SN 方块是用户明确反馈「太抽象」的那个控件。
  assert.equal(page.includes('<button className="notebook-home" onClick={showCollection}>SN</button>'), false);
  assert.match(page, /className="notebook-home"[\s\S]{0,220}<ArrowLeft size=\{16\} \/>[\s\S]{0,80}<span>返回主页<\/span>/);
  assert.match(page, /onClick=\{\(\) => showCollection\(\)\}/);
  assert.match(page, /^import \{[^}]*\bArrowLeft\b/m);

  // 从固定 46px 方块变成自适应宽度的胶囊,文字不能被压断。
  assert.match(css, /\.notebook-home\s*{[^}]*white-space:\s*nowrap;/s);
  assert.match(css, /\.notebook-home\s*{[^}]*flex:\s*0 0 auto;/s);
  assert.doesNotMatch(css, /\.notebook-home\s*{[^}]*width:\s*46px;/s);
});
```

- [ ] **Step 2: 跑测试确认它失败**

```bash
cd /Users/hzf/workspace/silicon_notebook/frontend && node --test app/workspace-layout.test.mjs
```

预期：FAIL，第一条 `assert.equal(..., false)` 就红。

- [ ] **Step 3: 写实现**

**3a.** `page.tsx:3375` 整行替换：

```tsx
              <button className="notebook-home" onClick={() => showCollection()} title="返回笔记本列表">
                <ArrowLeft size={16} />
                <span>返回主页</span>
              </button>
```

**3b.** `globals.css:648-657` 的 `.notebook-home` 整块替换：

```css
.notebook-home {
  flex: 0 0 auto;
  height: 40px;
  padding: 0 14px;
  display: inline-flex;
  align-items: center;
  gap: 6px;
  border: 0;
  border-radius: 999px;
  background: #101820;
  color: #fff;
  font-size: 13px;
  font-weight: 700;
  white-space: nowrap;
  cursor: pointer;
  box-shadow: 0 8px 20px rgba(16, 24, 32, 0.18);
}

.notebook-home:hover {
  background: #1f2b38;
}
```

- [ ] **Step 4: 跑测试确认通过**

```bash
cd /Users/hzf/workspace/silicon_notebook/frontend && npm test && npm run lint
```

预期：全部 PASS。**特别确认 `workspace-layout.test.mjs` 的 "workspace toolbar has overflow protection" 仍然通过**——它锁着 `.workspace-title { max-width: min(48vw, 720px); }`，而返回控件变宽会挤压同一个 flex 容器里的标题输入框。

- [ ] **Step 5: 视觉验证（不能省）**

用 preview_start 起前端并截图。逐条确认：

1. 返回按钮与 notebook 标题输入框**基线对齐**，间距均匀
2. 标题很长时**标题被省略号截断**，返回按钮不被压变形、文字不折行
3. 窄窗口（1024px）下 `.workspace-title` 与 `.workspace-toolbar` 不重叠、不错位
4. 只读共享视图（`isReader` 分支，`page.tsx:3377-3391`）下同样正常——那条分支里标题旁还有「只读」徽标和「退出共享」按钮，是最挤的情况

不接受粗糙堆叠。有错位就调 `.workspace-title` 的 `flex-basis`（`globals.css:608` 的 `flex: 1 1 260px`），但**不要动被测试锁死的 `max-width: min(48vw, 720px)`**。

- [ ] **Step 6: 提交**

```bash
git add frontend/app/page.tsx frontend/app/globals.css frontend/app/workspace-layout.test.mjs
git commit -m "feat(frontend): 工作区返回入口从裸 SN 方块改为带文字的返回按钮"
```

---

## 收尾

- [ ] **全量测试 + 类型检查**

```bash
cd /Users/hzf/workspace/silicon_notebook/frontend && npm test && npm run lint
```

- [ ] **弯引号未被污染**

```bash
cd /Users/hzf/workspace/silicon_notebook && git diff master --stat && git diff master -- frontend/app/page.tsx | grep -c '^-.*[“”]'
```

预期：最后一个数字为 `0`。（`page.tsx` 中文文案里的 `“”` 是有意的合法 JSX 文本，历史上被批量替换污染过 7 处。）

- [ ] **提 PR**

```bash
cd /Users/hzf/workspace/silicon_notebook && git fetch origin && git rebase origin/master && git push -u origin HEAD
gh pr create --base master --title "feat(frontend): notebook 进出体验——打开恢复最近对话 + 明显的返回入口" --body "$(cat <<'EOF'
## 做了什么

1. **打开 notebook 落在最近一次对话**,而不是空白新会话。后端零改动——会话列表本来就按 `updated_at DESC` 排好、新会话是第一次提问时才隐式创建的,`openNotebook` 早已把列表拉到手却主动丢弃。
2. **工作区返回入口**从裸 `SN` 方块换成带箭头和文字的返回按钮(文案沿用仓库已有的「返回主页」)。
3. **浏览器返回键和刷新都能用**:补齐 `#notebook=<id>` 这条 hash「只写不读」的既有缺口(此前刷新必回集合页),进 notebook 改 `pushState` 并加 `popstate` 监听。

## 实现要点

- `openSession` 抽出 `applySessionDetail` 内核供 `openNotebook` 复用。**内核刻意不碰 epoch**:`openSession` 第一行就 `++workspaceEpochRef.current`,直接复用会让 `openNotebook` 自撞自己的 epoch 守卫。
- 内核里包含既有的在途 job 重连,所以「离开时正在跑的提问,回来自动接上」免费成立。
- `openNotebook` 加 `history: "push" | "none"` 参数;`openNotebookMemory` **签名不动**(被 `memory-navigation.test.mjs:46` 按字面锁定),内部传 `"none"` 并保留自己的 `replaceState`,memory 深链行为与改动前逐字一致。
- 同步修了 `workspace-layout.test.mjs:117` 的字面签名定位串(守卫意图不变,只是 `indexOf` 找不到加了参数的签名)。

## 验证

- `npm test` + `npm run lint` 全绿
- 真机:打开有历史的库落在最近对话 / 刷新仍在该库该对话 / 返回键回集合页 / 零对话的库落空白 / 只读共享视图布局正常

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```
