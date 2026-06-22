# HippoRAG-style PPR Cross-Document Retrieval (graph mode) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 `graph` 模式用 HippoRAG 式 Personalized PageRank 在「KG 节点 + chunk + 跨文档同义/同概念边」组成的图上传播,把别的文档里的相关 chunk 也召回进来,解决「对比题只引用被点名那一篇」的坍缩。

**Architecture:** 复用现有 rustworkx(`rx.pagerank` 0.17.1 原生支持 `personalization`)。新增一个纯函数模块 `app/services/kg/ppr.py` 构图 + 跑 PPR;在 `SQLiteRepository` 上加 `_ent_chunk_map`(实体↔chunk 成员映射)、`_ppr_graph`(版本缓存的无向图:relations 边 + membership 边 + 由 `concept_clusters` 共属派生的 synonym 星型边)、`_ppr_retrieve`(用 `federated_retrieve` 的 KG 种子 + chunk 向量种子构造 reset 向量 → 跑 PPR → 取 chunk 节点分数)。在 `ask_graph` 里用 `graph_ppr_enabled` 开关接入,命中时用 PPR 召回的 chunk 走 `_answer_chunks` 出 chunk 引用。守 `[0,1]/tau`:PPR 分 min-max 归一后写入 `RetrievedChunk.relevance`,再过 `classify_evidence`。

**Tech Stack:** Python 3, SQLite, rustworkx 0.17.1, pydantic Settings, pytest。

**Scope:** 仅 P1(PPR 检索主干 + 接 graph 模式,默认关)。P2(LLM fact-rerank「recognition memory」、specificity 权重、emb-KNN 补未聚类实体、communities、`variant_of` 版本边、跑 `review_pending_merges`)不在本计划。

**不变量(每个改动都要守):** ① 所有对外相关度 ∈ [0,1];PPR 原始分是概率,必须 min-max 归一再用。② 新功能默认 `False`,opt-in(对齐 `relation_retrieval_enabled` 等既有开关)。③ 纯函数零 I/O,可单测。④ 不写新边进 `knowledge_relations`(synonym/membership 边只活在内存 PPR 图里)。

---

## File Structure

- **Create** `app/services/kg/ppr.py` — 纯函数:`build_ppr_graph(...)`、`run_ppr(...)`。零 I/O,镜像 `graph_reason.py` 风格。
- **Create** `tests/test_ppr.py` — `ppr.py` 纯函数单测。
- **Create** `tests/test_ppr_retrieve.py` — repo 级 `_ent_chunk_map` / `_ppr_retrieve` / `ask_graph(graph_ppr_enabled)` 测试,含跨文档桥接「杀手测试」。
- **Modify** `app/core/config.py` — 新增 PPR 开关与参数字段。
- **Modify** `app/services/concept_merge_review.py:12-26` — 修 judge 提示词写死的 analog/RF/CMOS 旧域 bug(改为领域无关)。
- **Modify** `app/services/sqlite_repository.py` — 新增 `_ent_chunk_map`、`_ppr_graph`、`_ppr_retrieve`;在 `ask_graph` 接入开关。

---

## Task 0: PPR 配置开关

**Files:**
- Modify: `app/core/config.py`(在 `relation_seed_top_n` 附近的检索配置区追加)
- Test: `tests/test_ppr_retrieve.py`

- [ ] **Step 1: 写失败测试**

```python
# tests/test_ppr_retrieve.py
from app.core.config import Settings


def test_ppr_settings_defaults_off():
    s = Settings(_env_file=None)
    assert s.graph_ppr_enabled is False          # opt-in
    assert s.ppr_damping == 0.5                   # HippoRAG default
    assert s.ppr_passage_node_weight == 0.05      # HippoRAG default
    assert s.ppr_top_chunks == 20
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_ppr_retrieve.py::test_ppr_settings_defaults_off -v`
Expected: FAIL — `AttributeError: 'Settings' object has no attribute 'graph_ppr_enabled'`

- [ ] **Step 3: 加配置字段**

在 `app/core/config.py` 的 `relation_seed_top_n: int = Field(8, env="RELATION_SEED_TOP_N")` 之后插入:

```python
# HippoRAG 式 PPR 跨文档检索(graph 模式;默认关,opt-in)
graph_ppr_enabled: bool = Field(False, env="GRAPH_PPR_ENABLED")
ppr_damping: float = Field(0.5, env="PPR_DAMPING")               # rx.pagerank alpha
ppr_passage_node_weight: float = Field(0.05, env="PPR_PASSAGE_NODE_WEIGHT")
ppr_top_chunks: int = Field(20, env="PPR_TOP_CHUNKS")            # 最终喂答案的 chunk 数
ppr_kg_seed_top_n: int = Field(20, env="PPR_KG_SEED_TOP_N")      # reset 向量里的 KG 种子数
ppr_chunk_seed_top_n: int = Field(30, env="PPR_CHUNK_SEED_TOP_N")  # reset 向量里的 chunk 种子数
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_ppr_retrieve.py::test_ppr_settings_defaults_off -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/core/config.py backend/tests/test_ppr_retrieve.py
git commit -m "feat(ppr): add graph_ppr config flags (default off)"
```

---

## Task 1: 修 concept_merge_review 旧域提示词 bug

**Files:**
- Modify: `app/services/concept_merge_review.py:12-26`
- Test: `tests/test_ppr.py`

**背景:** judge 提示词写死「analog/RF/CMOS IC design knowledge graph」「related circuit」,对 LLM 架构语料是错域,会污染合并裁决。改成领域无关。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_ppr.py
from app.services.concept_merge_review import _prompt


def test_merge_review_prompt_is_domain_agnostic():
    p = _prompt([{"id": "c1", "score": 0.9, "canonical_a": "MoE",
                  "canonical_b": "Mixture-of-Experts"}])
    low = p.lower()
    # 旧的芯片设计字样必须清除
    assert "cmos" not in low and "rf" not in low and "circuit" not in low
    # 仍保留核心裁决语义:同概念/缩写才合并,子类/上下位保持分开
    assert "acronym" in low
    assert "merge" in low and ("keep separate" in low or "keep_separate" in low)
    # 候选名仍被带进提示词
    assert "MoE" in p and "Mixture-of-Experts" in p
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_ppr.py::test_merge_review_prompt_is_domain_agnostic -v`
Expected: FAIL — `assert "cmos" not in low`(当前提示词含 CMOS)

- [ ] **Step 3: 改提示词为领域无关**

把 `app/services/concept_merge_review.py` 的 `_prompt` 函数体里的返回串改为:

```python
    return (
        "Review candidate concept merges for a technical/scientific knowledge graph.\n"
        "Merge only when the two names denote the SAME concept, including "
        "acronym/full-name pairs (e.g. 'MoE' and 'Mixture-of-Experts') and trivial "
        "spelling/plural variants.\n"
        "Keep separate when one is a subtype, a different version/size/variant "
        "(e.g. 'V2' vs 'V3', '7B' vs '72B'), a related-but-distinct method, a "
        "parameter, a cause/effect, or a broader/narrower term.\n"
        "Return JSON only.\n\n"
        "Candidates:\n" + "\n".join(lines)
    )
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_ppr.py::test_merge_review_prompt_is_domain_agnostic -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/concept_merge_review.py backend/tests/test_ppr.py
git commit -m "fix(kg): merge-review prompt domain-agnostic + guard version/size over-merge"
```

---

## Task 2: `build_ppr_graph` 纯函数(构无向 PPR 图)

**Files:**
- Create: `app/services/kg/ppr.py`
- Test: `tests/test_ppr.py`

**设计:** 无向 `rx.PyGraph`。节点三类,用前缀 key 区分并存进同一索引:
- KG 实体:key = `object_id`(如 `ko-...`)
- chunk(passage):key = `chunk:{chunk_id}`
- 簇路由(synonym 星心,合成节点):key = `cluster:{canonical_id}`

边三类,payload `{"weight": float}`:
- relation 边(实体↔实体):weight=1.0
- membership 边(实体↔chunk):weight=1.0
- synonym 星型边(实体↔簇路由):每个簇成员连到该簇的合成路由节点,weight=1.0。PPR 经路由在同簇跨文档成员间传导(N 条边,非 N²)。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_ppr.py(追加)
import rustworkx as rx
from app.services.kg.ppr import build_ppr_graph


def test_build_ppr_graph_nodes_and_edges():
    kg_nodes = {"e1": {"type": "concept", "name": "MoE(paperA)"},
                "e2": {"type": "concept", "name": "MoE(paperB)"}}
    chunk_ids = ["cA", "cB"]
    relations = []  # no intra-doc relation needed for this minimal case
    memberships = [("e1", "cA"), ("e2", "cB")]
    cluster_groups = {"K-moe": ["e1", "e2"]}  # same concept across two docs

    G, key_to_idx, chunk_idx_to_id = build_ppr_graph(
        kg_nodes, chunk_ids, relations, memberships, cluster_groups)

    assert isinstance(G, rx.PyDiGraph)
    # 2 entity + 2 chunk + 1 cluster-router = 5 nodes
    assert G.num_nodes() == 5
    assert set(chunk_idx_to_id.values()) == {"cA", "cB"}
    # membership(2) + synonym star(2) = 4 logical edges → 8 directed (reciprocal)
    assert G.num_edges() == 8
    # cluster router connects e1 and e2 (cross-doc bridge exists)
    router = key_to_idx["cluster:K-moe"]
    assert set(G.successor_indices(router)) == {key_to_idx["e1"], key_to_idx["e2"]}


def test_build_ppr_graph_skips_dangling_and_empty_clusters():
    G, key_to_idx, chunk_idx_to_id = build_ppr_graph(
        {"e1": {"type": "concept", "name": "x"}}, ["cA"],
        relations=[{"source_object_id": "e1", "target_object_id": "MISSING"}],
        memberships=[("e1", "cA"), ("GHOST", "cA")],
        cluster_groups={"K-solo": ["e1"]})  # singleton → 1 star edge to router
    # dangling relation skipped, ghost membership skipped. 1 membership (e1↔cA)
    # + 1 star (e1↔router) = 2 logical edges → 4 directed (reciprocal).
    assert G.num_edges() == 4
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_ppr.py -k build_ppr_graph -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'app.services.kg.ppr'`

- [ ] **Step 3: 实现 `build_ppr_graph`**

新建 `app/services/kg/ppr.py`:

```python
"""rustworkx-backed in-memory graph for HippoRAG-style Personalized PageRank
cross-document retrieval. Pure functions, zero I/O — unit-testable.

Graph is a PyDiGraph with RECIPROCAL edges. rx.pagerank ONLY accepts PyDiGraph
(it rejects PyGraph); adding both directions per edge makes PPR flow
symmetrically — equivalent to HippoRAG's igraph directed=False. Three node kinds
share one index:
  - KG entity      key = object_id
  - passage(chunk) key = f"chunk:{chunk_id}"
  - cluster router key = f"cluster:{canonical_id}"  (synthetic synonym hub)
Synonym bridges are modelled as a star: every member of a concept cluster links
to the cluster's router node, so PPR mass flows between same-concept nodes that
live in different documents (N edges per cluster, not N^2).
"""
from __future__ import annotations

from typing import Dict, List, Tuple

import rustworkx as rx


def build_ppr_graph(
    kg_nodes: Dict[str, dict],
    chunk_ids: List[str],
    relations: List[dict],
    memberships: List[Tuple[str, str]],
    cluster_groups: Dict[str, List[str]],
) -> Tuple[rx.PyDiGraph, Dict[str, int], Dict[int, str]]:
    """Build the reciprocal-edge PPR digraph.

    kg_nodes       — {object_id: {"type": str, "name": str}}
    chunk_ids      — list of chunk_id strings (passage nodes)
    relations      — [{"source_object_id","target_object_id", ...}, ...] (KG↔KG)
    memberships    — [(object_id, chunk_id), ...] (KG↔chunk)
    cluster_groups — {canonical_id: [object_id, ...]} (synonym bridges)

    Returns (G, key_to_idx, chunk_idx_to_id):
      key_to_idx      — {node_key: vertex_idx} (object_id / chunk:* / cluster:*)
      chunk_idx_to_id — {vertex_idx: chunk_id} for passage nodes only
    """
    G: rx.PyDiGraph = rx.PyDiGraph()
    key_to_idx: Dict[str, int] = {}
    chunk_idx_to_id: Dict[int, str] = {}

    def _add(key: str, payload: dict) -> int:
        idx = key_to_idx.get(key)
        if idx is None:
            idx = G.add_node(payload)
            key_to_idx[key] = idx
        return idx

    for oid, meta in kg_nodes.items():
        _add(oid, {"kind": "entity", "object_id": oid,
                   "object_type": meta.get("type", ""), "name": meta.get("name", "")})

    for cid in chunk_ids:
        idx = _add(f"chunk:{cid}", {"kind": "chunk", "chunk_id": cid})
        chunk_idx_to_id[idx] = cid

    seen_pairs: set = set()

    def _edge(a: int, b: int, weight: float) -> None:
        # Add BOTH directions (rx.pagerank needs a PyDiGraph; reciprocal edges
        # make traversal symmetric). Dedup on the unordered pair so one logical
        # undirected edge yields exactly two directed edges.
        if a == b:
            return
        k = (a, b) if a < b else (b, a)
        if k in seen_pairs:
            return
        seen_pairs.add(k)
        G.add_edge(a, b, {"weight": weight})
        G.add_edge(b, a, {"weight": weight})

    for rel in relations:
        a = key_to_idx.get(rel["source_object_id"])
        b = key_to_idx.get(rel["target_object_id"])
        if a is None or b is None:
            continue  # dangling
        _edge(a, b, 1.0)

    for oid, cid in memberships:
        a = key_to_idx.get(oid)
        b = key_to_idx.get(f"chunk:{cid}")
        if a is None or b is None:
            continue
        _edge(a, b, 1.0)

    for canonical_id, members in cluster_groups.items():
        present = [key_to_idx[o] for o in members if o in key_to_idx]
        if not present:
            continue
        router = _add(f"cluster:{canonical_id}", {"kind": "cluster",
                                                  "canonical_id": canonical_id})
        for m in present:
            _edge(router, m, 1.0)

    return G, key_to_idx, chunk_idx_to_id
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_ppr.py -k build_ppr_graph -v`
Expected: PASS (2 passed)

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/kg/ppr.py backend/tests/test_ppr.py
git commit -m "feat(ppr): build_ppr_graph — undirected KG+chunk+synonym-star graph"
```

---

## Task 3: `run_ppr` 纯函数(跑 PPR + 取 chunk 分数,归一)

**Files:**
- Modify: `app/services/kg/ppr.py`
- Test: `tests/test_ppr.py`

- [ ] **Step 1: 写失败测试(含跨文档传导断言)**

```python
# tests/test_ppr.py(追加)
from app.services.kg.ppr import run_ppr


def test_run_ppr_bridges_across_documents():
    # paperA: e1(MoE)--cA ; paperB: e2(MoE)--cB ; e1,e2 同簇(桥)。
    # paperC: e3(unrelated)--cC,不在任何簇 → 与种子无通路(对照组)。
    kg_nodes = {"e1": {"type": "concept", "name": "MoE"},
                "e2": {"type": "concept", "name": "MoE"},
                "e3": {"type": "concept", "name": "Unrelated"}}
    G, key_to_idx, chunk_idx_to_id = build_ppr_graph(
        kg_nodes, ["cA", "cB", "cC"], [],
        [("e1", "cA"), ("e2", "cB"), ("e3", "cC")],
        {"K-moe": ["e1", "e2"]})

    # 只把全部初始概率放在 paperA 的实体 e1 上(模拟 query 只命中 A 篇)
    reset = {key_to_idx["e1"]: 1.0}
    ranked = run_ppr(G, chunk_idx_to_id, reset, damping=0.5)

    # 返回 [(chunk_id, normalized_score)],降序,分数 ∈ [0,1]
    assert all(0.0 <= s <= 1.0 for _, s in ranked)
    score = dict(ranked)
    # 关键:cB(别的文档,经同簇桥接)概率 > cC(无通路对照组)
    assert score["cB"] > score["cC"]
    assert score["cB"] > 0.0          # 桥接成功:跨文档 chunk 拿到正概率
    assert score["cA"] >= score["cB"]  # 被点名那篇仍最高(更近种子)
    assert ranked[0][0] == "cA"        # 降序,cA 居首


def test_run_ppr_empty_reset_returns_empty():
    G, key_to_idx, chunk_idx_to_id = build_ppr_graph(
        {"e1": {"type": "concept", "name": "x"}}, ["cA"], [],
        [("e1", "cA")], {})
    assert run_ppr(G, chunk_idx_to_id, {}, damping=0.5) == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_ppr.py -k run_ppr -v`
Expected: FAIL — `ImportError: cannot import name 'run_ppr'`

- [ ] **Step 3: 实现 `run_ppr`**

在 `app/services/kg/ppr.py` 末尾追加:

```python
def run_ppr(
    G: rx.PyGraph,
    chunk_idx_to_id: Dict[int, str],
    reset: Dict[int, float],
    damping: float = 0.5,
) -> List[Tuple[str, float]]:
    """Run Personalized PageRank and return chunk rankings.

    reset   — {vertex_idx: weight} personalization vector (≥1 non-zero entry).
    Returns [(chunk_id, normalized_score), ...] sorted desc; scores min-max
    normalized into [0,1] so they satisfy the relevance/tau invariant. Empty
    reset (or no non-zero weight) → [] (caller falls back to dense retrieval).
    """
    if not reset or not any(w > 0 for w in reset.values()) or G.num_nodes() == 0:
        return []
    scores = rx.pagerank(
        G,
        alpha=damping,
        personalization={int(k): float(v) for k, v in reset.items() if v > 0},
        weight_fn=lambda payload: float(payload.get("weight", 1.0)),
    )
    raw = [(cid, float(scores[idx])) for idx, cid in chunk_idx_to_id.items()]
    if not raw:
        return []
    vals = [s for _, s in raw]
    lo, hi = min(vals), max(vals)
    span = hi - lo
    norm = [(cid, (s - lo) / span if span > 0 else 0.0) for cid, s in raw]
    norm.sort(key=lambda x: x[1], reverse=True)
    return norm
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_ppr.py -k run_ppr -v`
Expected: PASS (2 passed)

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/kg/ppr.py backend/tests/test_ppr.py
git commit -m "feat(ppr): run_ppr — rx.pagerank personalization + min-max normalized chunk scores"
```

---

## Task 4: `_ent_chunk_map` 实体↔chunk 成员映射

**Files:**
- Modify: `app/services/sqlite_repository.py`(紧邻 `_kg_source_chunks`,约 5346 之后)
- Test: `tests/test_ppr_retrieve.py`

**复用既有 join 口径**(`_kg_source_chunks`):`knowledge_objects.evidence[].element_id` ∈ `chunks.element_ids[]`。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_ppr_retrieve.py(追加)
import json
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
    r = SQLiteRepository(Settings(_env_file=None))
    r.embedder = FakeEmbedder(dim=16)
    return r


def _seed_two_doc_moe(repo):
    """Two sources, each with an MoE concept node clustered together, each
    node's evidence pointing at a chunk in its own source."""
    nb = repo.create_notebook(NotebookCreate(name="kb"))
    with repo._write() as db:
        now = "2026-06-22T00:00:00"
        for sid, title in [("src-A", "DeepSeek paper"), ("src-B", "GLM paper")]:
            db.execute("INSERT INTO sources (id,notebook_id,title,kind,status,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (sid, nb.id, title, "md", "ready", now, now))
        # chunks: cA in src-A (element elA), cB in src-B (element elB)
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   ("cA", nb.id, "src-A", "DeepSeek-V3 uses a Mixture-of-Experts (MoE) architecture.",
                    "Arch", json.dumps(["elA"]), now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   ("cB", nb.id, "src-B", "GLM-4.5 is a Mixture-of-Experts (MoE) model.",
                    "Arch", json.dumps(["elB"]), now))
        # entity nodes e1(src-A), e2(src-B), each evidence → its chunk's element
        for oid, sid, el in [("e1", "src-A", "elA"), ("e2", "src-B", "elB")]:
            ev = json.dumps([{"source_id": sid, "source_title": "", "element_id": el,
                              "element_type": "paragraph", "location_label": "p1",
                              "quoted_span": "MoE", "confidence": 1.0}])
            db.execute("INSERT INTO knowledge_objects "
                       "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (oid, nb.id, "concept", "approved", "",
                        json.dumps({"name": "Mixture-of-Experts (MoE)"}), ev, sid, now, now))
        # concept cluster: e1,e2 share canonical K-moe (the cross-doc bridge)
        for oid in ("e1", "e2"):
            db.execute("INSERT INTO concept_clusters "
                       "(id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,created_at) "
                       "VALUES (?,?,?,?,?,?,?)",
                       (f"cl-{oid}", nb.id, "K-moe", oid, "Mixture-of-Experts (MoE)", "concept", now))
    return nb


def test_ent_chunk_map(repo):
    nb = _seed_two_doc_moe(repo)
    m = repo._ent_chunk_map(nb.id)
    assert m["e1"] == {"cA"}
    assert m["e2"] == {"cB"}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_ppr_retrieve.py::test_ent_chunk_map -v`
Expected: FAIL — `AttributeError: 'SQLiteRepository' object has no attribute '_ent_chunk_map'`

- [ ] **Step 3: 实现 `_ent_chunk_map`**

在 `app/services/sqlite_repository.py` 的 `_kg_source_chunks` 方法之后插入:

```python
    def _ent_chunk_map(self, notebook_id: str) -> Dict[str, set]:
        """{object_id: set(chunk_id)} — KG 实体出现在哪些 chunk 里。
        口径同 _kg_source_chunks:evidence[].element_id ∈ chunks.element_ids[]。
        用于 PPR 的 membership 边 + (P2) specificity 权重分母。"""
        with self._connect() as db:
            obj_rows = db.execute(
                "SELECT id, evidence FROM knowledge_objects WHERE notebook_id=?",
                (notebook_id,),
            ).fetchall()
            chunk_rows = db.execute(
                "SELECT id, element_ids FROM chunks WHERE notebook_id=?",
                (notebook_id,),
            ).fetchall()
        # element_id -> {chunk_id}
        elem_to_chunks: Dict[str, set] = {}
        for cr in chunk_rows:
            for el in json.loads(cr["element_ids"] or "[]"):
                elem_to_chunks.setdefault(el, set()).add(cr["id"])
        out: Dict[str, set] = {}
        for orow in obj_rows:
            chunks: set = set()
            for e in json.loads(orow["evidence"] or "[]"):
                if isinstance(e, dict) and e.get("element_id"):
                    chunks |= elem_to_chunks.get(e["element_id"], set())
            if chunks:
                out[orow["id"]] = chunks
        return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_ppr_retrieve.py::test_ent_chunk_map -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_ppr_retrieve.py
git commit -m "feat(ppr): _ent_chunk_map (entity↔chunk membership via evidence∩element_ids)"
```

---

## Task 5: `_ppr_graph` 版本缓存的图装配

**Files:**
- Modify: `app/services/sqlite_repository.py`(紧邻 `_federated_rx_graph` 之后)
- Test: `tests/test_ppr_retrieve.py`

**装配:** active notebook 的 KG 节点 + chunk + relations + membership(`_ent_chunk_map`)+ synonym 组(`concept_clusters` 按 canonical_id 聚合),调 `build_ppr_graph`,经 `_vector_cache` 版本键缓存(对齐 `_federated_rx_graph`)。P1 只取 active notebook(联邦留 P2)。

- [ ] **Step 1: 写失败测试**

```python
# tests/test_ppr_retrieve.py(追加)
def test_ppr_graph_has_cross_doc_bridge(repo):
    nb = _seed_two_doc_moe(repo)
    G, key_to_idx, chunk_idx_to_id = repo._ppr_graph(nb.id)
    # 两个实体 + 两个 chunk + 一个簇路由
    assert set(chunk_idx_to_id.values()) == {"cA", "cB"}
    assert "cluster:K-moe" in key_to_idx
    router = key_to_idx["cluster:K-moe"]
    assert set(G.successor_indices(router)) == {key_to_idx["e1"], key_to_idx["e2"]}
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_ppr_retrieve.py::test_ppr_graph_has_cross_doc_bridge -v`
Expected: FAIL — `AttributeError: ... '_ppr_graph'`

- [ ] **Step 3: 实现 `_ppr_graph`**

在 `_federated_rx_graph` 之后插入:

```python
    def _ppr_graph(self, notebook_id: str):
        """Build (and version-cache) the undirected PPR graph for `notebook_id`:
        KG nodes + chunk nodes + relation/membership/synonym edges. Synonym
        groups come from concept_clusters (members of one canonical_id). P1 是单
        notebook(联邦留 P2)。返回 (G, key_to_idx, chunk_idx_to_id)。"""
        from app.services.kg.ppr import build_ppr_graph
        with self._connect() as db:
            rel_ver = db.execute(
                "SELECT COUNT(*) AS c, COALESCE(MAX(created_at),'') AS ts "
                "FROM knowledge_relations WHERE notebook_id=?", (notebook_id,)).fetchone()
            obj_ver = db.execute(
                "SELECT COUNT(*) AS c, COALESCE(MAX(updated_at),'') AS ts "
                "FROM knowledge_objects WHERE notebook_id=?", (notebook_id,)).fetchone()
            chunk_ver = db.execute(
                "SELECT COUNT(*) AS c, COALESCE(MAX(created_at),'') AS ts "
                "FROM chunks WHERE notebook_id=?", (notebook_id,)).fetchone()
            clu_ver = db.execute(
                "SELECT COUNT(*) AS c, COALESCE(MAX(created_at),'') AS ts "
                "FROM concept_clusters WHERE notebook_id=?", (notebook_id,)).fetchone()
        version = ("ppr_graph", obj_ver["c"], obj_ver["ts"], rel_ver["c"], rel_ver["ts"],
                   chunk_ver["c"], chunk_ver["ts"], clu_ver["c"], clu_ver["ts"])

        def _load():
            ph = ",".join("?" for _ in USABLE_STATUSES)
            with self._connect() as db:
                obj_rows = db.execute(
                    f"SELECT id, object_type, payload FROM knowledge_objects "
                    f"WHERE notebook_id=? AND status IN ({ph})",
                    (notebook_id, *USABLE_STATUSES)).fetchall()
                rel_rows = db.execute(
                    "SELECT source_object_id, target_object_id FROM knowledge_relations "
                    "WHERE notebook_id=?", (notebook_id,)).fetchall()
                chunk_rows = db.execute(
                    "SELECT id FROM chunks WHERE notebook_id=?", (notebook_id,)).fetchall()
                clu_rows = db.execute(
                    "SELECT canonical_id, member_object_id FROM concept_clusters "
                    "WHERE notebook_id=?", (notebook_id,)).fetchall()
            kg_nodes = {r["id"]: {"type": r["object_type"],
                                  "name": json.loads(r["payload"] or "{}").get("name", "")}
                        for r in obj_rows}
            chunk_ids = [r["id"] for r in chunk_rows]
            relations = [dict(r) for r in rel_rows]
            memberships = [(oid, cid)
                           for oid, cids in self._ent_chunk_map(notebook_id).items()
                           for cid in cids]
            cluster_groups: Dict[str, list] = {}
            for r in clu_rows:
                cluster_groups.setdefault(r["canonical_id"], []).append(r["member_object_id"])
            return build_ppr_graph(kg_nodes, chunk_ids, relations, memberships, cluster_groups)

        return self._vector_cache.get(f"{notebook_id}:ppr_graph", version, _load)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_ppr_retrieve.py::test_ppr_graph_has_cross_doc_bridge -v`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_ppr_retrieve.py
git commit -m "feat(ppr): _ppr_graph — version-cached KG+chunk+synonym graph assembly"
```

---

## Task 6: `_ppr_retrieve` —— reset 向量 + PPR → 排序 chunk(含杀手测试)

**Files:**
- Modify: `app/services/sqlite_repository.py`(紧邻 `_ppr_graph` 之后)
- Test: `tests/test_ppr_retrieve.py`

**reset 向量:** KG 种子来自 `federated_retrieve(notebook_id, question)[:ppr_kg_seed_top_n]`,权重=其 `.relevance`,落到对应实体节点;chunk 种子来自 `_retrieve_chunks(notebook_id, question)`(已有,返回 `(scored, ids, mat)`)取前 `ppr_chunk_seed_top_n`,权重=`relevance × ppr_passage_node_weight`,落到 `chunk:{id}` 节点。跑 `run_ppr`,返回前 `ppr_top_chunks` 的 `RetrievedChunk`(`relevance` = 归一 PPR 分)。

- [ ] **Step 1: 写失败测试(杀手测试:跨文档桥接)**

```python
# tests/test_ppr_retrieve.py(追加)
def test_ppr_retrieve_surfaces_other_document(repo):
    """问 DeepSeek 的 MoE,PPR 应经同概念簇把 GLM 那篇的 chunk(cB)也召回。"""
    nb = _seed_two_doc_moe(repo)
    chunks = repo._ppr_retrieve(nb.id, "DeepSeek-V3 Mixture-of-Experts architecture")
    ids = [c.chunk_id for c in chunks]
    assert "cA" in ids                     # 被点名那篇
    assert "cB" in ids                     # 关键:别的文档也进来了(桥接成功)
    assert all(0.0 <= c.relevance <= 1.0 for c in chunks)   # 守 [0,1]


def test_ppr_retrieve_empty_when_no_kg(repo):
    nb = repo.create_notebook(NotebookCreate(name="empty"))
    assert repo._ppr_retrieve(nb.id, "anything") == []
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_ppr_retrieve.py -k ppr_retrieve -v`
Expected: FAIL — `AttributeError: ... '_ppr_retrieve'`

- [ ] **Step 3: 实现 `_ppr_retrieve`**

在 `_ppr_graph` 之后插入:

```python
    def _ppr_retrieve(self, notebook_id: str, question: str) -> List["RetrievedChunk"]:
        """HippoRAG 式 PPR 检索:KG 种子 + chunk 种子 → reset 向量 → rx PPR →
        取 chunk 节点分数。返回前 ppr_top_chunks 的 RetrievedChunk(relevance=
        归一 PPR 分,守 [0,1])。无 KG/无 chunk 时返回 []。"""
        from app.services.kg.ppr import run_ppr
        G, key_to_idx, chunk_idx_to_id = self._ppr_graph(notebook_id)
        if G.num_nodes() == 0 or not chunk_idx_to_id:
            return []

        reset: Dict[int, float] = {}
        # KG 种子:federated_retrieve 的 relevance 落到实体节点
        kg_hits = self.federated_retrieve(notebook_id, question)[: self.settings.ppr_kg_seed_top_n]
        for h in kg_hits:
            idx = key_to_idx.get(h.object_id)
            if idx is not None and h.relevance > 0:
                reset[idx] = reset.get(idx, 0.0) + float(h.relevance)
        # chunk 种子:dense chunk 检索分 × passage_node_weight,落到 chunk 节点
        scored, _ids, _mat = self._retrieve_chunks(notebook_id, question)
        pw = self.settings.ppr_passage_node_weight
        for c in scored[: self.settings.ppr_chunk_seed_top_n]:
            idx = key_to_idx.get(f"chunk:{c.chunk_id}")
            if idx is not None and c.relevance > 0:
                reset[idx] = reset.get(idx, 0.0) + float(c.relevance) * pw
        if not reset:
            return []

        ranked = run_ppr(G, chunk_idx_to_id, reset, damping=self.settings.ppr_damping)
        ranked = ranked[: self.settings.ppr_top_chunks]
        if not ranked:
            return []

        # 取 chunk 详情,组装 RetrievedChunk(relevance = 归一 PPR 分)
        score_map = dict(ranked)
        with self._connect() as db:
            ph = ",".join("?" for _ in score_map)
            rows = db.execute(
                f"SELECT c.id, c.source_id, c.text, c.section_path, c.element_ids, "
                f"s.title AS source_title FROM chunks c JOIN sources s ON s.id=c.source_id "
                f"WHERE c.id IN ({ph})", list(score_map)).fetchall()
        from app.services.retrieval import RetrievedChunk
        out = [RetrievedChunk(
            chunk_id=r["id"], source_id=r["source_id"], source_title=r["source_title"],
            section_path=r["section_path"], text=r["text"],
            element_ids=json.loads(r["element_ids"] or "[]"),
            relevance=score_map[r["id"]]) for r in rows]
        out.sort(key=lambda c: c.relevance, reverse=True)
        return out
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_ppr_retrieve.py -k ppr_retrieve -v`
Expected: PASS (2 passed) — 尤其 `cB` 被召回,证明跨文档桥接生效

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_ppr_retrieve.py
git commit -m "feat(ppr): _ppr_retrieve — reset vector + PPR → cross-doc ranked chunks"
```

---

## Task 7: 接入 `ask_graph`(`graph_ppr_enabled` 开关)

**Files:**
- Modify: `app/services/sqlite_repository.py`(`ask_graph` 内,`top_hits` 求出后、构图 BFS 之前)
- Test: `tests/test_ppr_retrieve.py`

**接法:** `ask_graph` 拿到 `top_hits` 后,若 `graph_ppr_enabled` 且 `_ppr_retrieve` 有结果,则改走「PPR chunk → `_answer_chunks` → chunk 引用」分支,`mode` 仍为 `graph`,`reasoning_trace` 记一条 `ppr` 步;否则保持现有 BFS 行为不变(零回归)。

- [ ] **Step 1: 写失败测试(flag 开关 + 引用跨文档)**

```python
# tests/test_ppr_retrieve.py(追加)
class _StubAnswerLLM:
    configured = True
    def chat_json(self, *a, **k):
        # 引用两个 chunk 锚点(k1,k2);_answer_chunks 的 id_map 即 selected chunks
        return '{"answer": "DeepSeek 与 GLM 都用 MoE [k1][k2].", "grounded": true}'


def test_ask_graph_ppr_cites_multiple_documents(repo, monkeypatch):
    nb = _seed_two_doc_moe(repo)
    monkeypatch.setattr(repo.settings, "graph_ppr_enabled", True)
    repo.llm_client = _StubAnswerLLM()
    repo.reasoning_llm_client = _StubAnswerLLM()
    from app.models.schemas import AskRequest
    resp = repo.ask_graph(nb.id, AskRequest(question="DeepSeek-V3 MoE 相比其他模型", mode="graph"))
    assert resp.mode == "graph"
    src_ids = {c.source_id for c in resp.citations}
    assert "src-A" in src_ids and "src-B" in src_ids   # 引用跨两篇文档


def test_ask_graph_ppr_off_keeps_kg_path(repo, monkeypatch):
    nb = _seed_two_doc_moe(repo)
    monkeypatch.setattr(repo.settings, "graph_ppr_enabled", False)
    from app.models.schemas import AskRequest
    resp = repo.ask_graph(nb.id, AskRequest(question="MoE", mode="graph"))
    assert resp.mode == "graph"   # 旧路径不抛错(无 LLM → deterministic 兜底)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_ppr_retrieve.py -k ask_graph_ppr -v`
Expected: FAIL — `test_ask_graph_ppr_cites_multiple_documents` 失败(citations 为空,PPR 分支未接)

- [ ] **Step 3: 在 `ask_graph` 接入 PPR 分支**

在 `ask_graph` 中,`base_seeds = seed_ids if seed_ids else [h.object_id for h in top_hits[:5]]` 这一行**之前**,插入以下分支(`top_hits` 已求出、空候选已早返回):

```python
        # HippoRAG 式 PPR 跨文档检索(opt-in)。命中即走 chunk 答案路径:PPR 把
        # 别的文档相关 chunk 也召回,_answer_chunks 出 chunk 引用(跨多篇)。
        if self.settings.graph_ppr_enabled:
            ppr_chunks = self._ppr_retrieve(notebook_id, question)
            if ppr_chunks:
                answer, llm_grounded, anchors = "", False, []
                if getattr(self.llm_client, "configured", False):
                    try:
                        answer, llm_grounded, anchors = self._answer_chunks(
                            question, ppr_chunks, history)
                    except Exception as exc:
                        self._note_model_error("answer", self.settings.openai_compat_model, exc)
                        answer, llm_grounded, anchors = "", False, []
                citations: List[Citation] = []
                by_id = {c.chunk_id: c for c in ppr_chunks}
                for a in anchors:
                    if a.object_type == "chunk" and a.object_id in by_id:
                        c = by_id[a.object_id]
                        eid = c.element_ids[0] if c.element_ids else ""
                        citations.append(Citation(
                            label=f"{c.source_title} · {c.section_path}".strip(" ·"),
                            source_id=c.source_id, element_id=eid,
                            location_label=c.section_path, quoted_span=c.text[:200]))
                evidence_level, top_relevance = classify_evidence(
                    ppr_chunks, anchors, llm_grounded,
                    self.settings.evidence_tau_low, self.settings.evidence_tau_high)
                grounded = evidence_level == "grounded"
                if answer:
                    conclusion = _MARKER_RE.sub("", answer).strip()
                    llm_mode = "grounded" if grounded else "ungrounded"
                else:
                    conclusion = f"PPR retrieved {len(ppr_chunks)} cross-document passage(s)."
                    llm_mode = "deterministic"
                from app.models.schemas import TraceStep
                resp = AskResponse(
                    answer_id="", conclusion=conclusion, answer=answer, grounded=grounded,
                    evidence_level=evidence_level, anchors=anchors, related_knowledge=[],
                    citations=citations, llm_mode=llm_mode, conversation_id=conversation_id,
                    retrieval_query=question, top_relevance=top_relevance,
                    reasoning_trace=[TraceStep(step_type="ppr",
                        summary=f"PPR 跨文档召回 {len(ppr_chunks)} 个 chunk",
                        detail={"chunks": len(ppr_chunks),
                                "sources": len({c.source_id for c in ppr_chunks})})])
                resp.mode = "graph"
                resp.model_errors = [ModelError(**e) for e in _err_sink]
                resp.answer_id = self._save_answer(notebook_id, question, resp, conversation_id)
                return resp
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_ppr_retrieve.py -k ask_graph_ppr -v`
Expected: PASS (2 passed)

- [ ] **Step 5: 跑全量 PPR 测试 + 回归**

Run: `cd backend && python -m pytest tests/test_ppr.py tests/test_ppr_retrieve.py -v && python -m pytest tests/ -q`
Expected: PPR 全绿;既有用例无回归(`graph_ppr_enabled` 默认 False,旧路径不变)。

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_ppr_retrieve.py
git commit -m "feat(ppr): wire PPR retrieval into graph mode behind graph_ppr_enabled"
```

---

## 收尾(P1 完成后)

- [ ] 全量测试:`cd backend && python -m pytest tests/ -q`
- [ ] rebase 到 master 保持线性,push,`gh pr create --base master`(默认关,PR 描述写明 opt-in + 真机 recall 待对照)。
- [ ] 真机验证(需重启后端加载新代码 + `.env` 设 `GRAPH_PPR_ENABLED=true`,由用户决定何时重启):在 nb-b37185f4ae 用 graph 模式问「deepseek v3 相比其他 llm 结构独特在哪」,确认引用跨多篇(出现 Mamba/Qwen/GLM 等而非全 DeepSeek)。

## P2(后续,不在本计划)

- LLM fact-rerank(recognition memory)做 reset 向量前的事实过滤(每查一次 LLM)。
- specificity 权重:KG 种子权重 ÷ `len(_ent_chunk_map[oid])`(抑制 Transformer 等大众概念霸权)。
- emb-KNN(over `knowledge_embeddings`,cosine≥0.8)补 `concept_clusters` 没覆盖的同义实体 synonym 边。
- `variant_of` 低权重版本边(V2/V3、7B/72B);跑 `review_pending_merges`(已修域提示词)提升簇质量。
- 联邦:`_ppr_graph` 纳入 base-tier notebook(对齐 `_federated_rx_graph`)。
- communities(GraphRAG Leiden)支撑「全领域横向对比」。
```
