# 索引与构建统一整合 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把三个「构建」系统(知识图谱抽取 / 概念合并 / 检索索引)的状态聚合到一个端点、正名去除「已同步」歧义、给检索索引加取消与时间戳,前端收拢成一个统一「索引与构建」面板。

**Architecture:** 后端加一个只读聚合端点 `GET /index-status`(合并三个已有 status)、一个 `POST /scale-index/cancel`(空闲队列出队;在建拒绝)、manifest 加 `built_at`;前端把散落的管理入口收进改造后的「知识分析看板」面板,消费聚合端点、一条轮询、状态词单一真相源、检索索引加取消按钮。后端先行、前端后接、同一 PR co-design。

**Tech Stack:** Python 3.13 / FastAPI / SQLite / pytest;前端 Next.js + TS(page.tsx 5755 行大文件 + app/scale-index.ts + node --test *.test.mjs)。

## Global Constraints

- 后端测试从 `backend/` 跑:`python -m pytest tests/<file> -q`;本机系统 `python`(共享 conda),不建 venv。
- 前端 `frontend/app/page.tsx` 是 5755 行且被并发改动:**每个前端任务先 grep 重新锚定行号**(不要信本计划的行号,只信函数/字符串锚点);中文文案弯引号“”是合法 JSX 文本,严禁全文件批量替引号,自查 `git diff frontend/app/page.tsx | grep -c '^-.*[“”]'` = 0。
- 前端类型检查:`cd frontend && npm ci >/dev/null 2>&1 && npx tsc --noEmit` = 0 errors;`app/scale-index.ts` 改动后跑 `node --test app/scale-index.test.mjs`。
- **正名不变量**:「已同步」字符串从两个系统都退役;检索索引 indexed 态标签改「最新」,概念合并 not-dirty 态标签改「最新」;同一屏不出现两个相同状态词指不同系统。
- **兼容不变量**:旧单系统 status 端点(`/scale-index/status`、`/unified-kg/status`)保留不删;聚合端点是新增。manifest 旧无 `built_at` 键 → 显示空,不报错。
- **效率**:聚合端点纯只读、不触发任何 build(尤其不触发 viz build:复用 `_viz_index_probe` 只读语义);把前端 4 条轮询收成 1 条。
- 「大库」「取消在建」等非目标见 spec §非目标:本轮 building 明确拒绝取消,不做协作式打断。
- TDD:每后端任务先写失败测试→跑失败→实现→跑通过→commit;前端任务以 tsc + node test + 弯引号自查把关。
- Commit 中文 conventional,尾行 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。
- 分支 `feat/index-build-consolidation`,worktree `/Users/hzf/workspace/silicon_notebook/.claude/worktrees/index-hub`。⚠️ 只在此 worktree 跑 git;**绝不在** `/Users/hzf/workspace/silicon_notebook`(root checkout)跑 git。提交前核 `pwd` 与 `git rev-parse --abbrev-ref HEAD`。
- 收尾后端全量 `python -m pytest tests/ -q` 全绿;前端 tsc + node test 绿。

## 文件结构

- `backend/app/services/sqlite_repository.py`:`index_status`(聚合)、`_dequeue_scale_idle` + `cancel_scale_index`、manifest `built_at`(build/fold/status 三处)。
- `backend/app/api/routes.py`:`GET /index-status`、`POST /scale-index/cancel`。
- `backend/app/models/schemas.py`:`IndexStatus` 响应模型(可选,或直接返 dict)。
- `backend/tests/test_index_build_consolidation.py`(新建):后端测试。
- `frontend/app/scale-index.ts`:STATE_LABELS 正名(+ test.mjs)。
- `frontend/app/page.tsx`:聚合状态消费 + 一条轮询 + 面板改造 + 取消按钮 + CTA 统一 + 状态词表。

---

### Task 1: 聚合状态端点 `GET /index-status`(后端)

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(新增 `index_status` 方法,放在 `scale_index_status` 定义之后,约 10066 行后)
- Modify: `backend/app/api/routes.py`(新增路由,放在 `/scale-index/status` 路由之后,约 1108 行后)
- Test: `backend/tests/test_index_build_consolidation.py`(新建)

**Interfaces:**
- Produces: `SQLiteRepository.index_status(notebook_id: str) -> dict`,形如
  `{"kg": {"ready": bool, "building": bool, "pending_sources": int}, "unified_kg": {"dirty": bool, "building": bool, "last_rebuild_at": str}, "scale_index": <scale_index_status() 原样 dict>}`。
- Produces: 路由 `GET /notebooks/{id}/index-status`(dependencies=`require_notebook_access`,与 scale-index/status 一致)。

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_index_build_consolidation.py`:

```python
"""索引与构建统一整合:聚合状态 / 取消 / built_at。"""
import json
import os

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_PROVIDER": "dashscope", "EMBED_BASE_URL": "https://e.test",
                 "EMBED_API_KEY": "k", "EMBED_MODEL": "m", "EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def test_index_status_aggregates_three_systems(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="n"))
    # 纯读、不得触发 viz build
    called = {"viz": 0}
    monkeypatch.setattr(repo, "_spawn_viz_build", lambda *a, **k: called.__setitem__("viz", called["viz"] + 1))
    out = repo.index_status(nb.id)
    assert set(out) == {"kg", "unified_kg", "scale_index"}
    assert set(out["kg"]) >= {"ready", "building", "pending_sources"}
    assert set(out["unified_kg"]) >= {"dirty", "building", "last_rebuild_at"}
    assert "state" in out["scale_index"]
    # 与各自旧 status 一致
    assert out["scale_index"]["state"] == repo.scale_index_status(nb.id)["state"]
    assert out["unified_kg"]["dirty"] == repo.unified_kg_status(nb.id)["dirty"]
    assert called["viz"] == 0   # 聚合是纯读
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_index_build_consolidation.py::test_index_status_aggregates_three_systems -q`
Expected: FAIL(`AttributeError: ... 'index_status'`)

- [ ] **Step 3: 实现方法**

在 `scale_index_status` 方法之后加(先 grep 确认 `def scale_index_status` 与 `def unified_kg_status` 与 NotebookSummary 的 kg 字段来源;kg 三字段来自 notebook 概要——用现成的 `get_notebook`/summary 逻辑,若无直接 getter 则内联最小查询):

```python
    def index_status(self, notebook_id: str) -> dict:
        """三系统构建状态聚合(纯只读,不触发任何 build)——供前端「索引与构建」面板
        一次拉齐,替代 4 条独立轮询。kg=抽取,unified_kg=概念合并,scale_index=检索索引。
        scale_index 原样复用 scale_index_status();unified_kg 取 dirty/building/last_rebuild_at
        子集;kg 取 ready/building/pending_sources。"""
        self.get_notebook(notebook_id)  # KeyError → 404
        scale = self.scale_index_status(notebook_id)
        uk = self.unified_kg_status(notebook_id)
        with self._connect() as db:
            n_obj = db.execute(
                "SELECT COUNT(*) c FROM knowledge_objects WHERE notebook_id=?",
                (notebook_id,)).fetchone()["c"]
            # pending = 已解析未抽取的 source 数(镜像 NotebookSummary.kg_pending_sources 口径:
            # 先 grep NotebookSummary 里 kg_pending_sources 的真实计算,照抄同一 SQL 避免口径漂移)。
            pending = db.execute(
                "SELECT COUNT(*) c FROM sources WHERE notebook_id=? AND status='parsed'",
                (notebook_id,)).fetchone()["c"]
        return {
            "kg": {
                "ready": n_obj > 0,
                "building": notebook_id in self._kg_building,
                "pending_sources": int(pending),
            },
            "unified_kg": {
                "dirty": bool(uk.get("dirty", False)),
                "building": bool(uk.get("viz_building", False)),
                "last_rebuild_at": uk.get("last_rebuild_at", ""),
            },
            "scale_index": scale,
        }
```

⚠️ 实现者务必先 grep `kg_pending_sources` 在 `sqlite_repository.py` 里的真实计算(概要构造处),照抄那段 SQL/逻辑,而非用上面的占位 `status='parsed'` —— 口径必须与 NotebookSummary 一致。同理 `kg_ready`/`kg_building` 用与概要相同的判据(`_kg_building` set 成员 / 有无 KG 对象)。

- [ ] **Step 4: 加路由**

在 `/scale-index/status` 路由后加:

```python
@router.get("/notebooks/{notebook_id}/index-status", dependencies=[Depends(require_notebook_access)])
def index_status(notebook_id: str) -> dict:
    """三系统构建状态聚合(kg/unified_kg/scale_index)。"""
    try:
        return repository().index_status(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
```

- [ ] **Step 5: 跑测试 + 路由冒烟**

Run: `cd backend && python -m pytest tests/test_index_build_consolidation.py::test_index_status_aggregates_three_systems -q`
Expected: PASS

Run: `python -m pytest tests/test_routes*.py -q -k "index_status or scale_index" 2>/dev/null; python -c "import app.api.routes"`（确保导入无误）
Expected: 无 import 错

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/app/api/routes.py backend/tests/test_index_build_consolidation.py
git commit -m "feat(api): GET /index-status 聚合三系统构建状态(纯只读,替代前端4条轮询)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 2: 检索索引取消/出队(后端)

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(新增 `_dequeue_scale_idle` + `cancel_scale_index`,放在 `trigger_scale_index_rebuild` 附近,约 10222 行)
- Modify: `backend/app/api/routes.py`(新增 `POST /scale-index/cancel`)
- Test: `backend/tests/test_index_build_consolidation.py`(追加)

**Interfaces:**
- Consumes: `self._scale_idle_queue`(dict)、`self._scale_building_lock`(Lock)、`self._scale_building`(set)。
- Produces: `_dequeue_scale_idle(notebook_id) -> bool`(加锁移除,幂等);`cancel_scale_index(notebook_id) -> dict` = `{"cancelled": bool, "state": <new state>, "reason": str}`;路由 `POST /notebooks/{id}/scale-index/cancel`。

- [ ] **Step 1: 写失败测试(追加)**

```python
def test_cancel_dequeues_queued(repo):
    nb = repo.create_notebook(NotebookCreate(name="q"))
    # 手动放入空闲队列(镜像 trigger_scale_index_rebuild(when=idle) 的效果)
    with repo._scale_building_lock:
        repo._scale_idle_queue[nb.id] = "auto"
    assert repo.scale_index_status(nb.id)["state"] == "queued"
    out = repo.cancel_scale_index(nb.id)
    assert out["cancelled"] is True
    assert nb.id not in repo._scale_idle_queue
    assert repo.scale_index_status(nb.id)["state"] != "queued"


def test_cancel_building_refuses(repo):
    nb = repo.create_notebook(NotebookCreate(name="b"))
    with repo._scale_building_lock:
        repo._scale_building.add(nb.id)
    try:
        out = repo.cancel_scale_index(nb.id)
        assert out["cancelled"] is False
        assert out["reason"] == "building_not_interruptible"
    finally:
        with repo._scale_building_lock:
            repo._scale_building.discard(nb.id)


def test_cancel_noop_idempotent(repo):
    nb = repo.create_notebook(NotebookCreate(name="x"))
    out = repo.cancel_scale_index(nb.id)   # 无队列项、未在建
    assert out["cancelled"] is False


def test_dequeue_returns_bool(repo):
    nb = repo.create_notebook(NotebookCreate(name="d"))
    assert repo._dequeue_scale_idle(nb.id) is False   # 不存在
    with repo._scale_building_lock:
        repo._scale_idle_queue[nb.id] = "auto"
    assert repo._dequeue_scale_idle(nb.id) is True     # 移除
    assert repo._dequeue_scale_idle(nb.id) is False    # 再移除幂等
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_index_build_consolidation.py -q -k "cancel or dequeue"`
Expected: FAIL(方法不存在)

- [ ] **Step 3: 实现**

在 `trigger_scale_index_rebuild` 之后加:

```python
    def _dequeue_scale_idle(self, notebook_id: str) -> bool:
        """从空闲重建队列移除 notebook(加锁,幂等)。返回是否移除了一项。"""
        with self._scale_building_lock:
            return self._scale_idle_queue.pop(notebook_id, None) is not None

    def cancel_scale_index(self, notebook_id: str) -> dict:
        """取消检索索引构建:
        - state=queued(在空闲队列)→ 出队,cancelled=True。
        - state=building(后台守护线程在建)→ 无句柄不可协作打断,cancelled=False,
          reason=building_not_interruptible(前端提示「正在构建,完成后自动更新」)。
        - 其它 → 幂等 no-op,cancelled=False。
        返回 {cancelled, state(取消后的新 state), reason}。"""
        self.get_notebook(notebook_id)  # KeyError → 404
        if notebook_id in self._scale_building:
            return {"cancelled": False,
                    "state": self.scale_index_status(notebook_id)["state"],
                    "reason": "building_not_interruptible"}
        removed = self._dequeue_scale_idle(notebook_id)
        return {"cancelled": bool(removed),
                "state": self.scale_index_status(notebook_id)["state"],
                "reason": "" if removed else "not_queued"}
```

- [ ] **Step 4: 加路由**

```python
@router.post("/notebooks/{notebook_id}/scale-index/cancel", dependencies=[Depends(require_notebook_access)])
def cancel_scale_index(notebook_id: str) -> dict:
    """取消检索索引:排队中→出队;构建中→拒绝(不可打断)。"""
    try:
        return repository().cancel_scale_index(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
```

- [ ] **Step 5: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_index_build_consolidation.py -q -k "cancel or dequeue"`
Expected: PASS

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/app/api/routes.py backend/tests/test_index_build_consolidation.py
git commit -m "feat(scale): POST /scale-index/cancel——空闲队列出队;在建拒绝(不可打断)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 3: 检索索引 manifest `built_at` 时间戳(后端)

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(build_scale_index 的 manifest dict ~9710;fold_scale_index_delta 的 `manifest = dict(idx.manifest)` ~9877;scale_index_status 透出 ~10053/10064)
- Test: `backend/tests/test_index_build_consolidation.py`(追加)

**Interfaces:**
- Produces: build/fold 写 manifest 时含 `"built_at": <ISO 字符串>`;`scale_index_status` 与 `index_status` 在 exists 时透出 `last_built_at`(旧 manifest 缺键→"")。

- [ ] **Step 1: 写失败测试(追加)**

需要一个能建成 scale 索引的小库(镜像既有 scale 测试:插 source+chunk+embedding 后 build_scale_index)。参照 `backend/tests/test_scale_index.py` 或 `test_scale_delta_policy.py` 的最小构造:

```python
def _tiny_indexed_nb(repo):
    nb = repo.create_notebook(NotebookCreate(name="idx"))
    now = "2026-07-09T00:00:00"
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                   ("s1", nb.id, "t", "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) VALUES (?,?,?,?,?,?,?)",
                   ("c1", nb.id, "s1", "alpha", "", "[]", now))
        v = repo.embedder.embed_query("alpha")
        db.execute("INSERT INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                   ("c1", nb.id, json.dumps(v), now))
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    return nb


def test_build_writes_built_at(repo):
    nb = _tiny_indexed_nb(repo)
    out_dir = os.path.join(repo.settings.storage_dir, "kg_index", nb.id)
    with open(os.path.join(out_dir, "manifest.json")) as fh:
        manifest = json.load(fh)
    assert manifest.get("built_at")   # 非空
    st = repo.scale_index_status(nb.id)
    assert st.get("last_built_at") == manifest["built_at"]


def test_status_last_built_at_absent_manifest_safe(repo):
    nb = _tiny_indexed_nb(repo)
    out_dir = os.path.join(repo.settings.storage_dir, "kg_index", nb.id)
    mpath = os.path.join(out_dir, "manifest.json")
    with open(mpath) as fh:
        manifest = json.load(fh)
    manifest.pop("built_at", None)        # 模拟旧索引
    with open(mpath, "w") as fh:
        json.dump(manifest, fh)
    repo._scale_idx_cache.pop(nb.id, None)  # 清进程缓存强制重读
    st = repo.scale_index_status(nb.id)
    assert st.get("last_built_at", "") == ""   # 缺键→空,不报错
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_index_build_consolidation.py -q -k built_at`
Expected: FAIL(manifest 无 built_at / status 无 last_built_at)

- [ ] **Step 3: 实现**

(a) build_scale_index 的 manifest dict(~9710)加一行(用文件里既有的 `_now()` helper——grep 确认其名;若无则 `datetime.now().isoformat()`):

```python
            "watermark_sources": watermark_sources,
            "built_at": _now(),
            "build_ms": dict(timings),
```

(b) fold_scale_index_delta 的 `manifest = dict(idx.manifest)`(~9877)之后加:

```python
            manifest = dict(idx.manifest)
            manifest["built_at"] = _now()
```

(c) scale_index_status 的 exists 分支(~10053 的 `base.update({...})`)加 `"last_built_at"`;未建/构建中分支(~10064)也补默认:

```python
            base.update({
                "stale": bool(version_stale or delta_over or dim_stale),
                "last_built_at": str(manifest.get("built_at", "")),
                ...(既有字段)...})
```
以及未建分支的 `base.update({..., "last_built_at": "", ...})`。

同时给 `ScaleIndexStatus` pydantic 模型(schemas.py,grep `class ScaleIndexStatus`)加 `last_built_at: str = ""` 字段(否则路由 `ScaleIndexStatus(**...)` 会丢弃该键)。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_index_build_consolidation.py -q && python -m pytest tests/test_scale_index.py tests/test_scale_index_repo.py -q`
Expected: 全 PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/app/models/schemas.py backend/tests/test_index_build_consolidation.py
git commit -m "feat(scale): manifest 记 built_at,scale_index_status 透出 last_built_at(检索索引『上次建于』)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 4: 状态词正名(前端 scale-index.ts)

**Files:**
- Modify: `frontend/app/scale-index.ts`(`STATE_LABELS`,indexed 态)
- Modify: `frontend/app/scale-index.test.mjs`(断言随改)
- Test: `frontend/app/scale-index.test.mjs`(node --test)

**Interfaces:**
- Produces: 检索索引 indexed 态 `stateLabel` 从「已同步」改为「最新」;`ScaleIndexStatus` TS 类型加 `last_built_at?: string`(消费 Task 3)。

- [ ] **Step 1: 先读定位**

Run: `cd frontend && grep -n "已同步\|STATE_LABELS\|last_built_at\|type ScaleIndexStatus\|interface ScaleIndexStatus" app/scale-index.ts`

- [ ] **Step 2: 改标签 + 改断言(TDD:先改 test 期望)**

在 `app/scale-index.test.mjs` 把 `indexed → 已同步` 用例期望的 `stateLabel: "已同步"` 改为 `"最新"`(那一行 assert)。

Run: `cd frontend && node --test app/scale-index.test.mjs`
Expected: FAIL(实现仍返回「已同步」)

- [ ] **Step 3: 实现**

在 `app/scale-index.ts` 的 `STATE_LABELS` 里把 `indexed: "已同步"` 改为 `indexed: "最新"`;`ScaleIndexStatus` 类型加 `last_built_at?: string;`。

- [ ] **Step 4: 跑 node test + tsc**

Run: `cd frontend && node --test app/scale-index.test.mjs && npx tsc --noEmit`
Expected: PASS / 0 errors

- [ ] **Step 5: Commit**

```bash
git add frontend/app/scale-index.ts frontend/app/scale-index.test.mjs
git commit -m "feat(ui): 检索索引 indexed 态标签「已同步」→「最新」(退役与概念合并重名)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 5: 聚合状态消费 + 一条轮询 + 修「已排队不刷新」(前端 page.tsx)

**Files:**
- Modify: `frontend/app/page.tsx`(新增 fetchIndexStatus 包装 + IndexStatus 类型;轮询恢复条件含 scale queued)

**Interfaces:**
- Consumes: 后端 `GET /index-status`(Task 1)。
- Produces: `fetchIndexStatus(nb)` 包装 + `IndexStatus` 类型;检索索引轮询在 `state==="queued"` 时也继续(修 bug)。

- [ ] **Step 1: 先读定位**

Run: `cd frontend && grep -n "fetchScaleIndexStatus\|buildingScaleIndex\b\|scaleIndexStatus\b\|shouldResumeScaleIndex\|in-progress-resume" app/page.tsx app/in-progress-resume.ts`
读:检索索引轮询 effect(page.tsx ~1115-1136)、`in-progress-resume.ts` 的 `shouldResumeScaleIndex`(它只对 building 恢复,不含 queued——这是 bug 源)。

- [ ] **Step 2: 加类型 + 包装**

在 `fetchScaleIndexStatus`(page.tsx ~700)旁加:

```typescript
type IndexStatus = {
  kg: { ready: boolean; building: boolean; pending_sources: number };
  unified_kg: { dirty: boolean; building: boolean; last_rebuild_at: string };
  scale_index: ScaleIndexStatus;
};
const fetchIndexStatus = (nb: string) => api<IndexStatus>(`/notebooks/${nb}/index-status`);
```

- [ ] **Step 3: 修「已排队不刷新」**

在 `in-progress-resume.ts` 的 `shouldResumeScaleIndex`(grep 定位)把恢复条件从「仅 building」改为「building 或 state==="queued"」,使排队态也进 6s 轮询(排队被低峰调度器执行→building→完成时前端能看到)。附:若该函数有对应 `*.test.mjs`,同步加一个 queued→true 的断言。

- [ ] **Step 4: tsc + node test + 弯引号自查**

Run: `cd frontend && npx tsc --noEmit && (ls app/in-progress-resume.test.mjs >/dev/null 2>&1 && node --test app/in-progress-resume.test.mjs || true)`
Expected: 0 errors / test PASS

Run: `git diff frontend/app/page.tsx | grep -c '^-.*[“”]'`
Expected: `0`

- [ ] **Step 5: Commit**

```bash
git add frontend/app/page.tsx frontend/app/in-progress-resume.ts frontend/app/in-progress-resume.test.mjs
git commit -m "feat(ui): index-status 聚合消费入口 + 修「已排队」不进轮询(排队态也刷新)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 6: 「索引与构建」面板改造 + 取消按钮(前端 page.tsx)

**Files:**
- Modify: `frontend/app/page.tsx`(改造「知识分析看板」弹窗为统一面板;新增 cancelScaleIndex 包装)

**Interfaces:**
- Consumes: `fetchIndexStatus`(Task 5)、后端 `POST /scale-index/cancel`(Task 2)、正名标签(Task 4)。

- [ ] **Step 1: 先读定位(务必)**

Run: `cd frontend && grep -n "openAnalytics\|知识分析看板\|检索索引\|重新合并\|完整重抽\|runScaleIndexOp\|scaleIndexStatus\|unifiedKgStatus\|已排队\|window.confirm" app/page.tsx`
读:看板弹窗 JSX(openAnalytics 打开的 modal)、检索索引卡片、概念合并「重新合并」+ 状态 tag、KG 抽取按钮群。识别当前散落的重复管理入口(admin 动作列表两条、看板旧卡按钮)。

- [ ] **Step 2: 加取消包装**

在 `rebuildScaleIndex`(page.tsx ~698)旁加:

```typescript
const cancelScaleIndex = (nb: string) => api<{ cancelled: boolean; state: string; reason: string }>(`/notebooks/${nb}/scale-index/cancel`, { method: "POST" });
```

- [ ] **Step 3: 面板三行结构改造**

把看板弹窗里与索引/构建相关的部分改造成**统一三行**(读现有 JSX,保留 modal 容器与样式类,替换内部内容),每行 `[正名] [状态 chip · 时间戳] [动作按钮…]`,统一走一个确认函数。结构要求(实现者按文件既有样式类落地,不要新造粗糙样式——遵循 UI 对齐精致约束):
- **知识图谱** 行:状态来自 `indexStatus.kg`(未建/就绪/抽取中/N 篇待抽);动作复用既有 `startKgBuild`/`startKgRebuild`/`relinkFromKgView`,统一确认。
- **概念合并** 行:状态来自 `indexStatus.unified_kg`(最新/待重建/重建中 · 上次 N 前,`formatRelativeTime(last_rebuild_at)`);动作 = 既有「重新合并」`refreshUnifiedKg`,统一确认(去掉同面板重复的第二入口)。
- **检索索引** 行:状态来自 `indexStatus.scale_index`(经既有 `describeScaleIndex` 得 stateLabel;正名后 indexed=「最新」;加 `· 上次 formatRelativeTime(last_built_at)` 当 last_built_at 非空);动作 = 既有构建/全量重建;**state∈{queued,building} 时显示「取消」按钮** → `cancelScaleIndex(nb)`,building 点取消后按返回 `reason` 弹「正在构建,完成后自动更新」提示(setToast),queued 取消成功后刷新 indexStatus。
- 面板打开与轮询改用 `fetchIndexStatus`(一次拉三系统)。

统一确认:抽出一个 `confirmIndexAction(message: string): boolean { return window.confirm(message); }`(或复用既有 confirm 模式),三系统动作前统一调用,文案模板一致(动作名 + 后果 + 「后台进行,完成后自动更新」)。

- [ ] **Step 4: tsc + 弯引号自查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 0 errors

Run: `git diff frontend/app/page.tsx | grep -c '^-.*[“”]'`
Expected: `0`

- [ ] **Step 5: Commit**

```bash
git add frontend/app/page.tsx
git commit -m "feat(ui): 「索引与构建」统一面板——三系统三行·统一确认·检索索引取消按钮

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 7: 就地 CTA 统一 + 收敛重复管理入口(前端 page.tsx)

**Files:**
- Modify: `frontend/app/page.tsx`(降级答案 banner / 严格推理提示 / 来源栏补抽 三处就地 CTA 统一到同一动作+确认;删除已被面板覆盖的重复管理入口)

**Interfaces:**
- Consumes: Task 6 的统一确认 + 动作函数。

- [ ] **Step 1: 先读定位**

Run: `cd frontend && grep -n "index_required\|构建索引\|全量构建知识图谱\|补抽\|onBuildScaleIndex\|立即重建检索索引\|空闲时重建" app/page.tsx`
读:①降级答案「构建索引」banner(index_required);②严格推理提示「全量构建知识图谱」;③来源栏「补抽 N 篇/全量构建」;④admin 动作列表里的「立即重建检索索引/空闲时重建检索索引」两条(现在是面板外的重复管理入口)。

- [ ] **Step 2: 统一就地 CTA**

三处就地 CTA 保留(上下文有用),但:①点击走与面板同一动作函数 + 同一 `confirmIndexAction`(不再各自 `window.confirm` 文案不一/或无确认);②文案对齐。不改它们的出现条件(index_required / !kgAvailable / kg_pending_sources)。

- [ ] **Step 3: 收敛重复管理入口**

把 admin 动作列表里的「立即重建检索索引」「空闲时重建检索索引」两条(纯管理入口、已被面板覆盖)移除或改为「打开索引面板」的跳转(避免同一动作 5 处入口 3 种确认)。保留面板为唯一管理处。若「空闲时重建(when=idle)」是面板未提供的能力,则在面板检索索引行补一个「空闲时建」次动作,再删列表两条——确保能力不丢失。

- [ ] **Step 4: tsc + 弯引号自查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 0 errors

Run: `git diff frontend/app/page.tsx | grep -c '^-.*[“”]'`
Expected: `0`

- [ ] **Step 5: Commit**

```bash
git add frontend/app/page.tsx
git commit -m "feat(ui): 就地建索引 CTA 统一确认 + 收敛重复管理入口到索引面板

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

### Task 8: 全量验证 + PR

- [ ] **Step 1: 后端全量**

Run: `cd backend && python -m pytest tests/ -q`
Expected: 全绿(基线 + 新增 test_index_build_consolidation.py;1 skipped 保持)

- [ ] **Step 2: 前端 tsc + node tests**

Run: `cd frontend && npm ci >/dev/null 2>&1 && npx tsc --noEmit && for f in app/*.test.mjs; do node --test "$f"; done`
Expected: 0 errors / 全 PASS

- [ ] **Step 3: check.sh**

Run: `bash scripts/check.sh`
Expected: 绿

- [ ] **Step 4: rebase + push + PR**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/index-hub
git fetch origin && git rebase origin/master
git push -u origin feat/index-build-consolidation
gh pr create --base master --title "feat: 索引与构建统一整合(正名 + 聚合状态 + 取消)" --body "见 docs/superpowers/specs/2026-07-09-index-build-consolidation-design.md。后端聚合 /index-status + /scale-index/cancel + manifest built_at;前端统一「索引与构建」面板(三系统三行·统一确认·取消按钮)、退役「已同步」重名、修「已排队不刷新」。真机视觉验证待做。

🤖 Generated with [Claude Code](https://claude.com/claude-code)"
```

- [ ] **Step 5: 真机视觉验证提示**

面板/状态切换/取消按钮本地难完整复现(需大库+各种索引态),PR 后真机走查:三行对齐、状态词无重名、排队态可取消、构建中取消给提示、时间戳显示。

---

## Self-Review 结论

- **Spec 覆盖**:A 聚合=Task1;B 取消=Task2;C built_at=Task3;正名 G=Task4(+Task6 落地);聚合消费+轮询修 D=Task5;面板 E=Task6;CTA 统一 F=Task7;验证=Task8。全覆盖 ✓
- **占位符**:后端任务含完整代码;前端大 JSX 任务给了精确锚点 + 结构契约 + 「先 grep 定位」硬要求(5755 行churning 文件无法把整段 JSX 写进计划且会 stale,这是诚实处理)。两处口径依赖(kg_pending_sources SQL、_now() 名)明确要求实现者 grep 照抄,不留猜测 ✓
- **类型一致**:`index_status`/`cancel_scale_index`/`_dequeue_scale_idle`(后端)与路由/前端 `fetchIndexStatus`/`cancelScaleIndex` 契约一致;`IndexStatus` TS 结构与后端 dict 一致;`last_built_at` 贯穿 Task3(后端)→Task4(类型)→Task6(渲染)✓
- **已知风险**:前端 Task6/7 是大文件重构,依赖实现者读现场;面板视觉需真机验证(计划已列)。kg 三字段口径若与 NotebookSummary 漂移会不一致——已要求照抄现成 SQL。
