# Memory 确认后抽取进 notebook KG — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用户确认 Memory 后，若 notebook 满足 KG 抽取门（`should_extract_kg ∧ tier!='base'`）且未显式否决，则以合成 source（`source_type='memory'`）走真实抽取管线（对象+关系+证据+增量融合）进当前 notebook 的 KG。

**Architecture:** memory-as-source：确认时 upsert 一条 `sources.memory_id` 唯一关联的合成源，`content_md` 经 markdown 结构化解析成 source_elements，复用 `run_extraction`（分窗抽取→证据绑 element→relink→store_kg→incremental_fuse→auto_index）；编辑=指纹变化才重抽（reparse 语义），弃用=delete_source 级联。MemoryService 经运行时注入的桥接口触达 source_ingestion 域，不产生环依赖。

**Tech Stack:** Python 3.11+ / FastAPI / SQLite（标准库 sqlite3）/ pytest；前端 Next.js + TypeScript + node --test。

**Spec:** `docs/superpowers/specs/2026-07-14-memory-kg-extraction-design.md`（本计划的行为真源，冲突以 spec 为准）

## Global Constraints

- 工作目录：worktree `.claude/worktrees/memory-kg-extract`，分支 `claude/memory-kg-extract`；所有命令在 worktree 根执行。
- 后端测试解释器：`/opt/homebrew/Caskroom/miniconda/base/bin/python`（本机共享 conda，勿建 venv）。
- 迁移惯例：新表/列 = 新增 `_migration_14` + `SCHEMA_VERSION` 13→14（`backend/app/repositories/sqlite/migrations.py:14`），绝不改已封版迁移。
- 冻结契约：facade 新成员/签名变化会触发 `test_repository_api_contract.py` / `test_repository_facade_contract.py` / `test_repository_surface_manifest.py` / `test_startup_readiness*`（allowlist `STARTUP_READINESS_ALLOWED_NEW_MEMBERS`）失败——**按失败信息里的指引改 fixture/manifest/allowlist**，不得绕过或手工乱编 fixture；facade 只做一跳委托。
- 不新增环境变量；不改 memory 晋升/Track F；base 库（`tier='base'`）永不自动抽取。
- **不给合成源建 chunk**（memory 文本已由 MemoryRetriever 注入 prompt，建 chunk 会双份注入）；只建 elements + element embedding。
- 效率一等：抽取全部经 `kg_scheduler.submit_job` 异步；仅改 tags 的编辑靠 `file_hash=sha256(title+"\n"+content_md)` 指纹零代价跳过。
- 前后端同 PR；提交末尾 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`；分支保持线性（rebase 到 origin/master）。
- 完整门禁：`bash scripts/check.sh`（含全量 pytest + 前端测试 + tsc + production build）。

---

### Task 1: sources.memory_id 列 + 迁移 + schema golden

**Files:**
- Modify: `backend/app/repositories/sqlite/migrations.py`（`SCHEMA_VERSION` 与新 `_migration_14`）
- Modify: `backend/app/repositories/sqlite/source_store.py:243`（`insert_source` 增可选 `memory_id`）
- Test: `backend/tests/test_memory_kg_schema.py`（新建）

**Interfaces:**
- Produces: `sources.memory_id TEXT NULL` 列 + 部分唯一索引 `idx_sources_memory_id`；`SourceStore.insert_source(..., memory_id: str = "")`；`SourceStore.source_id_for_memory(memory_id: str) -> Optional[str]`。
- 后续任务依赖：Task 2 用 `insert_source(memory_id=...)` 与 `source_id_for_memory`。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_memory_kg_schema.py
import sqlite3
from pathlib import Path

from app.repositories.sqlite.database import SqliteDatabase  # 若路径不符，按 migrations.py 的既有测试(如 test_schema_*)的构造方式实例化
from tests.conftest import make_repository  # 用仓库既有的 repo fixture 工厂；没有则直接复用其他 schema 测试的建库方式


def test_fresh_db_has_sources_memory_id(tmp_path):
    repo = make_repository(tmp_path)  # 与 test_schema/migration 既有测试同款建库
    with repo._connect() as db:
        cols = {r["name"] for r in db.execute("PRAGMA table_info(sources)")}
        assert "memory_id" in cols
        idx = db.execute(
            "SELECT sql FROM sqlite_master WHERE name='idx_sources_memory_id'"
        ).fetchone()
        assert idx is not None and "WHERE memory_id" in idx["sql"]


def test_old_db_upgrades_with_memory_id(tmp_path):
    # 模拟已部署库：先建全新库，再人工降 user_version 与删列不可行(SQLite)，
    # 所以按仓库既有升级测试模式：直接断言 SCHEMA_VERSION 提升且 _migrate 幂等。
    from app.repositories.sqlite import migrations
    assert migrations.SCHEMA_VERSION == 14
```

注意：先 `grep -n "def test_.*schema\|make_repository\|SqliteDatabase(" backend/tests/test_*.py | head` 找既有 schema 测试的建库写法并照抄，不要发明新 fixture。仓库已有「旧库升级」测试模式（v9→v10 fixture，见 `test_repository_phase_contracts.py` 或 migrations 相关测试）——若存在升级 fixture 惯例，加一条「旧库经 `_migration_14` 后有该列」的用例替代上面第二个占位断言。

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_memory_kg_schema.py -q
```
预期：FAIL（无 memory_id 列 / SCHEMA_VERSION==13）。

- [ ] **Step 3: 实现迁移与 store 变更**

`migrations.py`：`SCHEMA_VERSION = 14`；仿照 `_migration_13`（`migrations.py:973`）的注册模式追加：

```python
def _migration_14(self) -> None:
    """Memory 派生源：sources.memory_id 关联列 + 每 Memory 至多一条派生源。"""
    self._add_column_if_missing("sources", "memory_id", "TEXT")
    self.connection.execute(
        "CREATE UNIQUE INDEX IF NOT EXISTS idx_sources_memory_id "
        "ON sources(memory_id) WHERE memory_id IS NOT NULL AND memory_id != ''"
    )
```

（`_add_column_if_missing` 若不存在，参照文件内既有 ALTER 助手命名——W1 止血时加过 idempotent ALTER 助手，先 grep `add_column\|ALTER TABLE` 找到它的真名并复用。）同时在**全新库 baseline** 的 `sources` CREATE TABLE（找 `CREATE TABLE IF NOT EXISTS sources`）加 `memory_id TEXT` 列与同名索引——保持「全新库=升级库同构」。

`source_store.py:243` `insert_source` 增 `memory_id: str = ""` 入参并写入列；同文件新增：

```python
def source_id_for_memory(self, memory_id: str) -> Optional[str]:
    with self.database.connect() as db:  # 按本文件其他方法的连接习惯写
        row = db.execute(
            "SELECT id FROM sources WHERE memory_id = ?", (memory_id,)
        ).fetchone()
    return str(row["id"]) if row else None
```

- [ ] **Step 4: schema golden regen + 全 schema 测试**

```bash
cd backend && UPDATE_SCHEMA_GOLDEN=1 /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests -k "schema" -q
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_memory_kg_schema.py -k "schema or migration or contract" -q
```
预期：golden 更新后相关测试 PASS。若 `test_repository_surface_manifest` / store contract 测试红，按其失败输出指引补 manifest 条目。

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(memory-kg): add sources.memory_id link column (migration 14)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: source_ingestion 域的 memory 派生源原语

**Files:**
- Modify: `backend/app/services/source_ingestion.py`
- Modify: `backend/app/services/parsers.py:81`（抽出可复用的文本解析入口）
- Test: `backend/tests/test_memory_source_ingestion.py`（新建）

**Interfaces:**
- Consumes（本服务已注入的依赖，见 `source_ingestion.py` `__init__`）：`self.sources.insert_source/get_source/set_status/replace_elements/source_id_for_memory`、`self.should_extract_kg(nb)`（:390）、`self.notebook_tier(nb)`、`self.run_extraction(source_id)`（抽取全链，含 store_kg+incremental_fuse+maybe_auto_index）、`self.clear_source_extraction_state(...)`、`self.embedding.embed_source(source_id)`、`self.kg_mutations.mark_unified_kg_dirty(nb)`、`self.delete_source(source_id, hooks)`(:697)、`self.pipeline_hooks()`、`self.new_id`、`self.event_log`。
- Produces（Task 3/4 依赖，签名逐字）：
  - `memory_kg_eligible(self, notebook_id: str) -> bool`
  - `memory_source_id(self, memory_id: str) -> Optional[str]`
  - `ingest_memory_source(self, notebook_id: str, memory_id: str, title: str, content_md: str) -> Optional[str]`（返回 source_id；指纹未变返回现有 id 且不重抽；失败置 `failed` 不抛）
  - `remove_memory_source(self, memory_id: str) -> None`（无派生源=幂等 no-op）

- [ ] **Step 1: 写失败测试**

先读 `backend/tests/` 里现有 source_ingestion/process_source 测试（`grep -rln "process_source\|source_ingestion" backend/tests | head`）照抄其 repo/service 构造方式。用例（离线，无 LLM）：

```python
# backend/tests/test_memory_source_ingestion.py 核心断言（构造方式照抄邻居测试）
def test_eligible_truth_table(repo_factory):
    # notebook 无 KG + KG_AUTO_EXTRACT 关 → False；显式写入一个 KG 对象后 → True；
    # mark_notebook_base 后 → False（base 永不自动抽）
    ...

def test_ingest_creates_hidden_source_and_extracts(repo, monkeypatch):
    called = {}
    monkeypatch.setattr(svc, "run_extraction", lambda sid: called.setdefault("sid", sid))
    sid = svc.ingest_memory_source(nb, "memory-1", "标题", "正文 **加粗**\n\n- 步骤一\n- 步骤二")
    src = svc.sources.get_source(sid)
    assert src.source_type == "memory" and src.parse_status == "extracted"
    assert called["sid"] == sid
    assert svc.memory_source_id("memory-1") == sid
    # elements 已落库
    assert any(e.text for e in src.elements)

def test_ingest_fingerprint_skip(repo, monkeypatch):
    # 同内容第二次调用：run_extraction 不再被调，source_id 不变
    ...

def test_ingest_content_change_reingests(repo, monkeypatch):
    # 内容变化：clear_source_extraction_state 被调、elements 被替换、再次抽取
    ...

def test_remove_memory_source(repo):
    # remove 后 source 行消失、无派生源时幂等
    ...

def test_ingest_no_llm_marks_failed_not_fabricate(repo):
    # 不打桩 run_extraction，离线跑真链路：parse_status 到达 extracted 或 failed，
    # 且 error_message 含 'no-llm'（对齐上传流程 no-llm 边界），绝无伪造对象
    ...
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_memory_source_ingestion.py -q
```
预期：FAIL（AttributeError: 无 ingest_memory_source）。

- [ ] **Step 3: 实现**

`parsers.py`：把 `parse_markdown`（:81，读文件后调 `structural_markdown` 解析）拆出文本入口，保持原函数行为不变：

```python
def parse_markdown_text(source_id: str, text: str) -> List[SourceElement]:
    """parse_markdown 的无文件版本：直接解析给定 markdown 文本。"""
    # 逐字复用 parse_markdown 内部对 parse_blocks/元素装配的调用，仅去掉读文件
    ...

def parse_markdown(source_id: str, path: Path) -> List[SourceElement]:
    return parse_markdown_text(source_id, path.read_text(encoding="utf-8"))
```

`source_ingestion.py` 新增（放在 `delete_source` 附近，同域聚合）：

```python
def memory_kg_eligible(self, notebook_id: str) -> bool:
    """Memory 确认后是否自动抽 KG：与上传同门 + base 库排除（进 base 走晋升人审）。"""
    return self.should_extract_kg(notebook_id) and self.notebook_tier(notebook_id) != "base"

def memory_source_id(self, memory_id: str) -> Optional[str]:
    return self.sources.source_id_for_memory(memory_id)

def ingest_memory_source(self, notebook_id: str, memory_id: str,
                         title: str, content_md: str) -> Optional[str]:
    """Memory→派生源→真实抽取管线。job 线程内运行；失败置 failed 不抛（Memory 本体不受影响）。"""
    import hashlib
    fingerprint = hashlib.sha256(f"{title}\n{content_md}".encode("utf-8")).hexdigest()
    source_id = self.sources.source_id_for_memory(memory_id)
    if source_id is not None:
        if self.sources.get_source(source_id).file_hash == fingerprint:
            return source_id  # 内容未变（如仅改 tags）：零代价跳过
        # reparse 语义：清旧抽取派生（extraction runs / source-derived knowledge / 旧向量）
        self.clear_source_extraction_state(...)  # 入参照 parse_source 里的既有调用逐字复制
    else:
        source_id = self.new_id("source")
        self.sources.insert_source(
            source_id=source_id, notebook_id=notebook_id, title=title,
            source_type="memory", status="active", parse_status="parsed",
            file_name="", file_path="", file_size=0, file_hash=fingerprint,
            summary="", doc_type="", memory_id=memory_id,
            # 其余必填参照 insert_source 签名(:243)与 import_sources 的既有调用取默认
        )
    from app.services.parsers import parse_markdown_text
    elements = parse_markdown_text(source_id, content_md)
    self.sources.replace_elements(source_id, elements)  # 入参照 :500 的既有调用
    # 指纹在重抽路径也要落库（insert 已带；update 路径补一条 UPDATE file_hash）
    try:
        self.embedding.embed_source(source_id)  # 刻意不建 chunk、不调 embed_chunks_for_source
    except Exception:
        self.event_log.logger.exception("memory source embed failed for %s", source_id)
    try:
        self.sources.set_status(source_id, "extracting")  # 入参照 :369 签名
        self.run_extraction(source_id)
        self.sources.set_status(source_id, "extracted")
        self.kg_mutations.mark_unified_kg_dirty(notebook_id)
    except Exception as exc:
        self.sources.set_status(source_id, "failed", error_message=f"{type(exc).__name__}: {exc}")
        self.event_log.logger.exception("memory KG extraction failed for %s", source_id)
    return source_id

def remove_memory_source(self, memory_id: str) -> None:
    source_id = self.sources.source_id_for_memory(memory_id)
    if source_id is not None:
        self.delete_source(source_id, self.pipeline_hooks())
```

实现时必须核对的三处既有调用（逐字对齐，不得凭记忆写参数）：`clear_source_extraction_state` 在 `parse_source`/`process_source` 里的实参；`replace_elements(:305)` 签名；`set_status(:286→:369 包装)` 签名（error_message 传法）。`delete_source` 对 `file_path=''` 的删文件调用需确认空路径安全（若 `_delete_file` 不守卫空串，加一行守卫）。事件打点：在 ingest 成功/失败处 `self.event_log.emit({"kind": "memory_kg", ...})` 与管线事件风格一致。

- [ ] **Step 4: 跑测试确认通过**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_memory_source_ingestion.py -q
```
预期：PASS。

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(memory-kg): memory-derived source ingestion primitives

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: MemoryService 生命周期挂钩 + facade 接线

**Files:**
- Modify: `backend/app/services/memory_service.py`
- Modify: `backend/app/services/repository_runtime.py:303-315`（wire_memory）与 `:833` 附近（接线段）
- Modify: `backend/app/services/sqlite_repository.py:1183-1207`（memory facade 委托）+ 新成员 `memory_kg_eligible`
- Modify: `backend/app/models/schemas.py:93/:139`（`MemoryCreateFromAnswer.extract_kg` / `MemoryReviewRequest.extract_kg`）
- Test: `backend/tests/test_memory_kg_lifecycle.py`（新建）

**Interfaces:**
- Consumes: Task 2 的四个原语（签名见 Task 2 Produces）。
- Produces:
  - `MemoryService.set_memory_kg_service(svc)`（svc=source_ingestion 服务实例，鸭子类型用其四个原语）与构造参数 `kg_ingest_scheduler=None`（None→同步执行，测试友好；运行时注入 `kg_scheduler.submit_job`）。
  - `MemoryService.create_from_answer(..., extract_kg: bool = True)`；`confirm(memory_id, user_id, patch)` 从 patch 读 `extract_kg`（None 视为 True）。
  - facade：`create_memory_from_answer(..., extract_kg: bool = True)`（签名演进）；**新成员** `memory_kg_eligible(notebook_id: str) -> bool`（一跳委托 `self._runtime.source_ingestion.memory_kg_eligible`）。
  - 行为：confirm/from-answer 门通过且 extract_kg→调度 `_kg_ingest_job(memory_id, user_id)`；update 后 status==confirmed 且已有派生源→调度同 job（指纹跳过在 Task 2 内）；deprecate→同步 `remove_memory_source`；job 开跑重读 memory，非 confirmed 即跳过。

- [ ] **Step 1: 写失败测试**

构造方式照抄 `backend/tests/test_memory_*.py` 既有 MemoryService 测试（`grep -rln "MemoryService(" backend/tests | head`）。用桩 kg service 记录调用：

```python
class _KgStub:
    def __init__(self, eligible=True):
        self.calls = []
        self._eligible = eligible
        self._sources = {}
    def memory_kg_eligible(self, nb): return self._eligible
    def memory_source_id(self, mid): return self._sources.get(mid)
    def ingest_memory_source(self, nb, mid, title, content):
        self.calls.append(("ingest", mid)); self._sources[mid] = f"src-{mid}"; return self._sources[mid]
    def remove_memory_source(self, mid):
        self.calls.append(("remove", mid)); self._sources.pop(mid, None)

# 断言（每条一个 test）：
# 1) confirm 默认触发 ingest；payload extract_kg=False 不触发；eligible=False 不触发
# 2) create_from_answer 默认触发；extract_kg=False 不触发
# 3) update 已确认且已有派生源 → 再触发；无派生源（确认时否决过）→ 不触发
# 4) deprecate → remove 被调
# 5) reject / candidate 编辑 → 零调用
# 6) job 竞态：调度后、执行前把 memory deprecate 掉 → job 跳过（用 kg_ingest_scheduler 收集 fn 手动延后执行）
# 7) 未注入 kg service（set_memory_kg_service 未调）→ 一切照旧零副作用
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_memory_kg_lifecycle.py -q
```

- [ ] **Step 3: 实现**

`memory_service.py`：

```python
# __init__ 增末位参数 kg_ingest_scheduler=None（同 embedding_scheduler 风格）
self.kg_ingest_scheduler = kg_ingest_scheduler or (lambda fn, item: fn(item))
self.memory_kg: Any | None = None

def set_memory_kg_service(self, service: Any) -> None:
    self.memory_kg = service

def _maybe_schedule_kg(self, item: MemoryRecord, extract_kg: bool) -> None:
    if (self.memory_kg is None or not extract_kg
            or not self.memory_kg.memory_kg_eligible(item.notebook_id)):
        return
    self.kg_ingest_scheduler(self._kg_ingest_job, (item.id, item.created_by))

def _kg_ingest_job(self, key: tuple[str, str]) -> None:
    memory_id, user_id = key
    try:
        item = self.store.memory_for_user(memory_id, user_id)
    except KeyError:
        return
    if item.status != "confirmed":
        return  # 弃用/拒绝竞态：自然收敛
    self.memory_kg.ingest_memory_source(
        item.notebook_id, item.id, item.title, item.content_md)
    self._event("memory_kg", item, action="ingested")
```

挂钩点：`create_from_answer(..., extract_kg: bool = True)` 末尾（`_schedule_embed` 旁）加 `self._maybe_schedule_kg(item, extract_kg)`；`confirm()` 里 `extract_kg = getattr(patch, "extract_kg", None)`（在 `_patch` 前取），`_patch()` 的 `values.pop` 白名单前加 `values.pop("extract_kg", None)`（与 `reason` 同款），确认成功后 `self._maybe_schedule_kg(item, extract_kg is not False)`；`update()` 在 `status=='confirmed'` 分支加：

```python
if item.status == "confirmed" and self.memory_kg is not None \
        and self.memory_kg.memory_source_id(item.id) is not None:
    self.kg_ingest_scheduler(self._kg_ingest_job, (item.id, item.created_by))
```

`deprecate()` 成功转移后：`if self.memory_kg is not None: self.memory_kg.remove_memory_source(item.id)`。

`schemas.py`：`MemoryCreateFromAnswer` 加 `extract_kg: bool = True`；`MemoryReviewRequest` 加 `extract_kg: Optional[bool] = None`。

`repository_runtime.py`：`wire_memory(...)` 的 `MemoryService(...)` 构造加 `kg_ingest_scheduler=lambda fn, item: kg_scheduler.submit_job(fn, item)`；在 `:833` `set_promotion_service` 旁加 `self.memory_service.set_memory_kg_service(self.source_ingestion)`（确认该属性名：`grep -n "source_ingestion" backend/app/services/repository_runtime.py | head`，若 wiring 顺序上 source_ingestion 晚于 memory 装配则把这行放到两者都就绪的接线段）。

`sqlite_repository.py`：`create_memory_from_answer`（:1183）签名加 `extract_kg: bool = True` 并透传；新增：

```python
def memory_kg_eligible(self, notebook_id: str) -> bool:
    return self._runtime.source_ingestion.memory_kg_eligible(notebook_id)
```

- [ ] **Step 4: 跑测试 + 修冻结契约**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_memory_kg_lifecycle.py -q
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_repository_api_contract.py tests/test_repository_facade_contract.py tests/test_repository_surface_manifest.py -q
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests -k "startup_readiness" -q
```
预期：契约测试对「create_memory_from_answer 签名变化」「新成员 memory_kg_eligible」报错并给出修复指引——按指引更新 `backend/tests/fixtures/repository_contract/api_contract.json`、surface manifest、`STARTUP_READINESS_ALLOWED_NEW_MEMBERS`。全部绿后进入下一步。

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(memory-kg): wire memory lifecycle to KG ingestion via runtime bridge

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: API 面 — extract_kg 入参与 kg_extract_eligible 出参

**Files:**
- Modify: `backend/app/api/memory_routes.py`（from-answer :362 透传；notebook 级列表 :208 与 memory-preview :309 补 eligibility）
- Modify: `backend/app/models/schemas.py`（`PaginatedMemories.kg_extract_eligible: Optional[bool] = None`、`MemoryPreview.kg_extract_eligible: bool = False`）
- Test: `backend/tests/test_memory_routes.py`（扩已有路由测试文件；若不存在则按 `grep -rln "memory-preview\|from-answer" backend/tests` 找到真实文件名）

**Interfaces:**
- Consumes: Task 3 的 facade `memory_kg_eligible(notebook_id)`、`create_memory_from_answer(..., extract_kg=...)`。
- Produces（前端 Task 7 依赖的线上契约）:
  - `POST /notebooks/{id}/memories/from-answer` body 接受 `extract_kg: bool = true`。
  - `POST /memories/{id}/confirm` body 接受 `extract_kg?: boolean`。
  - `GET /notebooks/{id}/memories` 响应含 `kg_extract_eligible: bool`（用户级 `GET /memories` 恒为 null/缺省）。
  - `POST /answers/{id}/memory-preview` 响应含 `kg_extract_eligible: bool`。

- [ ] **Step 1: 写失败测试**（TestClient 直打，照抄该文件既有用例的 app/auth 构造）

```python
# 1) from-answer 带 extract_kg=false → 派生源不创建（repo.memory_kg_eligible 为 True 的库）
# 2) notebook memories 列表响应 kg_extract_eligible 随门变化（无 KG 库 False；写入 KG 对象后 True；base 库 False）
# 3) memory-preview 响应含 kg_extract_eligible
# 4) confirm body {"extract_kg": false} 被接受（200）且不触发派生源
```

- [ ] **Step 2: 确认失败** `pytest <该文件> -q`

- [ ] **Step 3: 实现**

`from-answer` handler（:362）追加透传 `payload.extract_kg`；`list_notebook_memories`（:208）拿到 page 后：

```python
page = await _memory_call(service.list_memories, user.id, notebook_id, ...)
page.kg_extract_eligible = await run_in_threadpool(
    service.memory_kg_eligible, notebook_id)
return page
```

`preview_answer_memory`（:309）在拿到 `source["notebook_id"]` 后同样回填 `fallback.kg_extract_eligible`（LLM 分支返回前同置）。用户级 `GET /memories`（:185）不动。

- [ ] **Step 4: 确认通过 + openapi/文档契约检查**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests -k "memory" -q
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests -k "openapi or api_contract or architecture_documentation" -q
```
若存在 openapi golden（grep `openapi` in backend/tests 定位），按其 regen 惯例（`_write_json(sort_keys=True)` 风格的 UPDATE 环境变量）更新 golden，diff 应只有新增字段。

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(memory-kg): extract_kg opt-out and kg_extract_eligible on memory APIs

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: 用户可见面过滤合成源

**Files:**
- Modify: `backend/app/repositories/sqlite/source_store.py:52/:60`（`list_sources` / `list_sources_page` 加 `AND source_type != 'memory'`）
- Modify: NotebookSummary 来源计数与看板 parse_status 分布的两条 SQL（定位命令见 Step 3）
- Test: `backend/tests/test_memory_source_visibility.py`（新建）

**Interfaces:**
- Consumes: Task 1 的 memory_id 列（建带 `source_type='memory'` 的行即可测，无需 Task 2/3）。
- Produces: 左栏来源列表、来源计数、看板 parse_status 不含合成源；`get_source`、pending_kg、copy、scale-index 等内部路径**保持不过滤**。

- [ ] **Step 1: 写失败测试**

```python
# 建 notebook + 1 普通源 + 1 source_type='memory' 源：
# 1) list_sources / list_sources_page 只见普通源（total 也不含）
# 2) NotebookSummary 的来源计数 == 1
# 3) /analytics 的 parse_status 分布不含 memory 源
# 4) get_source(memory 源 id) 仍可读（证据回查不受影响）
```

- [ ] **Step 2: 确认失败。**

- [ ] **Step 3: 实现**

`source_store.py` 两个列表查询的 WHERE 追加 `AND source_type != 'memory'`（连 COUNT 总数一起）。来源计数与看板定位：

```bash
grep -rn "FROM sources" backend/app/repositories/sqlite/query_store.py backend/app/services/notebook_catalog.py | grep -i "count\|group"
grep -rn "parse_status" backend/app/api/routes.py backend/app/services/*.py | grep -i "analytic\|GROUP\|分布"
```
对 NotebookSummary 的 source 计数聚合与 analytics 的 `GROUP BY parse_status` 两条 SQL 加同款过滤。**不要**动 `pending_kg_source_count`、copy 物化、scale-index 的 sources 查询（内部真集）。若计数走了记忆化缓存（#249 的 memo），确认过滤进的是被缓存的底层 SQL 而非缓存外补丁。

- [ ] **Step 4: 确认通过 + 波及面**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_memory_source_visibility.py -q
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests -k "source or catalog or analytics" -q
```

- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(memory-kg): hide memory-derived sources from user-facing lists/counts

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: notebook 深拷贝置空 memory_id

**Files:**
- Modify: `backend/app/services/sqlite_notebook_sharing.py`（拷贝 sources 的写入/重映射处）
- Test: 扩 `backend/tests/` 中既有 copy_notebook 测试文件（`grep -rln "copy_notebook" backend/tests | head`）

**Interfaces:**
- Consumes: Task 1 的列。拷贝表清单在 `backend/app/repositories/sqlite/sharing_store.py:16`（`("sources", "SELECT * FROM sources WHERE notebook_id = ?")`）。
- Produces: 拷贝副本中 `sources.memory_id` 全部为 NULL/''（Memory 行本身 owner 私有不随拷贝，不留悬挂引用；副本源退化为普通内容源）；完整性自检不把 memory_id 当可重映射 id。

- [ ] **Step 1: 写失败测试**：源 notebook 建一条 `memory_id='mem-x'` 的源 → `copy_notebook` → 断言副本对应行 `memory_id` 为空、其余字段完整、既有完整性自检通过。
- [ ] **Step 2: 确认失败。**
- [ ] **Step 3: 实现**：在 sources 行写入副本处（跟随 `SELECT *` 行字典的插入逻辑，grep `_remap_json_ids\|_COPY_CHUNK` 定位）对 `memory_id` 键强制置空。若唯一索引 `idx_sources_memory_id` 会因两行同为 `''` 冲突——索引已用 `WHERE memory_id IS NOT NULL AND memory_id != ''` 排除空值，置 `''` 或 NULL 均安全。
- [ ] **Step 4: 确认通过**：`pytest tests -k "copy" -q`。
- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(memory-kg): null memory_id on notebook deep copy

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: 前端 — 确认/保存弹窗的「同时抽取到知识图谱」开关

**Files:**
- Modify: `frontend/app/memory-model.ts`（类型：`MemoryRecord`/`PaginatedMemories` 加 `kg_extract_eligible?: boolean`；from-answer/confirm 请求体加 `extract_kg?: boolean`）
- Modify: `frontend/app/memory-panel.tsx`（确认动作 `updateMemory(memory, "confirm")` :550 的 payload；from-answer 保存弹窗 :937 区域）
- Modify: `frontend/app/answer-panel.tsx`（若「保存到 Memory」弹窗状态在此，:394 按钮一带）
- Test: `frontend/app/memory-model.test.mjs` / `frontend/app/answer-memory.test.mjs`（扩既有）

**Interfaces:**
- Consumes: Task 4 的线上契约（`kg_extract_eligible` 出参、`extract_kg` 入参，字段名逐字）。
- Produces: 两个弹窗在 `kg_extract_eligible===true` 时显示复选框「同时抽取到知识图谱」默认勾选；不 eligible 时不渲染也不传字段；请求体带 `extract_kg`。

- [ ] **Step 1: 写失败测试**（node --test，照抄同文件既有用例风格）：模型层构造 confirm/from-answer 请求体的纯函数断言——eligible+勾选→`extract_kg:true`；取消勾选→`false`；不 eligible→字段缺省。若现状请求体在组件内联拼装，先抽成 memory-model.ts 纯函数再测（顶层 `.test.mjs`，嵌套目录不被 node --test 收集）。
- [ ] **Step 2: 确认失败**：`cd frontend && node --test app/*.test.mjs`。
- [ ] **Step 3: 实现**：memory-model.ts 类型与请求构造；memory-panel.tsx 确认按钮（:804 一带）与保存弹窗加受控 checkbox（复用面板既有 checkbox/开关样式，勿新造样式类）；`kg_extract_eligible` 从列表响应（notebook 级）与 preview 响应读取。文案固定「同时抽取到知识图谱」。
- [ ] **Step 4: 验证**：

```bash
cd frontend && node --test app/*.test.mjs && npx tsc --noEmit
```
预期全绿。UI 对齐要求（本项目 UI 精致约束）：checkbox 与弹窗既有控件同列对齐、间距一致。
- [ ] **Step 5: Commit**

```bash
git add -A && git commit -m "feat(memory-kg): extract-to-KG toggle in confirm/save dialogs

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: 文档 + 全量门禁 + PR

**Files:**
- Modify: `README.md` / `README_zh.md`（Memory 相关小节各加一句：确认时若 notebook 已有 KG 会默认把 Memory 抽取进该 notebook 的知识图谱，可在弹窗取消；base 库除外）
- Verify: `backend/tests/test_architecture_documentation.py`（文档契约不被新句破坏）

**Interfaces:** 无新接口；本任务是收尾门禁。

- [ ] **Step 1: 写文档**（两语言同改；保持通用口径，无机器路径）。
- [ ] **Step 2: 全量门禁**

```bash
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
```
预期 exit 0（后端全量 + 前端测试 + tsc + production build）。任何红条修到绿，不得跳过。
- [ ] **Step 3: rebase + push + PR**

```bash
git fetch origin master && git rebase origin/master
git push -u origin claude/memory-kg-extract
gh pr create --base master --title "feat(memory): extract confirmed Memory into notebook KG" --body "<按仓库 PR 体例：背景/改动/影响/验证，末尾 🤖 Generated with [Claude Code](https://claude.com/claude-code)>"
```
- [ ] **Step 4: Commit**（文档单独提交）

```bash
git add README.md README_zh.md && git commit -m "docs(memory-kg): document confirm-time KG extraction

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

## Self-Review 记录

- Spec 覆盖：§3 门/挂钩/异步=Task 2+3；§4 数据模型=Task 1；§5 数据流=Task 2；§6 生命周期=Task 3（指纹跳过在 Task 2）；§7 前端+过滤=Task 4/5/7；§8 拷贝=Task 6；§9 效率=约束贯穿（无新轮询/异步/指纹跳过）；§10 非目标=未建任务（正确）；§11 测试面逐条映射到各任务 Step 1；§12 惯例=Global Constraints。无缺口。
- 占位扫描：所有「照抄/核对既有调用」处均给出了定位命令与行号锚点，属于「在真实代码处取实参」的指令而非 TBD。
- 类型一致性：`memory_kg_eligible / memory_source_id / ingest_memory_source / remove_memory_source` 四签名在 Task 2 Produces 与 Task 3 桩/接线中逐字一致；`extract_kg` / `kg_extract_eligible` 字段名后端（Task 3/4）与前端（Task 7）逐字一致。
