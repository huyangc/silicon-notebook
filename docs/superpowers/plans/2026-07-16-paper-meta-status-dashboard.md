# 论文元数据补抽状态化 + 看板总览 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 PR#271 的论文元数据补抽从「点了没反应」升级为和 KG 构建对齐的有状态长任务（进行中/进度/完成可见），铃铛「待确认中心」出现进行中项与完成 toast，看板新增「论文元数据」总览三态，来源列表与详情展示每源抽取状态。

**Architecture:** 全部复用房内已有机制——进程内内存 dict 标注 backfill 状态（镜像 `knowledge_lifecycle.kg_building`）、`NotebookSummary` 加 O(1) 布尔字段、`SourceSummary` 加派生 status 字段（复用 PR#271 水合帮手、零新查询）、`NotebookAnalytics` 加两条覆盖索引 GROUP BY（不用 `kg_mutation_seq` 缓存，会读脏值）、`pending_bus` 复用现有 emit/mark_dirty 出完成事件、前端 `pending-center` 泛化既有 `index_done` 分支为按事件类型分派。

**Tech Stack:** FastAPI + SQLite + Next.js + pydantic-settings v2；worktree `/Users/hzf/workspace/silicon_notebook/.claude/worktrees/paper-meta-status`；分支 `claude/paper-meta-status-dashboard`。

## Global Constraints

- **零迁移、零新表、零新端点**：SCHEMA_VERSION 保持 17；仅响应模型加字段。
- **列表路径零新增查询**：`paper_meta_backfilling` 走 O(1) 内存 dict membership；篇数 COUNT 只在 analytics 端点。
- **`SourceSummary.paper_meta_status` 派生零新查询**：走 PR#271 已有的 `paper_meta_for_sources` 批量 IN + `source_from_row` 单点查——批量水合与详情单取共用一个 helper。
- **看板计数不用 `kg_mutation_seq` 缓存**：论文元数据写入不 bump 该 seq，seq-keyed 缓存会读脏值；走裸的带索引 GROUP BY，与 `source_status_counts` 同款纪律。
- **`api_contract.json`**：仅允许 `openapi`/`serialization` 按测试自身计算重算，`source_commit` 不动；先例见 PR#271 Task 3 报告。
- **`surface-manifest` / `facade_surface` / `callers-static`**：按测试输出对齐新消费点；不弱化断言。
- **前端 API 路径不带 `/api` 前缀**（双前缀 404 坑）。
- **中文文案沿用弯引号**：`git diff | grep -c '^-.*[""]'` = 0（PR#271 教训）。
- **`background_jobs.submit`**：`copy_context` 已传播 per-user ContextVar，铃铛 emit 的 uid 归属正确，无需额外处理。
- **失败/异常路径**：`backfill_paper_metadata` 的 `finally` 必然 pop dict；异常路径不 emit `paper_meta_done`（只成功路径 emit），零队列路径不 emit。
- **commit 消息**：每 task 一条，结尾附 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`。

## 文件结构

| 文件 | 职责 |
| --- | --- |
| `backend/app/services/source_ingestion.py` | `_paper_meta_backfilling: dict[nb_id, {"total","done"}]` + 锁；`backfill_paper_metadata` 内 add/incr/pop；`finally` 里 emit `paper_meta_done` + `mark_dirty`。 |
| `backend/app/services/sqlite_repository.py` | 门面属性/方法：`paper_meta_backfilling(nb_id)`（bool）与 `paper_meta_backfill_progress(nb_id)`（dict 或 None）供 catalog/pending 消费。 |
| `backend/app/services/notebook_catalog.py` | `get_notebook` 时把 `paper_meta_backfilling` 布尔灌进 `NotebookSummary`（镜像 `kg_building` 现有 wiring）。 |
| `backend/app/services/pending_actions_service.py` | 现有 `list_for_user` 扩展：新增 paper_meta building 项。 |
| `backend/app/repositories/sqlite/source_store.py` | `_paper_meta_status_for(row, meta_dict_or_None) -> str|None` helper；`source_from_row` 与 `sources_from_rows` 在构造 `SourceSummary` 前调用。 |
| `backend/app/repositories/sqlite/query_store.py` | `notebook_analytics` 加两条 GROUP BY，产 `paper_meta_counts`。 |
| `backend/app/models/schemas.py` | `NotebookSummary.paper_meta_backfilling: bool = False`；`NotebookAnalytics.paper_meta_counts: Dict[str,int]`；`SourceSummary.paper_meta_status: Optional[str] = None`。 |
| `backend/app/api/routes.py` | POST `/paper-meta/backfill` 的 `background_jobs.submit(..., notify_pending=True)`。 |
| `backend/app/repositories/ownership_manifest.py` | 新消费点行号（新方法+新调用）按测试输出对齐。 |
| `backend/tests/fixtures/repository_contract/api_contract.json` | `openapi`/`serialization` 键按测试自身计算重算。 |
| `backend/tests/fixtures/repository_contract/facade_surface.json` | 新 facade 消费点按测试输出对齐。 |
| `frontend/app/workspace-model.ts` | 三处新字段：`NotebookSummary.paper_meta_backfilling?`、`NotebookAnalytics.paper_meta_counts`、`SourceSummary.paper_meta_status?`；`PendingItem` union 加 `"paper_meta"`。 |
| `frontend/app/pending-center.tsx` | `PendingItem.type` 扩 `"paper_meta"`；泛化 `index_done` 分支为按 `event` 取文案；渲染 paper_meta 项。 |
| `frontend/app/pending-actions.ts` | 分组标签「论文元数据」、`itemSig` 补 paper_meta 分支。 |
| `frontend/app/page.tsx` | 完成轮询块（克隆 `buildingKg`，不弹 toast）；resume 钩子；看板「论文元数据」区块；来源列表行 `paper_meta_status` 徽章；详情按四态渲染。 |

## Task Right-Sizing 说明

10 个 task 按「一个独立可 review 的交付面」切：内存 dict → NotebookSummary → SourceSummary → NotebookAnalytics → PendingActions → 完成事件（后端串起来） → 前端轮询/resume → 前端 pending-center → 前端看板+徽章 → 端到端冒烟+契约同步。中间三个 schema 字段各自独立 task 是因为它们要单独过 api_contract regen 与 surface-manifest 对齐；如果合并 review 面会太大。

---

### Task 1: 后端 backfill runtime — 内存 dict + 进度

**Files:**
- Modify: `backend/app/services/source_ingestion.py`（`_paper_meta_backfilling` dict + lock，`backfill_paper_metadata` add/incr/pop）
- Modify: `backend/app/services/sqlite_repository.py`（facade 加 `paper_meta_backfilling(nb)`, `paper_meta_backfill_progress(nb)`）
- Test: `backend/tests/test_paper_meta_service.py`（追加 EOF）

**Interfaces produced:**
- `SourceIngestionService._paper_meta_backfilling: dict[str, dict]`（nb_id → {"total": N, "done": k}），仅 service 内部使用；facade 上通过两个查询方法暴露：
  - `SQLiteRepository.paper_meta_backfilling(notebook_id: str) -> bool`（O(1) membership）
  - `SQLiteRepository.paper_meta_backfill_progress(notebook_id: str) -> Optional[dict]`（返回 dict 副本或 None）

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_paper_meta_service.py` EOF 追加：

```python
def test_backfill_runtime_registers_and_pops_progress(repo, notebook_id, service, monkeypatch):
    """job 期间 facade paper_meta_backfilling(nb)=True + progress dict 反映 done/total；
    结束后（正常/异常）自动 pop 为 False/None。"""
    from unittest.mock import patch
    _insert_source(repo, notebook_id, "src-a")
    _insert_source(repo, notebook_id, "src-b")
    fake = _FakeKgLLM(PAYLOAD)
    repo._kg_llm_client = fake

    captured = {"during": None}
    original = service.ensure_paper_metadata

    def _spy(source, **kw):
        # 第一次调用时读一次进度快照
        if captured["during"] is None:
            captured["during"] = (
                repo.paper_meta_backfilling(notebook_id),
                repo.paper_meta_backfill_progress(notebook_id),
            )
        return original(source, **kw)

    monkeypatch.setattr(service, "ensure_paper_metadata", _spy)
    counts = service.backfill_paper_metadata(notebook_id)
    assert counts["total"] == 2
    during_flag, during_prog = captured["during"]
    assert during_flag is True
    assert during_prog is not None
    assert during_prog["total"] == 2
    assert 0 <= during_prog["done"] <= 2
    # 结束后 pop
    assert repo.paper_meta_backfilling(notebook_id) is False
    assert repo.paper_meta_backfill_progress(notebook_id) is None


def test_backfill_runtime_pops_on_exception(repo, notebook_id, service, monkeypatch):
    """异常也 pop——finally 保证；exception 从 as_completed 冒出到调用侧。"""
    _insert_source(repo, notebook_id, "src-c")
    repo._kg_llm_client = _FakeKgLLM(PAYLOAD)

    def _boom(*a, **kw):
        raise RuntimeError("boom")

    monkeypatch.setattr(service, "ensure_paper_metadata", _boom)
    # backfill 内部 _one 里 try 包了 ensure（已存在），异常不冒到 as_completed
    counts = service.backfill_paper_metadata(notebook_id)
    assert counts["total"] == 1
    assert counts.get("failed", 0) == 1
    assert repo.paper_meta_backfilling(notebook_id) is False
    assert repo.paper_meta_backfill_progress(notebook_id) is None


def test_backfill_empty_no_registration(repo, notebook_id):
    """queued=0 不 register。"""
    counts = repo.backfill_paper_metadata(notebook_id)
    assert counts == {"total": 0}
    assert repo.paper_meta_backfilling(notebook_id) is False
    assert repo.paper_meta_backfill_progress(notebook_id) is None
```

- [ ] **Step 2: 跑失败**

```bash
cd backend && python -m pytest tests/test_paper_meta_service.py::test_backfill_runtime_registers_and_pops_progress tests/test_paper_meta_service.py::test_backfill_runtime_pops_on_exception tests/test_paper_meta_service.py::test_backfill_empty_no_registration -q
```

期望：3 fail（`AttributeError: 'SQLiteRepository' object has no attribute 'paper_meta_backfilling'` 等）。

- [ ] **Step 3: 实现 service 内 dict + backfill 改造**

在 `backend/app/services/source_ingestion.py` 的 `SourceIngestionService.__init__`（或 class 顶）加：

```python
# 论文元数据 backfill 进程内状态镜像 kg_building（重启即清）
# nb_id → {"total": N, "done": k}
self._paper_meta_backfilling: dict[str, dict] = {}
self._paper_meta_backfilling_lock = threading.Lock()
```

改造 `backfill_paper_metadata`（既有方法体在 :1124-1174 附近）：

```python
def backfill_paper_metadata(
    self,
    notebook_id: str,
    force: bool = False,
    progress: Optional[Callable[[int, int, str, str], None]] = None,
) -> dict:
    self.notebooks.get_row(notebook_id)  # KeyError if missing
    targets = self.sources.sources_missing_paper_meta(
        notebook_id, include_existing=force
    )
    counts: dict = {"total": len(targets)}
    if not targets:
        return counts
    workers = max(1, min(8, int(getattr(self.settings, "kg_extract_workers", 4))))
    lock = threading.Lock()
    done = 0

    # 注册状态（重复 backfill 同一 nb 会覆盖，符合"最新一次"语义）
    with self._paper_meta_backfilling_lock:
        self._paper_meta_backfilling[notebook_id] = {
            "total": len(targets), "done": 0
        }
    try:
        def _one(source_id: str) -> None:
            nonlocal done
            try:
                row = self.sources.get_source(source_id)
                status = self.ensure_paper_metadata(row, force=force)
            except Exception:
                status = "failed"
                self.event_log.logger.exception(
                    "paper metadata backfill failed for %s", source_id
                )
            with lock:
                done += 1
                counts[status] = counts.get(status, 0) + 1
                current = done
            # 同步进度到 backfilling dict（供 pending-actions 读）
            with self._paper_meta_backfilling_lock:
                if notebook_id in self._paper_meta_backfilling:
                    self._paper_meta_backfilling[notebook_id]["done"] = current
            if progress is not None:
                progress(current, len(targets), source_id, status)

        with concurrent.futures.ThreadPoolExecutor(
            max_workers=workers, thread_name_prefix="paper-meta"
        ) as pool:
            futures = [
                pool.submit(contextvars.copy_context().run, _one, sid)
                for sid in targets
            ]
            for future in concurrent.futures.as_completed(futures):
                future.result()
        self.event_log.emit(
            {"kind": "paper_meta", "notebook_id": notebook_id, "backfill": counts}
        )
        return counts
    finally:
        with self._paper_meta_backfilling_lock:
            self._paper_meta_backfilling.pop(notebook_id, None)
```

- [ ] **Step 4: 实现 facade 方法**

在 `backend/app/services/sqlite_repository.py` 找到 `backfill_paper_metadata` facade 方法（:3285 附近），紧邻加：

```python
def paper_meta_backfilling(self, notebook_id: str) -> bool:
    """O(1) 内存 membership；重启后天然为 False（未在跑）。"""
    svc = self._runtime.source_ingestion
    return notebook_id in svc._paper_meta_backfilling

def paper_meta_backfill_progress(self, notebook_id: str) -> Optional[dict]:
    """返回 {"total","done"} 的浅拷贝或 None（未在跑）。锁内取快照。"""
    svc = self._runtime.source_ingestion
    with svc._paper_meta_backfilling_lock:
        prog = svc._paper_meta_backfilling.get(notebook_id)
        return dict(prog) if prog else None
```

- [ ] **Step 5: 跑测试通过**

```bash
cd backend && python -m pytest tests/test_paper_meta_service.py -q
```

期望：全部 pass（含既有测试保持绿 + 3 新测试通过）。

- [ ] **Step 6: 对齐 manifest**

```bash
cd backend && python -m pytest tests/test_repository_surface_manifest.py tests/test_repository_callers_static.py -q
```

按失败输出把 `paper_meta_backfilling` / `paper_meta_backfill_progress` 加进 `backend/app/repositories/ownership_manifest.py` 的 `SURFACE_MEMBERS` 与 `facade_surface.json`（新 facade 方法，owner=`SourceIngestionService`，method 类型；consumer 首例=service 上下文暴露方法本身，consumers 现在只包含这个 facade 方法定义行）。参考 PR#271 Task 5 `backfill_paper_metadata` 的登记方式。再跑：

```bash
cd backend && python -m pytest tests/test_repository_surface_manifest.py tests/test_repository_callers_static.py -q
```

期望：全绿。

- [ ] **Step 7: commit**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/paper-meta-status
git add backend/app/services/source_ingestion.py backend/app/services/sqlite_repository.py \
        backend/tests/test_paper_meta_service.py \
        backend/app/repositories/ownership_manifest.py \
        backend/tests/fixtures/repository_contract/facade_surface.json
git commit -m "feat(ingest): paper-meta backfill in-memory progress state + facade query

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: `NotebookSummary.paper_meta_backfilling` 字段

**Files:**
- Modify: `backend/app/models/schemas.py`（`NotebookSummary` 加字段，跟 `kg_building` 相邻）
- Modify: `backend/app/services/notebook_catalog.py`（`get_notebook` 塞入布尔；镜像 `kg_building` 现有 wiring）
- Modify: `backend/tests/fixtures/repository_contract/api_contract.json`（openapi + serialization 重算）
- Test: `backend/tests/test_notebook_catalog.py` 或 `test_repository_read.py`（追加 EOF）

**Interfaces consumed:** Task 1 的 `repo.paper_meta_backfilling(nb_id) -> bool`。

**Interfaces produced:** `NotebookSummary.paper_meta_backfilling: bool = False`。

- [ ] **Step 1: 写失败测试**

找到 `backend/tests/test_notebook_catalog.py`（或类似位置，grep `get_notebook` 用 pattern `NotebookSummary`）。追加：

```python
def test_notebook_summary_reflects_paper_meta_backfilling(repo, notebook_id):
    """summary.paper_meta_backfilling 反映 service 内存 dict membership。"""
    svc = repo._runtime.source_ingestion
    # 未在跑 → False
    assert repo.get_notebook(notebook_id).paper_meta_backfilling is False
    # 手动注入 → True
    with svc._paper_meta_backfilling_lock:
        svc._paper_meta_backfilling[notebook_id] = {"total": 3, "done": 1}
    try:
        assert repo.get_notebook(notebook_id).paper_meta_backfilling is True
    finally:
        with svc._paper_meta_backfilling_lock:
            svc._paper_meta_backfilling.pop(notebook_id, None)
    assert repo.get_notebook(notebook_id).paper_meta_backfilling is False
```

- [ ] **Step 2: 跑失败**

```bash
cd backend && python -m pytest tests/test_notebook_catalog.py::test_notebook_summary_reflects_paper_meta_backfilling -q
```

期望：fail（`AttributeError: 'NotebookSummary' object has no attribute 'paper_meta_backfilling'`）。

- [ ] **Step 3: schemas 加字段**

在 `backend/app/models/schemas.py` `NotebookSummary` 里，紧邻 `kg_building` 加：

```python
paper_meta_backfilling: bool = False  # 进程内 backfill 状态镜像，重启即 False
```

- [ ] **Step 4: catalog 回填**

在 `backend/app/services/notebook_catalog.py` 找 `get(kg_building=...)` 调用（:264 附近）。看 `get_notebook` 从哪里访问 `kg_building` set——本项目现有 wiring 是通过 `NotebookCatalog.get_row` 的 `kg_building=...` 关键字参数塞进 `from_row`。找到这条调用链，仿照 `kg_building` 加 `paper_meta_backfilling=source_ingestion._paper_meta_backfilling` membership 判定并回填到 `NotebookSummary`。

具体锚点：
- `NotebookCatalog.__init__` 已注入 `self.source_ingestion`（如未注入，加进 runtime wiring）。
- `NotebookCatalog.from_row(row, connection, kg_building=False)` 是 `NotebookSummary` 装配点，加 `paper_meta_backfilling: bool = False` kwarg，末尾 `summary.paper_meta_backfilling = paper_meta_backfilling`。
- `NotebookCatalog.get_notebook`（或 `get_row` 消费者）算 `paper_meta_backfilling = notebook_id in self.source_ingestion._paper_meta_backfilling` 传下去。

如果 catalog 目前不持 source_ingestion 引用，需要在 runtime wiring（找 `NotebookCatalog(...)` 构造点）加参数——一并改，改动局部。

- [ ] **Step 5: 跑测试通过**

```bash
cd backend && python -m pytest tests/test_notebook_catalog.py -q
```

期望：全绿。

- [ ] **Step 6: 全套 + api_contract 重算**

```bash
cd backend && python -m pytest tests/test_repository_api_contract.py -q
```

期望：fail（openapi diff `paper_meta_backfilling` 新增）。按 PR#271 Task 3 报告里的 api_contract regen 方法（read `test_repository_api_contract.py._contract()` / `_runtime_serialization()` 的计算，跑一次性 scratchpad 脚本重算 openapi/serialization 两键，`source_commit` 不动，写回 fixture）。再跑：

```bash
cd backend && python -m pytest tests/test_repository_api_contract.py -q
```

期望：全绿。

- [ ] **Step 7: commit**

```bash
git add backend/app/models/schemas.py backend/app/services/notebook_catalog.py \
        backend/tests/test_notebook_catalog.py \
        backend/tests/fixtures/repository_contract/api_contract.json
git commit -m "feat(catalog): NotebookSummary.paper_meta_backfilling field

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: `SourceSummary.paper_meta_status` 派生字段

**Files:**
- Modify: `backend/app/models/schemas.py`（`SourceSummary` 加 `paper_meta_status: Optional[str] = None`）
- Modify: `backend/app/repositories/sqlite/source_store.py`（新 helper + 两处装配点调用）
- Modify: `backend/tests/fixtures/repository_contract/api_contract.json`
- Test: `backend/tests/test_paper_meta_store.py`（追加 EOF）

**Interfaces produced:**
- `SourceStore._paper_meta_status_for(row: sqlite3.Row, meta: Optional[dict]) -> Optional[str]`：内部纯函数，从 row 的 `source_type`、`doc_type`、`parse_status` 与 meta 字典派生四态。
- `SourceSummary.paper_meta_status: Optional[str] = None`：`"has_meta" | "not_paper" | "missing" | None`。

- [ ] **Step 1: 写失败测试**

在 `backend/tests/test_paper_meta_store.py` EOF 追加：

```python
def test_source_summary_paper_meta_status_four_states(repo, notebook_id):
    """四态：has_meta / not_paper / missing / None。"""
    store = repo._runtime.source_store

    # a: 合规源 + has_meta 行
    store.insert_source(
        source_id="src-a", notebook_id=notebook_id, title="A",
        source_type="pdf", status="parsed", parse_status="parsed",
        file_name="a.pdf", file_path="/tmp/a.pdf", file_size=0,
        file_hash="h-a", summary="", doc_type="",
    )
    store.upsert_paper_meta("src-a", notebook_id, {
        "is_paper": True, "paper_title": "T", "venue": None,
        "pub_year": None, "doi": None, "keywords": [], "authors": [],
        "raw_json": "{}", "model": "test",
    })

    # b: 合规源 + not_paper 标记行
    store.insert_source(
        source_id="src-b", notebook_id=notebook_id, title="B",
        source_type="pdf", status="parsed", parse_status="parsed",
        file_name="b.pdf", file_path="/tmp/b.pdf", file_size=0,
        file_hash="h-b", summary="", doc_type="",
    )
    store.upsert_paper_meta("src-b", notebook_id, {
        "is_paper": False, "paper_title": None, "venue": None,
        "pub_year": None, "doi": None, "keywords": [], "authors": [],
        "raw_json": "{}", "model": "test",
    })

    # c: 合规源 + 无 meta 行（missing）
    store.insert_source(
        source_id="src-c", notebook_id=notebook_id, title="C",
        source_type="pdf", status="parsed", parse_status="parsed",
        file_name="c.pdf", file_path="/tmp/c.pdf", file_size=0,
        file_hash="h-c", summary="", doc_type="",
    )

    # d: 非合规源（memory）→ None
    store.insert_source(
        source_id="src-d", notebook_id=notebook_id, title="D",
        source_type="memory", status="parsed", parse_status="parsed",
        file_name="", file_path="", file_size=0,
        file_hash="h-d", summary="", doc_type="",
    )

    # 详情单取
    assert repo.get_source("src-a").paper_meta_status == "has_meta"
    assert repo.get_source("src-b").paper_meta_status == "not_paper"
    assert repo.get_source("src-c").paper_meta_status == "missing"
    assert repo.get_source("src-d").paper_meta_status is None

    # 列表批量：口径一致
    page = repo.list_sources_page(notebook_id)
    by_id = {s.id: s for s in page.items}
    assert by_id["src-a"].paper_meta_status == "has_meta"
    assert by_id["src-b"].paper_meta_status == "not_paper"
    assert by_id["src-c"].paper_meta_status == "missing"
    # 注意 memory 源可能不在 list_sources_page（既有过滤），若 assertion 失败可去掉此项
```

- [ ] **Step 2: 跑失败**

```bash
cd backend && python -m pytest tests/test_paper_meta_store.py::test_source_summary_paper_meta_status_four_states -q
```

期望：fail（`AttributeError: 'SourceSummary' object has no attribute 'paper_meta_status'`）。

- [ ] **Step 3: schemas 加字段**

在 `backend/app/models/schemas.py` `SourceSummary` 里紧邻现有 `authors: List[str]` 或 `pub_year` 加：

```python
paper_meta_status: Optional[str] = None  # "has_meta"|"not_paper"|"missing"|None
```

- [ ] **Step 4: source_store 加 helper + 两处装配点**

在 `backend/app/repositories/sqlite/source_store.py` 的 `_paper_meta_dict` staticmethod（:721）之后加：

```python
@staticmethod
def _paper_meta_status_for(row: sqlite3.Row, meta: Optional[dict]) -> Optional[str]:
    """纯函数：从 sources 行 + 可选 meta 字典派生四态。零 DB 访问。"""
    if meta is not None:
        return "has_meta" if meta.get("is_paper") else "not_paper"
    source_type = row["source_type"] if "source_type" in row.keys() else ""
    doc_type = row["doc_type"] if "doc_type" in row.keys() else ""
    parse_status = row["parse_status"] if "parse_status" in row.keys() else ""
    if source_type in ("memory", "knowhow"):
        return None
    if doc_type not in ("", "academic_paper"):
        return None
    if parse_status not in ("parsed", "extracting", "extracted"):
        return None
    return "missing"
```

在 `source_from_row`（构造 `SourceSummary` 处，:522 附近有 `pm = self.paper_meta_for_sources(db, [row["id"]]).get(row["id"])`）加：

```python
summary.paper_meta_status = self._paper_meta_status_for(row, pm)
```

在 `sources_from_rows`（批量装配，:568 `paper_meta = self.paper_meta_for_sources(db, source_ids)` 之后的循环里）：

```python
summary.paper_meta_status = self._paper_meta_status_for(row, paper_meta.get(row["id"]))
```

- [ ] **Step 5: 跑测试通过**

```bash
cd backend && python -m pytest tests/test_paper_meta_store.py -q
```

期望：全绿（含新测试 + 既有测试保持）。

- [ ] **Step 6: api_contract 重算**

```bash
cd backend && python -m pytest tests/test_repository_api_contract.py -q
```

fail 后重算（同 Task 2 Step 6），再跑通过。

- [ ] **Step 7: commit**

```bash
git add backend/app/models/schemas.py backend/app/repositories/sqlite/source_store.py \
        backend/tests/test_paper_meta_store.py \
        backend/tests/fixtures/repository_contract/api_contract.json
git commit -m "feat(sources): SourceSummary.paper_meta_status derived field (four states)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: `NotebookAnalytics.paper_meta_counts` 三态计数

**Files:**
- Modify: `backend/app/models/schemas.py`（`NotebookAnalytics` 加 `paper_meta_counts`）
- Modify: `backend/app/repositories/sqlite/query_store.py`（`notebook_analytics` 加两条 GROUP BY）
- Modify: `backend/tests/fixtures/repository_contract/api_contract.json`
- Test: `backend/tests/test_analytics.py` 或 `test_query_store.py`（EOF 或新增文件）

**Interfaces produced:** `NotebookAnalytics.paper_meta_counts: Dict[str,int]`，键 `has_meta` / `marker` / `missing`。

- [ ] **Step 1: 写失败测试**

grep `notebook_analytics` 相关既有测试文件位置。新建或追加：

```python
def test_notebook_analytics_paper_meta_counts_three_states(repo, notebook_id):
    """构造 has_meta/marker/missing/非合规 混合，断言三键精确。"""
    store = repo._runtime.source_store
    # 2 has_meta
    for sid, is_paper in [("h1", True), ("h2", True), ("m1", False)]:
        store.insert_source(
            source_id=sid, notebook_id=notebook_id, title=sid,
            source_type="pdf", status="parsed", parse_status="parsed",
            file_name=f"{sid}.pdf", file_path=f"/tmp/{sid}.pdf",
            file_size=0, file_hash=f"h-{sid}", summary="", doc_type="",
        )
        store.upsert_paper_meta(sid, notebook_id, {
            "is_paper": is_paper, "paper_title": None, "venue": None,
            "pub_year": None, "doi": None, "keywords": [], "authors": [],
            "raw_json": "{}", "model": "t",
        })
    # 2 missing
    for sid in ("mi1", "mi2"):
        store.insert_source(
            source_id=sid, notebook_id=notebook_id, title=sid,
            source_type="pdf", status="parsed", parse_status="parsed",
            file_name=f"{sid}.pdf", file_path=f"/tmp/{sid}.pdf",
            file_size=0, file_hash=f"h-{sid}", summary="", doc_type="",
        )
    # 1 非合规（memory）不计
    store.insert_source(
        source_id="mm", notebook_id=notebook_id, title="mm",
        source_type="memory", status="parsed", parse_status="parsed",
        file_name="", file_path="", file_size=0, file_hash="h-mm",
        summary="", doc_type="",
    )
    a = repo.notebook_analytics(notebook_id)
    assert a.paper_meta_counts == {"has_meta": 2, "marker": 1, "missing": 2}


def test_notebook_analytics_paper_meta_counts_no_stale_after_write(repo, notebook_id):
    """meta 写入后立即查得新计数（不用 kg_mutation_seq 缓存）。"""
    store = repo._runtime.source_store
    store.insert_source(
        source_id="s1", notebook_id=notebook_id, title="s1",
        source_type="pdf", status="parsed", parse_status="parsed",
        file_name="s1.pdf", file_path="/tmp/s1.pdf", file_size=0,
        file_hash="h", summary="", doc_type="",
    )
    assert repo.notebook_analytics(notebook_id).paper_meta_counts == {
        "has_meta": 0, "marker": 0, "missing": 1
    }
    store.upsert_paper_meta("s1", notebook_id, {
        "is_paper": True, "paper_title": None, "venue": None,
        "pub_year": None, "doi": None, "keywords": [], "authors": [],
        "raw_json": "{}", "model": "t",
    })
    # 无 seq bump 也应立即刷新
    assert repo.notebook_analytics(notebook_id).paper_meta_counts == {
        "has_meta": 1, "marker": 0, "missing": 0
    }
```

- [ ] **Step 2: 跑失败**

```bash
cd backend && python -m pytest tests/test_analytics.py -q
```

期望：fail。

- [ ] **Step 3: schemas 加字段**

```python
paper_meta_counts: Dict[str, int] = Field(default_factory=dict)
```

放 `NotebookAnalytics` 里，紧挨 `source_status_counts`。

- [ ] **Step 4: query_store 加两条 GROUP BY**

在 `backend/app/repositories/sqlite/query_store.py` `notebook_analytics` 里 `source_status_counts` 之后加：

```python
# is_paper 计数（走 idx_source_paper_meta_nb）
by_is_paper = {
    int(row["is_paper"]): int(row["c"])
    for row in db.execute(
        "SELECT is_paper, COUNT(*) AS c FROM source_paper_meta "
        "WHERE notebook_id = ? GROUP BY is_paper",
        (notebook_id,),
    ).fetchall()
}
# missing 计数（sources_missing_paper_meta 的 COUNT 镜像，
# 走 idx_sources_nb_parse_status_type）
missing = int(db.execute(
    "SELECT COUNT(*) AS c FROM sources s "
    "WHERE s.notebook_id = ? "
    "  AND s.source_type NOT IN ('memory', 'knowhow') "
    "  AND s.doc_type IN ('', 'academic_paper') "
    "  AND s.parse_status IN ('parsed', 'extracting', 'extracted') "
    "  AND NOT EXISTS (SELECT 1 FROM source_paper_meta m WHERE m.source_id = s.id)",
    (notebook_id,),
).fetchone()["c"])
paper_meta_counts = {
    "has_meta": by_is_paper.get(1, 0),
    "marker":   by_is_paper.get(0, 0),
    "missing":  missing,
}
```

在 `NotebookAnalytics(...)` 构造里加 `paper_meta_counts=paper_meta_counts`。

- [ ] **Step 5: 跑测试通过**

```bash
cd backend && python -m pytest tests/test_analytics.py -q
```

- [ ] **Step 6: api_contract 重算**

同 Task 2 Step 6。

- [ ] **Step 7: commit**

```bash
git add backend/app/models/schemas.py backend/app/repositories/sqlite/query_store.py \
        backend/tests/test_analytics.py \
        backend/tests/fixtures/repository_contract/api_contract.json
git commit -m "feat(analytics): NotebookAnalytics.paper_meta_counts three-state groupby

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: `PendingActionsService` — 补抽进行中项

**Files:**
- Modify: `backend/app/services/pending_actions_service.py`（extend `list_for_user`）
- Modify: `backend/app/services/sqlite_repository.py`（如需注入 catalog 让 pending 遍历 user's notebooks）
- Test: `backend/tests/test_pending_actions.py` 或 `test_pending_center.py`

**Interfaces consumed:** Task 1 的 `paper_meta_backfilling` / `paper_meta_backfill_progress`；user 拥有的 notebook 列表通过 `projections.pending_actions_projection_rows(user_id)["notebook_ids"]` 与 `notebook_names` 已可用（现有 index 分支就用它）。

**Interfaces produced:** `list_for_user(uid)["items"]` 里 `{"type":"paper_meta","state":"building","notebook_id","notebook_name","progress":{"done","total"}}` 项。

- [ ] **Step 1: 写失败测试**

```python
def test_pending_actions_includes_paper_meta_building(repo, notebook_id):
    """补抽进行中，list_for_user 含 type=paper_meta 项，progress 反映内存 dict。"""
    svc = repo._runtime.source_ingestion
    # 找 owner uid（用 create_notebook 时的 user，或既有 helper）
    from app.services.pending_actions_service import PendingActionsService
    pending = repo._runtime.pending_actions_service
    uid = repo._runtime.projections._current_uid_for_test  # 或直接 repo owner uid

    # 未在跑 → 无 paper_meta 项
    items0 = pending.list_for_user(uid)["items"]
    assert all(i["type"] != "paper_meta" for i in items0)

    # 手动注入进行中
    with svc._paper_meta_backfilling_lock:
        svc._paper_meta_backfilling[notebook_id] = {"total": 5, "done": 2}
    try:
        items = pending.list_for_user(uid)["items"]
        pm = [i for i in items if i["type"] == "paper_meta"]
        assert len(pm) == 1
        assert pm[0]["state"] == "building"
        assert pm[0]["notebook_id"] == notebook_id
        assert pm[0]["progress"] == {"done": 2, "total": 5}
    finally:
        with svc._paper_meta_backfilling_lock:
            svc._paper_meta_backfilling.pop(notebook_id, None)


def test_pending_actions_paper_meta_per_user_filter(repo, notebook_id):
    """非 owner 看不到该 notebook 的 paper_meta 项。"""
    svc = repo._runtime.source_ingestion
    with svc._paper_meta_backfilling_lock:
        svc._paper_meta_backfilling[notebook_id] = {"total": 1, "done": 0}
    try:
        pending = repo._runtime.pending_actions_service
        # 用一个陌生 uid（非 notebook owner）
        items = pending.list_for_user("user-stranger-999")["items"]
        assert all(i.get("notebook_id") != notebook_id for i in items
                    if i["type"] == "paper_meta")
    finally:
        with svc._paper_meta_backfilling_lock:
            svc._paper_meta_backfilling.pop(notebook_id, None)
```

（`_current_uid_for_test` 若没有，用固定 owner uid——参考现有 test_pending fixture 里怎么取 owner。）

- [ ] **Step 2: 跑失败**

```bash
cd backend && python -m pytest tests/test_pending_actions.py::test_pending_actions_includes_paper_meta_building tests/test_pending_actions.py::test_pending_actions_paper_meta_per_user_filter -q
```

期望：fail（items 不含 paper_meta）。

- [ ] **Step 3: 扩展 PendingActionsService**

```python
class PendingActionsService:
    def __init__(self, projections, *, scale_runtime, source_ingestion) -> None:
        self.projections = projections
        self.scale_runtime = scale_runtime
        self.source_ingestion = source_ingestion  # 新增

    def list_for_user(self, user_id: str) -> dict:
        projection = self.projections.pending_actions_projection_rows(user_id)
        items = projection["items"]
        for notebook_id in projection["notebook_ids"]:
            # 既有 index 分支保持
            try:
                status = self.scale_runtime.status(notebook_id)
            except Exception:
                pass
            else:
                state = status.get("state")
                if state in ("stale", "suggested", "building", "queued"):
                    item: dict[str, Any] = {
                        "type": "index",
                        "state": "building" if state == "queued" else state,
                        "notebook_id": notebook_id,
                        "notebook_name": projection["notebook_names"].get(notebook_id, ""),
                    }
                    total = status.get("total_chunks") or 0
                    delta = status.get("delta_chunks") or 0
                    if state in ("building", "queued") and total:
                        item["progress"] = round(100.0 * max(0, total - delta) / total)
                    items.append(item)
            # 新增 paper_meta 分支
            with self.source_ingestion._paper_meta_backfilling_lock:
                prog = self.source_ingestion._paper_meta_backfilling.get(notebook_id)
                prog_copy = dict(prog) if prog else None
            if prog_copy is not None:
                items.append({
                    "type": "paper_meta",
                    "state": "building",
                    "notebook_id": notebook_id,
                    "notebook_name": projection["notebook_names"].get(notebook_id, ""),
                    "progress": prog_copy,
                })

        count = sum(
            1 for item in items
            if item["type"] in ("report_outline", "governance")
            or (item["type"] == "index" and item["state"] in ("stale", "suggested"))
        )
        # paper_meta building 不计入 count（跟 index building 一致——只显示，不响铃）
        return {"count": count, "items": items}
```

在 runtime wiring（找 `PendingActionsService(projections, scale_runtime=...)` 构造点）加 `source_ingestion=...` 参数。

- [ ] **Step 4: 跑测试通过**

- [ ] **Step 5: 全套回归 + manifest**

```bash
cd backend && python -m pytest tests/test_pending_actions.py tests/test_repository_surface_manifest.py tests/test_repository_callers_static.py -q
```

按输出对齐 manifest（`PendingActionsService.__init__` 消费 source_ingestion 是新签名）。

- [ ] **Step 6: commit**

```bash
git add backend/app/services/pending_actions_service.py backend/app/services/sqlite_repository.py \
        backend/tests/test_pending_actions.py \
        backend/app/repositories/ownership_manifest.py \
        backend/tests/fixtures/repository_contract/facade_surface.json
git commit -m "feat(pending): paper_meta building items in list_for_user

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: 完成事件 emit + 端点 notify_pending

**Files:**
- Modify: `backend/app/services/source_ingestion.py`（`backfill_paper_metadata` 的 `finally` 前成功路径 emit paper_meta_done + mark_dirty）
- Modify: `backend/app/api/routes.py`（backfill 端点 `background_jobs.submit(..., notify_pending=True)`）
- Test: `backend/tests/test_paper_meta_service.py`（追加 EOF）+ `backend/tests/test_paper_meta_api.py`（追加 EOF）

**Interfaces consumed:** 现有 `pending_bus.emit(uid, dict)` 与 `pending_bus.mark_dirty(uid)`；`background_jobs.submit(..., notify_pending=True)` 兜底。

**Interfaces produced:** SSE 上 `{"kind":"event","event":"paper_meta_done","notebook_id","notebook_name","stored":N}`；无 stored 时（异常/零队列）不 emit。

- [ ] **Step 1: 写失败测试**

在 `test_paper_meta_service.py`：

```python
def test_backfill_emits_paper_meta_done_on_success(repo, notebook_id, service, monkeypatch):
    """成功路径 finally 前 emit paper_meta_done（stored=N）+ mark_dirty。"""
    _insert_source(repo, notebook_id, "src-x")
    repo._kg_llm_client = _FakeKgLLM(PAYLOAD)

    events: list = []
    marked: list = []
    from app.services import pending_bus as pb_module
    monkeypatch.setattr(pb_module.pending_bus, "emit",
                        lambda uid, evt: events.append((uid, evt)))
    monkeypatch.setattr(pb_module.pending_bus, "mark_dirty",
                        lambda uid: marked.append(uid))
    service.backfill_paper_metadata(notebook_id)
    done = [e for _, e in events if e.get("event") == "paper_meta_done"]
    assert len(done) == 1
    assert done[0]["notebook_id"] == notebook_id
    assert done[0]["stored"] >= 1
    assert len(marked) >= 1


def test_backfill_no_done_on_zero_queue(repo, notebook_id, monkeypatch):
    """零队列不 emit done。"""
    events: list = []
    from app.services import pending_bus as pb_module
    monkeypatch.setattr(pb_module.pending_bus, "emit",
                        lambda uid, evt: events.append((uid, evt)))
    repo.backfill_paper_metadata(notebook_id)
    assert not any(e.get("event") == "paper_meta_done" for _, e in events)


def test_backfill_no_done_on_all_failed(repo, notebook_id, service, monkeypatch):
    """全 failed 不 emit done（stored=0 视为无成果）。"""
    _insert_source(repo, notebook_id, "src-y")
    repo._kg_llm_client = _RaisingLLM()
    events: list = []
    from app.services import pending_bus as pb_module
    monkeypatch.setattr(pb_module.pending_bus, "emit",
                        lambda uid, evt: events.append((uid, evt)))
    service.backfill_paper_metadata(notebook_id)
    assert not any(e.get("event") == "paper_meta_done" for _, e in events)
```

- [ ] **Step 2: 跑失败**

- [ ] **Step 3: 实现 emit**

在 `backfill_paper_metadata` 里 event_log.emit 之后、`return counts` 之前加：

```python
stored = int(counts.get("stored", 0))
if stored > 0:
    from app.core.current_user import get_current_user_id  # 或既有 helper
    from app.services import pending_bus as pb
    uid = get_current_user_id()  # ContextVar，job 内 copy_context 传播
    if uid:
        nb_name = self.notebooks.get_row(notebook_id).get("name", "")
        pb.pending_bus.emit(uid, {
            "event": "paper_meta_done",
            "notebook_id": notebook_id,
            "notebook_name": nb_name,
            "stored": stored,
        })
        pb.pending_bus.mark_dirty(uid)
```

（uid 取法看现有 `notify_index_done` 里怎么拿 owner——mirror 之。若 `get_current_user_id` 名字不同，grep 找。）

- [ ] **Step 4: 端点加 notify_pending**

`backend/app/api/routes.py` 的 `backfill_paper_metadata` route 里 `background_jobs.submit(...)` 改：

```python
background_jobs.submit(
    repo.backfill_paper_metadata, notebook_id,
    name=f"papermeta-{notebook_id}",
    notify_pending=True,   # 新增：兜底刷新 pending 快照
)
```

- [ ] **Step 5: 跑测试通过**

```bash
cd backend && python -m pytest tests/test_paper_meta_service.py tests/test_paper_meta_api.py -q
```

- [ ] **Step 6: commit**

```bash
git add backend/app/services/source_ingestion.py backend/app/api/routes.py \
        backend/tests/test_paper_meta_service.py backend/tests/test_paper_meta_api.py
git commit -m "feat(pending): emit paper_meta_done + notify_pending on backfill

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: 前端 — 完成轮询 + resume + 徽章

**Files:**
- Modify: `frontend/app/workspace-model.ts`（三处字段：NotebookSummary、NotebookAnalytics、SourceSummary）
- Modify: `frontend/app/page.tsx`（轮询块 + resume + 徽章 + 详情四态渲染）

**Interfaces consumed:** Task 2/3/4 的三处 schema 字段。

- [ ] **Step 1: 加 TS 类型**

`workspace-model.ts` 三处补：

```ts
// NotebookSummary
paper_meta_backfilling?: boolean;

// NotebookAnalytics
paper_meta_counts?: { has_meta: number; marker: number; missing: number };

// SourceSummary
paper_meta_status?: "has_meta" | "not_paper" | "missing" | null;
```

- [ ] **Step 2: 完成轮询块**

`page.tsx` 找 `buildingKg` 轮询 useEffect（:963-983 附近）。克隆一份给 `backfillingMeta`：

```tsx
useEffect(() => {
  if (!backfillingMeta || !currentNotebookId || analytics) return;
  const start = Date.now();
  const timer = window.setInterval(async () => {
    const refreshed = await fetchNotebook(currentNotebookId).catch(() => null);
    if (!refreshed) return;
    if (!refreshed.paper_meta_backfilling) {
      window.clearInterval(timer);
      setBackfillingMeta(false);
      setCurrentNotebook(refreshed);
      reloadSources();  // 刷新使新元数据带出
    } else if (Date.now() - start > 20 * 60 * 1000) {
      window.clearInterval(timer);
      setBackfillingMeta(false);
    }
  }, 6000);
  return () => window.clearInterval(timer);
}, [backfillingMeta, currentNotebookId, analytics]);
```

（`reloadSources` 若名字不同，用现有 `refreshSources`/`loadSources` 类似 helper。完成 toast 不弹，交给铃铛 done 分支。）

- [ ] **Step 3: resume 钩子**

打开 notebook 时（`openAnalytics` 或 notebook select 分支，找 `if (s.kg.building) setBuildingKg(true)` 处）加：

```tsx
if (nb.paper_meta_backfilling) setBackfillingMeta(true);
```

- [ ] **Step 4: 徽章 + 详情**

来源列表行找「已入图」徽章渲染位（grep `已入图`）。旁边加：

```tsx
{s.paper_meta_status === "missing" && (
  <span className="tag" style={{ color: "var(--color-warn,#b97a00)" }}>待补全</span>
)}
{s.paper_meta_status === "not_paper" && (
  <span className="tag" style={{ opacity: 0.6 }}>非论文</span>
)}
```

详情「论文信息」块（PR#271 现有）扩展为四态：

```tsx
{sourceDetail.paper_meta_status === "has_meta" && sourceDetail.paper_meta && (
  /* 现有 has_meta 卡 */
)}
{sourceDetail.paper_meta_status === "not_paper" && (
  <div className="source-detail-paper muted">该来源非学术论文</div>
)}
{sourceDetail.paper_meta_status === "missing" && (
  <div className="source-detail-paper warn">
    论文信息未补全（点击上方"补全论文信息"）
  </div>
)}
```

- [ ] **Step 5: 验证**

```bash
cd frontend && npx tsc --noEmit
```

期望：0 errors。

- [ ] **Step 6: 弯引号护栏**

```bash
git diff frontend/app/page.tsx | grep -c $'^-.*[“”]'
```

期望：`0`。

- [ ] **Step 7: commit**

```bash
git add frontend/app/workspace-model.ts frontend/app/page.tsx
git commit -m "feat(frontend): backfill completion polling + resume + per-source badge

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: 前端 — pending-center paper_meta 支持

**Files:**
- Modify: `frontend/app/pending-center.tsx`（PendingItem union + done toast 泛化）
- Modify: `frontend/app/pending-actions.ts`（分组标签、itemSig）

**Interfaces consumed:** Task 5 的 items 结构、Task 6 的 SSE event。

- [ ] **Step 1: PendingItem union 扩展**

`pending-center.tsx`:17：

```ts
export type PendingItem = {
  type: "report_outline" | "governance" | "index" | "paper_meta";
  // ...其余不变
  progress?: number | { done: number; total: number };  // index 是 %，paper_meta 是 dict
};
```

- [ ] **Step 2: done toast 分派化**

`pending-center.tsx`:87-91 硬编码 index_done 改为分派表：

```ts
const DONE_MESSAGES: Record<string, (msg: any) => string> = {
  index_done: (m) => `${m.notebook_name || "该笔记本"}：索引构建完成 ✓`,
  paper_meta_done: (m) => `${m.notebook_name || "该笔记本"}：论文信息补全完成 ✓ · 已补全 ${m.stored} 篇`,
};

// 消息分派
if (msg.kind === "event" && msg.event && DONE_MESSAGES[msg.event]) {
  const d: DoneToast = {
    notebook_id: msg.notebook_id,
    notebook_name: msg.notebook_name || "",
    ts: Date.now(),
    // 扩 DoneToast 加 kind/text 字段供 UI 渲染
    kind: msg.event,
    text: DONE_MESSAGES[msg.event](msg),
  } as DoneToast;
  setDoneItems((xs) => [d, ...xs.filter((x) => x.notebook_id !== d.notebook_id || x.kind !== d.kind)]);
  setToast(d);
}
```

对应 `DoneToast` 类型加 `kind?: string; text?: string`。渲染处（找 PendingToast 组件）用 `d.text` 代替硬编码文案；如无 `text` 回落到既有 index 文案（向后兼容）。

- [ ] **Step 3: PendingItem 渲染**

pending-center 面板列表渲染（找 `snapshot.items.map`），加 `paper_meta` 分组标题「论文元数据」与 item 渲染：

```tsx
{item.type === "paper_meta" && (
  <button onClick={() => onOpenItem(item)}>
    <span>{item.notebook_name}</span>
    <span className="muted">
      论文信息补全中 · {(item.progress as any)?.done ?? 0}/{(item.progress as any)?.total ?? 0}
    </span>
  </button>
)}
```

`pending-actions.ts` 里如果有 `groupLabel` / `itemLabel` 字典，加 paper_meta 分支。

- [ ] **Step 4: 验证**

```bash
cd frontend && npx tsc --noEmit
```

- [ ] **Step 5: commit**

```bash
git add frontend/app/pending-center.tsx frontend/app/pending-actions.ts
git commit -m "feat(frontend): pending-center paper_meta items + generalized done toast

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: 前端 — 看板「论文元数据」区块

**Files:**
- Modify: `frontend/app/page.tsx`（analytics 弹窗新增区块）

**Interfaces consumed:** Task 4 的 `paper_meta_counts`。

- [ ] **Step 1: 加区块**

找「来源状态」区块（grep `source_status_counts`），在其后加：

```tsx
{analytics.paper_meta_counts && (
  <section>
    <h3>论文元数据</h3>
    <div className="tags">
      <span className="tag" style={{ color: "var(--color-ok,#1a7f5a)" }}>
        有元数据 {analytics.paper_meta_counts.has_meta}
      </span>
      {analytics.paper_meta_counts.missing > 0 && (
        <span className="tag" style={{ color: "var(--color-warn,#b97a00)" }}>
          缺失 {analytics.paper_meta_counts.missing}
        </span>
      )}
      {analytics.paper_meta_counts.marker > 0 && (
        <span className="tag" style={{ opacity: 0.6 }}>
          非论文 {analytics.paper_meta_counts.marker}
        </span>
      )}
    </div>
  </section>
)}
```

（class 名与结构 mirror 现有「来源状态」区块。）

- [ ] **Step 2: 验证**

```bash
cd frontend && npx tsc --noEmit
```

- [ ] **Step 3: 弯引号护栏**

```bash
git diff frontend/app/page.tsx | grep -c $'^-.*[“”]'
```

期望：`0`。

- [ ] **Step 4: commit**

```bash
git add frontend/app/page.tsx
git commit -m "feat(frontend): dashboard paper-meta three-state overview section

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 10: 全套回归 + 端到端冒烟 + PR

**Files:** 无代码变更（除非发现回归）；仅跑测试与冒烟。

- [ ] **Step 1: 后端全套**

```bash
cd backend && python -m pytest tests/ -q
```

期望：全绿（3199 baseline + 本 PR 新增测试）。

- [ ] **Step 2: 前端全套 + tsc**

```bash
cd frontend && npx tsc --noEmit && npm test
```

- [ ] **Step 3: 端到端冒烟脚本**

扩展 PR#271 的 `scratchpad/smoke_paper_meta.py`（若已删则新建，参考 PR#271 报告）为 `smoke_paper_meta_status.py`：

```python
# 关键断言（复用 PR#271 冒烟脚手架）：
# 1. 补抽 job 期间 repo.paper_meta_backfilling(nb) is True
#    repo.paper_meta_backfill_progress(nb) = {"total": N, "done": k}
# 2. 结束后 False / None
# 3. NotebookSummary.paper_meta_backfilling 反映相同
# 4. NotebookAnalytics.paper_meta_counts 三态精确
# 5. SourceSummary.paper_meta_status 四态精确（构造 has/marker/missing/memory 混合）
# 6. PendingActionsService.list_for_user 进行中含 paper_meta 项
```

```bash
PYTHONPATH=. python /path/to/scratchpad/smoke_paper_meta_status.py
```

期望：ALL PASS。

- [ ] **Step 4: 分支线性 + push + PR**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/paper-meta-status
git fetch origin
git rebase origin/master     # 若 master 有新提交则线性化
git push -u origin claude/paper-meta-status-dashboard
gh pr create --base master \
  --title "feat: 论文元数据补抽状态化 + 看板总览 + 每源追踪" \
  --body "$(cat <<'EOF'
## 摘要

PR#271 交付了抽取但真机暴露两个体验缺口：补抽按钮"点了没反应"、看板与列表看不到论文元数据整体/每源状态。本 PR 全部补齐，全部复用房内已有机制：

- **补抽状态化**（对齐 KG 构建）：进程内内存 dict 标注 `notebook_id → {total,done}`，`NotebookSummary.paper_meta_backfilling` O(1) 布尔字段；前端克隆 `buildingKg` 6s 轮询检测完成，`GET /notebooks/{nb}`，无新端点。
- **铃铛集成**：`PendingActionsService` 扩展补抽进行中项（type=paper_meta,state=building,progress={done,total}）；job finally 成功 emit `paper_meta_done`（stored=N）+ mark_dirty；前端 `pending-center` 泛化 `index_done` 分派为按事件类型取文案。
- **看板总览**：`NotebookAnalytics.paper_meta_counts`（has_meta/marker/missing 三态），两条覆盖索引 GROUP BY，不走 kg_mutation_seq 缓存（避免脏读）。
- **每源追踪**：`SourceSummary.paper_meta_status` 派生字段（has_meta/not_paper/missing/None 四态），零新查询——复用 PR#271 的 `paper_meta_for_sources` 水合。列表行 `待补全` warn 徽章、`非论文` muted 徽章；详情按四态渲染。

## 明确不做

- 过时检测（`sources.updated_at > meta.updated_at` 近似法在本项目"上传→必跑 KG 抽取"流程下几乎全误报；精确法需 +1 迁移，待后续需要再上）。
- 机构展示形态（继续 hover title，用户 2026-07-16 拍板）。

## 效率约束

零迁移、零新表、零新端点、零列表路径新增查询。补抽 progress dict 全内存，铃铛 items 零 DB。

## 验证

- 后端全套 X 绿；前端 tsc 0 错误 + 281/281；端到端冒烟 ALL PASS。
- 契约：`api_contract.json` 按测试自身计算重算（source_commit 不动）；`facade_surface.json` / `ownership_manifest.py` 按测试输出对齐新消费点。

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

- [ ] **Step 5: PR URL 给用户**

## Self-Review

**Spec coverage:** §3.1 → Task 1；§3.2 → Task 7；§3.3 铃铛 → Task 5/6/8；§4.1/4.2 看板 → Task 4/9；§4.3 每源追踪 → Task 3/7；§5 效率 → 贯穿；§6 触点清单 → Task 7/8/9；§7 测试 → 各 task 的 test step。§8 契约 → Task 2/3/4 的 api_contract regen + Task 5 的 manifest。§9 风险 → Task 1（进度锁）、Task 6（uid 归属）、Task 8（index 文案不变）测试覆盖。

**Placeholder scan:** 已过（每步都有具体代码块或具体命令）。若发现 catalog wiring 名字与真实不同（Task 2 Step 4），按 grep 结果原地修正——已明示搜索路径。

**Type consistency:** `paper_meta_backfilling` 后端 bool / 前端 `?: boolean` 一致；`paper_meta_status` 四态 union 后端/前端一致；`paper_meta_counts` 三键一致；PendingItem `progress` 前端接受 `number | {done,total}`——因为 index 用 %、paper_meta 用 dict。这是显式接口分歧，测试与渲染分支各自照顾。
