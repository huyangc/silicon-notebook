# chunk×graph mix + qwen3-rerank 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.
>
> **提交纪律:** commit 末尾追加 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。**叠在当前分支 `claude/wonderful-bell-3b27db`(PR #59),只 commit、不 push、不另开 PR。**

**Goal:** 把 `ask_chunk` 升级成忠实 LightRAG 的三路 mix(chunk + 节点1hop + 关系),chunk 选择改 qwen3-rerank,token 预算照 LightRAG;删旧 LLM 打分 rerank。`CHUNK_KG_OVERLAY_ENABLED` 默认 True。

**Architecture:** 复用现有原语(`_retrieve_chunks` / `federated_retrieve` / `federated_retrieve_relations` / `_federated_rx_graph` / `multihop_subgraph` / `render_subgraph_context` / `node_context`)。chunk 选择 = 召回→rerank(`RerankClient`)→token 预算;KG = 种子(节点+关系端点)→1-hop 子图→并进统一 `[k]` 上下文。**rerank 分只排序,`.relevance`(融合分)管 grounding/tau。** flag 关时与现状字节等价。

**Tech Stack:** Python3 / SQLite / DashScope qwen3-rerank(HTTP)/ pytest / FakeEmbedder。

**Spec:** `docs/superpowers/specs/2026-06-18-chunk-graph-mix-design.md`

**关键已知形状(实读)**:`RetrievedChunk`(retrieval.py:588)有 `.object_id`(=chunk_id)/`.relevance`/`.element_ids`/`.text`/`.section_path`/`.source_title`。`_chunk_answer_context`(sqlite_repository.py:4420)与 `render_subgraph_context`(graph_reason.py:165)产出**同形 id_map**;`_parse_answer_anchors`(5144)按 id_map 统一出 `AnswerAnchor`。`classify_evidence(top_hits, anchors, tau_low, tau_high)` 读 `.object_id`/`.relevance`。`DashscopeEmbedder`(embedding_dashscope.py)= OpenAI client + 429 backoff 范式。

---

## Phase A — RerankClient(独立)

### Task 1: RerankClient + 配置

**Files:**
- Create: `backend/app/services/rerank_client.py`
- Modify: `backend/app/core/config.py`
- Test: `backend/tests/test_rerank_client.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_rerank_client.py
from app.services.rerank_client import RerankClient


class _Settings:
    rerank_model = "qwen3-rerank"
    rerank_base_url = "http://fake/v1"
    rerank_api_key = "k"
    rerank_top_n = 0
    rerank_max_docs = 500
    embed_concurrency = 8
    openai_compat_timeout_seconds = 30


def test_rerank_unconfigured_returns_identity():
    s = _Settings(); s.rerank_model = ""
    rc = RerankClient(s)
    assert not rc.configured
    # 未配置 → 原序索引
    assert rc.rerank("q", ["a", "b", "c"]) == [0, 1, 2]


def test_rerank_orders_by_score(monkeypatch):
    rc = RerankClient(_Settings())
    # mock 单次 HTTP:返回把 index2 排最前
    def fake_post(query, docs):
        return [{"index": 2, "relevance_score": 0.9},
                {"index": 0, "relevance_score": 0.5},
                {"index": 1, "relevance_score": 0.1}]
    monkeypatch.setattr(rc, "_rerank_batch", lambda q, docs: fake_post(q, docs))
    assert rc.rerank("q", ["a", "b", "c"]) == [2, 0, 1]


def test_rerank_failure_falls_back_to_identity(monkeypatch):
    rc = RerankClient(_Settings())
    def boom(q, docs): raise RuntimeError("net")
    monkeypatch.setattr(rc, "_rerank_batch", boom)
    assert rc.rerank("q", ["a", "b"]) == [0, 1]
```

- [ ] **Step 2: 跑测试确认失败** — `cd backend && python -m pytest tests/test_rerank_client.py -q` → FAIL(ImportError)。

- [ ] **Step 3: 实现 `rerank_client.py`**

```python
"""qwen3-rerank (DashScope text-rerank) 客户端。单次批量调用;候选超限自动切 batch
并发调用 + 按 relevance_score 合并。失败/未配置 → 返回原序索引(降级)。"""
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
        """返回按相关性降序的原始下标列表。未配置/失败 → range(len) 原序。"""
        if not self.configured or not documents:
            return list(range(len(documents)))
        try:
            n = len(documents)
            if n <= self.max_docs:
                scored = self._rerank_batch(query, documents)  # [{index, relevance_score}]
            else:
                scored = self._rerank_split(query, documents)
            order = [r["index"] for r in sorted(
                scored, key=lambda r: r["relevance_score"], reverse=True)]
            seen, out = set(), []
            for idx in order:
                if 0 <= idx < len(documents) and idx not in seen:
                    seen.add(idx); out.append(idx)
            for idx in range(len(documents)):       # 任何漏掉的补回(防截断)
                if idx not in seen:
                    out.append(idx)
            return out
        except Exception:
            return list(range(len(documents)))

    def _rerank_batch(self, query: str, documents: List[str]) -> List[dict]:
        resp = requests.post(
            f"{self.base_url}/reranks",
            headers={"Authorization": f"Bearer {self.api_key}",
                     "Content-Type": "application/json"},
            json={"model": self.model, "query": query, "documents": documents},
            timeout=getattr(self.settings, "openai_compat_timeout_seconds", 30),
        )
        resp.raise_for_status()
        return resp.json()["results"]

    def _rerank_split(self, query: str, documents: List[str]) -> List[dict]:
        """超 max_docs:切 batch + 线程池并发 + 全局下标重映射。score 跨 batch 同尺可比。"""
        batches = [(i, documents[i:i + self.max_docs])
                   for i in range(0, len(documents), self.max_docs)]
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

- [ ] **Step 4: 配置(config.py,在删掉的旧 rerank 字段位置附近;旧字段在 Task 9 删)**

```python
    rerank_model: str = Field("", env="RERANK_MODEL")
    rerank_base_url: str = Field("https://dashscope.aliyuncs.com/compatible-api/v1", env="RERANK_BASE_URL")
    rerank_api_key: str = Field("", env="RERANK_API_KEY")
    rerank_top_n: int = Field(0, env="RERANK_TOP_N")
    rerank_max_docs: int = Field(500, env="RERANK_MAX_DOCS")
```

- [ ] **Step 5: 跑测试 + 提交**

`cd backend && python -m pytest tests/test_rerank_client.py -q` → PASS(3)。
```bash
git add backend/app/services/rerank_client.py backend/app/core/config.py backend/tests/test_rerank_client.py
git commit -m "feat(mix): RerankClient(qwen3-rerank,单批+超限并发切批,失败降级原序)"
```

---

## Phase B — token 预算助手

### Task 2: token 估算 + 预算截断 + 配置

**Files:**
- Modify: `backend/app/services/retrieval.py`、`backend/app/core/config.py`
- Test: `backend/tests/test_mix_budget.py`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_mix_budget.py
from app.services.retrieval import est_tokens, truncate_by_tokens


def test_est_tokens_rough():
    assert est_tokens("") == 0
    assert est_tokens("abcd") >= 1


def test_truncate_by_tokens_keeps_prefix_under_budget():
    items = ["x" * 40, "y" * 40, "z" * 40]   # 各 ~10+ tokens
    kept = truncate_by_tokens(items, key=lambda s: s, max_tokens=20)
    assert kept == items[:2] or kept == items[:1]   # 累加超 20 即停
    assert len(kept) < len(items)
```

- [ ] **Step 2: 跑确认失败** — `cd backend && python -m pytest tests/test_mix_budget.py -q` → FAIL(ImportError)。

- [ ] **Step 3: 实现(retrieval.py)**

```python
def est_tokens(text: str) -> int:
    """粗估 token(无 tiktoken 依赖):中英混排约 3.5 字符/token,向上取整。
    仅用于上下文预算截断(只需不溢出,不求精确)。"""
    import math
    return math.ceil(len(text or "") / 3.5)


def truncate_by_tokens(items, key, max_tokens):
    """按顺序累加 est_tokens(key(item)),首次超 max_tokens 即停(保留之前的)。
    镜像 LightRAG truncate_list_by_token_size。"""
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

- [ ] **Step 5: 跑 + 提交**

`cd backend && python -m pytest tests/test_mix_budget.py -q` → PASS。
```bash
git add backend/app/services/retrieval.py backend/app/core/config.py backend/tests/test_mix_budget.py
git commit -m "feat(mix): est_tokens + truncate_by_tokens + token 预算配置(照 LightRAG 6000/8000/30000)"
```

---

## Phase C — KG 叠加检索

### Task 3: `_chunk_kg_overlay`(种子→1-hop 子图)

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`
- Test: `backend/tests/test_mix_overlay.py`

**Context:** 复用 graph 模式原语。种子 = 节点检索命中(`federated_retrieve`,ll/query)∪ 关系检索端点(`federated_retrieve_relations`,hl);`_federated_rx_graph(nb)` 取联邦图;`multihop_subgraph(..., max_depth=1, max_fan_out=_MIX_FANOUT)` 取 1-hop 子图;`render_subgraph_context(subgraph, id_offset)` 出 KG 上下文块 + id_map。常量硬编码(不加 env 旋钮):`_MIX_NODE_SEEDS=20`、`_MIX_REL_SEEDS=10`、`_MIX_FANOUT=8`。

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
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _seed(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None,
        [{"local_id": "a", "object_type": "concept", "payload": {"name": "Cascode"}, "evidence": []},
         {"local_id": "b", "object_type": "claim", "payload": {"name": "Cascode raises output resistance"}, "evidence": []}],
        [{"source_local_id": "b", "target_local_id": "a", "edge_type": "about", "evidence": []}])
    return nb


def test_chunk_kg_overlay_returns_block_and_idmap(repo):
    nb = _seed(repo)
    block, id_map = repo._chunk_kg_overlay(nb.id, "cascode output resistance", "", id_offset=5)
    assert isinstance(block, str)
    # 命中的概念/claim 进了 id_map,且 key 从 k6 起(id_offset=5)
    assert id_map and all(k.startswith("k") for k in id_map)
    assert any(int(k[1:]) >= 6 for k in id_map)


def test_chunk_kg_overlay_empty_when_no_kg(repo):
    nb = repo.create_notebook(NotebookCreate(name="empty"))
    block, id_map = repo._chunk_kg_overlay(nb.id, "anything", "", id_offset=0)
    assert id_map == {} and block == ""
```

- [ ] **Step 2: 跑确认失败** — FAIL(AttributeError `_chunk_kg_overlay`)。

- [ ] **Step 3: 实现(sqlite_repository.py,加在 `_graph_seed_fusion` 附近)**

```python
    _MIX_NODE_SEEDS = 20
    _MIX_REL_SEEDS = 10
    _MIX_FANOUT = 8

    def _chunk_kg_overlay(self, notebook_id: str, query: str, hl: str, id_offset: int):
        """KG 局部子图叠加:节点命中 ∪ 关系端点 = 种子 → 1-hop 子图 → 渲染。
        返回 (kg_context_block, id_map)。无 KG / 无种子 → ("", {})。"""
        from app.services.kg.graph_reason import multihop_subgraph, render_subgraph_context
        node_hits = self.federated_retrieve(notebook_id, query)[: self._MIX_NODE_SEEDS]
        rel_hits = self.federated_retrieve_relations(notebook_id, hl or query)[: self._MIX_REL_SEEDS]
        seeds = [h.object_id for h in node_hits]
        for r in rel_hits:
            seeds.extend((r.source_object_id, r.target_object_id))
        seeds = list(dict.fromkeys(s for s in seeds if s))   # 去重保序
        if not seeds:
            return "", {}
        G, idx_to_oid, oid_to_idx = self._federated_rx_graph(notebook_id)
        if G is None or G.num_nodes() == 0:
            return "", {}
        subgraph = multihop_subgraph(
            G, oid_to_idx, idx_to_oid, seed_ids=seeds, edge_types=None,
            max_depth=1, max_fan_out=self._MIX_FANOUT)
        if not subgraph:
            return "", {}
        return render_subgraph_context(subgraph, id_offset=id_offset)
```

> 注:`multihop_subgraph` 的 `edge_types=None` 表示不限边类型(取全部 1-hop);确认其签名支持 None=所有类型,若不支持则传入全 12 类 `EDGE_TYPES`。`_federated_rx_graph` 返回 `(G, idx_to_oid, oid_to_idx)`。

- [ ] **Step 4: 跑 + 提交**

`cd backend && python -m pytest tests/test_mix_overlay.py -q` → PASS。
```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_mix_overlay.py
git commit -m "feat(mix): _chunk_kg_overlay(节点∪关系端点种子→1-hop 子图→render)"
```

---

## Phase D — 统一上下文 + 引用 + ask_chunk 接线

### Task 4: rerank 选 chunk + KG 叠加注入 _answer_chunks

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(`__init__` 加 rerank_client、`_answer_chunks`、`ask_chunk`)
- Test: `backend/tests/test_mix_answer.py`

- [ ] **Step 1: 写失败测试**(验证:flag 开时 KG 项进 id_map 且可被 `[k]` 引用解析)

```python
# backend/tests/test_mix_answer.py
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
    monkeypatch.setenv("CHUNK_KG_OVERLAY_ENABLED", "true")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def test_mix_context_merges_chunk_and_kg(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None,
        [{"local_id": "a", "object_type": "concept", "payload": {"name": "Cascode"}, "evidence": []}], [])
    # 构造一个假 chunk
    from app.services.retrieval import RetrievedChunk
    chunks = [RetrievedChunk(chunk_id="ck1", source_id="s", source_title="Doc",
                             section_path="1", text="cascode boosts Rout", relevance=0.8)]
    block, id_map = repo._mix_answer_context(nb.id, "cascode output resistance", "", chunks)
    # chunk 在 k1;KG 概念在 k2+(id_offset=len(chunks))
    assert "k1" in id_map and id_map["k1"]["object_type"] == "chunk"
    assert any(v["object_type"] == "concept" for v in id_map.values())
```

- [ ] **Step 2: 跑确认失败** — FAIL(AttributeError `_mix_answer_context`)。

- [ ] **Step 3: 实现**

3a. `__init__` 加 rerank 客户端(在创建 embedder 附近):
```python
        from app.services.rerank_client import RerankClient
        self.rerank_client = RerankClient(self.settings)
```

3b. 新增 `_mix_answer_context`(组合 chunk 上下文 + KG 叠加,统一 id_map):
```python
    def _mix_answer_context(self, notebook_id, query, hl, chunks):
        """chunk 上下文 + (flag+有KG时) KG 1-hop 子图,合并成统一 context+id_map。"""
        chunk_block, id_map = self._chunk_answer_context(chunks)
        if not self.settings.chunk_kg_overlay_enabled:
            return chunk_block, id_map
        if not (self._notebook_has_kg(notebook_id) or self._any_base_notebook_has_kg()):
            return chunk_block, id_map
        kg_block, kg_map = self._chunk_kg_overlay(notebook_id, query, hl, id_offset=len(id_map))
        if not kg_map:
            return chunk_block, id_map
        id_map.update(kg_map)
        block = f"{chunk_block}\n\n## 知识图谱(结构化线索)\n{kg_block}"
        return block, id_map
```

3c. 改 `_answer_chunks` 用 `_mix_answer_context`(需把 nb/query/hl 传进来):
```python
    def _answer_chunks(self, notebook_id, question, hl, chunks, history="") -> tuple:
        from app.services.prompts import answer_prompt, ANSWER_SCHEMA_HINT
        context_block, id_map = self._mix_answer_context(notebook_id, question, hl, chunks)
        raw = self.llm_client.chat_json(
            [{"role": "user", "content": answer_prompt(question, context_block, history)}],
            ANSWER_SCHEMA_HINT)
        data = json.loads(raw)
        if not isinstance(data, dict):
            raise ValueError("answer did not return a JSON object")
        answer = str(data.get("answer", "")).strip()
        llm_grounded = bool(data.get("grounded", False))
        anchors = self._parse_answer_anchors(answer, id_map)
        return answer, llm_grounded, anchors
```

3d. `ask_chunk` 改 3 处:(i) 拿 `ex.high_level_keywords`;(ii) chunk 选择走 rerank(见 Task 5,本 Task 先传 hl + 调新签名);(iii) `_answer_chunks(notebook_id, question, hl, selected, history)`。本 Task 仅改调用签名 + 传 hl:
```python
        hl = " ".join(getattr(ex, "high_level_keywords", []) or []) if self.settings.query_rewrite_enabled else ""
        ...
        answer, llm_grounded, anchors = self._answer_chunks(notebook_id, question, hl, selected, history)
```
(`ex` 在 `query_rewrite_enabled` 分支已有;若关着,`hl=""`、`ex` 不存在则跳过——保持 `expand_query` 调用结构,把 `ex` 提到分支外或在关闭时 `hl=""`。)

config(config.py):
```python
    chunk_kg_overlay_enabled: bool = Field(True, env="CHUNK_KG_OVERLAY_ENABLED")
```

- [ ] **Step 4: 跑 + 提交**

`cd backend && python -m pytest tests/test_mix_answer.py tests/test_chunk_retrieval.py -q` → PASS。
```bash
git add backend/app/services/sqlite_repository.py backend/app/core/config.py backend/tests/test_mix_answer.py
git commit -m "feat(mix): _mix_answer_context 合并 chunk+KG 统一 id_map + ask_chunk 接线(CHUNK_KG_OVERLAY_ENABLED 默认开)"
```

### Task 5: chunk 选择改 rerank → token 预算(MMR fallback)

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(`ask_chunk` 选择段)
- Test: `backend/tests/test_mix_answer.py`(追加)

- [ ] **Step 1: 写失败测试**

```python
def test_chunk_selection_uses_rerank_order(repo, monkeypatch):
    from app.services.retrieval import RetrievedChunk
    cand = [RetrievedChunk(chunk_id=f"ck{i}", source_id="s", source_title="D",
                           section_path="1", text=f"t{i}", relevance=0.5) for i in range(5)]
    # rerank 把最后一个排第一
    monkeypatch.setattr(repo.rerank_client, "configured", True, raising=False)
    monkeypatch.setattr(repo.rerank_client, "rerank", lambda q, docs: [4, 0, 1, 2, 3])
    out = repo._select_chunks_rerank("q", cand, budget_tokens=10_000)
    assert out[0].chunk_id == "ck4"
```

- [ ] **Step 2: 跑确认失败** — FAIL(AttributeError `_select_chunks_rerank`)。

- [ ] **Step 3: 实现 `_select_chunks_rerank` + 接进 ask_chunk**

```python
    def _select_chunks_rerank(self, query, candidates, budget_tokens):
        """rerank 候选(配置则用 RerankClient,否则保持原序=已是 MMR/quota 序),再按
        token 预算截断。rerank 分只定序,不改 .relevance(grounding/tau 仍用融合分)。"""
        from app.services.retrieval import truncate_by_tokens
        if self.rerank_client.configured and candidates:
            order = self.rerank_client.rerank(query, [c.text for c in candidates])
            candidates = [candidates[i] for i in order]
        return truncate_by_tokens(candidates, key=lambda c: c.text, max_tokens=budget_tokens)
```

`ask_chunk` 选择段改为:召回拿到候选(单/多子查询合并后,**不再先 MMR 砍到 k**,而是把召回候选交给 rerank)→ rerank → token 预算。当 rerank 未配:回退现有 MMR/quota_fuse(保持原行为)。
```python
        # chunk 预算 = 总预算 − 实体/关系预算 − buffer(粗留)
        chunk_budget = max(2000, self.settings.max_total_tokens
                           - self.settings.max_entity_tokens
                           - self.settings.max_relation_tokens - 1000)
        if self.rerank_client.configured:
            # 召回候选(多子查询合并去重;单查询直接候选),交 rerank + token 预算
            cand = self._gather_chunk_candidates(notebook_id, sub_queries)
            selected = self._select_chunks_rerank(retrieval_query, cand, chunk_budget)
        else:
            # 现有 MMR / quota_fuse 路径(K 提高到按预算估算的条数,或保留 chunk_mmr_k)
            ...(保留现状)
```
新增小助手 `_gather_chunk_candidates(nb, sub_queries)`:多子查询时合并 `_retrieve_chunks_multi` 的 collected 去重;单查询时 `_retrieve_chunks` 的 scored。返回 `List[RetrievedChunk]`。

> 注:rerank 路下**不再用 `chunk_mmr_k` 砍**——靠 token 预算决定条数(这正是"用更多 chunk")。`CHUNK_RECALL` 升到 ~200(config:`chunk_recall: int = Field(200, ...)`)以喂饱 rerank。

- [ ] **Step 4: 跑 + 提交**

`cd backend && python -m pytest tests/test_mix_answer.py tests/test_chunk_retrieval.py tests/test_quota_fuse.py tests/test_mmr.py -q` → PASS。
```bash
git add backend/app/services/sqlite_repository.py backend/app/core/config.py backend/tests/test_mix_answer.py
git commit -m "feat(mix): chunk 选择 rerank→token 预算(MMR fallback),CHUNK_RECALL 升至 200"
```

### Task 6: grounding 在合并集上算

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(`ask_chunk` 的 classify_evidence 调用)
- Test: `backend/tests/test_mix_answer.py`(追加)

- [ ] **Step 1: 写失败测试** — 验证被 `[k]` 引用的 KG 概念(高 relevance)能让答案判 grounded。

```python
def test_grounding_counts_kg_anchor(repo):
    from app.services.retrieval import classify_evidence, RetrievedChunk, RetrievedKnowledge
    from app.models.schemas import AnswerAnchor
    chunk = RetrievedChunk(chunk_id="ck1", source_id="s", source_title="D", section_path="1",
                           text="t", relevance=0.1)
    kg = RetrievedKnowledge(object_id="ko1", object_type="concept", payload={}, relevance=0.9)
    anchors = [AnswerAnchor(key="k2", object_id="ko1", object_type="concept", label="C", name="C")]
    lvl, top = classify_evidence([chunk, kg], anchors, True, 0.18, 0.35)
    assert lvl == "grounded"   # KG 锚点 relevance 0.9 ≥ tau_high
```

- [ ] **Step 2: 跑确认失败/通过** — classify_evidence 已支持(读 .object_id/.relevance);此测试主要锁 ask_chunk 会把 KG 命中并进 `top_hits`。先跑确认 classify_evidence 行为(应 PASS),再改 ask_chunk 传合并集。

- [ ] **Step 3: 实现** — `ask_chunk` 收集 KG 命中(seed 命中,有 relevance)与 selected chunks 合并喂 classify_evidence。`_mix_answer_context` 额外返回 kg_hits(seed 节点/关系命中,带 relevance);ask_chunk:
```python
        evidence_level, top_relevance = classify_evidence(
            list(selected) + kg_hits, anchors, llm_grounded,
            self.settings.evidence_tau_low, self.settings.evidence_tau_high)
```
(`_answer_chunks` / `_mix_answer_context` 返回 `kg_hits`;无 KG/flag 关时为 `[]` → 等价现状。)

- [ ] **Step 4: 跑 + 提交**
```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_mix_answer.py
git commit -m "feat(mix): grounding 在 chunk∪KG 合并集上算(KG 锚点可计入,tau 用融合分)"
```

---

## Phase E — 删旧 LLM rerank

### Task 7: 移除旧 LLM 打分 rerank

**Files:**
- Modify: `backend/app/core/config.py`、`backend/app/services/sqlite_repository.py`、`backend/app/services/reasoning_retrieval.py`、`backend/app/services/prompts.py`
- Delete: `backend/tests/test_rerank.py`

- [ ] **Step 1: 删除**
  - config.py:删 `rerank_enabled`/`rerank_candidates`/`rerank_timeout_seconds`(:103-106)。
  - sqlite_repository.py:删 `_rerank_hits`(:4653-~4670)+ import `RERANK_SCHEMA_HINT`/`rerank_prompt`(:89/96);改 `ask_graph`(:5008)`top_hits = self._rerank_hits(question, top_hits)` → 删该行(top_hits 保持上一步结果)。
  - reasoning_retrieval.py:删 `:320` `top_hits = self.repo._rerank_hits(question, top_hits)` 行。
  - prompts.py:删 `RERANK_SCHEMA_HINT`(:229)、`rerank_prompt`(:232)。
  - 删 `tests/test_rerank.py`。

- [ ] **Step 2: 跑全量确认无死引用** — `cd backend && python -m pytest -q` → 全绿(`grep -rn "_rerank_hits\|rerank_prompt\|RERANK_SCHEMA_HINT\|rerank_enabled" app/` 应为空)。

- [ ] **Step 3: 提交**
```bash
git add -A
git commit -m "refactor(mix): 删旧 LLM 打分 rerank(被 qwen3-rerank 取代;调用点均 no-op,行为不变)"
```

---

## 收尾:全量 + 文档 + 真机 eval

- [ ] **全量**:`cd backend && python -m pytest -q` 全绿;`bash scripts/check.sh` EXIT=0(ask-mode 契约不变)。
- [ ] **等价自检**:`CHUNK_KG_OVERLAY_ENABLED=false` + 无 rerank → `ask_chunk` 应与改前同(MMR 路径)。加一条等价测试。
- [ ] **env 文档**:`.env.example` + README 增 `CHUNK_KG_OVERLAY_ENABLED`/`RERANK_MODEL`/`RERANK_BASE_URL`/`RERANK_API_KEY`/`MAX_ENTITY/RELATION/TOTAL_TOKENS`/`CHUNK_RECALL`;删 `RERANK_ENABLED`/`RERANK_CANDIDATES`/`RERANK_TIMEOUT_SECONDS`。
- [ ] **真机 eval(用户跑;.env 已配 qwen3-rerank)**:nb-b37185f4ae chunk 问答 LLM-judge,`CHUNK_KG_OVERLAY_ENABLED` ON/OFF + rerank 有/无 对照(correctness/grounding/伪引用/覆盖面/延迟)。数字落 PR #59 评论。
- [ ] 全绿后按 dev-flow 系统提 PR(3-way 并 master → push;关于是否单独 PR 由用户定)。

---

## Self-Review

- **Spec 覆盖**:三路 mix(Task 3 local+global 子图 / Task 5 naive chunk)✓;rerank(Task 1/5)✓;删旧 rerank(Task 7)✓;token 预算(Task 2/5)✓;统一 `[k]` 引用(Task 4)✓;grounding 合并集(Task 6)✓;flag+门控+等价(Task 4 + 收尾)✓;eval(收尾)✓。**未覆盖的 spec 项**:KG 源 chunk 并入 chunk 池(§5.2 local/global"关联 chunk")—— v1 先注入 KG 结构(实体/关系子图);**KG 源 chunk round-robin 并池标为 v1.1**(evidence→chunk 映射较重,先验证结构注入收益再加),已在此记为已知缺口。
- **占位符**:Task 5 的"保留现状 MMR 分支"用 `...(保留现状)` 指代既有代码(非新写),其余 code step 均完整。
- **类型一致**:`RerankClient.rerank(query, docs)->List[int]`、`_chunk_kg_overlay(nb,query,hl,id_offset)->(block,id_map)`、`_mix_answer_context(nb,query,hl,chunks)->(block,id_map[,kg_hits])`、`_select_chunks_rerank(query,cands,budget)`、`est_tokens`/`truncate_by_tokens`、`chunk_kg_overlay_enabled`/`max_*_tokens`/`rerank_*` —— 跨任务一致。
- **不变量**:rerank 分仅定序(`_select_chunks_rerank` 不写 `.relevance`);grounding/tau 用融合 `.relevance`(Task 6);flag 关 + 无 rerank → 等价;无 KG → 纯 chunk。
- **依赖顺序**:Task 6 需 `_mix_answer_context` 返回 kg_hits(Task 4 加),Task 5 需 Task 1/2。建议顺序 1→2→3→4→5→6→7。
