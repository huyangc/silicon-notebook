# chunk×graph mix + qwen3-rerank 实现计划(v1 完整照 LightRAG)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.
>
> **提交纪律:** commit 末尾追加 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。**叠在当前分支 `claude/wonderful-bell-3b27db`(PR #59),只 commit、不 push、不另开 PR。**

**Goal:** 把 `ask_chunk` 升级成**完整三路 LightRAG mix**:naive(向量 chunk)+ local(节点+1hop)+ global(关系),**含实体/关系的源 chunk 并入候选池**;chunk 选择用 qwen3-rerank + token 预算;删旧 LLM 打分 rerank。`CHUNK_KG_OVERLAY_ENABLED` 默认 True。

**Architecture:** 关键顺序——**KG 检索提前到 chunk 选择之前**:`_mix_retrieve` 先做 KG(种子→1-hop 子图 + 源 chunk),再把 KG 源 chunk 与向量 chunk **round-robin 合并**成候选池,统一交 rerank→token 预算选出 chunk;KG 结构子图另渲染进统一 `[k]` 上下文。复用 `_retrieve_chunks` / `federated_retrieve(_relations)` / `_federated_rx_graph` / `multihop_subgraph` / `render_subgraph_context`。**rerank 分只排序,`.relevance`(融合分)管 grounding/tau。** flag 关 + 无 rerank → 与现状字节等价。

**Tech Stack:** Python3 / SQLite / DashScope qwen3-rerank(HTTP)/ pytest / FakeEmbedder。

**Spec:** `docs/superpowers/specs/2026-06-18-chunk-graph-mix-design.md`

**已知形状(实读)**:`RetrievedChunk`(retrieval.py:588)有 `.object_id`(=chunk_id)/`.relevance`/`.element_ids`/`.text`/`.section_path`/`.source_title`/`.source_id`。`_chunk_answer_context`(4420)与 `render_subgraph_context`(graph_reason.py:165)产**同形 id_map**;`_parse_answer_anchors`(5144)统一出 `AnswerAnchor`。`classify_evidence` 读 `.object_id`/`.relevance`。chunks 表有 `element_ids`(JSON list)、`source_elements` 行有 element_id。`DashscopeEmbedder` = OpenAI client + 429 backoff。

**建议执行顺序:** 1→2→3→4→5→6→7→8。

---

## Phase A — RerankClient

### Task 1: RerankClient + 配置

**Files:** Create `backend/app/services/rerank_client.py`;Modify `backend/app/core/config.py`;Test `backend/tests/test_rerank_client.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_rerank_client.py
from app.services.rerank_client import RerankClient


class _S:
    rerank_model = "qwen3-rerank"; rerank_base_url = "http://fake/v1"
    rerank_api_key = "k"; rerank_max_docs = 500; embed_concurrency = 8
    openai_compat_timeout_seconds = 30


def test_unconfigured_identity():
    s = _S(); s.rerank_model = ""
    rc = RerankClient(s)
    assert not rc.configured and rc.rerank("q", ["a", "b", "c"]) == [0, 1, 2]


def test_orders_by_score(monkeypatch):
    rc = RerankClient(_S())
    monkeypatch.setattr(rc, "_rerank_batch", lambda q, d: [
        {"index": 2, "relevance_score": 0.9}, {"index": 0, "relevance_score": 0.5},
        {"index": 1, "relevance_score": 0.1}])
    assert rc.rerank("q", ["a", "b", "c"]) == [2, 0, 1]


def test_failure_identity(monkeypatch):
    rc = RerankClient(_S())
    monkeypatch.setattr(rc, "_rerank_batch", lambda q, d: (_ for _ in ()).throw(RuntimeError()))
    assert rc.rerank("q", ["a", "b"]) == [0, 1]
```

- [ ] **Step 2: 跑确认失败** — `cd backend && python -m pytest tests/test_rerank_client.py -q` → FAIL(ImportError)。

- [ ] **Step 3: 实现 `rerank_client.py`**

```python
"""qwen3-rerank(DashScope text-rerank)。单次批量调用;候选超 max_docs 自动切 batch
线程池并发 + 按 relevance_score 合并。失败/未配置 → 原序下标(降级)。"""
from __future__ import annotations
import concurrent.futures as _cf
from typing import List
import requests


class RerankClient:
    def __init__(self, settings):
        self.settings = settings
        self.model = (getattr(settings, "rerank_model", "") or "").strip()
        self.base_url = (getattr(settings, "rerank_base_url", "") or "").rstrip("/")
        self.api_key = getattr(settings, "rerank_api_key", "") or ""
        self.max_docs = max(1, getattr(settings, "rerank_max_docs", 500))

    @property
    def configured(self) -> bool:
        return bool(self.model and self.base_url and self.api_key)

    def rerank(self, query: str, documents: List[str]) -> List[int]:
        if not self.configured or not documents:
            return list(range(len(documents)))
        try:
            scored = (self._rerank_batch(query, documents) if len(documents) <= self.max_docs
                      else self._rerank_split(query, documents))
            order, seen = [], set()
            for r in sorted(scored, key=lambda r: r["relevance_score"], reverse=True):
                i = r["index"]
                if 0 <= i < len(documents) and i not in seen:
                    seen.add(i); order.append(i)
            order += [i for i in range(len(documents)) if i not in seen]   # 补漏
            return order
        except Exception:
            return list(range(len(documents)))

    def _rerank_batch(self, query: str, documents: List[str]) -> List[dict]:
        resp = requests.post(
            f"{self.base_url}/reranks",
            headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
            json={"model": self.model, "query": query, "documents": documents},
            timeout=getattr(self.settings, "openai_compat_timeout_seconds", 30))
        resp.raise_for_status()
        return resp.json()["results"]

    def _rerank_split(self, query: str, documents: List[str]) -> List[dict]:
        batches = [(i, documents[i:i + self.max_docs]) for i in range(0, len(documents), self.max_docs)]
        workers = max(1, min(getattr(self.settings, "embed_concurrency", 8), len(batches)))
        out: List[dict] = []
        def one(item):
            base, docs = item
            return [{"index": base + r["index"], "relevance_score": r["relevance_score"]}
                    for r in self._rerank_batch(query, docs)]
        with _cf.ThreadPoolExecutor(max_workers=workers) as ex:
            for part in ex.map(one, batches):
                out.extend(part)
        return out
```

- [ ] **Step 4: 配置(config.py)**

```python
    rerank_model: str = Field("", env="RERANK_MODEL")
    rerank_base_url: str = Field("https://dashscope.aliyuncs.com/compatible-api/v1", env="RERANK_BASE_URL")
    rerank_api_key: str = Field("", env="RERANK_API_KEY")
    rerank_max_docs: int = Field(500, env="RERANK_MAX_DOCS")
```

- [ ] **Step 5: 跑 + 提交** — `pytest tests/test_rerank_client.py -q` PASS(3)。
```bash
git add backend/app/services/rerank_client.py backend/app/core/config.py backend/tests/test_rerank_client.py
git commit -m "feat(mix): RerankClient(qwen3-rerank,单批+超限并发切批,失败降级)"
```

---

## Phase B — token 预算助手

### Task 2: est_tokens + truncate_by_tokens + 预算配置

**Files:** Modify `backend/app/services/retrieval.py`、`config.py`;Test `backend/tests/test_mix_budget.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_mix_budget.py
from app.services.retrieval import est_tokens, truncate_by_tokens


def test_est_tokens():
    assert est_tokens("") == 0 and est_tokens("abcd") >= 1


def test_truncate_keeps_prefix():
    items = ["x" * 40, "y" * 40, "z" * 40]
    kept = truncate_by_tokens(items, key=lambda s: s, max_tokens=20)
    assert 0 < len(kept) < 3
```

- [ ] **Step 2: 跑确认失败** — FAIL(ImportError)。

- [ ] **Step 3: 实现(retrieval.py)**

```python
def est_tokens(text: str) -> int:
    """粗估 token(无 tiktoken):中英混排约 3.5 字符/token,向上取整。仅用于预算截断。"""
    import math
    return math.ceil(len(text or "") / 3.5)


def truncate_by_tokens(items, key, max_tokens):
    """按序累加 est_tokens(key(item)),首次超 max_tokens 即停(保留之前的);镜像 LightRAG。"""
    out, used = [], 0
    for it in items:
        used += est_tokens(key(it))
        if used > max_tokens and out:
            break
        out.append(it)
    return out
```

- [ ] **Step 4: 配置(config.py)**

```python
    max_entity_tokens: int = Field(6000, env="MAX_ENTITY_TOKENS")
    max_relation_tokens: int = Field(8000, env="MAX_RELATION_TOKENS")
    max_total_tokens: int = Field(30000, env="MAX_TOTAL_TOKENS")
```

- [ ] **Step 5: 跑 + 提交** — PASS。
```bash
git add backend/app/services/retrieval.py backend/app/core/config.py backend/tests/test_mix_budget.py
git commit -m "feat(mix): est_tokens + truncate_by_tokens + token 预算(照 LightRAG 6000/8000/30000)"
```

---

## Phase C — KG 检索(结构子图 + 源 chunk)

### Task 3: `_chunk_kg_overlay`(种子→1-hop 子图)

**Files:** Modify `backend/app/services/sqlite_repository.py`;Test `backend/tests/test_mix_overlay.py`

**Context:** 种子 = `federated_retrieve`(节点,ll/query)∪ `federated_retrieve_relations`(关系,hl)端点;`_federated_rx_graph(nb)` → `multihop_subgraph(depth=1, fan_out=_MIX_FANOUT)` → `render_subgraph_context(id_offset)`。常量硬编码:`_MIX_NODE_SEEDS=20`/`_MIX_REL_SEEDS=10`/`_MIX_FANOUT=8`。返回 `(block, id_map, kg_hits)`,其中 `kg_hits`=种子命中(带 `.relevance`,供 grounding)。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_mix_overlay.py
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings()); r.embedder = FakeEmbedder(dim=16); return r


def _seed(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None,
        [{"local_id": "a", "object_type": "concept", "payload": {"name": "Cascode"},
          "evidence": [{"quoted_span": "cascode raises Rout", "element_id": "el-x-0001"}]},
         {"local_id": "b", "object_type": "claim", "payload": {"name": "Cascode raises output resistance"}, "evidence": []}],
        [{"source_local_id": "b", "target_local_id": "a", "edge_type": "about", "evidence": []}])
    return nb


def test_overlay_block_idmap_hits(repo):
    nb = _seed(repo)
    block, id_map, hits = repo._chunk_kg_overlay(nb.id, "cascode output resistance", "", id_offset=5)
    assert isinstance(block, str) and id_map
    assert any(int(k[1:]) >= 6 for k in id_map)          # id_offset=5 → key 从 k6
    assert all(hasattr(h, "relevance") for h in hits)


def test_overlay_empty_no_kg(repo):
    nb = repo.create_notebook(NotebookCreate(name="e"))
    assert repo._chunk_kg_overlay(nb.id, "x", "", 0) == ("", {}, [])
```

- [ ] **Step 2: 跑确认失败** — FAIL(AttributeError)。

- [ ] **Step 3: 实现(加在 `_graph_seed_fusion` 附近)**

```python
    _MIX_NODE_SEEDS = 20
    _MIX_REL_SEEDS = 10
    _MIX_FANOUT = 8

    def _chunk_kg_overlay(self, notebook_id, query, hl, id_offset):
        """种子(节点∪关系端点)→1-hop 子图→渲染。返回 (block, id_map, kg_hits)。
        kg_hits=种子命中(带 .relevance),供 grounding。无 KG/种子 → ("", {}, [])。"""
        from app.services.kg.graph_reason import multihop_subgraph, render_subgraph_context
        node_hits = self.federated_retrieve(notebook_id, query)[: self._MIX_NODE_SEEDS]
        rel_hits = self.federated_retrieve_relations(notebook_id, hl or query)[: self._MIX_REL_SEEDS]
        seeds = [h.object_id for h in node_hits]
        for r in rel_hits:
            seeds.extend((r.source_object_id, r.target_object_id))
        seeds = list(dict.fromkeys(s for s in seeds if s))
        if not seeds:
            return "", {}, []
        G, idx_to_oid, oid_to_idx = self._federated_rx_graph(notebook_id)
        if G is None or G.num_nodes() == 0:
            return "", {}, []
        subgraph = multihop_subgraph(G, oid_to_idx, idx_to_oid, seed_ids=seeds,
                                     edge_types=None, max_depth=1, max_fan_out=self._MIX_FANOUT)
        if not subgraph:
            return "", {}, []
        block, id_map = render_subgraph_context(subgraph, id_offset=id_offset)
        return block, id_map, node_hits   # node_hits 带 .relevance 供 grounding
```

> 确认 `multihop_subgraph(edge_types=None)` = 所有边类型;否则传 `EDGE_TYPES` 全集。`_federated_rx_graph` 返回 `(G, idx_to_oid, oid_to_idx)`。

- [ ] **Step 4: 跑 + 提交** — PASS。
```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_mix_overlay.py
git commit -m "feat(mix): _chunk_kg_overlay(种子→1hop 子图→render,返回 kg_hits)"
```

### Task 4: `_kg_source_chunks`(evidence→chunk)

**Files:** Modify `backend/app/services/sqlite_repository.py`;Test `backend/tests/test_mix_overlay.py`(追加)

**Context:** LightRAG local/global 第三步:把 KG 项的源 chunk 拉进来。我们的映射:KG 对象 `evidence[].element_id` → 含该 element 的 chunk(chunks 表 `element_ids` JSON 含之)→ `RetrievedChunk`。relevance 给个占位(0.3,rerank 会重排;MMR fallback 下也有个序)。

- [ ] **Step 1: 写失败测试**

```python
def test_kg_source_chunks_maps_evidence_to_chunk(repo, monkeypatch):
    nb = _seed(repo)
    # 造一个含 el-x-0001 的 chunk
    from uuid import uuid4
    from app.services.sqlite_repository import _now
    import json as _j
    with repo._write() as db:
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   ("ck-mix1", nb.id, "src-x", "cascode raises Rout via stacking",
                    "1", _j.dumps(["el-x-0001"]), _now()))
    with repo._connect() as db:
        oid = db.execute("SELECT id FROM knowledge_objects WHERE notebook_id=? AND object_type='concept'",
                         (nb.id,)).fetchone()["id"]
    chunks = repo._kg_source_chunks(nb.id, [oid])
    assert any(c.chunk_id == "ck-mix1" for c in chunks)
```

- [ ] **Step 2: 跑确认失败** — FAIL(AttributeError)。

- [ ] **Step 3: 实现**

```python
    def _kg_source_chunks(self, notebook_id, object_ids):
        """KG 对象 evidence 的 element_id → 含该 element 的 chunk(LightRAG 源 chunk)。
        返回 List[RetrievedChunk](relevance 占位 0.3,后续 rerank 重排)。"""
        from app.services.retrieval import RetrievedChunk
        if not object_ids:
            return []
        with self._connect() as db:
            ph = ",".join("?" * len(object_ids))
            erows = db.execute(
                f"SELECT evidence FROM knowledge_objects WHERE id IN ({ph})", list(object_ids)).fetchall()
            elem_ids = set()
            for r in erows:
                for e in json.loads(r["evidence"] or "[]"):
                    if isinstance(e, dict) and e.get("element_id"):
                        elem_ids.add(e["element_id"])
            if not elem_ids:
                return []
            crows = db.execute(
                "SELECT id, source_id, text, section_path, element_ids FROM chunks WHERE notebook_id=?",
                (notebook_id,)).fetchall()
        out, seen = [], set()
        for cr in crows:
            cids = set(json.loads(cr["element_ids"] or "[]"))
            if cids & elem_ids and cr["id"] not in seen:
                seen.add(cr["id"])
                out.append(RetrievedChunk(
                    chunk_id=cr["id"], source_id=cr["source_id"], source_title="",
                    section_path=cr["section_path"], text=cr["text"],
                    element_ids=json.loads(cr["element_ids"] or "[]"), relevance=0.3))
        return out
```

> 注:`source_title` 留空(可后补 join sources);v1 够用(rerank 重排不靠 title)。大 notebook 全扫 chunks 可接受(单本有界);若过大可后续加 element→chunk 索引。

- [ ] **Step 4: 跑 + 提交** — PASS。
```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_mix_overlay.py
git commit -m "feat(mix): _kg_source_chunks(KG evidence element_id→源 chunk,LightRAG 第三步)"
```

---

## Phase D — mix 编排 + ask_chunk 接线

### Task 5: `_mix_retrieve` 编排(KG 在前 + 源 chunk round-robin 并池)

**Files:** Modify `backend/app/services/sqlite_repository.py`(`__init__` 加 rerank_client;新增 `_mix_retrieve`);Test `backend/tests/test_mix_answer.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_mix_answer.py
import pytest, json as _j
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository, _now
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("CHUNK_KG_OVERLAY_ENABLED", "true")
    r = SQLiteRepository(Settings()); r.embedder = FakeEmbedder(dim=16); return r


def test_mix_retrieve_merges_vector_and_kg_source_chunks(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None,
        [{"local_id": "a", "object_type": "concept", "payload": {"name": "Cascode"},
          "evidence": [{"quoted_span": "x", "element_id": "el-x-1"}]}], [])
    with repo._write() as db:
        for cid, els in [("ck-vec", ["el-y-1"]), ("ck-kg", ["el-x-1"])]:
            db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                       "VALUES (?,?,?,?,?,?,?)", (cid, nb.id, "s", "cascode "+cid, "1", _j.dumps(els), _now()))
    repo._embed_chunks_for_source("s") if hasattr(repo, "_embed_chunks_for_source") else None
    cand, block, id_map, kg_hits = repo._mix_retrieve(nb.id, "cascode", "", ["cascode"])
    ids = {c.chunk_id for c in cand}
    assert "ck-kg" in ids          # KG 源 chunk 进了候选池
    assert isinstance(block, str) and isinstance(id_map, dict)
```

- [ ] **Step 2: 跑确认失败** — FAIL(AttributeError `_mix_retrieve`)。

- [ ] **Step 3: 实现**

3a. `__init__` 加(创建 embedder 附近):
```python
        from app.services.rerank_client import RerankClient
        self.rerank_client = RerankClient(self.settings)
```

3b. 新增 `_mix_retrieve`(KG 先行 → 源 chunk + 向量 chunk round-robin → 候选池):
```python
    def _gather_vector_chunks(self, notebook_id, sub_queries):
        """召回向量 chunk 候选(多子查询合并去重;单查询直接 scored)。返回 List[RetrievedChunk]。"""
        if len(sub_queries) >= 2:
            collected, _per, _ids, _mat = self._retrieve_chunks_multi(notebook_id, sub_queries)
            seen, out = set(), []
            for c in collected:
                if c.chunk_id not in seen:
                    seen.add(c.chunk_id); out.append(c)
            return out
        scored, _ids, _mat = self._retrieve_chunks(notebook_id, sub_queries[0])
        return scored

    def _mix_retrieve(self, notebook_id, query, hl, sub_queries):
        """三路 mix 检索。KG 在前:种子→子图(结构 block)+源 chunk;与向量 chunk
        round-robin 合并成候选池。返回 (candidates, kg_block, kg_id_map, kg_hits)。"""
        vector_chunks = self._gather_vector_chunks(notebook_id, sub_queries)
        kg_block, kg_id_map, kg_hits = "", {}, []
        kg_chunks = []
        overlay_on = self.settings.chunk_kg_overlay_enabled and (
            self._notebook_has_kg(notebook_id) or self._any_base_notebook_has_kg())
        if overlay_on:
            # id_offset 先占位(chunk 段编号在选定后才知;render 用相对偏移,统一在 _answer 时重排)
            kg_block, kg_id_map, kg_hits = self._chunk_kg_overlay(notebook_id, query, hl, id_offset=0)
            kg_chunks = self._kg_source_chunks(notebook_id, [v["object_id"] for v in kg_id_map.values()])
        # round-robin 合并 vector + kg 源 chunk(vector 优先),按 chunk_id 去重
        merged, seen = [], set()
        for i in range(max(len(vector_chunks), len(kg_chunks))):
            for src in (vector_chunks, kg_chunks):
                if i < len(src) and src[i].chunk_id not in seen:
                    seen.add(src[i].chunk_id); merged.append(src[i])
        return merged, kg_block, kg_id_map, kg_hits
```

> 注:`kg_id_map` 用 `id_offset=0` 渲染;Task 6 在选定 chunk 后,把 KG 段**重映射到 chunk 段之后的 key**(见 Task 6)。这里只需 kg 项的 object_id 拉源 chunk。

- [ ] **Step 4: 跑 + 提交**(config 加 `chunk_kg_overlay_enabled: bool = Field(True, env="CHUNK_KG_OVERLAY_ENABLED")`)
```bash
git add backend/app/services/sqlite_repository.py backend/app/core/config.py backend/tests/test_mix_answer.py
git commit -m "feat(mix): _mix_retrieve(KG 先行+源 chunk round-robin 并池,CHUNK_KG_OVERLAY_ENABLED 默认开)"
```

### Task 6: ask_chunk 接线:rerank 选 chunk + 统一上下文 + 合并集 grounding

**Files:** Modify `backend/app/services/sqlite_repository.py`(`ask_chunk` + `_answer_chunks`);Test `backend/tests/test_mix_answer.py`(追加)

- [ ] **Step 1: 写失败测试**

```python
def test_ask_chunk_rerank_order_and_kg_in_idmap(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None,
        [{"local_id": "a", "object_type": "concept", "payload": {"name": "Cascode"},
          "evidence": [{"quoted_span": "x", "element_id": "el-x-1"}]}], [])
    with repo._write() as db:
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("ck1", nb.id, "s", "cascode boosts Rout", "1", _j.dumps(["el-x-1"]), _now()))
    # 用统一上下文构造,断言 chunk 在 k1、KG 概念在更大 key
    from app.services.retrieval import RetrievedChunk
    chunks = [RetrievedChunk(chunk_id="ck1", source_id="s", source_title="D", section_path="1",
                             text="cascode boosts Rout", relevance=0.8)]
    block, id_map, kg_hits = repo._mix_answer_context(nb.id, "cascode", "", chunks)
    assert id_map["k1"]["object_type"] == "chunk"
    assert any(v["object_type"] == "concept" for v in id_map.values())
```

- [ ] **Step 2: 跑确认失败** — FAIL(AttributeError `_mix_answer_context`)。

- [ ] **Step 3: 实现**

3a. `_mix_answer_context(nb, query, hl, selected_chunks)`(把已选 chunk 的上下文 + KG 结构合并,KG key 偏移到 chunk 之后):
```python
    def _mix_answer_context(self, notebook_id, query, hl, chunks):
        """已选 chunk 上下文 + (flag+有KG) KG 子图,统一 context+id_map。返回 (block, id_map, kg_hits)。"""
        chunk_block, id_map = self._chunk_answer_context(chunks)
        if not (self.settings.chunk_kg_overlay_enabled and
                (self._notebook_has_kg(notebook_id) or self._any_base_notebook_has_kg())):
            return chunk_block, id_map, []
        kg_block, kg_map, kg_hits = self._chunk_kg_overlay(notebook_id, query, hl, id_offset=len(id_map))
        if not kg_map:
            return chunk_block, id_map, []
        id_map.update(kg_map)
        return f"{chunk_block}\n\n## 知识图谱(结构化线索)\n{kg_block}", id_map, kg_hits
```

3b. `_answer_chunks` 改签名用 `_mix_answer_context`(返回 kg_hits 透传):
```python
    def _answer_chunks(self, notebook_id, question, hl, chunks, history="") -> tuple:
        from app.services.prompts import answer_prompt, ANSWER_SCHEMA_HINT
        context_block, id_map, kg_hits = self._mix_answer_context(notebook_id, question, hl, chunks)
        raw = self.llm_client.chat_json(
            [{"role": "user", "content": answer_prompt(question, context_block, history)}], ANSWER_SCHEMA_HINT)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("answer did not return a JSON object")
        answer = str(data.get("answer", "")).strip()
        anchors = self._parse_answer_anchors(answer, id_map)
        return answer, bool(data.get("grounded", False)), anchors, kg_hits
```

3c. `ask_chunk` 重写检索+选择+grounding 段:
```python
        hl = " ".join(getattr(ex, "high_level_keywords", []) or []) if self.settings.query_rewrite_enabled else ""
        cand, _kgblock, _kgmap, _kghits = self._mix_retrieve(notebook_id, retrieval_query, hl, sub_queries)
        chunk_budget = max(2000, self.settings.max_total_tokens
                           - self.settings.max_entity_tokens - self.settings.max_relation_tokens - 1000)
        if self.rerank_client.configured and cand:
            from app.services.retrieval import truncate_by_tokens
            order = self.rerank_client.rerank(retrieval_query, [c.text for c in cand])
            selected = truncate_by_tokens([cand[i] for i in order], key=lambda c: c.text, max_tokens=chunk_budget)
        else:
            # 无 rerank:对合并候选做 MMR(沿用既有 _mmr_select_chunks 的多样性)+ 预算
            selected = self._mmr_select_chunks_fallback(cand, self.settings.chunk_mmr_k, self.settings.chunk_mmr_lambda)
        # citations(绑回 chunk;沿用现状)+ 答案
        ...(citations 段不变)...
        answer, llm_grounded, anchors, kg_hits = self._answer_chunks(notebook_id, question, hl, selected, history)
        evidence_level, top_relevance = classify_evidence(
            list(selected) + kg_hits, anchors, llm_grounded,
            self.settings.evidence_tau_low, self.settings.evidence_tau_high)
```

> `_mmr_select_chunks_fallback`:对 List[RetrievedChunk] 做 MMR 的薄封装(现有 `_mmr_select_chunks` 接收 (scored, ids, mat);若签名不便,fallback 直接按 relevance 排序取前 N 即可——无 rerank 时的退化路径,非主路)。本 Task 用最简实现:`sorted(cand, key=lambda c: c.relevance, reverse=True)[:k]`,并注明 MMR 多样性在无 rerank 时可选保留。

- [ ] **Step 4: 跑 + 提交**

`cd backend && python -m pytest tests/test_mix_answer.py tests/test_mix_overlay.py tests/test_chunk_retrieval.py -q` → PASS。
```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_mix_answer.py
git commit -m "feat(mix): ask_chunk 三路 mix 接线(rerank 选 chunk→token 预算,统一 [k] 上下文,合并集 grounding)"
```

---

## Phase E — 删旧 LLM rerank

### Task 7: 移除旧 LLM 打分 rerank

**Files:** Modify `config.py`、`sqlite_repository.py`、`reasoning_retrieval.py`、`prompts.py`;Delete `tests/test_rerank.py`

- [ ] **Step 1: 删除**
  - config.py:删 `rerank_enabled`/`rerank_candidates`/`rerank_timeout_seconds`。
  - sqlite_repository.py:删 `_rerank_hits` + import `RERANK_SCHEMA_HINT`/`rerank_prompt`;`ask_graph` 删 `top_hits = self._rerank_hits(...)` 行(top_hits 用上一步结果)。
  - reasoning_retrieval.py:删 `_rerank_hits` 调用行。
  - prompts.py:删 `RERANK_SCHEMA_HINT` / `rerank_prompt`。
  - 删 `tests/test_rerank.py`。
- [ ] **Step 2: 全量** — `cd backend && python -m pytest -q` 全绿;`grep -rn "_rerank_hits\|rerank_prompt\|RERANK_SCHEMA_HINT\|rerank_enabled" app/` 为空。
- [ ] **Step 3: 提交**
```bash
git add -A
git commit -m "refactor(mix): 删旧 LLM 打分 rerank(被 qwen3-rerank 取代;调用点 no-op,行为不变)"
```

---

## Phase F — 收尾

### Task 8: 等价测试 + 文档 + 全量
- [ ] **等价**:加测试——`CHUNK_KG_OVERLAY_ENABLED=false` 且 rerank 未配 → `ask_chunk` 走 MMR、不注入 KG(与改前同结构)。
- [ ] **全量**:`python -m pytest -q` 全绿;`bash scripts/check.sh` EXIT=0(ask-mode 契约 `['chunk','graph','reasoning']` 不变)。
- [ ] **env 文档**:`.env.example`+README 增 `CHUNK_KG_OVERLAY_ENABLED`/`RERANK_MODEL`/`RERANK_BASE_URL`/`RERANK_API_KEY`/`RERANK_MAX_DOCS`/`MAX_ENTITY/RELATION/TOTAL_TOKENS`/`CHUNK_RECALL=200`;删 `RERANK_ENABLED`/`RERANK_CANDIDATES`/`RERANK_TIMEOUT_SECONDS`。
- [ ] 提交 + (用户跑)真机 eval:nb-b37185f4ae chunk 问答 ON/OFF + rerank 有/无 对照,数字落 PR #59。

---

## Self-Review

- **Spec 覆盖**:三路 mix —— naive(Task 5 `_gather_vector_chunks`)、local+global(Task 3 子图)、**源 chunk 并池(Task 4+5,B 方案已纳入 v1)**✓;rerank 选 chunk + token 预算(Task 1/2/6)✓;统一 `[k]`(Task 6 `_mix_answer_context`)✓;合并集 grounding(Task 6)✓;删旧 rerank(Task 7)✓;flag+门控+等价(Task 5/6 + Task 8)✓;eval(Task 8)✓。**无遗留缺口**(v1 已完整照 LightRAG)。
- **占位符**:Task 6 的 citations 段“不变”指沿用 ask_chunk 现有 Citation 构造(非新写);MMR fallback 用最简 relevance 排序(已注明,非主路)。其余 code step 完整。
- **类型一致**:`RerankClient.rerank->List[int]`;`_chunk_kg_overlay(nb,query,hl,id_offset)->(block,id_map,kg_hits)`;`_kg_source_chunks(nb,object_ids)->List[RetrievedChunk]`;`_mix_retrieve(nb,query,hl,sub_queries)->(candidates,kg_block,kg_id_map,kg_hits)`;`_mix_answer_context(nb,query,hl,chunks)->(block,id_map,kg_hits)`;`_answer_chunks(nb,question,hl,chunks,history)->(answer,grounded,anchors,kg_hits)`;`est_tokens`/`truncate_by_tokens`;config `chunk_kg_overlay_enabled`/`max_*_tokens`/`rerank_*`/`chunk_recall` —— 跨任务一致。
- **不变量**:rerank 分仅定 chunk 序(Task 6 用 order 重排,不写 `.relevance`);grounding/tau 用融合 `.relevance`(Task 6 classify_evidence 喂 selected∪kg_hits);flag 关+无 rerank→MMR 等价;无 KG→纯 chunk。
- **必须 KG 只检索一次(实现纪律,纠正 render 两次)**:`_chunk_kg_overlay` 在 `_mix_retrieve` 里**只调一次**,用**固定高 key-base** `_MIX_KG_KEY_BASE=1000` 作 `id_offset`(chunk 段 key 是 k1..k~100,永不撞到 k1001+),返回的 `(kg_block, kg_id_map, kg_hits)` **一路透传**到 `_answer_chunks`:即 Task 5 `_mix_retrieve` 返回 `(candidates, kg_block, kg_id_map, kg_hits)`;`ask_chunk` 把这四样拿到后,`_answer_chunks(nb, q, selected, kg_block, kg_id_map, kg_hits)` **不再重新检索/渲染 KG**,只 `_chunk_answer_context(selected)` 出 chunk 段(k1..),再 `id_map.update(kg_id_map)` + `chunk_block + "\n\n## 知识图谱\n" + kg_block`。**KG 检索 1 次、render 1 次**。(故 Task 6 的 `_mix_answer_context` 签名改为接收 `kg_block/kg_id_map/kg_hits`,不自己调 `_chunk_kg_overlay`;Task 3 的 `_chunk_kg_overlay` 调用方传 `id_offset=1000`。)
