# 待确认中心(头像旁铃铛)实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 在头像旁加一个全局"待确认中心"(铃铛+徽章+下拉),事件驱动实时聚合当前用户「我创建的」notebook 里的三类待办(深度报告待确认 / 治理队列 / 索引状态),点击精确直达;索引完成提示覆盖跨会话。

**Architecture:** 单进程后端复用现有 NDJSON-over-fetch 流式范式:REST 快照端点(首屏+兜底)+ 流式端点(实时推送)共用一个 `pending_actions(user_id)` 计算。进程内 `PendingBus` 事件总线管理 per-connection 队列 + per-user 内存缓冲(跨会话补发);job 完成经 `background_jobs.submit(notify_pending=True)` 与索引 job 收尾触发。前端铃铛组件复用 `getReader+TextDecoder+takeNdjsonLines`,snapshot 整体替换、event 弹 toast,断线指数退避重连。

**Tech Stack:** FastAPI + SQLite(`StreamingResponse`,`application/x-ndjson`);Python `contextvars`/`threading`/`asyncio`;Next.js/React(page.tsx 巨型单文件 + 新 `pending-center.tsx`);TDD via pytest + 前端现有测试。

**基线约束(须遵守)**:
- 单进程 uvicorn(`_scale_building` 内存集即证);事件总线用进程内内存,**不新增表**。
- `config.py` 若加 env 用 `validation_alias`;本计划**不新增 env**。
- 中文交互;前端中文弯引号 `""` 是有意的,**勿批量替换**;完工校验 `git diff | grep -c '^-.*[""]'` = 0(真弯引号)。
- 后端 job 在线程、SSE 端点在 asyncio loop:**snapshot 的 DB 计算绝不在 loop 线程跑**(用 job 线程预算 / `run_in_executor`)。
- 每任务末尾 commit;全部完成后 rebase 到 origin/master → push → `gh pr create --base master`。

---

## 文件结构

**后端**
- Create `backend/app/services/pending_bus.py` — `PendingBus` 事件总线(连接注册、fan-out、跨会话内存缓冲、TTL、loop 绑定、recompute 注入);模块级单例 `pending_bus`。
- Modify `backend/app/services/sqlite_repository.py` — 新增 `pending_actions(self, user_id: str) -> dict`(三源聚合)。
- Modify `backend/app/services/background_jobs.py` — `submit(...)` 加 keyword-only `notify_pending: bool = False`;`_run` finally 里触发 `pending_bus.mark_dirty(uid)`。
- Modify `backend/app/api/routes.py` — REST `GET /api/me/pending-actions` + 流式 `GET /api/me/pending-actions/stream`;报告 plan/generate 与 KG rebuild 的 submit 处传 `notify_pending=True`;索引 job 收尾 emit。
- Modify `backend/app/main.py`(或 app 装配处)— 启动时 `pending_bus.set_recompute(...)` 注入计算闭包。
- Modify `backend/app/services/sqlite_repository.py`(索引收尾)— `fold_scale_index_delta` / `_run_scale_op` 成功路径 emit `index_done`。

**前端**
- Create `frontend/app/pending-center.tsx` — `usePendingActions()` hook(流式连接+REST 兜底+重连) + `PendingBell` 组件(铃铛+徽章+下拉+分组+toast)。
- Modify `frontend/app/page.tsx` — topbar 接入 `<PendingBell>`;`openPendingItem(item)` deep-link;`pendingReportFocusId` state 传给 ReportsPanel。
- Modify `frontend/app/report-view.tsx` — `ReportsPanel` 加 `focusReportId?: string` prop,消费后打开对应报告。

**测试**
- Create `backend/tests/test_pending_actions.py` — `pending_actions` 三源聚合 + 跨用户隔离 + 空。
- Create `backend/tests/test_pending_bus.py` — 连接 fan-out / 无连接缓冲 / 重连 flush / TTL。
- Create `backend/tests/test_pending_actions_api.py` — REST 端点结构 + 权限;流式端点首帧 snapshot。

---

## 批次划分
- **批次 A(后端计算+REST)**:Task 1–2。可独立交付(铃铛能靠 REST 兜底工作)。
- **批次 B(后端事件总线+流+触发)**:Task 3–6。
- **批次 C(前端)**:Task 7–10。
- **批次 D(收尾验证)**:Task 11。

---

### Task 1: `pending_actions(user_id)` 计算核心

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(在 reports 相关方法附近新增方法)
- Test: `backend/tests/test_pending_actions.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_pending_actions.py
import json
from app.services.sqlite_repository import SqliteRepository


def _repo(tmp_path):
    return SqliteRepository(str(tmp_path / "t.db"))


def _seed_user_nb(repo, uid, name="NB"):
    """以 uid 为 created_by 建一个 notebook,返回其 id。"""
    with repo._connect() as db:
        db.execute(
            "INSERT INTO notebooks (id, name, purpose, primary_domain, status, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (f"nb-{uid}-{name}", name, "", "", "ready", uid, "2026-07-07T00:00:00", "2026-07-07T00:00:00"),
        )
    return f"nb-{uid}-{name}"


def test_pending_actions_empty(tmp_path):
    repo = _repo(tmp_path)
    out = repo.pending_actions("user-x")
    assert out == {"count": 0, "items": []}


def test_pending_actions_report_outline(tmp_path):
    repo = _repo(tmp_path)
    nb = _seed_user_nb(repo, "user-a")
    with repo._connect() as db:
        db.execute(
            "INSERT INTO reports (id, notebook_id, question, status, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("r1", nb, "带隙基准的温漂机理?", "outline_ready", "user-a", "2026-07-07T01:00:00", "2026-07-07T01:00:00"),
        )
        # 干扰项:非 outline_ready、他人的报告 —— 都不该出现
        db.execute(
            "INSERT INTO reports (id, notebook_id, question, status, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("r2", nb, "x", "generating", "user-a", "2026-07-07T01:00:00", "2026-07-07T01:00:00"),
        )
    out = repo.pending_actions("user-a")
    items = [it for it in out["items"] if it["type"] == "report_outline"]
    assert len(items) == 1
    assert items[0]["report_id"] == "r1"
    assert items[0]["notebook_id"] == nb
    assert items[0]["title"]  # question 截断非空
    assert out["count"] == 1


def test_pending_actions_governance_counts(tmp_path):
    repo = _repo(tmp_path)
    nb = _seed_user_nb(repo, "user-a")
    with repo._connect() as db:
        db.execute("INSERT INTO concept_merge_candidates (id, notebook_id, canonical_a, canonical_b, score, status) "
                   "VALUES (?,?,?,?,?,?)", ("m1", nb, "K-A", "K-B", 0.9, "pending"))
        db.execute("INSERT INTO concept_merge_candidates (id, notebook_id, canonical_a, canonical_b, score, status) "
                   "VALUES (?,?,?,?,?,?)", ("m2", nb, "K-C", "K-D", 0.9, "confirmed"))  # 非 pending 不计
    out = repo.pending_actions("user-a")
    gov = [it for it in out["items"] if it["type"] == "governance" and it["subtype"] == "merge"]
    assert len(gov) == 1
    assert gov[0]["count"] == 1
    assert gov[0]["notebook_id"] == nb


def test_pending_actions_isolation(tmp_path):
    """他人创建的 notebook 的待办不出现在我的中心。"""
    repo = _repo(tmp_path)
    nb_other = _seed_user_nb(repo, "user-b")
    with repo._connect() as db:
        db.execute(
            "INSERT INTO reports (id, notebook_id, question, status, created_by, created_at, updated_at) "
            "VALUES (?,?,?,?,?,?,?)",
            ("r1", nb_other, "x", "outline_ready", "user-b", "2026-07-07T01:00:00", "2026-07-07T01:00:00"),
        )
    out = repo.pending_actions("user-a")
    assert out == {"count": 0, "items": []}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_pending_actions.py -x -q`
Expected: FAIL(`AttributeError: 'SqliteRepository' object has no attribute 'pending_actions'`)

- [ ] **Step 3: 实现 `pending_actions`**

在 `sqlite_repository.py`(reports 相关方法附近)新增。注意:①严格 `created_by`(不含分享只读);②`scale_index_status` 已存在,用其 `state`;③index 分类 `stale`/`suggested` 计入 count、`building`/`queued` 不计。

```python
def pending_actions(self, user_id: str) -> dict:
    """聚合当前用户「我创建的」notebook 的三类待办。REST 与流式端点共用。

    只读、无 LLM/embed。index 项走 scale_index_status().state 分类。
    """
    items: list[dict] = []
    with self._connect() as db:
        my = db.execute(
            "SELECT id, name FROM notebooks WHERE created_by = ? AND status != 'copying'",
            (user_id,),
        ).fetchall()
        name_of = {r["id"]: r["name"] for r in my}
        nb_ids = list(name_of.keys())

        if nb_ids:
            # ① 深度报告待确认
            rows = db.execute(
                "SELECT id, question, notebook_id, created_at FROM reports "
                "WHERE status = 'outline_ready' AND created_by = ? ORDER BY updated_at DESC",
                (user_id,),
            ).fetchall()
            for r in rows:
                items.append({
                    "type": "report_outline",
                    "notebook_id": r["notebook_id"],
                    "notebook_name": name_of.get(r["notebook_id"], ""),
                    "report_id": r["id"],
                    "title": (r["question"] or "")[:60],
                    "created_at": r["created_at"],
                })

            # ② 治理三队列(count>0 才出项)
            placeholders = ",".join("?" for _ in nb_ids)
            gov_specs = [
                ("merge", "concept_merge_candidates", "status = 'pending'"),
                ("edge", "knowledge_relations", "review_status = 'pending'"),
                ("promotion", "promotion_candidates", "status IN ('proposed','under_review')"),
            ]
            for subtype, table, pred in gov_specs:
                grp = db.execute(
                    f"SELECT notebook_id, COUNT(*) AS c FROM {table} "
                    f"WHERE notebook_id IN ({placeholders}) AND {pred} GROUP BY notebook_id",
                    nb_ids,
                ).fetchall()
                for g in grp:
                    if g["c"] > 0:
                        items.append({
                            "type": "governance",
                            "subtype": subtype,
                            "notebook_id": g["notebook_id"],
                            "notebook_name": name_of.get(g["notebook_id"], ""),
                            "count": g["c"],
                        })

    # ③ 索引状态(scale_index_status 自管连接)
    for nb_id in nb_ids:
        try:
            st = self.scale_index_status(nb_id)
        except Exception:  # noqa: BLE001 — 单库状态异常不拖垮整个中心
            continue
        state = st.get("state")
        if state in ("stale", "suggested", "building", "queued"):
            item = {
                "type": "index",
                "state": "building" if state == "queued" else state,
                "notebook_id": nb_id,
                "notebook_name": name_of.get(nb_id, ""),
            }
            total = st.get("total_chunks") or 0
            delta = st.get("delta_chunks") or 0
            if state in ("building", "queued") and total:
                item["progress"] = round(100.0 * max(0, total - delta) / total)
            items.append(item)

    actionable = sum(
        1 for it in items
        if it["type"] in ("report_outline", "governance")
        or (it["type"] == "index" and it["state"] in ("stale", "suggested"))
    )
    return {"count": actionable, "items": items}
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_pending_actions.py -x -q`
Expected: PASS(4 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_pending_actions.py
git commit -m "feat(pending): pending_actions(user_id) 三源聚合(报告/治理/索引)"
```

---

### Task 2: REST 快照端点 `GET /api/me/pending-actions`

**Files:**
- Modify: `backend/app/api/routes.py`(用户级路由,`get_current_user` 依赖)
- Test: `backend/tests/test_pending_actions_api.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_pending_actions_api.py
from fastapi.testclient import TestClient
from app.api.main import app  # 若 app 装配在别处,implementer 按现有测试的 import 路径调整

client = TestClient(app)


def test_me_pending_actions_shape():
    # 匿名(user-local)也应 200,返回结构合法(不校验具体项)
    resp = client.get("/api/me/pending-actions")
    assert resp.status_code == 200
    body = resp.json()
    assert set(body.keys()) == {"count", "items"}
    assert isinstance(body["count"], int)
    assert isinstance(body["items"], list)
```

> 注:import 路径 `app.api.main`/`app.main` 依现有测试惯例;implementer 参照 `backend/tests/` 内其它 API 测试的 `TestClient` 搭建方式(可能有 fixture)。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_pending_actions_api.py -x -q`
Expected: FAIL(404 Not Found)

- [ ] **Step 3: 实现端点**

在 routes.py 找到 `get_current_user` 与 `repo` 的既有获取方式(参照现有用户级端点,如 `/promotion-queue`),新增:

```python
@router.get("/api/me/pending-actions")
async def me_pending_actions(user: UserProfile = Depends(get_current_user)) -> dict:
    return repo.pending_actions(user.id)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_pending_actions_api.py -x -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/routes.py backend/tests/test_pending_actions_api.py
git commit -m "feat(pending): REST 快照端点 GET /api/me/pending-actions"
```

---

### Task 3: `PendingBus` 事件总线

**Files:**
- Create: `backend/app/services/pending_bus.py`
- Test: `backend/tests/test_pending_bus.py`

**设计要点(务必遵守)**:
- 线程安全:`_conns`(user→queues)只在 loop 线程访问(端点注册/注销/fan-out);`_buffer` 与 `_loop` 用 `threading.Lock` 保护(job 线程与 loop 线程都可能碰)。
- **snapshot 数据在调用线程(job 线程)预算好再投递**,loop 侧只 fan-out,绝不在 loop 里查 DB。
- `emit` 无活跃连接 → 事件入 per-user 缓冲(TTL 30min);新连接 flush。`mark_dirty` 无连接 → 忽略(存量待办持久)。
- `_now` 可注入(`time.monotonic`)便于 TTL 测试。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_pending_bus.py
import asyncio
from app.services.pending_bus import PendingBus


def test_emit_buffers_when_no_connection():
    bus = PendingBus()
    bus.set_recompute(lambda uid: {"count": 0, "items": []})
    # 从没有连接过 → loop 未绑定 → event 入缓冲
    bus.emit("u1", {"event": "index_done", "notebook_id": "nb1"})
    assert bus._buffered_count("u1") == 1


def test_ttl_prunes_old_events():
    clock = {"t": 1000.0}
    bus = PendingBus(now=lambda: clock["t"], ttl=100.0)
    bus.emit("u1", {"event": "index_done", "notebook_id": "nb1"})
    clock["t"] = 1050.0
    bus.emit("u1", {"event": "index_done", "notebook_id": "nb2"})
    clock["t"] = 1200.0  # 首条已超 TTL(>100s)
    bus.emit("u1", {"event": "index_done", "notebook_id": "nb3"})
    assert bus._buffered_count("u1") == 2  # nb2, nb3 存活;nb1 被 prune


def test_fanout_and_flush_via_loop():
    async def scenario():
        bus = PendingBus()
        bus.set_recompute(lambda uid: {"count": 1, "items": [{"type": "x"}]})
        bus.bind_loop()  # 在 async 上下文绑定当前 loop

        # 无连接时 emit → 缓冲
        bus.emit("u1", {"event": "index_done", "notebook_id": "nbA"})

        # 建立连接 → flush 缓冲补发 + 能收到后续 fan-out
        q = bus.register("u1")
        try:
            drained = bus.flush_buffer("u1")
            assert [d["notebook_id"] for d in drained] == ["nbA"]
            assert bus._buffered_count("u1") == 0

            # mark_dirty(有连接) → snapshot 进队列
            bus.mark_dirty("u1")
            await asyncio.sleep(0.01)
            msg = q.get_nowait()
            assert msg["kind"] == "snapshot"
            assert msg["data"]["count"] == 1

            # emit(有连接) → event 进队列(不缓冲)
            bus.emit("u1", {"event": "index_done", "notebook_id": "nbB"})
            await asyncio.sleep(0.01)
            msg2 = q.get_nowait()
            assert msg2["kind"] == "event"
            assert msg2["notebook_id"] == "nbB"
        finally:
            bus.unregister("u1", q)

    asyncio.run(scenario())
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_pending_bus.py -x -q`
Expected: FAIL(`ModuleNotFoundError: app.services.pending_bus`)

- [ ] **Step 3: 实现 `PendingBus`**

```python
# backend/app/services/pending_bus.py
"""进程内待办事件总线(单进程部署)。

- REST/流式端点共用 recompute 计算 snapshot。
- job(线程)完成 → mark_dirty/emit → 经 loop.call_soon_threadsafe 投递给 SSE 连接。
- 无连接时 emit 的瞬时事件入 per-user 内存缓冲(TTL),新连接 flush 补发(跨会话)。
- **snapshot 的 DB 计算在调用线程预算,loop 侧只 fan-out,绝不在 loop 里查 DB。**
"""
from __future__ import annotations

import asyncio
import threading
import time
from typing import Callable, Optional


class PendingBus:
    def __init__(self, now: Callable[[], float] = time.monotonic, ttl: float = 1800.0):
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._conns: dict[str, set[asyncio.Queue]] = {}          # 仅 loop 线程访问
        self._buffer: dict[str, list[tuple[float, dict]]] = {}    # 锁保护
        self._lock = threading.Lock()
        self._recompute: Callable[[str], dict] = lambda uid: {"count": 0, "items": []}
        self._now = now
        self._ttl = ttl

    # ---- 装配 ----
    def set_recompute(self, fn: Callable[[str], dict]) -> None:
        self._recompute = fn

    def bind_loop(self) -> None:
        """在 async(端点)上下文调用,记录主事件循环。"""
        with self._lock:
            if self._loop is None:
                self._loop = asyncio.get_running_loop()

    # ---- 连接管理(loop 线程) ----
    def register(self, user_id: str) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self._conns.setdefault(user_id, set()).add(q)
        return q

    def unregister(self, user_id: str, q: asyncio.Queue) -> None:
        conns = self._conns.get(user_id)
        if conns:
            conns.discard(q)
            if not conns:
                self._conns.pop(user_id, None)

    def flush_buffer(self, user_id: str) -> list[dict]:
        """取出并清空该 user 的缓冲事件(新连接补发用)。"""
        with self._lock:
            entries = self._buffer.pop(user_id, [])
        fresh = self._within_ttl(entries)
        return [ev for _, ev in fresh]

    # ---- 触发(job 线程调) ----
    def mark_dirty(self, user_id: str) -> None:
        """待办可能变化 → 重算 snapshot 并推给该 user 所有连接(无连接则忽略)。"""
        loop = self._get_loop()
        if loop is None:
            return  # 无人连接;存量待办持久,重开会拉到
        data = self._recompute(user_id)  # 在调用线程(job 线程)算,不阻塞 loop
        loop.call_soon_threadsafe(self._fanout_snapshot, user_id, data)

    def emit(self, user_id: str, event: dict) -> None:
        """瞬时事件(index_done 等):有连接 fan-out,无连接入缓冲。"""
        loop = self._get_loop()
        if loop is None:
            self._buffer_event(user_id, event)
            return
        loop.call_soon_threadsafe(self._fanout_or_buffer_event, user_id, event)

    # ---- loop 线程内(串行,无并发) ----
    def _fanout_snapshot(self, user_id: str, data: dict) -> None:
        conns = self._conns.get(user_id)
        if not conns:
            return
        for q in conns:
            q.put_nowait({"kind": "snapshot", "data": data})

    def _fanout_or_buffer_event(self, user_id: str, event: dict) -> None:
        conns = self._conns.get(user_id)
        if not conns:
            self._buffer_event(user_id, event)
            return
        for q in conns:
            q.put_nowait({"kind": "event", **event})

    # ---- 内部 ----
    def _get_loop(self) -> Optional[asyncio.AbstractEventLoop]:
        with self._lock:
            return self._loop

    def _buffer_event(self, user_id: str, event: dict) -> None:
        with self._lock:
            lst = self._buffer.setdefault(user_id, [])
            lst.append((self._now(), event))
            self._buffer[user_id] = self._within_ttl(lst)

    def _within_ttl(self, entries: list[tuple[float, dict]]) -> list[tuple[float, dict]]:
        cutoff = self._now() - self._ttl
        return [(t, ev) for (t, ev) in entries if t >= cutoff]

    def _buffered_count(self, user_id: str) -> int:  # 测试辅助
        with self._lock:
            return len(self._within_ttl(self._buffer.get(user_id, [])))


pending_bus = PendingBus()
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_pending_bus.py -x -q`
Expected: PASS(3 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/pending_bus.py backend/tests/test_pending_bus.py
git commit -m "feat(pending): PendingBus 事件总线(fan-out/跨会话缓冲/TTL)"
```

---

### Task 4: 流式端点 `GET /api/me/pending-actions/stream` + recompute 注入

**Files:**
- Modify: `backend/app/api/routes.py`(流式端点)
- Modify: `backend/app/main.py`(启动注入 recompute)
- Test: `backend/tests/test_pending_actions_api.py`(追加首帧断言)

- [ ] **Step 1: 追加失败测试(首帧 snapshot)**

```python
def test_me_pending_stream_first_frame_is_snapshot():
    # TestClient 的 stream:读第一块 NDJSON 行应为 snapshot
    with client.stream("GET", "/api/me/pending-actions/stream") as resp:
        assert resp.status_code == 200
        assert "application/x-ndjson" in resp.headers.get("content-type", "")
        first_line = ""
        for chunk in resp.iter_lines():
            if chunk:
                first_line = chunk if isinstance(chunk, str) else chunk.decode()
                break
        import json as _json
        msg = _json.loads(first_line)
        assert msg["kind"] == "snapshot"
        assert "data" in msg and set(msg["data"].keys()) == {"count", "items"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_pending_actions_api.py::test_me_pending_stream_first_frame_is_snapshot -x -q`
Expected: FAIL(404)

- [ ] **Step 3a: 启动注入 recompute**(main.py app 装配处,repo 单例可见的地方)

```python
from app.services.pending_bus import pending_bus
# repo 为现有全局仓库单例(参照现有获取方式)
pending_bus.set_recompute(lambda uid: repo.pending_actions(uid))
```

- [ ] **Step 3b: 实现流式端点**(routes.py)

```python
import asyncio, json
from app.services.pending_bus import pending_bus

@router.get("/api/me/pending-actions/stream")
async def me_pending_stream(request: Request, user: UserProfile = Depends(get_current_user)):
    uid = user.id
    pending_bus.bind_loop()

    async def gen():
        # 1) 先补发离线期间缓冲的瞬时事件(跨会话)
        for ev in pending_bus.flush_buffer(uid):
            yield json.dumps({"kind": "event", **ev}, ensure_ascii=False) + "\n"
        # 2) 初始 snapshot —— DB 计算放线程池,勿阻塞 loop
        loop = asyncio.get_running_loop()
        data = await loop.run_in_executor(None, pending_bus._recompute, uid)
        yield json.dumps({"kind": "snapshot", "data": data}, ensure_ascii=False) + "\n"
        # 3) 注册连接,循环等待推送 + keepalive
        q = pending_bus.register(uid)
        try:
            while True:
                if await request.is_disconnected():
                    break
                try:
                    msg = await asyncio.wait_for(q.get(), timeout=15.0)
                except asyncio.TimeoutError:
                    yield ": keepalive\n"   # 注释帧,前端忽略(非 JSON 行)
                    continue
                yield json.dumps(msg, ensure_ascii=False) + "\n"
        finally:
            pending_bus.unregister(uid, q)

    return StreamingResponse(gen(), media_type="application/x-ndjson")
```

> 前端 `takeNdjsonLines`/`JSON.parse` 须跳过以 `:` 开头的 keepalive 行(Task 7 处理)。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_pending_actions_api.py -x -q`
Expected: PASS(2 passed)

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/routes.py backend/app/main.py backend/tests/test_pending_actions_api.py
git commit -m "feat(pending): 流式端点 /stream(首帧snapshot+缓冲补发+keepalive)+recompute注入"
```

---

### Task 5: `background_jobs.submit` 的 `notify_pending` 钩子

**Files:**
- Modify: `backend/app/services/background_jobs.py`
- Test: `backend/tests/test_pending_bus.py`(追加)

- [ ] **Step 1: 追加失败测试**

```python
def test_submit_notify_pending_marks_dirty(monkeypatch):
    import app.services.background_jobs as bj
    called = []
    monkeypatch.setattr(bj.pending_bus, "mark_dirty", lambda uid: called.append(uid))
    # 让 job 线程内解析到某 uid
    monkeypatch.setattr(bj, "_resolve_job_user", lambda: "user-a")
    done = __import__("threading").Event()
    t = bj.submit(lambda: done.set(), name="t", notify_pending=True)
    done.wait(2.0)
    t.join(2.0)
    assert called == ["user-a"]


def test_submit_without_notify_does_not_mark(monkeypatch):
    import app.services.background_jobs as bj
    called = []
    monkeypatch.setattr(bj.pending_bus, "mark_dirty", lambda uid: called.append(uid))
    done = __import__("threading").Event()
    t = bj.submit(lambda: done.set(), name="t")  # notify_pending 默认 False
    done.wait(2.0)
    t.join(2.0)
    assert called == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_pending_bus.py -k notify -x -q`
Expected: FAIL(`submit() got an unexpected keyword argument 'notify_pending'`)

- [ ] **Step 3: 实现**(改 `submit` + 加 user 解析辅助)

```python
# background_jobs.py 顶部 import 区新增
from app.services.pending_bus import pending_bus

def _resolve_job_user() -> str | None:
    """在 job 线程(copy_context 已传播)里解析发起用户 id。"""
    try:
        from app.services.sqlite_repository import _REQUEST_USER
        u = _REQUEST_USER.get()
        return u.id if u is not None else None
    except Exception:  # noqa: BLE001
        return None


def submit(fn: Callable, *args, name: str | None = None,
           notify_pending: bool = False, **kwargs) -> threading.Thread:
    ctx = contextvars.copy_context()
    label = name or getattr(fn, "__name__", "job")

    def _run() -> None:
        try:
            fn(*args, **kwargs)
        except Exception:  # noqa: BLE001
            _log.exception("background job failed: %s", label)
        finally:
            if notify_pending:
                uid = _resolve_job_user()
                if uid:
                    try:
                        pending_bus.mark_dirty(uid)
                    except Exception:  # noqa: BLE001 — 通知失败绝不影响 job
                        _log.exception("pending mark_dirty failed: %s", label)

    thread = threading.Thread(target=lambda: ctx.run(_run), name=name, daemon=True)
    thread.start()
    return thread
```

> 循环 import 注意:`pending_bus` 不 import `background_jobs`;`_resolve_job_user` 里**延迟** import `_REQUEST_USER`,避免模块加载期环。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_pending_bus.py -x -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/background_jobs.py backend/tests/test_pending_bus.py
git commit -m "feat(pending): submit(notify_pending=) 完成后刷新待办 snapshot"
```

---

### Task 6: 触发接线(报告/KG rebuild 刷新 + 索引完成 emit)

**Files:**
- Modify: `backend/app/api/routes.py`(报告 plan/generate、KG rebuild 的 submit 处)
- Modify: `backend/app/services/sqlite_repository.py`(`fold_scale_index_delta` / `_run_scale_op` 成功收尾)

- [ ] **Step 1: 报告/rebuild 的 submit 传 `notify_pending=True`**

在 routes.py 找到深度报告 plan job、generate job、KG rebuild(刷新图谱)job 的 `background_jobs.submit(...)` 调用点,补 `notify_pending=True`。例:

```python
background_jobs.submit(_launch_plan_job, repo, nb, rid, q, history, auto_generate,
                       name=f"report-plan-{rid}", notify_pending=True)
```

> 逐处最小改动;不改 job 逻辑。plan/generate 完成后 outline_ready 出现/消失,mark_dirty 会刷新中心。

- [ ] **Step 2: 索引成功收尾 emit `index_done`**

`sqlite_repository.py` 的 `_run_scale_op`(9173)与 `fold_scale_index_delta`(8868):在**成功完成索引写入之后、`finally` 的 `_scale_building.discard` 之前**,emit 一次瞬时完成事件。用局部标志区分成功/异常:

```python
# _run_scale_op 内(示意):
def _run_scale_op(self, notebook_id: str, mode: str) -> None:
    ...
    with self._scale_building_lock:
        if notebook_id in self._scale_building:
            return
        self._scale_building.add(notebook_id)
    ok = False
    try:
        ...  # 现有构建逻辑
        ok = True
    finally:
        with self._scale_building_lock:
            self._scale_building.discard(notebook_id)
        if ok:
            try:
                from app.services.pending_bus import pending_bus
                uid = self._resolve_index_owner(notebook_id)  # 见下
                if uid:
                    name = self._notebook_name(notebook_id)
                    pending_bus.mark_dirty(uid)  # 索引状态变化 → 刷 snapshot
                    pending_bus.emit(uid, {"event": "index_done",
                                           "notebook_id": notebook_id,
                                           "notebook_name": name})
            except Exception:  # noqa: BLE001
                pass
```

`_resolve_index_owner(notebook_id)`:优先 `_REQUEST_USER.get().id`(发起线程上下文已传播),回退查 `notebooks.created_by`:

```python
def _resolve_index_owner(self, notebook_id: str) -> str | None:
    try:
        u = _REQUEST_USER.get()
        if u is not None:
            return u.id
    except Exception:  # noqa: BLE001
        pass
    try:
        with self._connect() as db:
            row = db.execute("SELECT created_by FROM notebooks WHERE id = ?", (notebook_id,)).fetchone()
            return row["created_by"] if row else None
    except Exception:  # noqa: BLE001
        return None
```

`_notebook_name(notebook_id)`:若无现成方法,`SELECT name FROM notebooks WHERE id=?`。fold 路径(8868)同法处理。

- [ ] **Step 3: 冒烟测试(手动断言不回归)**

Run: `cd backend && python -m pytest tests/ -k "scale or fold or report" -q`
Expected: 现有相关测试仍 PASS(本步不新增单测,靠 Task 11 真机验收 index_done)。

- [ ] **Step 4: Commit**

```bash
git add backend/app/api/routes.py backend/app/services/sqlite_repository.py
git commit -m "feat(pending): 报告/rebuild 完成刷新中心 + 索引成功 emit index_done"
```

---

### Task 7: 前端 `usePendingActions()` hook(流式+REST 兜底+重连)

**Files:**
- Create: `frontend/app/pending-center.tsx`
- (前端测试框架若有,追加 hook 单测;否则靠 Task 11 tsc + 真机)

- [ ] **Step 1: 写 hook**

复用 `readAskStream` 的 NDJSON 读法(`getReader`+`TextDecoder`)。跳过 keepalive(`:` 前缀)行。

```tsx
// frontend/app/pending-center.tsx
"use client";
import { useCallback, useEffect, useRef, useState } from "react";
import { API_BASE, authHeaders, getToken } from "./auth";

export type PendingItem = {
  type: "report_outline" | "governance" | "index";
  notebook_id: string;
  notebook_name: string;
  subtype?: "merge" | "edge" | "promotion";
  report_id?: string;
  title?: string;
  count?: number;
  state?: string;
  progress?: number;
  _key?: string;  // 客户端 done 项用
};
export type DoneToast = { notebook_id: string; notebook_name: string; ts: number };

type Snapshot = { count: number; items: PendingItem[] };

export function usePendingActions(enabled: boolean) {
  const [snapshot, setSnapshot] = useState<Snapshot>({ count: 0, items: [] });
  const [doneItems, setDoneItems] = useState<DoneToast[]>([]);
  const [toast, setToast] = useState<DoneToast | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const retryRef = useRef(0);
  const stoppedRef = useRef(false);

  const dismissDone = useCallback((notebook_id: string) => {
    setDoneItems((xs) => xs.filter((d) => d.notebook_id !== notebook_id));
  }, []);

  useEffect(() => {
    if (!enabled || !getToken()) return;
    stoppedRef.current = false;

    // REST 兜底:先拉一次秒开
    (async () => {
      try {
        const r = await fetch(`${API_BASE}/api/me/pending-actions`, { headers: authHeaders() });
        if (r.ok) setSnapshot(await r.json());
      } catch { /* 交给流 */ }
    })();

    const connect = async () => {
      if (stoppedRef.current) return;
      const ac = new AbortController();
      abortRef.current = ac;
      try {
        const resp = await fetch(`${API_BASE}/api/me/pending-actions/stream`, {
          headers: authHeaders(), signal: ac.signal,
        });
        if (!resp.ok || !resp.body) throw new Error(`stream ${resp.status}`);
        retryRef.current = 0;
        const reader = resp.body.getReader();
        const decoder = new TextDecoder();
        let buffer = "";
        while (true) {
          const { value, done } = await reader.read();
          if (done) break;
          buffer += decoder.decode(value, { stream: true });
          let nl: number;
          while ((nl = buffer.indexOf("\n")) >= 0) {
            const line = buffer.slice(0, nl).trim();
            buffer = buffer.slice(nl + 1);
            if (!line || line.startsWith(":")) continue;  // 跳过 keepalive
            let msg: any;
            try { msg = JSON.parse(line); } catch { continue; }
            if (msg.kind === "snapshot") {
              setSnapshot(msg.data as Snapshot);
            } else if (msg.kind === "event" && msg.event === "index_done") {
              const d: DoneToast = { notebook_id: msg.notebook_id, notebook_name: msg.notebook_name || "", ts: Date.now() };
              setDoneItems((xs) => [d, ...xs.filter((x) => x.notebook_id !== d.notebook_id)]);
              setToast(d);
            }
          }
        }
      } catch { /* 断线 → 退避重连 */ }
      if (stoppedRef.current) return;
      const delay = Math.min(30000, 1000 * 2 ** retryRef.current++);
      setTimeout(connect, delay);
    };
    connect();

    return () => { stoppedRef.current = true; abortRef.current?.abort(); };
  }, [enabled]);

  return { snapshot, doneItems, toast, setToast, dismissDone };
}
```

- [ ] **Step 2: tsc 校验**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无新增类型错误(组件在 Task 8 引用前,单独 hook 应类型自洽)。

- [ ] **Step 3: Commit**

```bash
git add frontend/app/pending-center.tsx
git commit -m "feat(fe/pending): usePendingActions hook(流式+REST兜底+退避重连+done toast)"
```

---

### Task 8: 前端 `PendingBell` 组件(铃铛+徽章+下拉+分组+toast)

**Files:**
- Modify: `frontend/app/pending-center.tsx`(追加组件)
- Modify: `frontend/app/globals.css`(或现有样式文件)加样式

- [ ] **Step 1: 写组件**

达 [[ui-polish-bar]]:徽章对齐、圆角阴影、分组标题、hover、空态。用 lucide `Bell`(项目已用 lucide,见 `ChevronDown`)。

```tsx
// 追加到 pending-center.tsx
import { Bell } from "lucide-react";
import type { PendingItem, DoneToast } from "./pending-center";  // 同文件内直接用类型

export function PendingBell(props: {
  snapshot: { count: number; items: PendingItem[] };
  doneItems: DoneToast[];
  onOpenItem: (item: PendingItem) => void;
  onOpenDone: (d: DoneToast) => void;
  onDismissDone: (notebook_id: string) => void;
}) {
  const { snapshot, doneItems, onOpenItem, onOpenDone, onDismissDone } = props;
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement | null>(null);
  const badge = snapshot.count + doneItems.length;

  useEffect(() => {
    if (!open) return;
    const onDoc = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener("mousedown", onDoc);
    return () => document.removeEventListener("mousedown", onDoc);
  }, [open]);

  const groups: { key: string; label: string; items: PendingItem[] }[] = [
    { key: "report_outline", label: "深度报告待确认", items: snapshot.items.filter((i) => i.type === "report_outline") },
    { key: "governance", label: "治理待办", items: snapshot.items.filter((i) => i.type === "governance") },
    { key: "index", label: "索引状态", items: snapshot.items.filter((i) => i.type === "index") },
  ];

  const labelFor = (it: PendingItem): string => {
    if (it.type === "report_outline") return `深度报告《${it.title}》`;
    if (it.type === "governance") {
      const n = it.subtype === "merge" ? "待合并" : it.subtype === "edge" ? "边审" : "晋升";
      return `${it.notebook_name} · ${n} ${it.count}`;
    }
    const s = it.state === "building" ? (it.progress != null ? `索引构建中(${it.progress}%)` : "索引构建中")
      : it.state === "suggested" ? "建议建立索引" : "建议重建索引";
    return `${it.notebook_name} · ${s}`;
  };

  return (
    <div className="pending-center" ref={ref}>
      <button className="pending-bell" onClick={() => setOpen((o) => !o)} aria-label="待确认中心">
        <Bell size={18} />
        {badge > 0 && <span className="pending-badge">{badge > 99 ? "99+" : badge}</span>}
      </button>
      {open && (
        <div className="pending-popover">
          {badge === 0 && <p className="pending-empty">暂无待确认</p>}
          {groups.map((g) => g.items.length > 0 && (
            <div className="pending-group" key={g.key}>
              <div className="pending-group-title">{g.label}</div>
              {g.items.map((it, idx) => (
                <button className="pending-row" key={`${g.key}-${idx}`}
                        onClick={() => { setOpen(false); onOpenItem(it); }}>
                  {it.type !== "report_outline" && <span className="pending-row-nb">{it.notebook_name}</span>}
                  <span className="pending-row-label">{labelFor(it)}</span>
                </button>
              ))}
            </div>
          ))}
          {doneItems.length > 0 && (
            <div className="pending-group pending-group-done">
              <div className="pending-group-title">已完成</div>
              {doneItems.map((d) => (
                <button className="pending-row pending-row-done" key={d.notebook_id}
                        onClick={() => { setOpen(false); onOpenDone(d); onDismissDone(d.notebook_id); }}>
                  <span className="pending-row-label">{d.notebook_name} · 索引构建完成</span>
                  <span className="pending-row-x" onClick={(e) => { e.stopPropagation(); onDismissDone(d.notebook_id); }}>×</span>
                </button>
              ))}
            </div>
          )}
        </div>
      )}
    </div>
  );
}

export function PendingToast(props: { toast: DoneToast | null; onClose: () => void; onClick: () => void }) {
  const { toast, onClose, onClick } = props;
  useEffect(() => {
    if (!toast) return;
    const t = setTimeout(onClose, 6000);
    return () => clearTimeout(t);
  }, [toast, onClose]);
  if (!toast) return null;
  return (
    <div className="pending-toast" onClick={() => { onClick(); onClose(); }}>
      「{toast.notebook_name}」索引构建完成,点击查看
    </div>
  );
}
```

- [ ] **Step 2: 加样式**(globals.css;对齐现有 `user-menu-popover` 观感)

```css
.pending-center { position: relative; }
.pending-bell { position: relative; display: inline-flex; align-items: center; justify-content: center;
  width: 34px; height: 34px; border-radius: 8px; border: 1px solid var(--border, #2a2a2a);
  background: transparent; cursor: pointer; color: inherit; }
.pending-bell:hover { background: rgba(255,255,255,0.06); }
.pending-badge { position: absolute; top: -4px; right: -4px; min-width: 16px; height: 16px; padding: 0 4px;
  border-radius: 8px; background: #e5484d; color: #fff; font-size: 10px; line-height: 16px; text-align: center; }
.pending-popover { position: absolute; right: 0; top: calc(100% + 8px); width: 320px; max-height: 60vh; overflow-y: auto;
  background: var(--panel, #161616); border: 1px solid var(--border, #2a2a2a); border-radius: 12px;
  box-shadow: 0 12px 32px rgba(0,0,0,0.4); padding: 8px; z-index: 60; }
.pending-empty { color: var(--muted, #888); text-align: center; padding: 20px 0; font-size: 13px; }
.pending-group + .pending-group { margin-top: 6px; border-top: 1px solid var(--border, #242424); padding-top: 6px; }
.pending-group-title { font-size: 11px; color: var(--muted, #888); padding: 4px 8px; text-transform: none; }
.pending-row { display: flex; flex-direction: column; align-items: flex-start; gap: 2px; width: 100%;
  text-align: left; background: transparent; border: 0; border-radius: 8px; padding: 8px; cursor: pointer; color: inherit; }
.pending-row:hover { background: rgba(255,255,255,0.06); }
.pending-row-nb { font-size: 11px; color: var(--muted, #888); }
.pending-row-label { font-size: 13px; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 290px; }
.pending-group-done .pending-row-done { flex-direction: row; align-items: center; justify-content: space-between; color: var(--muted, #999); }
.pending-row-x { padding: 0 6px; opacity: 0.6; }
.pending-row-x:hover { opacity: 1; }
.pending-toast { position: fixed; right: 24px; bottom: 24px; z-index: 80; max-width: 320px;
  background: var(--panel, #161616); border: 1px solid var(--border, #2a2a2a); border-left: 3px solid #30a46c;
  border-radius: 10px; box-shadow: 0 12px 32px rgba(0,0,0,0.4); padding: 12px 14px; font-size: 13px; cursor: pointer; }
```

- [ ] **Step 3: tsc 校验**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无新增类型错误。

- [ ] **Step 4: Commit**

```bash
git add frontend/app/pending-center.tsx frontend/app/globals.css
git commit -m "feat(fe/pending): PendingBell 铃铛下拉 + PendingToast(分组/徽章/空态/样式)"
```

---

### Task 9: page.tsx 接入 topbar + `openPendingItem` deep-link

**Files:**
- Modify: `frontend/app/page.tsx`(topbar ~2847-2889;Home 组件内新增 state/函数)

- [ ] **Step 1: 引入 + state**

page.tsx import 区加:
```tsx
import { usePendingActions, PendingBell, PendingToast, type PendingItem } from "./pending-center";
```
Home 组件内(currentNotebook 等 state 附近)加:
```tsx
const [pendingReportFocusId, setPendingReportFocusId] = useState<string | null>(null);
const pending = usePendingActions(Boolean(authChecked && getToken()));
```

- [ ] **Step 2: `openPendingItem` deep-link**(Home 内,`openNotebook` 定义之后)

```tsx
async function openPendingItem(item: PendingItem) {
  await openNotebook(item.notebook_id);
  if (item.type === "report_outline") {
    switchChatMode("reports");
    if (item.report_id) setPendingReportFocusId(item.report_id);
  } else if (item.type === "governance") {
    if (item.subtype === "edge") { await openEdgeReviewQueue(); }
    else if (item.subtype === "promotion") { await openPromoQueue(); }
    else { setKgViewOpen(true); }  // merge → KG 图谱视图内联「待确认合并」
  } else if (item.type === "index") {
    setKgViewOpen(true);  // 索引状态/重建入口在 KG 视图
  }
}
function openDoneItem(d: { notebook_id: string }) {
  openNotebook(d.notebook_id).then(() => setKgViewOpen(true));
}
```

> `openNotebook` 内部会 `setChatMode("ask")`,故报告分支在其后再 `switchChatMode("reports")`,顺序正确。

- [ ] **Step 3: topbar 渲染铃铛 + toast**

在 `<div className="topbar-right">` 内、`<div className="user-menu">` **之前**插入:
```tsx
<PendingBell
  snapshot={pending.snapshot}
  doneItems={pending.doneItems}
  onOpenItem={openPendingItem}
  onOpenDone={openDoneItem}
  onDismissDone={pending.dismissDone}
/>
```
在组件树末尾(与其它全局浮层同级)加:
```tsx
<PendingToast toast={pending.toast} onClose={() => pending.setToast(null)}
  onClick={() => { if (pending.toast) openDoneItem(pending.toast); }} />
```

- [ ] **Step 4: tsc + 弯引号校验**

Run: `cd frontend && npx tsc --noEmit && git diff -- app/page.tsx | grep -c '^-.*[""]'`
Expected: tsc 无错;弯引号删除计数 = `0`。

- [ ] **Step 5: Commit**

```bash
git add frontend/app/page.tsx
git commit -m "feat(fe/pending): topbar 接入铃铛 + openPendingItem 精确 deep-link"
```

---

### Task 10: ReportsPanel 消费 `focusReportId`

**Files:**
- Modify: `frontend/app/report-view.tsx`(ReportsPanel)
- Modify: `frontend/app/page.tsx`(传 prop + 消费后清空)

- [ ] **Step 1: ReportsPanel 加 prop + 消费**

report-view.tsx 的 `ReportsPanel` props 类型加 `focusReportId?: string | null; onFocusConsumed?: () => void;`。组件内、`reports` 列表就绪后:
```tsx
useEffect(() => {
  if (!focusReportId || reports === null) return;
  (async () => {
    try {
      const detail = await getReport(notebookId, focusReportId);  // 参照现有 getReport 签名
      setActive(detail);
    } finally {
      onFocusConsumed?.();
    }
  })();
}, [focusReportId, reports, notebookId]);
```

- [ ] **Step 2: page.tsx 传 prop**

找到 `<ReportsPanel ... />` 渲染处,加:
```tsx
focusReportId={pendingReportFocusId}
onFocusConsumed={() => setPendingReportFocusId(null)}
```

- [ ] **Step 3: tsc + 弯引号校验**

Run: `cd frontend && npx tsc --noEmit && git diff -- app/report-view.tsx app/page.tsx | grep -c '^-.*[""]'`
Expected: tsc 无错;弯引号删除计数 = `0`。

- [ ] **Step 4: Commit**

```bash
git add frontend/app/report-view.tsx frontend/app/page.tsx
git commit -m "feat(fe/pending): ReportsPanel focusReportId — 深链直达大纲编辑器"
```

---

### Task 11: 全量验证 + 真机验收清单 + PR

**Files:** 无代码改动(除修复回归)

- [ ] **Step 1: 后端全量**

Run: `cd backend && python -m pytest -q`
Expected: 全绿(含新增 `test_pending_*`)。

- [ ] **Step 2: 前端类型 + 测试 + 弯引号**

Run: `cd frontend && npx tsc --noEmit && npm test --silent 2>/dev/null; git diff | grep -c '^-.*[""]'`
Expected: tsc 无错;测试(若有)绿;弯引号删除计数 = `0`。

- [ ] **Step 3: 真机验收清单**(人工,由用户在部署环境执行)
  - 多 notebook 各造一类待办 → 铃铛徽章 = actionable 合计。
  - 报告 outline_ready 项 → 点击进该 nb + 报告 tab + 大纲编辑器打开该报告。
  - 治理三项 → 合并进 KG 视图「待确认合并」/ 边审开边审模态 / 晋升开晋升模态。
  - 索引 building 项显示、完成后 snapshot 自动去掉该项。
  - **跨会话**:发起索引重建 → **关闭页面** → 构建完成后重开 → 收到「索引构建完成」toast + "已完成"项(内存缓冲补发);点击直达。
  - 断网/后端重启后前端自动重连(退避)并恢复 snapshot。

- [ ] **Step 4: rebase + PR**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/pending-center
git fetch origin && git rebase origin/master
git push -u origin HEAD
gh pr create --base master --title "feat: 待确认中心(头像旁铃铛,事件驱动聚合报告/治理/索引 + 跨会话完成提示)" \
  --body "$(cat <<'EOF'
## 摘要
头像旁全局「待确认中心」(铃铛+徽章+下拉),事件驱动实时聚合当前用户「我创建的」notebook 三类待办:深度报告待确认 / 治理队列(合并·边审·晋升)/ 索引状态。点击精确直达;索引完成提示覆盖跨会话。

## 架构
- 后端 `pending_actions(user_id)` 三源聚合,REST 快照(首屏+兜底)+ NDJSON 流式(实时)共用。
- 进程内 `PendingBus`(单进程):per-connection 队列 fan-out + per-user 内存缓冲(跨会话补发,TTL 30min,不新增表)。
- job 完成经 `background_jobs.submit(notify_pending=True)` 刷 snapshot;索引成功 emit `index_done` toast。
- 前端复用 `getReader+TextDecoder+NDJSON` 流范式 + 退避重连 + REST 兜底。

## 测试
- `test_pending_actions`(三源聚合/隔离/空)、`test_pending_bus`(fan-out/缓冲/TTL/notify)、`test_pending_actions_api`(REST+流首帧)。
- 前端 tsc clean;弯引号零删除。

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## 自审(writing-plans 收尾)

- **Spec 覆盖**:①报告待确认=Task1(计算)+Task9/10(deep-link)✓ ②治理三队列=Task1(谓词)+Task9(三 opener)✓ ③索引状态=Task1(state 分类)+Task6(emit)+Task9 ✓;事件推送=Task3/4 ✓;跨会话补发=Task3(缓冲)+Task4(flush)+Task11(真机)✓;REST 兜底=Task2+Task7 ✓;严格 created_by=Task1 ✓;精确 deep-link=Task9/10 ✓。
- **Placeholder 扫描**:各步给出真实代码/命令;索引 emit 与 report submit 处依赖 implementer 定位既有调用点(已给方法名+行号+插入代码),非 placeholder。
- **类型一致**:`PendingItem`/`Snapshot`/`DoneToast` 前端统一;后端 item dict 键(`type/subtype/notebook_id/notebook_name/report_id/title/count/state/progress`)在 Task1 定义、Task8 消费一致;`mark_dirty`/`emit`/`register`/`unregister`/`flush_buffer`/`set_recompute`/`bind_loop` 在 Task3 定义、Task4/5/6 调用一致。
- **风险**:①晋升全局/admin — Task9 用 `openPromoQueue()`,若需对非 admin 隐藏晋升项,可在 Task1 计算处 gate(留实现者按 `user.is_admin` 判定,当前部署账号为 admin 不阻塞);②索引 emit 的 user 归属 — `_resolve_index_owner` 回退查 `created_by` 兜住自动 fold 场景。
