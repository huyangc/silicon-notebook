# chunk-native P1:chunking + chunk embedding 基础设施 实施计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把碎 element(47%<150字)合并成 ~600 字检索 chunk,并为每个 chunk 建向量,作为 P2 chunk-native 检索的基础设施(本 plan 不接检索,只产出 chunk + chunk_embeddings)。

**Architecture:** 新增纯函数 `build_chunks`(element 级贪心合并,借鉴 kg/windowing 思路);新增 `chunks`/`chunk_embeddings` 两表;摄取在"解析存 element"后插入"chunking + chunk embed"(复用 `_embed_objects_batch` 的并发+429退避);`build_chunks.py` 回填现有 notebook。

**Tech Stack:** Python/FastAPI、SQLite、pytest;复用 `_embed_objects_batch`、`vector_index`/`vector_cache`。

**对应 spec:** `docs/superpowers/specs/2026-06-15-chunk-native-retrieval-design.md`(P1 节)

---

## File Structure

- Create: `backend/app/services/chunking.py` — `build_chunks` 纯函数(element→chunk)
- Modify: `backend/app/services/sqlite_repository.py` — chunks/chunk_embeddings 表 DDL、`_chunk_and_embed_source`、`_vector_matrix` 支持 chunk 表、`process_source` 接线
- Create: `scripts/build_chunks.py` — 回填现有 notebook
- Test: `backend/tests/test_chunking.py`(新)、`backend/tests/test_chunk_embed.py`(新)

---

## Task 1: chunking 纯函数(element → ~600 字 chunk)

**Files:**
- Create: `backend/app/services/chunking.py`
- Test: `backend/tests/test_chunking.py`

- [ ] **Step 1: 写失败测试**

`backend/tests/test_chunking.py`:

```python
from app.services.chunking import build_chunks


def _el(eid, typ, text):
    return {"id": eid, "element_type": typ, "text": text}


def test_merges_small_elements_to_target():
    # 5 个 ~150 字段落, target 600 → 合并成 ~2 个 chunk
    els = [_el(f"e{i}", "paragraph", "x" * 150) for i in range(5)]
    chunks = build_chunks(els, target_chars=600, overlap_chars=0)
    assert 2 <= len(chunks) <= 3
    # 每个 chunk 记录其 element_ids
    assert all(c["element_ids"] for c in chunks)
    # 所有 element 都被覆盖
    covered = [eid for c in chunks for eid in c["element_ids"]]
    assert covered == [f"e{i}" for i in range(5)]


def test_heading_becomes_section_label_not_body():
    els = [_el("h1", "heading", "3 Architecture"),
           _el("p1", "paragraph", "y" * 300)]
    chunks = build_chunks(els, target_chars=600, overlap_chars=0)
    assert len(chunks) == 1
    assert chunks[0]["section_path"] == "3 Architecture"
    # heading 文本作为标签拼进 chunk 文本, heading 自身不在 element_ids
    assert "3 Architecture" in chunks[0]["text"]
    assert chunks[0]["element_ids"] == ["p1"]


def test_heading_cuts_chunk_boundary():
    # heading 切断: 前后 paragraph 属不同 section → 不同 chunk
    els = [_el("p1", "paragraph", "a" * 200), _el("h1", "heading", "Sec B"),
           _el("p2", "paragraph", "b" * 200)]
    chunks = build_chunks(els, target_chars=600, overlap_chars=0)
    assert len(chunks) == 2
    assert chunks[0]["element_ids"] == ["p1"]
    assert chunks[1]["element_ids"] == ["p2"] and chunks[1]["section_path"] == "Sec B"


def test_skips_image_and_empty():
    els = [_el("img", "image", "fig.png"), _el("e", "figure", ""),
           _el("p1", "paragraph", "real content here")]
    chunks = build_chunks(els, target_chars=600, overlap_chars=0)
    assert len(chunks) == 1
    assert chunks[0]["element_ids"] == ["p1"]


def test_oversize_element_becomes_own_chunk():
    els = [_el("big", "paragraph", "z" * 2000)]
    chunks = build_chunks(els, target_chars=600, overlap_chars=0)
    assert len(chunks) == 1 and chunks[0]["element_ids"] == ["big"]
```

- [ ] **Step 2: 跑测试确认失败**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/chunk-native
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_chunking.py -x -q
```
Expected: FAIL(ModuleNotFoundError: chunking)

- [ ] **Step 3: 实现**

`backend/app/services/chunking.py`:

```python
"""把存库的 source_elements 合并成检索用 chunk(~600字)。纯函数无 IO。
碎 element(47%<150字)直接做检索单元噪声大;此处贪心合并相邻 prose 到目标字数,
heading 作 section 标签(切 chunk 边界 + 拼进文本帮助语义),跳过 image/空。
借鉴 kg/windowing 的贪心打包,但在 element 粒度上做(检索 chunk 比 KG 抽取窗口小)。"""
from __future__ import annotations
from typing import Dict, List

_SKIP_TYPES = {"image", "figure"}


def build_chunks(elements: List[dict], target_chars: int = 600,
                 overlap_chars: int = 0) -> List[Dict]:
    """elements: 有序 [{"id","element_type","text"}]。
    返回 [{"text","section_path","element_ids"}]。overlap_chars 预留(P1 默认 0)。"""
    chunks: List[Dict] = []
    section = ""
    buf: List[tuple] = []   # [(id, text)]
    buf_len = 0

    def flush():
        nonlocal buf, buf_len
        if buf:
            body = "\n".join(t for _, t in buf)
            text = f"[{section}] {body}" if section else body
            chunks.append({"text": text, "section_path": section,
                           "element_ids": [i for i, _ in buf]})
        buf, buf_len = [], 0

    for e in elements:
        etype = (e.get("element_type") or e.get("type") or "").lower()
        text = (e.get("text") or "").strip()
        if etype == "heading":
            flush()                 # heading 切边界
            section = text          # 更新 section 标签
            continue
        if etype in _SKIP_TYPES or not text:
            continue                # 跳过 image/空
        buf.append((e["id"], text))
        buf_len += len(text)
        if buf_len >= target_chars:
            flush()
    flush()
    return chunks
```

- [ ] **Step 4: 跑测试确认通过**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_chunking.py -q
```
Expected: 5 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/chunking.py backend/tests/test_chunking.py
git commit -m "feat(chunk): build_chunks 把碎 element 合并成 ~600字检索chunk

element 47%<150字, 直接做检索单元噪声大。贪心合并相邻 prose 到目标
字数, heading 作 section 标签+切边界, 跳 image/空, 记 element_ids
(chunk↔KG 衔接用)。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 2: chunks + chunk_embeddings 表

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(表 DDL 区,`source_elements` 表附近 ~284行)
- Test: `backend/tests/test_chunk_embed.py`(新)

- [ ] **Step 1: 写失败测试**

`backend/tests/test_chunk_embed.py`:

```python
import json
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """Hermetic repo with FakeEmbedder but embedder_configured=True.
    Mirrors test_reasoning_retrieval.py::rrepo — EMBED_* MUST be set (else
    embedder_configured is False and every embed path early-returns, so chunk
    vectors never get written), and the network client is replaced by
    FakeEmbedder; LLM keys cleared so answer paths stay offline (the .env
    env_file would otherwise leak real keys)."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", "16")
    for _k in ("OPENAI_COMPAT_API_KEY", "OPENAI_COMPAT_BASE_URL",
               "REASONING_LLM_API_KEY", "REASONING_LLM_BASE_URL", "REASONING_LLM_MODEL"):
        monkeypatch.setenv(_k, "")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def test_chunks_tables_exist(repo):
    with repo._connect() as db:
        cols = {r["name"] for r in db.execute("PRAGMA table_info(chunks)").fetchall()}
        assert {"id","notebook_id","source_id","text","section_path","element_ids"} <= cols
        ecols = {r["name"] for r in db.execute("PRAGMA table_info(chunk_embeddings)").fetchall()}
        assert {"chunk_id","notebook_id","vector"} <= ecols
```

- [ ] **Step 2: 跑测试确认失败**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_chunk_embed.py::test_chunks_tables_exist -x -q
```
Expected: FAIL(no such table: chunks)

- [ ] **Step 3: 实现**

`backend/app/services/sqlite_repository.py`,在 `source_elements` 表 `CREATE TABLE` 之后(同一 schema 初始化块内,约 292 行后)追加:

```python
            db.execute("""
                CREATE TABLE IF NOT EXISTS chunks (
                  id TEXT PRIMARY KEY,
                  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                  source_id TEXT NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
                  text TEXT NOT NULL,
                  section_path TEXT NOT NULL DEFAULT '',
                  element_ids TEXT NOT NULL DEFAULT '[]',
                  created_at TEXT NOT NULL
                )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_chunks_source ON chunks(source_id)")
            db.execute("CREATE INDEX IF NOT EXISTS idx_chunks_nb ON chunks(notebook_id)")
            db.execute("""
                CREATE TABLE IF NOT EXISTS chunk_embeddings (
                  chunk_id TEXT PRIMARY KEY REFERENCES chunks(id) ON DELETE CASCADE,
                  notebook_id TEXT NOT NULL,
                  vector TEXT NOT NULL,
                  created_at TEXT NOT NULL
                )""")
            db.execute("CREATE INDEX IF NOT EXISTS idx_chunk_embeddings_nb ON chunk_embeddings(notebook_id)")
```

(注意:与既有 `db.execute(CREATE TABLE ...)` 同缩进、同 `with self._write() as db:` 或 schema 方法块内。实现者先读 280-300 行确认确切结构再插。)

- [ ] **Step 4: 跑测试确认通过**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_chunk_embed.py::test_chunks_tables_exist -q
```
Expected: 1 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_chunk_embed.py
git commit -m "feat(chunk): chunks + chunk_embeddings 表

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 3: _chunk_and_embed_source(写 chunks + 并发 embed)

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(新方法 + `_vector_matrix` 支持 chunk 表)
- Test: `backend/tests/test_chunk_embed.py`

- [ ] **Step 1: 写失败测试**

追加到 `backend/tests/test_chunk_embed.py`(imports/fixture 已在 Task 2 文件头):

```python
def _seed_source_with_elements(repo, texts):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    import uuid
    sid = f"src-{uuid.uuid4().hex[:8]}"
    now = "2026-01-01T00:00:00"
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,file_name,file_path,file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (sid, nb.id, "S", "s.md", "/tmp/s.md", 0, "h", "", "", "extracted", now, now))
        for i, t in enumerate(texts, 1):
            db.execute("INSERT INTO source_elements (id,source_id,element_type,location_label,text,metadata,created_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (f"el-{sid}-{i:04d}", sid, "paragraph", f"p{i}", t, "{}", now))
    return nb, sid


def test_chunk_and_embed_writes_chunks_and_vectors(repo):
    nb, sid = _seed_source_with_elements(repo, ["x"*300, "y"*300, "z"*300])
    repo._chunk_and_embed_source(sid)
    with repo._connect() as db:
        nchunks = db.execute("SELECT COUNT(*) c FROM chunks WHERE source_id=?", (sid,)).fetchone()["c"]
        nemb = db.execute("SELECT COUNT(*) c FROM chunk_embeddings WHERE notebook_id=?", (nb.id,)).fetchone()["c"]
        # element_ids 是合法 JSON 且非空
        row = db.execute("SELECT element_ids FROM chunks WHERE source_id=? LIMIT 1", (sid,)).fetchone()
    assert nchunks >= 1
    assert nemb == nchunks           # 每 chunk 一向量
    assert json.loads(row["element_ids"])


def test_chunk_matrix_loads(repo):
    nb, sid = _seed_source_with_elements(repo, ["alpha "*60, "beta "*60])
    repo._chunk_and_embed_source(sid)
    with repo._connect() as db:
        ids, mat = repo._vector_matrix(db, nb.id, "chunk_embeddings", "chunk_id")
    assert len(ids) >= 1 and mat.shape[0] == len(ids)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_chunk_embed.py -x -q -k "chunk_and_embed or chunk_matrix"
```
Expected: FAIL(no attribute _chunk_and_embed_source)

- [ ] **Step 3: 实现**

先确认 `_vector_matrix` 是否已是表名参数化(核查报告称是 `_vector_matrix(db, notebook_id, table, id_col)`)。若是,chunk 表自动支持,只需新增 `_chunk_and_embed_source`;若 `_vector_matrix` 硬编码了 element/knowledge 两表,在其表分派处加 `chunk_embeddings`/`chunk_id` 分支。

`backend/app/services/sqlite_repository.py` 新增方法(放在 `_embed_source` 附近):

三个方法分工:`_build_chunks_for_source`(合并 element→chunk,**纯写库无网络**,摄取时 INLINE 跑,query 立即可用)、`_embed_chunks_for_source`(给已写入的 chunk 补向量,**摄取时进后台线程**,与既有 element embed 同构不阻塞)、`_chunk_and_embed_source`(=build+embed,供回填脚本/测试同步调用)。

```python
    def _build_chunks_for_source(self, source_id: str) -> None:
        """合并一个 source 的 source_elements 成检索 chunk(纯写库, 无网络)。
        幂等:先删该 source 旧 chunk(级联删 chunk_embeddings)。元素 id 形如
        el-<sid>-0001 零补位, 故 ORDER BY id == 插入顺序。"""
        from app.services.chunking import build_chunks
        from uuid import uuid4
        src = self.get_source(source_id)
        notebook_id = src.notebook_id
        with self._connect() as db:
            erows = db.execute(
                "SELECT id, element_type, text FROM source_elements "
                "WHERE source_id=? ORDER BY id", (source_id,)).fetchall()
        elements = [{"id": r["id"], "element_type": r["element_type"], "text": r["text"]} for r in erows]
        chunks = build_chunks(elements,
                              target_chars=self.settings.chunk_target_chars,
                              overlap_chars=self.settings.chunk_overlap_chars)
        now = _now()
        rows = [(f"ck-{uuid4().hex[:12]}", notebook_id, source_id, c["text"],
                 c["section_path"], json.dumps(c["element_ids"]), now) for c in chunks]
        with self._write() as db:
            db.execute("DELETE FROM chunks WHERE source_id=?", (source_id,))  # 级联删 embeddings
            db.executemany(
                "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                "VALUES (?,?,?,?,?,?,?)", rows)

    def _embed_chunks_for_source(self, source_id: str) -> None:
        """给一个 source 已写入的 chunk 补向量(并发+429退避)。无网络则 no-op。"""
        if not self.settings.embedder_configured:
            return
        notebook_id = self.get_source(source_id).notebook_id
        with self._connect() as db:
            rows = db.execute(
                "SELECT id, text FROM chunks WHERE source_id=?", (source_id,)).fetchall()
        items = [{"_oid": r["id"], "payload": {"text": r["text"]}} for r in rows]
        self._embed_chunks_batch(notebook_id, items)

    def _chunk_and_embed_source(self, source_id: str) -> None:
        """build + embed(供回填脚本/测试同步调用)。"""
        self._build_chunks_for_source(source_id)
        self._embed_chunks_for_source(source_id)

    def _embed_chunks_batch(self, notebook_id: str, items: list) -> None:
        """与 _embed_objects_batch 同构(并发+429退避), 落 chunk_embeddings。"""
        if not self.settings.embedder_configured or not items:
            return
        import concurrent.futures as _cf
        size = max(1, self.settings.embed_batch_size)
        pending = [(it["_oid"], (it["payload"].get("text") or "")[:2000]) for it in items
                   if (it["payload"].get("text") or "").strip()]
        if not pending:
            return
        batches = [pending[i:i+size] for i in range(0, len(pending), size)]
        ensure = getattr(self.embedder, "_ensure", None)
        if callable(ensure):
            try: ensure()
            except Exception: pass

        def _emb(batch):
            try:
                vecs = self.embedder.embed_texts([t for _, t in batch])
            except Exception as exc:
                self.event_log.logger.warning("embed chunks batch failed (%d) for %s: %s",
                                              len(batch), notebook_id, exc)
                return []
            return [(cid, v) for (cid, _), v in zip(batch, vecs)]

        workers = max(1, min(self.settings.embed_concurrency, len(batches)))
        out = []
        with _cf.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="emb-ck") as pool:
            for part in pool.map(_emb, batches):
                out.extend(part)
        if not out:
            return
        now = _now()
        with self._write() as db:
            db.executemany(
                "INSERT OR REPLACE INTO chunk_embeddings (chunk_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                [(cid, notebook_id, json.dumps(v), now) for cid, v in out])
```

注:`_embed_chunks_batch` 与 `_embed_objects_batch` 几乎同构;若想 DRY 可把 embedder 调用抽公共 helper,但**本 plan 先各自独立**(避免改动既有 KG 路径,P4 再 DRY)。`DashscopeEmbedder.embed_texts` 已含 429 退避(PR #43)。

config 增(`backend/app/core/config.py`,reasoning 旋钮附近):

```python
    # chunk-native 检索分块: chunk 目标字数 / 相邻重叠(P1 overlap 默认 0)。
    chunk_target_chars: int = Field(600, env="CHUNK_TARGET_CHARS")
    chunk_overlap_chars: int = Field(0, env="CHUNK_OVERLAP_CHARS")
```

- [ ] **Step 4: 跑测试确认通过**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_chunk_embed.py -q
```
Expected: 全 passed

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/app/core/config.py backend/tests/test_chunk_embed.py
git commit -m "feat(chunk): build/embed chunk 方法 + chunk_embeddings 矩阵

_build_chunks_for_source(纯写库) / _embed_chunks_for_source(后台补
向量) / _chunk_and_embed_source(=两者, 供回填); _embed_chunks_batch
复用 429 退避并发模式落 chunk_embeddings; _vector_matrix 已表名参数
化故 chunk 表直接可用; config 加 chunk_target_chars/overlap。幂等。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
```

---

## Task 4: 摄取接线 + 回填脚本 + 验证

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(`process_source` 接线)
- Create: `scripts/build_chunks.py`
- Test: `backend/tests/test_chunk_embed.py`

- [ ] **Step 1: 写失败测试(接线)**

追加到 `backend/tests/test_chunk_embed.py`:

```python
def test_process_source_builds_chunks(repo, monkeypatch):
    """process_source 解析后应 INLINE 产出 chunks(轻摄取, query 立即可用)。
    chunk 构建是同步的(无网络), 故这里无需等后台 embed 线程即可断言行数。"""
    import app.services.sqlite_repository as mod
    # mock 解析: 返回固定 elements(不依赖真实文件/MinerU)
    monkeypatch.setattr(mod, "parse_source_file",
                        lambda *a, **k: [type("E", (), {"element_type": "paragraph",
                                         "location_label": "p1", "text": "chunk content " * 30,
                                         "metadata": {}})()])
    # 隔离重步骤: KG 抽取/摘要置 no-op, 聚焦验证 chunk 接线本身。
    monkeypatch.setattr(repo, "_run_extraction", lambda *a, **k: None)
    monkeypatch.setattr(repo, "_summarize_source", lambda *a, **k: "")
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    import uuid; sid = f"src-{uuid.uuid4().hex[:8]}"; now = "2026-01-01T00:00:00"
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,file_name,file_path,file_size,file_hash,summary,doc_type,parse_status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                   (sid, nb.id, "S", "s.md", "/tmp/s.md", 0, "h", "", "", "queued", now, now))
    repo.process_source(sid)
    with repo._connect() as db:
        n = db.execute("SELECT COUNT(*) c FROM chunks WHERE source_id=?", (sid,)).fetchone()["c"]
    assert n >= 1
```

(注:`_summarize_source` 真实实现可能调 LLM;hermetic 下虽会回退,但显式 no-op 让测试更确定。)

- [ ] **Step 2: 跑测试确认失败**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_chunk_embed.py::test_process_source_builds_chunks -x -q
```
Expected: FAIL(chunks 数 0,未接线)

- [ ] **Step 3: 实现接线**

`process_source`(~1063)里两处接线 —— **构建** inline、**embed** 进既有后台线程。

(a) 紧接 `self._set_source_status(source_id, "parsed", summary=summary)`(~1142),在 `import threading` / embed 线程块之前,插入 INLINE 构建:

```python
            # chunk-native 基础: 合并 element 成检索 chunk(纯写库无网络, query 立即可用)。
            # best-effort: 失败不阻塞既有 parse->extract 流水线。
            try:
                self._build_chunks_for_source(source_id)
            except Exception:
                self.event_log.logger.exception("chunk build failed for %s", source_id)
```

(b) 在既有后台线程 `_embed_bg` 内,`self._embed_source(source_id)` 之后追加一行,让 chunk 向量与 element 向量同样后台补、不阻塞:

```python
            def _embed_bg() -> None:
                try:
                    self._embed_source(source_id)
                    self._embed_chunks_for_source(source_id)   # chunk 向量后台补, 不阻塞流水线
                    stage("embed", "done", embed_started)
                except Exception as exc:  # noqa: BLE001 — best-effort; never fail the pipeline
                    stage("embed", "error", embed_started,
                          error=f"{type(exc).__name__}: {exc}")
                    self.event_log.logger.exception(
                        "background embed failed for %s", source_id
                    )
```

(测试里 `process_source` 同步返回后即可断言 chunk 行数——构建是 inline 的;chunk 向量由后台线程补,测试不对其断言,故无竞态。)

- [ ] **Step 4: 跑测试确认通过**

```bash
PYTHONPATH=backend /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest backend/tests/test_chunk_embed.py -q
```
Expected: 全 passed

- [ ] **Step 5: 回填脚本**

`scripts/build_chunks.py`:

```python
"""为现有 notebook 回填 chunk + chunk_embedding(不重抽 KG)。
用法: PYTHONPATH=backend python scripts/build_chunks.py <notebook_id>"""
import sys
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


def main():
    nb = sys.argv[1] if len(sys.argv) > 1 else None
    if not nb:
        print("usage: build_chunks.py <notebook_id>"); sys.exit(2)
    repo = SQLiteRepository(Settings())
    with repo._connect() as db:
        sids = [r["id"] for r in db.execute(
            "SELECT id FROM sources WHERE notebook_id=?", (nb,)).fetchall()]
    print(f"sources: {len(sids)}", flush=True)
    for i, sid in enumerate(sids, 1):
        try:
            repo._chunk_and_embed_source(sid)
            print(f"[{i}/{len(sids)}] {sid} ok", flush=True)
        except Exception as exc:
            print(f"[{i}/{len(sids)}] {sid} FAILED: {exc}", flush=True)
    with repo._connect() as db:
        n = db.execute("SELECT COUNT(*) c FROM chunks WHERE notebook_id=?", (nb,)).fetchone()["c"]
    print(f"total chunks: {n}", flush=True)


if __name__ == "__main__":
    main()
```

- [ ] **Step 6: 全量验证 + Commit + PR**

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/chunk-native
PYTHON_BIN=/opt/homebrew/Caskroom/miniconda/base/bin/python bash scripts/check.sh 2>&1 | tail -3
git add backend/app/services/sqlite_repository.py scripts/build_chunks.py backend/tests/test_chunk_embed.py
git commit -m "feat(chunk): 摄取接线 chunking + build_chunks 回填脚本

process_source 解析后产出 chunk(轻摄取, best-effort 不阻塞);
scripts/build_chunks.py 回填现有 notebook。

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>"
git push -u origin claude/chunk-native-retrieval 2>&1 | tail -2
```
PR 暂不开(P1 是基础设施,无用户效果);P2 完成后一并开 PR。或按需开 P1 PR。

> **基线已知失败(非本工作引入):** `test_prompts.py::test_extract_prompt_excludes_enumerated_values_and_meta_claims` 在 master/所有分支上都失败(抽取 prompt 在 775e30b 被重写,概念枚举约束改了措辞,但该测试断言仍是旧词;line 35)。验收标准是 **不新增失败**——`check.sh` 跑出的失败集合相对基线只应有这一条;P1 全部新增测试必须 PASS。该 stale 测试由独立任务修复(已 spawn)。

---

## 自检验证(每 task)

- 每 task 跑对应 `pytest`;Task 4 跑全量 `check.sh`(py_compile + hermetic smoke + tsc)。
- chunk 是 P2 检索的输入,P1 只验证"chunk 正确产出 + 向量落库",不验证检索效果(P2 做)。
- 注意:本 plan 在 worktree `.claude/worktrees/chunk-native`(分支 claude/chunk-native-retrieval)执行。

## P1 完成后

实施 **P2 plan**: [`2026-06-15-chunk-native-p2.md`](2026-06-15-chunk-native-p2.md)(chunk 检索 + MMR + 长上下文综合 + 引用绑 chunk + mode=chunk 默认路由)。P2 完成后用三基准问题(综述 / V3vsV2 差别 / 具体)对照 NotebookLM 验证。
