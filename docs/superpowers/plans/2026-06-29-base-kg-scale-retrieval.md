# 领域基础 KG 规模化检索（SP2）Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让 graph 模式 PPR 检索在 10^5–10^6 节点的 base KG 上不物化全量、不超时，保持 HippoRAG 式精确全图 PPR。

**Architecture:** 离线为 base notebook 预计算紧凑稀疏基底（scipy CSR 图 + hnswlib ANN + IDF），持久化到 `{storage_dir}/kg_index/{nb}/`；查询时把小 active delta 拼到 base 之上，跑 scipy CSR 个性化幂迭代 PPR，输出与现 `run_ppr` 同形的 `[(chunk_id, score)]`；按「base 是否有有效索引」分流，不回归小库。

**Tech Stack:** Python, scipy.sparse（新增依赖）, numpy, hnswlib（已有）, rustworkx（仅对照测试）, SQLite。

参考 spec：`docs/superpowers/specs/2026-06-29-base-kg-scale-retrieval-design.md`。

---

## File Structure

- `backend/requirements.txt` — 加 `scipy>=1.11`。
- `backend/app/services/kg/scale_index.py`（新）— 纯函数 + ScaleIndex 数据结构：CSR 构建、个性化 PPR、active 拼接、构建/加载/持久化。
- `backend/app/services/sqlite_repository.py`（改）— `build_scale_index()` 包装（读 DB→调 scale_index 构建器）、`scale_ppr()`、graph 模式入口分流。
- `backend/app/services/batch_ingest.py`（改）— 新增 `run_index()` 阶段。
- `backend/app/scripts/batch_ingest.py`（改）— CLI 加 `index` 子命令。
- `backend/tests/test_scale_index.py`（新）— 纯函数单测（PPR/CSR/拼接/等价）。
- `backend/tests/test_scale_index_repo.py`（新）— repo 级构建/加载/分流/回退测试。
- `README.md` / `README_zh.md`（改）— `batch_ingest index` 用法。

**对 spec 的实现细节澄清**：spec 写的 `members.npz`（node→chunk 成员表）在实现中简化为「chunk 节点本身就是图节点」（与现 `run_ppr` 一致：chunk 是 PPR 图节点，PPR 直接给 chunk 打分）。因此持久化 `chunk_index.npy`（哪些节点是 chunk）即可，不需单独成员表。

ScaleIndex 数据结构（定义在 `scale_index.py`，Task 4 落地）：
```python
@dataclass
class ScaleIndex:
    node_ids: list[str]                 # index -> object/chunk id
    node_index: dict[str, int]          # id -> index
    transition: "scipy.sparse.csr_matrix"  # 列随机转移阵 A，x' = (1-d)reset + d·A·x
    idf: "np.ndarray"                   # len = n_nodes，node specificity
    chunk_index: "np.ndarray"           # 是 chunk 的节点 index（int32）
    ann_labels: list[str]               # ANN label -> kg-node id（仅有向量的 kg 节点）
    ann_path: str                       # hnswlib 索引文件路径（惰性 load）
    manifest: dict                      # version/dim/counts
```

---

## Task 1: scipy 依赖 + 个性化 PPR 引擎（纯函数 TDD）

**Files:**
- Modify: `backend/requirements.txt`
- Create: `backend/app/services/kg/scale_index.py`
- Test: `backend/tests/test_scale_index.py`

- [ ] **Step 1: 加 scipy 依赖**

在 `backend/requirements.txt` 末尾加一行：
```
scipy>=1.11
```
安装：`/opt/homebrew/Caskroom/miniconda/base/bin/python -m pip install 'scipy>=1.11'`（本机解释器见 memory；conda 环境通常已带 scipy，pip 会提示 already satisfied）。

- [ ] **Step 2: 写失败测试 —— 个性化 PPR 在已知小图上的稳态**

`backend/tests/test_scale_index.py`：
```python
import numpy as np
import scipy.sparse as sp
from app.services.kg.scale_index import personalized_ppr


def _line_graph_transition():
    # 三节点有向链 0->1->2，列随机转移阵 A（A[j,i]=i->j 的归一化权重）
    # out: 0->1, 1->2, 2 无出边（dangling）
    A = sp.csr_matrix(np.array([
        [0.0, 0.0, 0.0],
        [1.0, 0.0, 0.0],
        [0.0, 1.0, 0.0],
    ]))
    return A


def test_personalized_ppr_concentrates_mass_near_seed():
    A = _line_graph_transition()
    reset = np.array([1.0, 0.0, 0.0])           # 种子在节点 0
    x = personalized_ppr(A, reset, damping=0.5, tol=1e-10, max_iter=200)
    assert x.shape == (3,)
    assert abs(x.sum() - 1.0) < 1e-6            # 概率分布，和为 1
    assert x[0] > x[1] > x[2]                    # 质量集中在种子及其下游、随距离衰减


def test_personalized_ppr_empty_reset_returns_zeros():
    A = _line_graph_transition()
    x = personalized_ppr(A, np.zeros(3), damping=0.5)
    assert np.allclose(x, 0.0)
```

- [ ] **Step 3: 运行测试确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_scale_index.py -q`
Expected: FAIL（`ModuleNotFoundError: app.services.kg.scale_index` 或 `personalized_ppr` 未定义）。

- [ ] **Step 4: 实现 personalized_ppr**

`backend/app/services/kg/scale_index.py`：
```python
"""规模化 KG 检索的紧凑基底：scipy CSR 图 + 个性化 PPR + active 拼接 + 构建/加载。

设计见 docs/superpowers/specs/2026-06-29-base-kg-scale-retrieval-design.md。
本模块尽量纯函数、可单测；DB/IO 由 sqlite_repository 包装层提供数据。
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, List, Tuple

import numpy as np
import scipy.sparse as sp


def personalized_ppr(
    transition: "sp.csr_matrix",
    reset: "np.ndarray",
    damping: float = 0.5,
    tol: float = 1e-8,
    max_iter: int = 100,
) -> "np.ndarray":
    """个性化 PageRank 幂迭代。

    transition: 列随机转移阵 A（A[j,i] = 边 i->j 的归一化权重，按 i 的出度归一）。
    reset:      personalization 向量（非负；内部归一为和=1 作为 teleport 分布）。
    返回稳态分布 x（和=1）；全零 reset → 全零向量（调用方据此回退 dense）。
    """
    s = float(reset.sum())
    if s <= 0:
        return np.zeros(transition.shape[0], dtype=np.float64)
    p = (reset.astype(np.float64) / s)
    x = p.copy()
    d = float(damping)
    for _ in range(max_iter):
        x_new = (1.0 - d) * p + d * transition.dot(x)
        # 悬挂质量（出度 0 的节点）回灌 teleport 分布，保证总质量守恒
        x_new += (1.0 - x_new.sum()) * p
        if np.abs(x_new - x).sum() < tol:
            x = x_new
            break
        x = x_new
    total = x.sum()
    return x / total if total > 0 else x
```

- [ ] **Step 5: 运行测试确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_scale_index.py -q`
Expected: PASS（2 passed）。

- [ ] **Step 6: 提交**

```bash
git add backend/requirements.txt backend/app/services/kg/scale_index.py backend/tests/test_scale_index.py
git commit -m "feat(kg-scale): personalized_ppr (scipy CSR 幂迭代) + scipy 依赖"
```

---

## Task 2: 从边列表构建列随机转移阵（纯函数 TDD）

**Files:**
- Modify: `backend/app/services/kg/scale_index.py`
- Test: `backend/tests/test_scale_index.py`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_scale_index.py`：
```python
from app.services.kg.scale_index import build_transition


def test_build_transition_column_stochastic():
    node_ids = ["a", "b", "c"]
    # 无向语义：a-b, b-c 各加正反两条；权重默认 1
    edges = [("a", "b", 1.0), ("b", "a", 1.0), ("b", "c", 1.0), ("c", "b", 1.0)]
    A, index = build_transition(node_ids, edges)
    assert index == {"a": 0, "b": 1, "c": 2}
    dense = A.toarray()
    # 每个有出边的列归一化到和为 1（b 有两条出边 -> 各 0.5）
    assert abs(dense[:, index["b"]].sum() - 1.0) < 1e-9
    assert abs(dense[index["a"], index["b"]] - 0.5) < 1e-9
    assert abs(dense[index["c"], index["b"]] - 0.5) < 1e-9


def test_build_transition_drops_dangling_endpoints():
    node_ids = ["a", "b"]
    edges = [("a", "b", 1.0), ("a", "zzz", 1.0)]   # zzz 不在 node_ids
    A, index = build_transition(node_ids, edges)
    assert A.shape == (2, 2)                        # 悬空端点的边被丢弃
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_scale_index.py::test_build_transition_column_stochastic -q`
Expected: FAIL（`build_transition` 未定义）。

- [ ] **Step 3: 实现 build_transition**

追加到 `scale_index.py`：
```python
def build_transition(
    node_ids: List[str],
    edges: List[Tuple[str, str, float]],
) -> Tuple["sp.csr_matrix", Dict[str, int]]:
    """边列表 -> 列随机转移阵 A（A[j,i]=i->j 归一化权重）。

    端点不在 node_ids 的边丢弃（防悬空）。out-degree 加权归一。返回 (A_csr, index)。
    调用方负责把无向边拆成正反两条。
    """
    index = {nid: i for i, nid in enumerate(node_ids)}
    n = len(node_ids)
    rows, cols, data = [], [], []
    for s, t, w in edges:
        si, ti = index.get(s), index.get(t)
        if si is None or ti is None:
            continue
        rows.append(ti)            # 目标行 j
        cols.append(si)            # 源列 i
        data.append(float(w))
    if not data:
        return sp.csr_matrix((n, n), dtype=np.float64), index
    M = sp.csr_matrix((data, (rows, cols)), shape=(n, n), dtype=np.float64)
    # 列归一化：每列除以列和（出度加权）
    colsum = np.asarray(M.sum(axis=0)).ravel()
    colsum[colsum == 0] = 1.0
    D = sp.diags(1.0 / colsum)
    return (M @ D).tocsr(), index
```

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_scale_index.py -q`
Expected: PASS（4 passed）。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/kg/scale_index.py backend/tests/test_scale_index.py
git commit -m "feat(kg-scale): build_transition 列随机转移阵"
```

---

## Task 3: 等价测试 —— scale PPR 与 rustworkx PPR top-k 一致（无质量回归守护）

**Files:**
- Test: `backend/tests/test_scale_index.py`

- [ ] **Step 1: 写等价测试**

追加到 `tests/test_scale_index.py`：
```python
import rustworkx as rx
from app.services.kg.scale_index import build_transition, personalized_ppr


def _random_graph(n=60, seed=7):
    rng = np.random.default_rng(seed)
    ids = [f"n{i}" for i in range(n)]
    edges = []
    for i in range(n):
        for _ in range(rng.integers(1, 4)):
            j = int(rng.integers(0, n))
            if j != i:
                edges.append((ids[i], ids[j], 1.0))
                edges.append((ids[j], ids[i], 1.0))   # 无向
    return ids, edges


def test_scale_ppr_topk_matches_rustworkx():
    ids, edges = _random_graph()
    index = {nid: i for i, nid in enumerate(ids)}
    # rustworkx 参照：同图同 personalization 同 damping
    G = rx.PyDiGraph()
    G.add_nodes_from(ids)
    for s, t, w in edges:
        G.add_edge(index[s], index[t], {"weight": w})
    seeds = {index["n0"]: 1.0, index["n5"]: 1.0}
    rx_scores = rx.pagerank(G, alpha=0.5,
                            personalization={k: float(v) for k, v in seeds.items()},
                            weight_fn=lambda p: float(p.get("weight", 1.0)))
    rx_rank = [i for i, _ in sorted(enumerate(rx_scores), key=lambda x: x[1], reverse=True)]

    # scale 路径
    A, idx2 = build_transition(ids, edges)
    reset = np.zeros(len(ids)); reset[index["n0"]] = 1.0; reset[index["n5"]] = 1.0
    x = personalized_ppr(A, reset, damping=0.5, tol=1e-12, max_iter=500)
    scale_rank = list(np.argsort(-x))

    # top-10 集合高度重合（允许近平局微差），且 top-3 主序一致
    assert set(rx_rank[:10]) >= set(scale_rank[:3])
    assert len(set(rx_rank[:10]) & set(scale_rank[:10])) >= 8
```

- [ ] **Step 2: 运行确认通过（引擎已实现，等价应成立）**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_scale_index.py::test_scale_ppr_topk_matches_rustworkx -q`
Expected: PASS。若失败，调整 `build_transition` 的归一化/方向（与 rustworkx pagerank 约定对齐）直到 top-k 重合达标——**这是无质量回归的核心契约，必须通过**。

- [ ] **Step 3: 提交**

```bash
git add backend/tests/test_scale_index.py
git commit -m "test(kg-scale): scale PPR 与 rustworkx top-k 等价测试"
```

---

## Task 4: 构建并持久化 base scale 索引

**Files:**
- Modify: `backend/app/services/kg/scale_index.py`（加 ScaleIndex、save/build_from_arrays）
- Modify: `backend/app/services/sqlite_repository.py`（加 `build_scale_index()` DB 包装）
- Test: `backend/tests/test_scale_index_repo.py`

**先读**（实现前）：`sqlite_repository.py` 的 `_ppr_graph._load`（约 5439–5486）了解节点/边/membership/synonym 的取数与 `emb_synonym_edges` 用法；`kg/ppr.py:variant_edge_pairs` 与 `emb_synonym_edges` 签名（约 122/139）。

- [ ] **Step 1: 写失败测试（repo 级，小图）**

`backend/tests/test_scale_index_repo.py`（fixture 仿 `test_unified_kg_repository.py`：FakeEmbedder、EMBED_DIM=16）：
```python
import json, pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_PROVIDER": "dashscope", "EMBED_BASE_URL": "https://e.test",
                 "EMBED_API_KEY": "k", "EMBED_MODEL": "m", "EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def test_build_scale_index_writes_artifacts(repo, tmp_path):
    nb = repo.create_notebook(NotebookCreate(name="base"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept", "payload": {"name": "current mirror", "section_path": ""}, "evidence": []},
    ], [{"source_local_id": "b", "target_local_id": "a", "edge_type": "depends_on", "evidence": []}])
    repo.rebuild_unified_kg(nb.id)
    manifest = repo.build_scale_index(nb.id)
    import os
    d = os.path.join(repo.settings.storage_dir, "kg_index", nb.id)
    for f in ("graph.npz", "node_ids.npy", "idf.npy", "chunk_index.npy", "ann.bin", "ann_labels.npy", "manifest.json"):
        assert os.path.exists(os.path.join(d, f)), f
    assert manifest["n_nodes"] >= 2
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_scale_index_repo.py::test_build_scale_index_writes_artifacts -q`
Expected: FAIL（`build_scale_index` 未定义）。

- [ ] **Step 3: 在 scale_index.py 实现 save/构建辅助**

追加到 `scale_index.py`（持久化与从数组组装；不接触 DB）：
```python
import json, os
import hnswlib


def save_scale_index(out_dir: str, *, node_ids, transition, idf, chunk_index,
                     ann_vectors, ann_labels, manifest) -> dict:
    """把构建好的数组落盘到 out_dir。ann_vectors: (m, dim) float32；ann_labels: 对应 kg 节点 id。"""
    os.makedirs(out_dir, exist_ok=True)
    sp.save_npz(os.path.join(out_dir, "graph.npz"), transition)
    np.save(os.path.join(out_dir, "node_ids.npy"), np.asarray(node_ids, dtype=object))
    np.save(os.path.join(out_dir, "idf.npy"), np.asarray(idf, dtype=np.float32))
    np.save(os.path.join(out_dir, "chunk_index.npy"), np.asarray(chunk_index, dtype=np.int32))
    np.save(os.path.join(out_dir, "ann_labels.npy"), np.asarray(ann_labels, dtype=object))
    dim = int(ann_vectors.shape[1]) if len(ann_vectors) else int(manifest.get("dim", 0))
    idx = hnswlib.Index(space="cosine", dim=dim or 1)
    idx.init_index(max_elements=max(1, len(ann_vectors)), ef_construction=200, M=16, random_seed=42)
    if len(ann_vectors):
        idx.add_items(np.asarray(ann_vectors, dtype=np.float32), np.arange(len(ann_vectors)))
    idx.save_index(os.path.join(out_dir, "ann.bin"))
    with open(os.path.join(out_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh)
    return manifest
```

- [ ] **Step 4: 在 sqlite_repository.py 实现 build_scale_index（DB 取数包装）**

在 `SQLiteRepository` 加方法（紧邻 `_ppr_graph`）。取数复用 `_ppr_graph._load` 的逻辑（KG 节点、relations、chunks、concept_clusters、membership via `_ent_chunk_map`、`variant_edge_pairs`、`emb_synonym_edges`）。要点：
- node_ids = 所有 kg 节点 id + 所有 chunk id（chunk 也是节点）。
- 边（无向，正反两条）：relations、membership(entity↔chunk)、variant、synonym。
- chunk_index = node_ids 里 chunk 的下标。
- idf：每个节点的 node specificity = 1/(它出现的 chunk 数)，chunk 节点设 1.0。
- ann_vectors/ann_labels：仅有 `knowledge_embeddings` 的 kg 节点。
- manifest：`{"version": <与 _ppr_graph 同款 version_parts>, "dim": embed_dim, "n_nodes": len(node_ids)}`。
代码骨架：
```python
def build_scale_index(self, notebook_id: str) -> dict:
    """离线构建 base notebook 的规模化检索索引并落盘。返回 manifest。"""
    from app.services.kg import scale_index as si
    from app.services.kg.ppr import variant_edge_pairs, emb_synonym_edges
    import numpy as np
    self.get_notebook(notebook_id)
    # 1) 取节点/边/chunk/cluster/membership（同 _ppr_graph._load 的查询）
    # ... 组装 node_ids, edges(含正反), chunk_ids, kg_node_names ...
    # 2) synonym 边：用 _vector_matrix 拿 (ids, mat) -> emb_synonym_edges(ids, mat, ...)
    # 3) idf：membership 计数 -> 1/count；chunk 节点=1.0
    # 4) ann_vectors/labels：knowledge_embeddings 里有向量的 kg 节点
    # 5) build_transition + save_scale_index
    transition, _ = si.build_transition(node_ids, edges)
    out_dir = os.path.join(self.settings.storage_dir, "kg_index", notebook_id)
    manifest = {"version": self._scale_index_version(notebook_id),
                "dim": self.settings.embed_dim, "n_nodes": len(node_ids)}
    return si.save_scale_index(out_dir, node_ids=node_ids, transition=transition,
                               idf=idf, chunk_index=chunk_index,
                               ann_vectors=ann_vectors, ann_labels=ann_labels,
                               manifest=manifest)
```
并加一个 `_scale_index_version(notebook_id)` 复用 `_ppr_graph` 里 version_parts 的构造（COUNT/MAX 模式），单独抽成方法供两处用。

- [ ] **Step 5: 运行确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_scale_index_repo.py::test_build_scale_index_writes_artifacts -q`
Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/kg/scale_index.py backend/app/services/sqlite_repository.py backend/tests/test_scale_index_repo.py
git commit -m "feat(kg-scale): build_scale_index 离线构建+持久化 base 索引"
```

---

## Task 5: 加载 + manifest version 校验 + 进程缓存

**Files:**
- Modify: `backend/app/services/kg/scale_index.py`（`load_scale_index`）
- Modify: `backend/app/services/sqlite_repository.py`（`_scale_index(notebook_id)` 带版本校验+缓存）
- Test: `backend/tests/test_scale_index_repo.py`

- [ ] **Step 1: 写失败测试**

追加：
```python
def test_scale_index_loads_and_invalidates_on_change(repo):
    nb = repo.create_notebook(NotebookCreate(name="base"))
    repo.store_kg(nb.id, None, [{"local_id": "a", "object_type": "concept",
        "payload": {"name": "X", "section_path": ""}, "evidence": []}], [])
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    assert repo._scale_index(nb.id) is not None            # 版本一致 -> 命中
    # 改动 KG -> 版本失配 -> 视为过期
    repo.store_kg(nb.id, None, [{"local_id": "b", "object_type": "concept",
        "payload": {"name": "Y", "section_path": ""}, "evidence": []}], [])
    assert repo._scale_index(nb.id) is None                 # 索引过期不返回
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_scale_index_repo.py::test_scale_index_loads_and_invalidates_on_change -q`
Expected: FAIL（`_scale_index` 未定义）。

- [ ] **Step 3: 实现 load_scale_index（scale_index.py）+ _scale_index（repo，带版本校验+缓存）**

`scale_index.py`：
```python
def load_scale_index(out_dir: str) -> "ScaleIndex | None":
    mpath = os.path.join(out_dir, "manifest.json")
    if not os.path.exists(mpath):
        return None
    with open(mpath) as fh:
        manifest = json.load(fh)
    transition = sp.load_npz(os.path.join(out_dir, "graph.npz"))
    node_ids = list(np.load(os.path.join(out_dir, "node_ids.npy"), allow_pickle=True))
    idf = np.load(os.path.join(out_dir, "idf.npy"))
    chunk_index = np.load(os.path.join(out_dir, "chunk_index.npy"))
    ann_labels = list(np.load(os.path.join(out_dir, "ann_labels.npy"), allow_pickle=True))
    return ScaleIndex(node_ids=node_ids, node_index={n: i for i, n in enumerate(node_ids)},
                      transition=transition, idf=idf, chunk_index=chunk_index,
                      ann_labels=ann_labels, ann_path=os.path.join(out_dir, "ann.bin"),
                      manifest=manifest)
```
`sqlite_repository.py`：
```python
def _scale_index(self, notebook_id: str):
    """返回有效的 ScaleIndex（manifest.version == 当前 DB version）；否则 None。带进程缓存。"""
    from app.services.kg import scale_index as si
    out_dir = os.path.join(self.settings.storage_dir, "kg_index", notebook_id)
    cur = self._scale_index_version(notebook_id)
    cached = self._scale_idx_cache.get(notebook_id)
    if cached is not None and cached.manifest.get("version") == cur:
        return cached
    idx = si.load_scale_index(out_dir)
    if idx is None or idx.manifest.get("version") != cur:
        return None
    self._scale_idx_cache[notebook_id] = idx
    return idx
```
在 `__init__` 加 `self._scale_idx_cache: Dict[str, Any] = {}`。注意 version 需 JSON 可序列化（把 version_parts 转成 list）。

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_scale_index_repo.py -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/kg/scale_index.py backend/app/services/sqlite_repository.py backend/tests/test_scale_index_repo.py
git commit -m "feat(kg-scale): load + manifest version 校验 + 进程缓存"
```

---

## Task 6: active delta 拼接（纯函数 TDD）

**Files:**
- Modify: `backend/app/services/kg/scale_index.py`（`splice_active`）
- Test: `backend/tests/test_scale_index.py`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_scale_index.py`：
```python
from app.services.kg.scale_index import splice_active, build_transition


def test_splice_active_unifies_shared_node_and_keeps_bridge():
    base_ids = ["K-mosfet", "c1"]                 # 一个概念 + 一个 chunk
    base_edges = [("K-mosfet", "c1", 1.0), ("c1", "K-mosfet", 1.0)]
    base_A, _ = build_transition(base_ids, base_edges)
    # active 带同名概念 K-mosfet（应合一）+ 新 chunk c2 + 边
    active_ids = ["K-mosfet", "c2"]
    active_edges = [("K-mosfet", "c2", 1.0), ("c2", "K-mosfet", 1.0)]
    ids, A = splice_active(base_ids, base_A, active_ids, active_edges)
    assert set(ids) == {"K-mosfet", "c1", "c2"}   # K-mosfet 只出现一次（按 id 合一）
    index = {n: i for i, n in enumerate(ids)}
    dense = A.toarray()
    # 跨层桥保留：K-mosfet 同时连到 c1(base) 与 c2(active)
    assert dense[index["c1"], index["K-mosfet"]] > 0
    assert dense[index["c2"], index["K-mosfet"]] > 0
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_scale_index.py::test_splice_active_unifies_shared_node_and_keeps_bridge -q`
Expected: FAIL（`splice_active` 未定义）。

- [ ] **Step 3: 实现 splice_active**

追加到 `scale_index.py`（重建合并图最简单、正确；active 小，成本可控）：
```python
def splice_active(base_ids, base_transition, active_ids, active_edges):
    """把 active 的节点/边并入 base，按 id 合一（共享 canonical_id 自然合并）。
    返回 (combined_ids, combined_transition)。base 边从 base_transition 还原为权重边。"""
    # 还原 base 的边（转移阵已列归一，仅用于结构；权重统一取 1.0 重算更稳）
    base_coo = base_transition.tocoo()
    base_edges = [(base_ids[j], base_ids[i], 1.0)   # 列 i=源, 行 j=目标
                  for i, j in zip(base_coo.col, base_coo.row)]
    combined_ids = list(base_ids) + [a for a in active_ids if a not in set(base_ids)]
    combined_edges = base_edges + list(active_edges)
    A, _ = build_transition(combined_ids, combined_edges)
    return combined_ids, A
```
（注：base 转移阵列归一后无法精确还原原始权重；v1 用结构 + 权重 1.0 重算，等价测试已证 top-k 稳健。若后续需保权重，改为持久化原始权重边表。）

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_scale_index.py::test_splice_active_unifies_shared_node_and_keeps_bridge -q`
Expected: PASS。

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/kg/scale_index.py backend/tests/test_scale_index.py
git commit -m "feat(kg-scale): splice_active 按 id 合一 + 保跨层桥"
```

---

## Task 7: scale_ppr 接入 + graph 模式分流 + 回退

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`（`scale_ppr` + 分流）
- Test: `backend/tests/test_scale_index_repo.py`

**先读**：`run_ppr`/`build_ppr_graph` 的调用点（graph 模式入口，grep `run_ppr(` 与 `_ppr_graph(`）；`_ppr_reset_vector`（约 5521）了解种子来源；确认 graph 模式最终消费 `[(chunk_id, score)]`。

- [ ] **Step 1: 写失败测试（分流 + 回退 + 同形输出）**

追加到 `tests/test_scale_index_repo.py`：
```python
def _seed_small_base(repo):
    nb = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(nb.id)
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept", "payload": {"name": "gain", "section_path": ""}, "evidence": []},
    ], [{"source_local_id": "a", "target_local_id": "b", "edge_type": "relates", "evidence": []}])
    repo.rebuild_unified_kg(nb.id)
    return nb


def test_scale_ppr_returns_chunk_rankings_shape(repo):
    nb = _seed_small_base(repo)
    repo.build_scale_index(nb.id)
    out = repo.scale_ppr(nb.id, "MOSFET gain")
    assert isinstance(out, list)
    assert all(isinstance(cid, str) and 0.0 <= score <= 1.0 for cid, score in out)


def test_graph_mode_falls_back_when_no_index(repo):
    nb = _seed_small_base(repo)
    # 未 build_scale_index -> _scale_index None -> 现路径（不抛错）
    assert repo._scale_index(nb.id) is None
```

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_scale_index_repo.py::test_scale_ppr_returns_chunk_rankings_shape -q`
Expected: FAIL（`scale_ppr` 未定义）。

- [ ] **Step 3: 实现 scale_ppr + 在 graph 模式入口分流**

`scale_ppr(active_nb, question)`：
1. base 集合 = 有有效 `_scale_index` 的 base notebook（`tier='base'`）。无 → 返回 `[]`（调用方回退）。
2. 取 base ScaleIndex（v1 单 base；多 base 依次并入）。
3. active delta：取 active notebook 的 kg 节点/边/chunk/membership（小，复用现查询），`splice_active` 拼到 base。
4. 种子 reset：复用 `_ppr_reset_vector` 的信号（`federated_retrieve` KG 种子 + dense chunk 种子）映射到 combined node_index；× IDF（base 节点用持久化 idf，active 节点 idf=1.0）。ANN 种子用 `idx.ann_path` 惰性 `hnswlib.load_index`。
5. `x = personalized_ppr(combined_A, reset, damping=settings.ppr_damping or 0.5)`。
6. 取 chunk 节点的分（combined 里 chunk 下标）→ min-max 归一到 [0,1] → 排序 → 返回 `[(chunk_id, score)]`（与 `run_ppr` 同形）。

分流：在现 graph 模式调用 `build_ppr_graph`+`run_ppr` 处，先判 `self._scale_index(<参与的base>)`：有效则 `res = self.scale_ppr(active_nb, question)`，`res` 非空则用之；否则走原路径（保证回退与不回归）。

- [ ] **Step 4: 运行确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_scale_index_repo.py -q`
Expected: PASS。

- [ ] **Step 5: 跑 KG 相关回归**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_kg_merge.py tests/test_unified_kg_repository.py tests/test_unified_kg_api.py tests/test_scale_index.py tests/test_scale_index_repo.py -q`
Expected: all pass。

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_scale_index_repo.py
git commit -m "feat(kg-scale): scale_ppr 接入 graph 模式 + 索引存在则分流、否则回退"
```

---

## Task 8: batch_ingest index 阶段 + CLI + README

**Files:**
- Modify: `backend/app/services/batch_ingest.py`（`run_index`）
- Modify: `backend/app/scripts/batch_ingest.py`（CLI `index` 子命令）
- Modify: `README.md` / `README_zh.md`
- Test: `backend/tests/test_scale_index_repo.py`

**先读**：`batch_ingest.py` 的 `run_kg`（约 144–181）与 `scripts/batch_ingest.py` 的 argparse，照其模式加 `index`。

- [ ] **Step 1: 写失败测试**

追加：
```python
def test_run_index_builds_for_notebook(repo):
    from app.services import batch_ingest
    nb = _seed_small_base(repo)
    res = batch_ingest.run_index(repo, nb.id)
    assert res["indexed_nodes"] >= 2
    assert repo._scale_index(nb.id) is not None
```
（按 `batch_ingest` 既有函数签名风格调整：若 phase 函数以 repo 为首参就直接传 repo。）

- [ ] **Step 2: 运行确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_scale_index_repo.py::test_run_index_builds_for_notebook -q`
Expected: FAIL（`run_index` 未定义）。

- [ ] **Step 3: 实现 run_index + CLI 子命令**

`batch_ingest.py`：
```python
def run_index(repo, notebook_id: str) -> dict:
    """Phase 3：为（base）notebook 构建规模化检索索引。"""
    manifest = repo.build_scale_index(notebook_id)
    return {"indexed_nodes": manifest.get("n_nodes", 0)}
```
`scripts/batch_ingest.py`：在 argparse 的 phase choices 加 `index`，`all` 末尾追加 index；dispatch 调 `run_index(repo, notebook_id)`。

- [ ] **Step 4: README 补用法（中英）**

`README.md` 的 batch ingest 段补：`batch_ingest index --notebook-id <base_nb>` 为 base KG 构建规模化检索索引（离线，静态 base 重建后重跑）。`README_zh.md` 同步中文。

- [ ] **Step 5: 运行确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_scale_index_repo.py -q`
Expected: PASS。

- [ ] **Step 6: 提交**

```bash
git add backend/app/services/batch_ingest.py backend/app/scripts/batch_ingest.py README.md README_zh.md backend/tests/test_scale_index_repo.py
git commit -m "feat(kg-scale): batch_ingest index 阶段 + CLI + README(中英)"
```

---

## Task 9: 规模慢测（gated）+ 全量回归

**Files:**
- Test: `backend/tests/test_scale_index.py`

- [ ] **Step 1: 加 gated 规模测试**

追加（合成大图，验证 PPR 在 ~10^5 节点/~10^6 边下亚秒~秒级收敛）：
```python
import time, pytest


@pytest.mark.slow
def test_personalized_ppr_scales_to_1e5():
    n, m = 100_000, 1_000_000
    rng = np.random.default_rng(1)
    rows = rng.integers(0, n, m); cols = rng.integers(0, n, m)
    A = sp.csr_matrix((np.ones(m), (rows, cols)), shape=(n, n))
    colsum = np.asarray(A.sum(0)).ravel(); colsum[colsum == 0] = 1
    A = (A @ sp.diags(1.0/colsum)).tocsr()
    reset = np.zeros(n); reset[rng.integers(0, n, 50)] = 1.0
    t = time.perf_counter()
    x = personalized_ppr(A, reset, damping=0.5, tol=1e-6, max_iter=100)
    dt = time.perf_counter() - t
    assert abs(x.sum() - 1.0) < 1e-6
    assert dt < 10.0     # 10^5/10^6 应 ≪10s（记录实际值，校验内存预算）
```
在 `backend/pytest.ini`/`pyproject` 注册 `slow` marker（若未注册），默认 `-m "not slow"` 跳过。

- [ ] **Step 2: 运行 slow 测试（手动）**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_scale_index.py -q -m slow`
Expected: PASS；记录 `dt` 实测值（写入 PR 描述，作为延迟/内存预算证据）。

- [ ] **Step 3: 全量后端回归**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q -m "not slow"`
Expected: all pass（含原有 1163+ 与新增）。

- [ ] **Step 4: 提交**

```bash
git add backend/tests/test_scale_index.py backend/pytest.ini
git commit -m "test(kg-scale): 10^5 规模 gated 慢测 + marker"
```

---

## 收尾

- [ ] rebase 到 origin/master 保持线性 → push → `gh pr create --base master`（按 memory「开发流程收尾提 PR」）。PR 描述附 Task 9 实测延迟、内存预算（ANN ~4GB 常驻一次性）、等价测试结论。
- [ ] PR 合并用 **Rebase and merge**。
