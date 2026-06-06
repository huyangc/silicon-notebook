# KG 摄取、Ask 与概念合并性能修复实施计划

> **给 agentic 执行者：** 必须使用 `superpowers:subagent-driven-development`（推荐）或 `superpowers:executing-plans` 按任务逐项执行本计划。每个步骤使用 checkbox（`- [ ]`）跟踪状态。

**目标：** 修复大体量教材笔记本在 KG 抽取、Ask 问答、Knowledge Graph 打开和跨文档概念合并时的长等待问题，让 `Analog CMOS IC Design` 这类多教材笔记本能保持可交互、可观测、可治理。

**架构：** 将用户交互路径和重型维护任务拆开：Ask 不再同步做全库 embedding backfill / unified-KG rebuild / 全量 source element 扫描；Knowledge Graph 打开只读取现有图谱和状态，用户显式刷新时才 rebuild；概念合并采用“确定性归一化 + 有界向量候选 + 可选 LLM 小批量预审”的分层流程。

**技术栈：** FastAPI、SQLite WAL、Python 标准库 `sqlite3`、numpy float32 向量矩阵、现有 OpenAI-compatible LLM/embedding adapter、Next.js `frontend/app/page.tsx`、现有 `scripts/check.sh` 和 backend pytest。

---

## 诊断快照

目标笔记本 `nb-012fb94249` 当前实际规模：

- Parsed source elements：49,897。
- KG objects：29,782，其中 claim 11,088、formula 9,032、concept 7,955、procedure 1,707。
- KG relations：38,638。
- 当前 concept clusters：6,783。
- pending merge candidates：0。原因是当前 `kg_merge.py` 在 concept seed 超过 `_MAX_REPS=4000` 时跳过向量相似度层，只做名称归一化合并。

日志里的实际等待：

- Markdown parse 每本书都小于 1 秒，慢点主要是 KG extraction。
- 五本书 KG extraction 分别约为 2m56s、6m12s、7m54s、9m33s、5m55s。
- Ask API 请求耗时出现过 67.7s、10.3s、148.5s、22.9s；但对应 answer LLM 调用只有 8.1s、3.4s、4.3s、5.5s。
- 因此 Ask 慢主要发生在回答模型调用前：检索准备、embedding backfill、SQLite 扫描、relation 扫描、unified-KG rebuild 竞争、node context enrichment。
- KG 抽取包含 Problems / index-like 章节，产生大量低价值概念名。

## 文件职责

**后端请求路径和持久化**

- 修改 `backend/app/services/sqlite_repository.py`
  - 增加 notebook 级查询索引。
  - 从 `ask()` 请求路径移除同步 embedding backfill。
  - 避免 Ask 为 citation validation 扫描全量 source elements。
  - 优化 `node_context()`、`concept_detail()`，只查询相关对象、关系和 evidence。
  - 增加 Ask stage timing 日志。
  - parse 后持久化自动识别出的 `doc_type`。
  - 增加 unified-KG dirty/status 状态。

- 修改 `backend/app/api/routes.py`
  - 增加 unified-KG status endpoint。
  - 增加 LLM merge review endpoint。

- 修改 `backend/app/models/schemas.py`
  - 增加 unified-KG status 和 merge review 的请求/响应模型。

**KG 抽取与合并逻辑**

- 新建 `backend/app/services/kg/filters.py`
  - 识别是否跳过某个 KG extraction window。
  - 跳过 textbook Problems、index/backmatter 等低价值窗口。

- 修改 `backend/app/services/kg_ingest.py`
  - 在提交 LLM extraction 前应用 window filter。
  - 在 extraction run 里记录 skipped window 数量。

- 修改 `backend/app/services/kg_merge.py`
  - 把 `_MAX_REPS` 直接跳过向量层改为有界 top-k 候选生成。
  - 增加常见缩写/别名归一化。
  - 生成 capped、ranked pending merge candidates。

- 新建 `backend/app/services/concept_merge_review.py`
  - 对 pending merge candidates 做小批量 LLM 预审。
  - 输出 canonical name、decision、confidence、rationale。

**前端**

- 修改 `frontend/app/page.tsx`
  - 打开 KG overlay 时不再自动 `rebuildUnifiedKg()`。
  - 显示 unified-KG 状态和显式刷新按钮。
  - 增加 pending merges 的 `LLM 预审` 操作。

- 修改 `frontend/app/globals.css`
  - 如果现有按钮/状态样式不足，补充 KG status 和 merge review 控件样式。

**测试**

- 新建 `backend/tests/test_sqlite_indexes.py`。
- 修改 `backend/tests/test_ask_vector_matrix.py`。
- 修改 `backend/tests/test_node_context.py`。
- 修改 `backend/tests/test_unified_kg_repository.py`。
- 修改 `backend/tests/test_kg_merge.py`。
- 新建 `backend/tests/kg/test_filters.py`。
- 新建 `backend/tests/test_concept_merge_review.py`。

**文档**

- 修改 `README.md`。
- 修改 `README_zh.md`。
- 修改 `AGENTS.md`。
- 只有当实现完成且 `scripts/check.sh` 通过后，才按 AGENTS 规则更新 `fangan_done.md`。

---

## 任务 1：增加 SQLite 索引，降低 notebook 级扫描成本

**文件：**
- 修改：`backend/app/services/sqlite_repository.py`
- 新建：`backend/tests/test_sqlite_indexes.py`

- [ ] **步骤 1：写索引覆盖测试**

创建 `backend/tests/test_sqlite_indexes.py`：

```python
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


def _index_names(repo, table):
    with repo._connect() as db:
        return {row["name"] for row in db.execute(f"PRAGMA index_list({table})").fetchall()}


def test_notebook_scale_indexes_exist(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings())

    assert "idx_sources_notebook_status" in _index_names(repo, "sources")
    assert "idx_source_elements_source" in _index_names(repo, "source_elements")
    assert "idx_knowledge_objects_nb_type_status" in _index_names(repo, "knowledge_objects")
    assert "idx_knowledge_objects_nb_status" in _index_names(repo, "knowledge_objects")
    assert "idx_knowledge_objects_source" in _index_names(repo, "knowledge_objects")
    assert "idx_knowledge_relations_nb_source" in _index_names(repo, "knowledge_relations")
    assert "idx_knowledge_relations_nb_target" in _index_names(repo, "knowledge_relations")
    assert "idx_knowledge_embeddings_nb" in _index_names(repo, "knowledge_embeddings")
    assert "idx_element_embeddings_nb" in _index_names(repo, "element_embeddings")
```

- [ ] **步骤 2：运行测试确认失败**

运行：

```bash
cd backend
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_sqlite_indexes.py -q
```

预期：FAIL，因为这些索引还不存在。

- [ ] **步骤 3：在 schema initialization 里创建索引**

在 `backend/app/services/sqlite_repository.py` 的表创建之后加入：

```python
            db.execute("CREATE INDEX IF NOT EXISTS idx_sources_notebook_status ON sources(notebook_id, status)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_sources_notebook_created ON sources(notebook_id, created_at)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_source_elements_source ON source_elements(source_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_source_elements_source_created ON source_elements(source_id, created_at, id)")
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_knowledge_objects_nb_type_status "
                "ON knowledge_objects(notebook_id, object_type, status)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_knowledge_objects_nb_status "
                "ON knowledge_objects(notebook_id, status)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_objects_source ON knowledge_objects(source_id)")
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_knowledge_relations_nb_source "
                "ON knowledge_relations(notebook_id, source_object_id)"
            )
            db.execute(
                "CREATE INDEX IF NOT EXISTS idx_knowledge_relations_nb_target "
                "ON knowledge_relations(notebook_id, target_object_id)"
            )
            db.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_relations_source ON knowledge_relations(source_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_knowledge_embeddings_nb ON knowledge_embeddings(notebook_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_element_embeddings_nb ON element_embeddings(notebook_id)")
```

- [ ] **步骤 4：验证测试通过**

运行：

```bash
cd backend
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_sqlite_indexes.py -q
```

预期：PASS。

---

## 任务 2：让 Ask 请求路径避开重型维护任务

**文件：**
- 修改：`backend/app/services/sqlite_repository.py`
- 修改：`backend/tests/test_ask_vector_matrix.py`

- [ ] **步骤 1：写 Ask fast-path 边界测试**

追加到 `backend/tests/test_ask_vector_matrix.py`：

```python
def test_ask_does_not_backfill_missing_knowledge_embeddings(repo, monkeypatch):
    repo.llm_client = _FakeLLM()
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo._test_insert_object(nb.id, "claim", {"name": "Channel loss depends on equalization"})

    def fail_backfill(*args, **kwargs):
        raise AssertionError("ask() must not synchronously backfill knowledge embeddings")

    monkeypatch.setattr(repo, "_backfill_knowledge_embeddings", fail_backfill)
    resp = repo.ask(nb.id, AskRequest(question="channel loss equalization"))

    assert resp.conversation_id
    assert resp.answer_id


def test_ask_does_not_load_all_source_elements_for_citation_validation(repo, monkeypatch):
    repo.llm_client = _FakeLLM()
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    oid = repo._test_insert_object(nb.id, "claim", {"name": "Finite cable bandwidth attenuates high frequencies"})
    repo._embed_objects_batch(nb.id, [{"_oid": oid, "payload": {"name": "Finite cable bandwidth attenuates high frequencies"}}])

    original = repo._gather_elements

    def guard(db, notebook_id, with_vectors=True):
        if with_vectors is False:
            raise AssertionError("ask() must not gather every element only to build a valid id set")
        return original(db, notebook_id, with_vectors=with_vectors)

    monkeypatch.setattr(repo, "_gather_elements", guard)
    resp = repo.ask(nb.id, AskRequest(question="why does cable bandwidth matter"))

    assert any("bandwidth" in r.headline.lower() for r in resp.related_knowledge)
```

- [ ] **步骤 2：运行测试确认失败**

运行：

```bash
cd backend
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest \
  tests/test_ask_vector_matrix.py::test_ask_does_not_backfill_missing_knowledge_embeddings \
  tests/test_ask_vector_matrix.py::test_ask_does_not_load_all_source_elements_for_citation_validation -q
```

预期：FAIL，因为当前 `ask()` 会同步调用 `_backfill_knowledge_embeddings()`，并用 `_gather_elements(..., with_vectors=False)` 扫全量元素。

- [ ] **步骤 3：移除 Ask 中的同步 knowledge embedding backfill**

在 `SQLiteRepository.ask()` 中删除：

```python
            all_kg = [o for objs in kg_objs.values() for o in objs]
            self._backfill_knowledge_embeddings(db, notebook_id, all_kg)
```

保留 `_backfill_knowledge_embeddings()` 方法本身，后续维护任务仍可使用。

- [ ] **步骤 4：避免 Ask 为 citation validation 扫全量 source elements**

在 `SQLiteRepository.ask()` 中删除：

```python
            elements = self._gather_elements(db, notebook_id, with_vectors=False)
```

并把 citation 构建从：

```python
        valid_element_ids = {element["element_id"] for element in elements}
        citations: List[Citation] = []
        citations.extend(self._citations_from(top_hits, valid_element_ids, "KG evidence"))
```

改为：

```python
        cited_element_ids = {
            evidence.element_id
            for item in top_hits
            for evidence in item.evidence
            if evidence.element_id
        }
        citations: List[Citation] = []
        citations.extend(self._citations_from(top_hits, cited_element_ids, "KG evidence"))
```

- [ ] **步骤 5：增加 Ask stage timing 事件**

在 `ask()` 里加入局部计时工具：

```python
        ask_started = time.perf_counter()

        def ask_stage(name: str, started: float, **extra) -> None:
            self.event_log.emit(
                {
                    "kind": "ask_stage",
                    "notebook_id": notebook_id,
                    "stage": name,
                    "latency_ms": round((time.perf_counter() - started) * 1000),
                    **extra,
                }
            )
```

用 `t = time.perf_counter()` 包住这些阶段：

- `conversation`
- `rewrite`
- `load_indexes`
- `score`
- `expand`
- `answer_llm`
- `save`
- `total`

每个阶段结束时调用：

```python
ask_stage("load_indexes", t, objects=sum(len(v) for v in kg_objs.values()))
```

- [ ] **步骤 6：验证 Ask 相关测试**

运行：

```bash
cd backend
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_ask_vector_matrix.py -q
```

预期：PASS。

---

## 任务 3：优化 `node_context()` 和 `concept_detail()` 的查询范围

**文件：**
- 修改：`backend/app/services/sqlite_repository.py`
- 修改：`backend/tests/test_node_context.py`
- 修改：`backend/tests/test_unified_kg_repository.py`

- [ ] **步骤 1：写 `_element_texts()` 查询范围回归测试**

追加到 `backend/tests/test_node_context.py`：

```python
def test_element_texts_does_not_scan_entire_notebook(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    sid, eids = _src_with_elements(repo, nb.id, ["A target sentence.", "Another sentence."])

    executed = []
    original_connect = repo._connect

    class TrackingConnection:
        def __init__(self, inner):
            self.inner = inner
        def __enter__(self):
            self.conn = self.inner.__enter__()
            return self
        def __exit__(self, *args):
            return self.inner.__exit__(*args)
        def execute(self, sql, params=()):
            executed.append(" ".join(sql.split()))
            return self.conn.execute(sql, params)
        def __getattr__(self, name):
            return getattr(self.conn, name)

    monkeypatch.setattr(repo, "_connect", lambda: TrackingConnection(original_connect()))
    with repo._connect() as db:
        texts, ordinal = repo._element_texts(db, [eids[0]])

    assert texts[eids[0]] == "A target sentence."
    assert ordinal == {}
    assert not any("ORDER BY se.created_at ASC, se.id ASC" in sql for sql in executed)
```

- [ ] **步骤 2：运行测试确认失败**

运行：

```bash
cd backend
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest \
  tests/test_node_context.py::test_element_texts_does_not_scan_entire_notebook -q
```

预期：FAIL，因为当前 `_element_texts()` 会枚举当前 notebook 的全部 source elements。

- [ ] **步骤 3：让 `_element_texts()` 默认只取目标 element**

把 `SQLiteRepository._element_texts()` 改为：

```python
    def _element_texts(self, db, element_ids, *, with_ordinal: bool = False):
        ids = [e for e in element_ids if e]
        if not ids:
            return {}, {}
        ph = ",".join("?" for _ in ids)
        rows = db.execute(f"SELECT id, text FROM source_elements WHERE id IN ({ph})", ids).fetchall()
        texts = {r["id"]: r["text"] for r in rows}
        if not with_ordinal:
            return texts, {}
        order_rows = db.execute(
            "SELECT se.id FROM source_elements se JOIN sources s ON se.source_id=s.id "
            "WHERE s.notebook_id=(SELECT notebook_id FROM sources WHERE id=("
            "SELECT source_id FROM source_elements WHERE id=? LIMIT 1)) "
            "ORDER BY se.created_at ASC, se.id ASC",
            (ids[0],),
        ).fetchall()
        ordinal = {r["id"]: i for i, r in enumerate(order_rows)}
        return texts, ordinal
```

- [ ] **步骤 4：只在 legacy procedure 排序时请求 ordinal**

如果 procedure legacy fallback 需要跨元素排序，显式调用：

```python
texts, ordinal = self._element_texts(db, eids, with_ordinal=True) if eids else ({}, {})
```

普通 evidence enrichment 保持：

```python
texts, _ = self._element_texts(db, [e.get("element_id") for e in evidence])
```

- [ ] **步骤 5：优化 `concept_detail()`**

把 `concept_detail()` 中“读取 notebook 全部 objects + 全部 relations 再过滤”的逻辑，替换为：

```python
        with self._connect() as db:
            member_rows = db.execute(
                """
                SELECT ko.id, ko.object_type, ko.payload, ko.evidence
                FROM concept_clusters cc
                JOIN knowledge_objects ko ON ko.id = cc.member_object_id
                WHERE cc.notebook_id = ? AND cc.canonical_id = ? AND ko.status != 'deprecated'
                """,
                (notebook_id, canonical_id),
            ).fetchall()
            nrow = db.execute(
                "SELECT canonical_name FROM concept_clusters WHERE notebook_id=? AND canonical_id=? LIMIT 1",
                (notebook_id, canonical_id),
            ).fetchone()
```

随后只针对 `members` 生成 placeholders，并用两条索引友好的 relation 查询找 attached 节点：

```python
SELECT * FROM knowledge_relations
WHERE notebook_id=? AND source_object_id IN (...)

SELECT * FROM knowledge_relations
WHERE notebook_id=? AND target_object_id IN (...)
```

再批量读取 attached object ids 对应的 `knowledge_objects`。

- [ ] **步骤 6：验证 context 与 unified KG 测试**

运行：

```bash
cd backend
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest \
  tests/test_node_context.py \
  tests/test_unified_kg_repository.py -q
```

预期：PASS。

---

## 任务 4：让 unified-KG rebuild 显式化、可观测，不再阻塞交互

**文件：**
- 修改：`backend/app/services/sqlite_repository.py`
- 修改：`backend/app/api/routes.py`
- 修改：`backend/app/models/schemas.py`
- 修改：`backend/tests/test_unified_kg_repository.py`
- 修改：`frontend/app/page.tsx`

- [ ] **步骤 1：写 dirty/status 生命周期测试**

追加到 `backend/tests/test_unified_kg_repository.py`：

```python
def test_unified_kg_dirty_status_lifecycle(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    status = repo.unified_kg_status(nb.id)
    assert status["dirty"] is False
    assert status["clusters"] == 0

    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []}
    ], [])
    status = repo.unified_kg_status(nb.id)
    assert status["dirty"] is True

    repo.rebuild_unified_kg(nb.id)
    status = repo.unified_kg_status(nb.id)
    assert status["dirty"] is False
    assert status["clusters"] == 1
```

- [ ] **步骤 2：新增 unified-KG 状态表**

在 schema 初始化中增加：

```python
                CREATE TABLE IF NOT EXISTS unified_kg_state (
                  notebook_id TEXT PRIMARY KEY REFERENCES notebooks(id) ON DELETE CASCADE,
                  dirty INTEGER NOT NULL DEFAULT 0,
                  last_rebuild_at TEXT NOT NULL DEFAULT '',
                  object_count INTEGER NOT NULL DEFAULT 0,
                  relation_count INTEGER NOT NULL DEFAULT 0,
                  cluster_count INTEGER NOT NULL DEFAULT 0,
                  updated_at TEXT NOT NULL
                )
```

- [ ] **步骤 3：新增 repository 状态方法**

在 `SQLiteRepository` 增加：

```python
    def _mark_unified_kg_dirty(self, notebook_id: str) -> None:
        now = _now()
        with self._connect() as db:
            db.execute(
                """
                INSERT INTO unified_kg_state (notebook_id, dirty, updated_at)
                VALUES (?, 1, ?)
                ON CONFLICT(notebook_id) DO UPDATE SET dirty=1, updated_at=excluded.updated_at
                """,
                (notebook_id, now),
            )

    def unified_kg_status(self, notebook_id: str) -> dict:
        self.get_notebook(notebook_id)
        with self._connect() as db:
            row = db.execute("SELECT * FROM unified_kg_state WHERE notebook_id=?", (notebook_id,)).fetchone()
            clusters = db.execute(
                "SELECT COUNT(DISTINCT canonical_id) AS c FROM concept_clusters WHERE notebook_id=?",
                (notebook_id,),
            ).fetchone()["c"]
        if row is None:
            return {"dirty": False, "last_rebuild_at": "", "objects": 0, "relations": 0, "clusters": int(clusters)}
        return {
            "dirty": bool(row["dirty"]),
            "last_rebuild_at": row["last_rebuild_at"],
            "objects": int(row["object_count"]),
            "relations": int(row["relation_count"]),
            "clusters": int(row["cluster_count"] or clusters),
        }
```

- [ ] **步骤 4：KG 变更时标记 dirty**

在这些路径调用 `_mark_unified_kg_dirty(notebook_id)`：

- `store_kg()` 完成 `_invalidate_unified_cache(notebook_id)` 后。
- source 删除完成 `_invalidate_unified_cache(source.notebook_id)` 后。
- `confirm_merge()` 和 `reject_merge()` 完成 cache invalidation 后。

- [ ] **步骤 5：rebuild 完成后清 dirty 并记录计数**

在 `rebuild_unified_kg()` 末尾记录状态：

```python
        with self._connect() as db:
            object_count = db.execute(
                "SELECT COUNT(*) AS c FROM knowledge_objects WHERE notebook_id=? AND status!='deprecated'",
                (notebook_id,),
            ).fetchone()["c"]
            relation_count = db.execute(
                "SELECT COUNT(*) AS c FROM knowledge_relations WHERE notebook_id=?",
                (notebook_id,),
            ).fetchone()["c"]
            db.execute(
                """
                INSERT INTO unified_kg_state
                (notebook_id, dirty, last_rebuild_at, object_count, relation_count, cluster_count, updated_at)
                VALUES (?, 0, ?, ?, ?, ?, ?)
                ON CONFLICT(notebook_id) DO UPDATE SET
                  dirty=0,
                  last_rebuild_at=excluded.last_rebuild_at,
                  object_count=excluded.object_count,
                  relation_count=excluded.relation_count,
                  cluster_count=excluded.cluster_count,
                  updated_at=excluded.updated_at
                """,
                (notebook_id, now, object_count, relation_count, len(set(res["cluster_map"].values())), now),
            )
```

- [ ] **步骤 6：source 变绿不再等待 rebuild**

在 `process_source()` 中删除同步调用：

```python
self.rebuild_unified_kg(self.get_source(source_id).notebook_id)
```

替换为：

```python
self._mark_unified_kg_dirty(source.notebook_id)
```

这样 source `extracted` 继续代表 KG extraction 完成，而不是 cluster rebuild 完成。

- [ ] **步骤 7：增加 status API**

在 `backend/app/models/schemas.py` 增加：

```python
class UnifiedKgStatus(BaseModel):
    dirty: bool
    last_rebuild_at: str = ""
    objects: int = 0
    relations: int = 0
    clusters: int = 0
```

在 `backend/app/api/routes.py` 增加：

```python
@router.get("/notebooks/{notebook_id}/unified-kg/status")
def unified_kg_status(notebook_id: str) -> UnifiedKgStatus:
    try:
        return UnifiedKgStatus(**repository().unified_kg_status(notebook_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
```

- [ ] **步骤 8：前端打开 KG overlay 时不再自动 rebuild**

在 `frontend/app/page.tsx` 增加 API helper：

```tsx
const fetchUnifiedKgStatus = (nb: string) => api<UnifiedKgStatus>(`/notebooks/${nb}/unified-kg/status`);
```

把 `openKgView()` 中的：

```tsx
await rebuildUnifiedKg(currentNotebookId);
const [g, pend] = await Promise.all([fetchUnifiedGraph(currentNotebookId), fetchPendingMerges(currentNotebookId)]);
```

改为：

```tsx
const [g, pend, status] = await Promise.all([
  fetchUnifiedGraph(currentNotebookId),
  fetchPendingMerges(currentNotebookId),
  fetchUnifiedKgStatus(currentNotebookId)
]);
setUnifiedKgStatus(status);
```

增加显式刷新函数：

```tsx
async function refreshUnifiedKg() {
  if (!currentNotebookId) return;
  setKgRefreshBusy(true);
  try {
    await rebuildUnifiedKg(currentNotebookId);
    const [g, pend, status] = await Promise.all([
      fetchUnifiedGraph(currentNotebookId),
      fetchPendingMerges(currentNotebookId),
      fetchUnifiedKgStatus(currentNotebookId)
    ]);
    setUGraph(g);
    setPendingMerges(pend);
    setUnifiedKgStatus(status);
  } catch (err) {
    reportError(err);
  } finally {
    setKgRefreshBusy(false);
  }
}
```

- [ ] **步骤 9：验证后端和前端**

运行：

```bash
cd backend
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_unified_kg_repository.py -q
cd ../frontend
npm run build
```

预期：pytest PASS，Next.js build PASS。

---

## 任务 5：过滤低价值教材窗口，并持久化 textbook `doc_type`

**文件：**
- 新建：`backend/app/services/kg/filters.py`
- 修改：`backend/app/services/kg_ingest.py`
- 修改：`backend/app/services/sqlite_repository.py`
- 新建：`backend/tests/kg/test_filters.py`
- 修改：`backend/tests/test_kg_ingest.py`
- 修改：`backend/tests/test_notebook_meta.py`

- [ ] **步骤 1：写 window filter 测试**

创建 `backend/tests/kg/test_filters.py`：

```python
from app.services.kg.filters import should_extract_window
from app.services.kg.parsing import SourceElementQ


def _el(text, typ="paragraph"):
    return SourceElementQ(
        file="book.md",
        type=typ,
        text=text,
        line_start=1,
        line_end=1,
        char_start=0,
        char_end=len(text),
    )


def test_skips_textbook_problem_sections():
    keep, reason = should_extract_window("7 > 7.5 > Problems", [_el("7.1 Calculate the gain.")], "textbook")
    assert keep is False
    assert reason == "textbook_problem_section"


def test_skips_index_like_windows():
    keep, reason = should_extract_window("Index", [_el("frequency response, 495"), _el("input offset voltage, 230")], "textbook")
    assert keep is False
    assert reason == "index_like_window"


def test_keeps_formula_rich_body_section():
    keep, reason = should_extract_window(
        "9 > 9.6 > Slew Rate",
        [_el("The slew rate is determined by the compensation capacitor."), _el("Slew rate = I/C", "formula")],
        "textbook",
    )
    assert keep is True
    assert reason == ""
```

- [ ] **步骤 2：实现 window filter**

创建 `backend/app/services/kg/filters.py`：

```python
from __future__ import annotations

import re
from typing import Sequence

from app.services.kg.parsing import SourceElementQ

_PROBLEM_RE = re.compile(r"(^|[>\s])problems?$|(^|[>\s])exercises?$|习题|练习", re.IGNORECASE)
_BACKMATTER_RE = re.compile(r"index|glossary|references|bibliography|索引|参考文献|术语表", re.IGNORECASE)
_INDEX_LINE_RE = re.compile(r"^[A-Za-z][A-Za-z0-9 /\-(),]+,\s*\d+([,–\-\s\d]+)?$")


def _index_like_ratio(elements: Sequence[SourceElementQ]) -> float:
    texts = [(element.text or "").strip() for element in elements if (element.text or "").strip()]
    if not texts:
        return 0.0
    hits = sum(1 for text in texts if _INDEX_LINE_RE.match(text))
    return hits / len(texts)


def should_extract_window(section_path: str, elements: Sequence[SourceElementQ], doc_type: str) -> tuple[bool, str]:
    path = section_path or ""
    lowered_doc_type = (doc_type or "").lower()
    if lowered_doc_type == "textbook" and _PROBLEM_RE.search(path):
        return False, "textbook_problem_section"
    if _BACKMATTER_RE.search(path):
        return False, "backmatter_section"
    if _index_like_ratio(elements) >= 0.6:
        return False, "index_like_window"
    return True, ""
```

- [ ] **步骤 3：在 `extract_graph()` 中应用过滤**

在 `backend/app/services/kg_ingest.py` 中，把：

```python
pairs = [(w, els) for w, els in windows_with_elements(raw_text, source_file, None, n, m) if els]
```

替换为：

```python
from app.services.kg.filters import should_extract_window

all_pairs = [(w, els) for w, els in windows_with_elements(raw_text, source_file, None, n, m) if els]
skipped = 0
pairs = []
for w, els in all_pairs:
    keep, _reason = should_extract_window(w.section_path, els, doc_type)
    if keep:
        pairs.append((w, els))
    else:
        skipped += 1
```

扩展返回对象或 run summary，使 extraction run message 变为：

```text
kg objects=<n> relations=<n> doc_type=<type> windows_failed=<failed>/<total> windows_skipped=<skipped>
```

- [ ] **步骤 4：parse 后持久化自动识别的 `doc_type`**

在 `process_source()` 的 parse 完成后加入：

```python
profile = resolve_profile("", source.title, elements)
resolved_doc_type = _normalize_doc_type(source.doc_type) or profile.id
```

写入 parsed elements 的同一个 DB block 里增加：

```python
db.execute("UPDATE sources SET doc_type = ? WHERE id = ?", (resolved_doc_type, source_id))
```

`_run_extraction()` 继续读取 source 上持久化的 `doc_type`。对教材文件，`kg_doc_type` 应从默认 `academic` 变为 `textbook`。

- [ ] **步骤 5：验证过滤和 ingest 测试**

运行：

```bash
cd backend
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest \
  tests/kg/test_filters.py \
  tests/test_kg_ingest.py \
  tests/test_notebook_meta.py -q
```

预期：PASS。

---

## 任务 6：在超过 4,000 个概念时仍生成可控合并候选

**文件：**
- 修改：`backend/app/services/kg_merge.py`
- 修改：`backend/tests/test_kg_merge.py`
- 修改：`backend/tests/test_unified_kg_repository.py`

- [ ] **步骤 1：写大规模概念合并测试**

追加到 `backend/tests/test_kg_merge.py`：

```python
def test_large_seed_set_still_uses_vector_candidates():
    concepts = [_concept(f"o{i}", f"concept {i}") for i in range(4500)]
    concepts.extend([
        _concept("mos_a", "voltage-controlled oscillator"),
        _concept("mos_b", "VCO"),
    ])
    vecs = {f"o{i}": [1.0 if (i % 16) == k else 0.0 for k in range(16)] for i in range(4500)}
    vecs["mos_a"] = [1.0] + [0.0] * 15
    vecs["mos_b"] = [0.99, 0.01] + [0.0] * 14

    res = cluster_concepts(concepts, vecs, confirmed=set(), rejected=set(), hi=0.94, lo=0.86)

    assert res["capped"] is False
    assert res["cluster_map"]["mos_a"] == res["cluster_map"]["mos_b"]


def test_pending_candidates_are_bounded_and_ranked():
    concepts = [_concept(f"o{i}", f"concept {i}") for i in range(200)]
    vecs = {f"o{i}": [1.0, i / 1000.0] for i in range(200)}

    res = cluster_concepts(
        concepts,
        vecs,
        confirmed=set(),
        rejected=set(),
        hi=0.9999,
        lo=0.90,
        top_k=3,
        max_pending=50,
    )

    assert len(res["pending"]) <= 50
    scores = [score for _a, _b, score in res["pending"]]
    assert scores == sorted(scores, reverse=True)
```

- [ ] **步骤 2：增强名称归一化和 alias 归一化**

在 `backend/app/services/kg_merge.py` 中加入：

```python
_ALIASES = {
    "vco": "voltage controlled oscillator",
    "pll": "phase locked loop",
    "lna": "low noise amplifier",
    "mos": "mos transistor",
    "mosfet": "mos transistor",
    "bjt": "bipolar junction transistor",
    "opamp": "op amp",
    "op amp": "op amp",
}


def _norm(name: str) -> str:
    cleaned = re.sub(r"[^a-z0-9+/ ]+", " ", (name or "").strip().lower())
    cleaned = re.sub(r"[\s\-_]+", " ", cleaned).strip()
    return _ALIASES.get(cleaned, cleaned)
```

保持 `_norm` 可 import，因为现有测试会导入它。

- [ ] **步骤 3：把 `_MAX_REPS` skip 改成有界 top-k 候选生成**

修改 `cluster_concepts()` 签名：

```python
def cluster_concepts(
    concepts: List[dict],
    vectors: Dict[str, List[float]],
    confirmed: Set[FrozenSet[str]],
    rejected: Set[FrozenSet[str]],
    hi: float = 0.94,
    lo: float = 0.86,
    top_k: int = 5,
    max_pending: int = 1000,
) -> dict:
```

用 chunked top-k 替代全量 pair 双循环：

```python
M = np.asarray([reps[i] for i in idx], dtype=np.float32)
M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-8)
candidates: List[tuple[str, str, float]] = []
block = 512
for start in range(0, len(idx), block):
    end = min(start + block, len(idx))
    sims = M[start:end] @ M.T
    for local_i, row in enumerate(sims):
        global_i = start + local_i
        row[global_i] = -1.0
        k = min(top_k, len(row) - 1)
        if k <= 0:
            continue
        top = np.argpartition(row, -k)[-k:]
        for global_j in top:
            if global_j <= global_i:
                continue
            sa, sb = seeds[idx[global_i]], seeds[idx[global_j]]
            if rej and frozenset((sa, sb)) in rej:
                continue
            sim = float(row[global_j])
            if sim >= lo:
                candidates.append((sa, sb, sim))
```

对 candidates 按 score 降序排序：

- `sim >= hi` 自动 union。
- `lo <= sim < hi` 进入 pending。
- pending pair 去重。
- pending 最多保留 `max_pending` 个。

- [ ] **步骤 4：保留 confirmed/rejected 语义**

保持现有行为：

- confirmed pairs 在向量候选前强制 union。
- rejected pairs 不自动合并，也不输出 pending。

- [ ] **步骤 5：验证 merge tests**

运行：

```bash
cd backend
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_kg_merge.py tests/test_unified_kg_repository.py -q
```

预期：PASS，包括已有 2000 reps 性能测试。

---

## 任务 7：增加可选 LLM 概念合并预审

**文件：**
- 新建：`backend/app/services/concept_merge_review.py`
- 修改：`backend/app/services/sqlite_repository.py`
- 修改：`backend/app/api/routes.py`
- 修改：`backend/app/models/schemas.py`
- 新建：`backend/tests/test_concept_merge_review.py`
- 修改：`frontend/app/page.tsx`

- [ ] **步骤 1：为 merge candidate 增加 review metadata 字段**

在 schema migration 中，`concept_merge_candidates` 表创建后加入：

```python
cm_cols = {r["name"] for r in db.execute("PRAGMA table_info(concept_merge_candidates)").fetchall()}
if "confidence" not in cm_cols:
    db.execute("ALTER TABLE concept_merge_candidates ADD COLUMN confidence REAL NOT NULL DEFAULT 0")
if "rationale" not in cm_cols:
    db.execute("ALTER TABLE concept_merge_candidates ADD COLUMN rationale TEXT NOT NULL DEFAULT ''")
if "reviewed_by" not in cm_cols:
    db.execute("ALTER TABLE concept_merge_candidates ADD COLUMN reviewed_by TEXT NOT NULL DEFAULT ''")
```

- [ ] **步骤 2：写 LLM review service 测试**

创建 `backend/tests/test_concept_merge_review.py`：

```python
from app.services.concept_merge_review import review_merge_candidates


class _ReviewLLM:
    configured = True
    def chat_json(self, messages, response_schema_hint):
        return """
        {
          "decisions": [
            {
              "candidate_id": "mc-1",
              "decision": "merge",
              "canonical_name": "voltage-controlled oscillator",
              "confidence": 0.96,
              "rationale": "VCO is the common acronym for voltage-controlled oscillator."
            },
            {
              "candidate_id": "mc-2",
              "decision": "keep_separate",
              "canonical_name": "",
              "confidence": 0.91,
              "rationale": "current mirror and current source are related but not identical."
            }
          ]
        }
        """


def test_review_merge_candidates_parses_decisions():
    candidates = [
        {"id": "mc-1", "canonical_a": "K-vco", "canonical_b": "K-voltage controlled oscillator", "score": 0.93},
        {"id": "mc-2", "canonical_a": "K-current mirror", "canonical_b": "K-current source", "score": 0.88},
    ]

    decisions = review_merge_candidates(_ReviewLLM(), candidates)

    assert decisions[0]["candidate_id"] == "mc-1"
    assert decisions[0]["decision"] == "merge"
    assert decisions[0]["confidence"] == 0.96
    assert decisions[1]["decision"] == "keep_separate"
```

- [ ] **步骤 3：实现 LLM review service**

创建 `backend/app/services/concept_merge_review.py`：

```python
from __future__ import annotations

import json
from typing import Any, List

_SCHEMA = (
    '{"decisions":[{"candidate_id":"","decision":"merge|keep_separate|unsure",'
    '"canonical_name":"","confidence":0.0,"rationale":""}]}'
)


def _prompt(candidates: List[dict]) -> str:
    lines = []
    for item in candidates:
        lines.append(
            f"- id={item['id']} score={item.get('score', 0):.3f}\n"
            f"  A: {item['canonical_a']}\n"
            f"  B: {item['canonical_b']}"
        )
    return (
        "Review candidate concept merges for an analog/RF/CMOS IC design knowledge graph.\n"
        "Merge only when the two names denote the same technical concept, including acronym/full-name pairs.\n"
        "Keep separate when one is a subtype, related circuit, parameter, cause/effect, or broader/narrower term.\n"
        "Return JSON only.\n\n"
        "Candidates:\n" + "\n".join(lines)
    )


def review_merge_candidates(llm_client: Any, candidates: List[dict]) -> List[dict]:
    if not getattr(llm_client, "configured", False) or not candidates:
        return []
    raw = llm_client.chat_json([{"role": "user", "content": _prompt(candidates)}], _SCHEMA)
    data = json.loads(raw)
    decisions = data.get("decisions") if isinstance(data, dict) else []
    out = []
    for item in decisions or []:
        if not isinstance(item, dict):
            continue
        decision = str(item.get("decision", "")).strip()
        if decision not in {"merge", "keep_separate", "unsure"}:
            continue
        out.append({
            "candidate_id": str(item.get("candidate_id", "")).strip(),
            "decision": decision,
            "canonical_name": str(item.get("canonical_name", "")).strip(),
            "confidence": float(item.get("confidence", 0) or 0),
            "rationale": str(item.get("rationale", "")).strip()[:500],
        })
    return [item for item in out if item["candidate_id"]]
```

- [ ] **步骤 4：增加 repository 方法**

在 `SQLiteRepository` 增加：

```python
def review_pending_merges(self, notebook_id: str, limit: int = 50, auto_confirm_threshold: float = 0.95) -> dict:
    self.get_notebook(notebook_id)
    pending = self.pending_merges(notebook_id)[: max(1, min(limit, 200))]
    from app.services.concept_merge_review import review_merge_candidates
    decisions = review_merge_candidates(self.llm_client, pending)
    confirmed = rejected = unsure = 0
    now = _now()
    with self._connect() as db:
        for decision in decisions:
            candidate_id = decision["candidate_id"]
            confidence = decision["confidence"]
            status = "pending"
            if decision["decision"] == "merge" and confidence >= auto_confirm_threshold:
                status = "confirmed"
                confirmed += 1
            elif decision["decision"] == "keep_separate" and confidence >= auto_confirm_threshold:
                status = "rejected"
                rejected += 1
            else:
                unsure += 1
            db.execute(
                """
                UPDATE concept_merge_candidates
                SET status=?, confidence=?, rationale=?, reviewed_by='llm', updated_at=?
                WHERE id=? AND notebook_id=?
                """,
                (status, confidence, decision["rationale"], now, candidate_id, notebook_id),
            )
    if confirmed or rejected:
        self._mark_unified_kg_dirty(notebook_id)
        self._invalidate_unified_cache(notebook_id)
    return {"reviewed": len(decisions), "confirmed": confirmed, "rejected": rejected, "unsure": unsure}
```

- [ ] **步骤 5：增加 API models 和 endpoint**

在 `backend/app/models/schemas.py` 增加：

```python
class MergeReviewRequest(BaseModel):
    limit: int = 50
    auto_confirm_threshold: float = 0.95


class MergeReviewSummary(BaseModel):
    reviewed: int = 0
    confirmed: int = 0
    rejected: int = 0
    unsure: int = 0
```

在 `backend/app/api/routes.py` 增加：

```python
@router.post("/notebooks/{notebook_id}/unified-kg/merges/review")
def review_unified_kg_merges(notebook_id: str, payload: MergeReviewRequest) -> MergeReviewSummary:
    try:
        return MergeReviewSummary(**repository().review_pending_merges(
            notebook_id,
            limit=payload.limit,
            auto_confirm_threshold=payload.auto_confirm_threshold,
        ))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
```

- [ ] **步骤 6：前端增加 `LLM 预审` 操作**

在 `frontend/app/page.tsx` 增加：

```tsx
const reviewPendingMergesApi = (nb: string) =>
  api<MergeReviewSummary>(`/notebooks/${nb}/unified-kg/merges/review`, {
    method: "POST",
    body: JSON.stringify({ limit: 50, auto_confirm_threshold: 0.95 })
  });
```

实现：

```tsx
async function reviewPendingMerges() {
  if (!currentNotebookId) return;
  setKgReviewBusy(true);
  try {
    const summary = await reviewPendingMergesApi(currentNotebookId);
    setToast(`已预审 ${summary.reviewed} 项：合并 ${summary.confirmed}，分开 ${summary.rejected}，保留 ${summary.unsure}`);
    const [pend, status] = await Promise.all([
      fetchPendingMerges(currentNotebookId),
      fetchUnifiedKgStatus(currentNotebookId)
    ]);
    setPendingMerges(pend);
    setUnifiedKgStatus(status);
  } catch (err) {
    reportError(err);
  } finally {
    setKgReviewBusy(false);
  }
}
```

在 pending merges 区域加按钮：

```tsx
<button className="ghost-button" onClick={reviewPendingMerges} disabled={!pendingMerges.length || kgReviewBusy}>
  LLM 预审
</button>
```

- [ ] **步骤 7：验证 LLM review 和前端 build**

运行：

```bash
cd backend
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_concept_merge_review.py tests/test_unified_kg_repository.py -q
cd ../frontend
npm run build
```

预期：PASS，build 成功。

---

## 任务 8：同步 README / README_zh / AGENTS / fangan_done

**文件：**
- 修改：`README.md`
- 修改：`README_zh.md`
- 修改：`AGENTS.md`
- 条件修改：`fangan_done.md`

- [ ] **步骤 1：更新英文 README**

在 `README.md` 的 large-document ingestion、Ask、Unified KG 相关段落加入：

```markdown
- Ask no longer performs synchronous embedding backfill; it uses available keyword/vector indexes and remains responsive while maintenance jobs finish.
- Unified KG rebuild is explicit/observable through `/unified-kg/status` and no longer runs automatically every time the graph overlay opens.
- Textbook KG extraction skips problem/index/backmatter windows to reduce noisy low-value concepts.
- Concept merge governance uses deterministic normalization plus bounded vector candidates; optional LLM pre-review can confirm or reject high-confidence pending merges.
```

- [ ] **步骤 2：更新中文 README**

在 `README_zh.md` 对应段落加入：

```markdown
- Ask 不再在请求路径里同步补齐 embedding；会使用当前已有的关键词/向量索引，因此在维护任务未完成时仍能响应。
- 统一 KG rebuild 改为显式且可观测，通过 `/unified-kg/status` 暴露状态；打开图谱浮层不再自动触发重建。
- 教材 KG 抽取会跳过习题、索引、参考文献等低价值窗口，减少噪声概念。
- 概念合并治理使用确定性归一化 + 有界向量候选；可选 LLM 预审用于高置信确认/拒绝候选合并。
```

- [ ] **步骤 3：更新 AGENTS 工作约束**

在 `AGENTS.md` 的 Architecture Baseline / Product Flow 中加入：

```markdown
- Ask must stay off heavy maintenance work: no synchronous whole-notebook embedding backfill, no synchronous unified-KG rebuild, and no full source-element scan for citation validation.
- Unified KG rebuild is explicit/observable. Opening the Knowledge Graph overlay should fetch the current graph/status and offer refresh when dirty; it should not automatically block on rebuild.
- Textbook extraction should avoid low-value problem/index/backmatter windows unless a future user-facing control explicitly opts into exercise extraction.
- Cross-document concept merge candidates must be bounded and reviewable. LLM review operates on small pending candidate batches, not on the entire concept universe at once.
```

- [ ] **步骤 4：满足完成条件后再更新 `fangan_done.md`**

只有当代码实现完成、`scripts/check.sh` 通过、前端 build 通过后，才在 `fangan_done.md` 追加事实记录：

```markdown
- 已完成：大笔记本 KG 治理改为有界概念合并候选 + 可选 LLM 预审；Ask 和 KG overlay 不再阻塞在维护 rebuild/backfill 上。已通过 `scripts/check.sh` 和前端 build 验证。
```

如果 `scripts/check.sh` 未通过，不更新 `fangan_done.md`。

---

## 任务 9：完整验证与 Analog 笔记本 spot check

**文件：**
- 不新增代码文件。
- 读取本地日志和数据库。

- [ ] **步骤 1：运行目标 backend 测试**

运行：

```bash
cd backend
/opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest \
  tests/test_sqlite_indexes.py \
  tests/test_ask_vector_matrix.py \
  tests/test_node_context.py \
  tests/test_unified_kg_repository.py \
  tests/test_kg_merge.py \
  tests/kg/test_filters.py \
  tests/test_concept_merge_review.py -q
```

预期：PASS。

- [ ] **步骤 2：运行全仓库检查**

运行：

```bash
cd /Users/hzf/workspace/silicon_notebook
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh
```

预期：PASS。

- [ ] **步骤 3：运行前端 production build**

运行：

```bash
cd /Users/hzf/workspace/silicon_notebook/frontend
npm run build
```

预期：PASS。

- [ ] **步骤 4：测量 Ask API**

启动后端后调用：

```bash
curl -s -X POST http://localhost:8000/api/notebooks/nb-012fb94249/ask \
  -H 'Content-Type: application/json' \
  -d '{"question":"基于7nm工作，112G PAM4 serdes可以支持多大的差损"}' \
  -o /tmp/ask_resp.json
```

检查 `.local/logs/requests.jsonl` 和 `.local/logs/events.jsonl`。

预期：

- Ask 请求不触发同步 `_backfill_knowledge_embeddings()`。
- `ask_stage` 中没有全库 source element 扫描。
- 如果 LLM 配置可用，answer LLM 调用仍是唯一必要的回答模型调用。

- [ ] **步骤 5：验证打开 KG overlay 不再自动 rebuild**

打开 `Analog CMOS IC Design` 的 KG overlay 后检查 `.local/logs/requests.jsonl`。

预期看到：

- `GET /api/notebooks/nb-012fb94249/unified-kg/status`
- `GET /api/notebooks/nb-012fb94249/unified-kg`
- `GET /api/notebooks/nb-012fb94249/unified-kg/pending-merges`

预期不出现：

- 自动 `POST /api/notebooks/nb-012fb94249/unified-kg/rebuild`

只有点击显式刷新按钮后，才出现 rebuild 请求。

- [ ] **步骤 6：验证 merge candidates 可 review**

显式 rebuild：

```bash
curl -s -X POST http://localhost:8000/api/notebooks/nb-012fb94249/unified-kg/rebuild
curl -s http://localhost:8000/api/notebooks/nb-012fb94249/unified-kg/pending-merges | head
```

预期：

- 大概念集合不再因为超过 4,000 seeds 而完全跳过向量候选层。
- pending candidates 是 capped、ranked、可人工 review 的集合。
- 如果存在 VCO / voltage-controlled oscillator 等别名对，应自动归并或进入高分候选。

---

## 推荐执行顺序

1. 先做任务 1-3：这是最低风险的性能修复，会直接改善 Ask、node detail、concept detail 的等待。
2. 再做任务 4：这会改变 KG overlay 的交互，从自动 rebuild 改为显式刷新，需要 UI review。
3. 再做任务 5：这影响未来 re-extract 的 KG 质量，适合在重新处理教材前合入。
4. 再做任务 6：让当前 7,955 concepts 规模下也能产生可控候选。
5. 最后做任务 7：在候选已经有界之后再接 LLM 预审。
6. 任务 8-9 作为收尾：文档同步和完整验证。

## 风险控制

- 本计划不删除已有 KG objects。教材窗口过滤只影响未来 extraction / re-extraction。
- merge review 先写入 `concept_merge_candidates` 的决策状态；原始 `knowledge_objects` 保留。
- Ask 在没有 embedding 或 LLM 的情况下仍保持 deterministic fallback。
- LLM merge reviewer 只看小批量 candidate 名称和 score，不把整本教材内容一次性送入模型。
- unified-KG dirty status 把“图谱可能未重建”显式展示出来，而不是藏在长时间 UI 等待里。

## 自检

- **覆盖性：** 覆盖了这次三个问题：KG 实际耗时、KG 过程中 Ask 长等待、跨文档相近概念合并不可 review。
- **占位检查：** 每个任务都有明确文件、代码片段、运行命令和预期结果。
- **接口一致性：** 新增接口名统一为 `unified_kg_status()`、`review_pending_merges()`、`UnifiedKgStatus`、`MergeReviewRequest`、`MergeReviewSummary`。
- **范围控制：** 任务 1-3 可单独作为 P0 latency patch 合入；任务 4-7 完整解决 KG 治理和合并体验。
