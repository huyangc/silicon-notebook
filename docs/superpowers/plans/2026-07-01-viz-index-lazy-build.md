# viz-only 索引懒构建 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 为无完整 scale 索引的大 notebook 单独持久化一份「只含 viz」的轻量索引,懒构建(同步)+ 重合并时刷新,点亮 KG 视图快路径,并在「图谱处理」面板暴露索引状态。

**Architecture:** 新增独立 `kg/viz_index.py`(与检索 `kg_index/` 隔离,落 `kg_viz/{nb}/`);`build_viz_index` 用 `json_extract` 取名避开 30 万行 `json.loads`;统一访问器 `_viz_index`(base 库复用完整 scale 索引 / 否则懒建);改接 `unified_graph`·`kg_neighbors`;`rebuild_unified_kg` 结尾主动刷新;`/unified-kg/status` 只读探针 + 前端三态徽章。等价性(与全量派生路径逐字段相等)是核心不变量。

**Tech Stack:** Python 3.13 / FastAPI / SQLite(JSON1 `json_extract`)/ numpy / scipy.sparse / pytest;前端 Next.js + React + TypeScript。

## Global Constraints

- **等价不变量**:`_viz_index` 驱动的 `unified_graph(level=object, limit=N)` 与 `kg_neighbors(nb, oid, cap)` 的 nodes/edges/totals,必须与全量派生(`_unified_graph_full` + `limit_graph_by_degree` / `_kg_neighbors_db`)**逐字段相等**(含度数并列顺序)。
- **检索隔离**:viz-only 索引落 `{storage_dir}/kg_viz/{nb}/`,**绝不**写入检索用的 `kg_index/{nb}/`;建了 viz 索引后 `_scale_index(nb)` 对该库仍须返回 None。
- **只读探针**:`/unified-kg/status` 相关代码路径**绝不触发构建**,只读 manifest 比对 version。
- **版本机制**:viz 索引 version == `self._scale_index_version(notebook_id)`(复用现有,不新造);进程缓存按 version O(1) 比对。
- **fail-open**:`rebuild_unified_kg` 里的主动刷新失败**绝不**中断 rebuild。
- 日志用 `self.event_log.logger`(该文件既有约定;文件内无模块级 `logger`)。
- 运行测试从 `backend/` 目录:`cd backend && python -m pytest ...`。

---

### Task 1: `kg/viz_index.py` — VizIndex + save/load 往返

**Files:**
- Create: `backend/app/services/kg/viz_index.py`
- Test: `backend/tests/test_viz_index.py`

**Interfaces:**
- Produces:
  - `class VizIndex` dataclass,字段:`viz_ids: list`, `viz_adj: sp.csr_matrix`, `viz_deg: np.ndarray`, `viz_types: list`, `viz_names: list`, `viz_edges: list`, `manifest: dict`。
  - `save_viz_index(out_dir: str, *, viz_ids, viz_adj, viz_deg, viz_types, viz_names, viz_payload: dict, manifest: dict) -> dict`(`viz_payload` 形如 `{"edges": [[src,dst,edge_type],...]}`;写 `viz.npz` + `viz_adj.npz` + `manifest.json`;返回 manifest)。
  - `load_viz_index(out_dir: str) -> Optional[VizIndex]`(缺 manifest 或缺数组文件 → None)。

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_viz_index.py
"""viz-only 索引产物:save/load 往返 + 缺文件返回 None。"""
import numpy as np
import scipy.sparse as sp
from app.services.kg import viz_index as vi


def _arrays():
    viz_ids = ["a", "b", "c"]
    # undirected a-b, a-c
    adj = sp.csr_matrix(np.array([[0, 1, 1], [1, 0, 0], [1, 0, 0]], dtype=np.int8))
    viz_deg = np.array([2, 1, 1], dtype=np.int32)
    viz_types = ["concept", "concept", "concept"]
    viz_names = ["MOSFET", "gain", "bias"]
    viz_payload = {"edges": [["a", "b", "relates"], ["a", "c", "relates"]]}
    return viz_ids, adj, viz_deg, viz_types, viz_names, viz_payload


def test_save_load_roundtrip(tmp_path):
    viz_ids, adj, viz_deg, viz_types, viz_names, viz_payload = _arrays()
    out = str(tmp_path / "kg_viz" / "nb-1")
    manifest = {"version": ["nb-1", 3, "2026-07-01T00:00:00"], "n_viz_nodes": 3, "n_viz_edges": 2}
    vi.save_viz_index(out, viz_ids=viz_ids, viz_adj=adj, viz_deg=viz_deg,
                      viz_types=viz_types, viz_names=viz_names,
                      viz_payload=viz_payload, manifest=manifest)
    idx = vi.load_viz_index(out)
    assert idx is not None
    assert idx.viz_ids == viz_ids
    assert idx.viz_types == viz_types
    assert idx.viz_names == viz_names
    assert idx.viz_edges == [["a", "b", "relates"], ["a", "c", "relates"]]
    assert list(idx.viz_deg) == [2, 1, 1]
    assert (idx.viz_adj.toarray() == adj.toarray()).all()
    assert idx.manifest["version"] == ["nb-1", 3, "2026-07-01T00:00:00"]
    assert idx.manifest["n_viz_nodes"] == 3


def test_load_missing_returns_none(tmp_path):
    assert vi.load_viz_index(str(tmp_path / "nope")) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_viz_index.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.kg.viz_index`.

- [ ] **Step 3: Write minimal implementation**

```python
# backend/app/services/kg/viz_index.py
"""viz-only 索引:折叠可视化图的紧凑数组(与检索 scale 索引隔离)。

普通(非 base 层)大 notebook 没有完整 scale 索引 → KG 视图慢路径。给它单独持久化
这一份只含 viz 的产物(canonical 折叠图),unified_graph/kg_neighbors 快路径即可点亮。
落盘目录与检索用的 kg_index/ 严格分开,避免污染 _scale_index/scale_ppr。
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Optional

import numpy as np
import scipy.sparse as sp


@dataclass
class VizIndex:
    """折叠 viz 图。属性名与 ScaleIndex 的 viz_* 对齐,so unified_graph 的有界分派与
    kg_neighbors 可鸭子类型地消费任一来源(base 库的 ScaleIndex 或本轻量索引)。"""
    viz_ids: list
    viz_adj: "sp.csr_matrix"
    viz_deg: "np.ndarray"
    viz_types: list
    viz_names: list
    viz_edges: list
    manifest: dict


def save_viz_index(out_dir: str, *, viz_ids, viz_adj, viz_deg, viz_types,
                   viz_names, viz_payload: dict, manifest: dict) -> dict:
    """写 viz.npz + viz_adj.npz + manifest.json 到 out_dir。返回 manifest。"""
    os.makedirs(out_dir, exist_ok=True)
    np.savez(
        os.path.join(out_dir, "viz.npz"),
        viz_ids=np.asarray(viz_ids, dtype=object),
        viz_deg=np.asarray(viz_deg, dtype=np.int32),
        viz_types=np.asarray(viz_types, dtype=object),
        viz_names=np.asarray(viz_names, dtype=object),
        viz_edges=json.dumps((viz_payload or {}).get("edges", [])),
    )
    sp.save_npz(os.path.join(out_dir, "viz_adj.npz"), viz_adj.tocsr())
    with open(os.path.join(out_dir, "manifest.json"), "w") as fh:
        json.dump(manifest, fh)
    return manifest


def load_viz_index(out_dir: str) -> Optional[VizIndex]:
    """加载持久化 VizIndex。manifest 或数组文件缺失 → None。"""
    mpath = os.path.join(out_dir, "manifest.json")
    viz_npz = os.path.join(out_dir, "viz.npz")
    viz_adj_path = os.path.join(out_dir, "viz_adj.npz")
    if not (os.path.exists(mpath) and os.path.exists(viz_npz) and os.path.exists(viz_adj_path)):
        return None
    with open(mpath) as fh:
        manifest = json.load(fh)
    with np.load(viz_npz, allow_pickle=True) as z:
        viz_ids = list(z["viz_ids"])
        viz_deg = z["viz_deg"]
        viz_types = list(z["viz_types"])
        viz_names = list(z["viz_names"])
        viz_edges = json.loads(str(z["viz_edges"]))
    viz_adj = sp.load_npz(viz_adj_path)
    return VizIndex(viz_ids=viz_ids, viz_adj=viz_adj, viz_deg=viz_deg,
                    viz_types=viz_types, viz_names=viz_names,
                    viz_edges=viz_edges, manifest=manifest)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_viz_index.py -v`
Expected: PASS (2 passed).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/kg/viz_index.py backend/tests/test_viz_index.py
git commit -m "feat(kg): viz-only 索引产物 save/load(与检索索引隔离)"
```

---

### Task 2: `build_viz_index` + lite 折叠 + 数组抽取重构

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(重构 `_build_viz_graph_arrays`;新增 `_viz_arrays_from_graph`、`_derive_object_graph_lite`、`_viz_index_dir`、`build_viz_index`)
- Test: `backend/tests/test_viz_index_build.py`

**Interfaces:**
- Consumes: `viz_index.save_viz_index` / `load_viz_index`(Task 1);既有 `self._unified_graph_full`、`self.relations_for_notebook`、`self.cluster_map`、`self._scale_index_version`、`app.services.kg_merge.derive_unified_graph`。
- Produces:
  - `_viz_arrays_from_graph(self, full: dict) -> tuple` → `(viz_ids, viz_adj, viz_deg, viz_types, viz_names, viz_payload)`(即原 `_build_viz_graph_arrays` 主体)。
  - `_derive_object_graph_lite(self, notebook_id: str) -> dict` → `{"nodes":[{id,object_type,payload:{name}}...], "edges":[{source_object_id,target_object_id,edge_type}...]}`,与 `_unified_graph_full(nb,"object")` 等价但名字走 `json_extract`。
  - `_viz_index_dir(self, notebook_id: str) -> str` → `{storage_dir}/kg_viz/{nb}`。
  - `build_viz_index(self, notebook_id: str) -> Optional[dict]` → 落盘 + 缓存,返回 manifest;空图返回 None。

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_viz_index_build.py
"""build_viz_index:lite 折叠等价 _unified_graph_full('object') + 落盘 + 空图 None。"""
import json
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
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _star(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept", "payload": {"name": "gain", "section_path": ""}, "evidence": []},
        {"local_id": "c", "object_type": "concept", "payload": {"name": "bias", "section_path": ""}, "evidence": []},
    ], [
        {"source_local_id": "a", "target_local_id": "b", "edge_type": "relates", "evidence": []},
        {"source_local_id": "a", "target_local_id": "c", "edge_type": "relates", "evidence": []},
    ])
    repo.rebuild_unified_kg(nb.id)
    return nb


def test_lite_graph_equals_full(repo):
    nb = _star(repo)
    full = repo._unified_graph_full(nb.id, "object")
    lite = repo._derive_object_graph_lite(nb.id)
    # 逐字段相等:节点(id/type/name,同序)与边集
    assert [(n["id"], n["object_type"], (n.get("payload") or {}).get("name", "")) for n in lite["nodes"]] == \
           [(n["id"], n["object_type"], (n.get("payload") or {}).get("name", "")) for n in full["nodes"]]
    assert [(e["source_object_id"], e["target_object_id"], e["edge_type"]) for e in lite["edges"]] == \
           [(e["source_object_id"], e["target_object_id"], e["edge_type"]) for e in full["edges"]]


def test_build_viz_index_persists_and_manifest(repo):
    nb = _star(repo)
    manifest = repo.build_viz_index(nb.id)
    assert manifest is not None
    assert manifest["n_viz_nodes"] == 3
    assert manifest["n_viz_edges"] == 2
    assert manifest["version"] == repo._scale_index_version(nb.id)
    # 落在 kg_viz/,不在 kg_index/
    import os
    assert os.path.exists(os.path.join(repo._viz_index_dir(nb.id), "manifest.json"))
    assert not os.path.exists(os.path.join(str(repo.settings.storage_dir), "kg_index", nb.id, "manifest.json"))


def test_build_viz_index_empty_notebook_returns_none(repo):
    nb = repo.create_notebook(NotebookCreate(name="empty"))
    assert repo.build_viz_index(nb.id) is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_viz_index_build.py -v`
Expected: FAIL — `AttributeError: 'SQLiteRepository' object has no attribute '_derive_object_graph_lite'`.

- [ ] **Step 3: Write minimal implementation**

先把现有 `_build_viz_graph_arrays`(在 `backend/app/services/sqlite_repository.py:6539`)的**主体**抽成 `_viz_arrays_from_graph(self, full)`,`_build_viz_graph_arrays` 改为薄封装。替换该方法为:

```python
    def _build_viz_graph_arrays(self, notebook_id: str):
        """Full-payload derivation (used by build_scale_index). Delegates the
        array math to _viz_arrays_from_graph so build_viz_index can reuse it with
        a lighter (json_extract) derivation."""
        return self._viz_arrays_from_graph(self._unified_graph_full(notebook_id, "object"))

    def _viz_arrays_from_graph(self, full: dict):
        """(viz_ids, viz_adj, viz_deg, viz_types, viz_names, viz_payload) from a
        folded object-level graph dict {nodes, edges}. Node order = input order
        (matters for degree-tie vs limit_graph_by_degree). Only reads id /
        object_type / payload.name — payload may be full or name-only."""
        import numpy as np
        import scipy.sparse as sp

        nodes = full["nodes"]
        edges = full["edges"]
        viz_ids = [n["id"] for n in nodes]
        viz_types = [n["object_type"] for n in nodes]
        viz_names = [(n.get("payload") or {}).get("name", "") for n in nodes]
        index = {nid: i for i, nid in enumerate(viz_ids)}
        n = len(viz_ids)

        deg = np.zeros(n, dtype=np.int64)
        und_rows, und_cols, und_seen = [], [], set()
        edge_list: List[list] = []
        for e in edges:
            s, t = e["source_object_id"], e["target_object_id"]
            si_, ti = index.get(s), index.get(t)
            if si_ is None or ti is None:
                continue
            edge_list.append([s, t, e["edge_type"]])
            deg[si_] += 1
            deg[ti] += 1
            if si_ != ti:
                pair = (si_, ti) if si_ < ti else (ti, si_)
                if pair not in und_seen:
                    und_seen.add(pair)
                    und_rows += [pair[0], pair[1]]
                    und_cols += [pair[1], pair[0]]

        if und_rows:
            data = np.ones(len(und_rows), dtype=np.int8)
            viz_adj = sp.csr_matrix((data, (und_rows, und_cols)), shape=(n, n))
        else:
            viz_adj = sp.csr_matrix((n, n), dtype=np.int8)
        viz_deg = deg.astype(np.int32)
        viz_payload = {"edges": edge_list}
        return viz_ids, viz_adj, viz_deg, viz_types, viz_names, viz_payload
```

然后在同类里新增 lite 折叠 + 目录 + 构建(建议放在 `build_scale_index` 附近,如 `_scale_index` 之后):

```python
    def _derive_object_graph_lite(self, notebook_id: str) -> dict:
        """Object-level folded graph EQUIVALENT to _unified_graph_full(nb,'object')
        but WITHOUT full-payload json.loads: node names come from SQL
        json_extract(payload,'$.name'). Same table + same WHERE (no ORDER BY) →
        same scan order → same fold order → identical viz arrays."""
        self.get_notebook(notebook_id)
        from app.services.kg_merge import derive_unified_graph
        with self._connect() as db:
            nrows = db.execute(
                "SELECT id, object_type, json_extract(payload,'$.name') AS name "
                "FROM knowledge_objects WHERE notebook_id=? AND status!='deprecated'",
                (notebook_id,),
            ).fetchall()
        nodes = [{"id": r["id"], "object_type": r["object_type"],
                  "payload": {"name": r["name"] or ""}} for r in nrows]
        edges = [{"source_object_id": r["source_object_id"],
                  "target_object_id": r["target_object_id"], "edge_type": r["edge_type"]}
                 for r in self.relations_for_notebook(notebook_id)]
        return derive_unified_graph(nodes, edges, self.cluster_map(notebook_id))

    def _viz_index_dir(self, notebook_id: str) -> str:
        return os.path.join(str(self.settings.storage_dir), "kg_viz", notebook_id)

    def build_viz_index(self, notebook_id: str) -> Optional[dict]:
        """Build + persist a viz-only index under {storage_dir}/kg_viz/{nb}/ so the
        KG-view fast paths light up for notebooks without a full scale index.
        json_extract names avoid the 300k-row json.loads. Returns manifest, or
        None for an empty graph (no non-deprecated objects). Caches on success."""
        from app.services.kg import viz_index as vi
        self.get_notebook(notebook_id)
        full = self._derive_object_graph_lite(notebook_id)
        if not full["nodes"]:
            return None
        viz_ids, viz_adj, viz_deg, viz_types, viz_names, viz_payload = \
            self._viz_arrays_from_graph(full)
        manifest = {
            "version": self._scale_index_version(notebook_id),
            "n_viz_nodes": len(viz_ids),
            "n_viz_edges": len(viz_payload.get("edges", [])),
        }
        out_dir = self._viz_index_dir(notebook_id)
        vi.save_viz_index(out_dir, viz_ids=viz_ids, viz_adj=viz_adj, viz_deg=viz_deg,
                          viz_types=viz_types, viz_names=viz_names,
                          viz_payload=viz_payload, manifest=manifest)
        self._viz_idx_cache[notebook_id] = vi.load_viz_index(out_dir)
        return manifest
```

注:`self._viz_idx_cache` 在 Task 3 的 `__init__` 里加。为让本 Task 测试独立通过,在本 Task **也**先在 `__init__`(`backend/app/services/sqlite_repository.py:294` 的 `self._scale_idx_cache` 之后)加一行:

```python
        self._viz_idx_cache: Dict[str, Any] = {}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_viz_index_build.py -v`
Expected: PASS (3 passed)。

顺带跑既有 viz/scale 回归确保重构无损:
Run: `cd backend && python -m pytest tests/test_viz_bounded.py tests/test_scale_index_repo.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_viz_index_build.py
git commit -m "feat(kg): build_viz_index(json_extract lite 折叠)+ 数组抽取重构"
```

---

### Task 3: `_viz_index` 访问器 + 改接 unified_graph / kg_neighbors

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(`__init__` 确认缓存;新增 `_viz_index`;改 `unified_graph`、`kg_neighbors`)
- Test: `backend/tests/test_viz_index_wire.py`

**Interfaces:**
- Consumes: `build_viz_index`、`_scale_index`、`_scale_index_version`、`viz_index.load_viz_index`(Tasks 1–2);既有 `_unified_graph_bounded`、`kg_neighbors` 快路径、`_kg_neighbors_db`。
- Produces: `_viz_index(self, notebook_id: str)` → 暴露 `viz_ids/viz_adj/viz_deg/viz_types/viz_names/viz_edges/manifest` 的对象(base 库返回 `ScaleIndex`,否则返回 `VizIndex`),空图 → None。

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_viz_index_wire.py
"""_viz_index 懒构建 + unified_graph/kg_neighbors 等价 + 检索隔离 + base 复用。"""
import os
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from app.services.kg_merge import limit_graph_by_degree


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _star(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept", "payload": {"name": "gain", "section_path": ""}, "evidence": []},
        {"local_id": "c", "object_type": "concept", "payload": {"name": "bias", "section_path": ""}, "evidence": []},
    ], [
        {"source_local_id": "a", "target_local_id": "b", "edge_type": "relates", "evidence": []},
        {"source_local_id": "a", "target_local_id": "c", "edge_type": "relates", "evidence": []},
    ])
    repo.rebuild_unified_kg(nb.id)
    return nb


def test_unified_graph_lazy_builds_and_matches(repo):
    nb = _star(repo)
    # 无任何预构建:unified_graph 触发懒建并等价全量派生
    legacy = repo._unified_graph_full(nb.id, "object")
    legacy_top2 = limit_graph_by_degree(legacy, 2)
    bounded = repo.unified_graph(nb.id, level="object", limit=2)
    assert len(bounded["nodes"]) == len(legacy_top2["nodes"]) == 2
    assert bounded["total_nodes"] == len(legacy["nodes"])
    assert bounded["total_edges"] == len(legacy["edges"])
    # 懒建落盘了
    assert os.path.exists(os.path.join(repo._viz_index_dir(nb.id), "manifest.json"))


def test_neighbors_lazy_matches_db(repo):
    nb = _star(repo)
    # canonical id 折叠后 "MOSFET" 概念:两路应一致
    db_res = repo._kg_neighbors_db(nb.id, "MOSFET", 50)
    viz_res = repo.kg_neighbors(nb.id, "MOSFET", 50)
    assert {n["id"] for n in viz_res["nodes"]} == {n["id"] for n in db_res["nodes"]}
    assert {(e["source_object_id"], e["target_object_id"]) for e in viz_res["edges"]} == \
           {(e["source_object_id"], e["target_object_id"]) for e in db_res["edges"]}


def test_scale_index_isolation(repo):
    nb = _star(repo)
    repo.unified_graph(nb.id, level="object", limit=2)  # 建 viz 索引
    # 检索路径不受污染:该库仍无检索 scale 索引
    assert repo._scale_index(nb.id) is None


def test_empty_notebook_falls_back(repo):
    nb = repo.create_notebook(NotebookCreate(name="empty"))
    # 空库:_viz_index None,unified_graph 走全量派生(不报错,空结果)
    assert repo._viz_index(nb.id) is None
    g = repo.unified_graph(nb.id, level="object", limit=2)
    assert g["nodes"] == [] and g["total_nodes"] == 0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_viz_index_wire.py -v`
Expected: FAIL — `test_scale_index_isolation` 之外的懒建断言失败(`unified_graph` 仍走全量派生、无 `kg_viz/manifest.json`),因 `_viz_index` 未接线。

- [ ] **Step 3: Write minimal implementation**

确认 `__init__`(`backend/app/services/sqlite_repository.py:294` 之后)已有(Task 2 已加):

```python
        self._viz_idx_cache: Dict[str, Any] = {}
```

新增 `_viz_index`(放在 `_scale_index` 之后):

```python
    def _viz_index(self, notebook_id: str):
        """Index exposing folded viz arrays for the KG-view fast paths, or None.

        Priority: (1) a valid full scale index (base library — already carries the
        viz arrays); (2) a persisted viz-only index whose version matches; (3)
        lazily build one (synchronous) + persist. None only for an empty graph."""
        scale = self._scale_index(notebook_id)
        if scale is not None and getattr(scale, "viz_ids", None) is not None:
            return scale
        from app.services.kg import viz_index as vi
        cur = self._scale_index_version(notebook_id)
        cached = self._viz_idx_cache.get(notebook_id)
        if cached is not None and cached.manifest.get("version") == cur:
            return cached
        idx = vi.load_viz_index(self._viz_index_dir(notebook_id))
        if idx is not None and idx.manifest.get("version") == cur:
            self._viz_idx_cache[notebook_id] = idx
            return idx
        self.build_viz_index(notebook_id)   # sync lazy build; sets cache on success
        return self._viz_idx_cache.get(notebook_id)
```

改 `unified_graph`(`backend/app/services/sqlite_repository.py:4433-4436`)——把 `_scale_index` 换成 `_viz_index`:

```python
        if limit is not None and level != "concept":
            idx = self._viz_index(notebook_id)
            if idx is not None and getattr(idx, "viz_ids", None) is not None:
                return self._unified_graph_bounded(idx, limit)
```

改 `kg_neighbors`(`backend/app/services/sqlite_repository.py:4527-4528`)——同样换:

```python
        idx = self._viz_index(notebook_id)
        if idx is not None and getattr(idx, "viz_ids", None) is not None:
            from app.services.kg.scale_index import viz_neighbors
            # ...（其余 viz_neighbors 快路径代码不变）
```

（仅替换 `idx = self._scale_index(notebook_id)` 这一行为 `idx = self._viz_index(notebook_id)`;其后逻辑不动。）

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_viz_index_wire.py -v`
Expected: PASS (4 passed)。

回归既有:
Run: `cd backend && python -m pytest tests/test_viz_bounded.py tests/test_unified_kg_repository.py tests/test_unified_kg_api.py -q`
Expected: PASS(既有 `test_viz_bounded` 仍用 `build_scale_index` 路径,`_viz_index` 优先返回它 → 不变)。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_viz_index_wire.py
git commit -m "feat(kg): _viz_index 懒建访问器 + 改接 unified_graph/kg_neighbors"
```

---

### Task 4: 重合并主动刷新 + `/unified-kg/status` 只读探针 + schema

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(`rebuild_unified_kg` 尾部主动刷新;新增 `_viz_index_probe`;`unified_kg_status` 加字段)
- Modify: `backend/app/models/schemas.py`(`UnifiedKgStatus` 加 4 字段)
- Test: `backend/tests/test_viz_index_status.py`

**Interfaces:**
- Consumes: `build_viz_index`、`_scale_index`、`_scale_index_version`、`_viz_index_dir`、`viz_index.load_viz_index`(Tasks 1–3)。
- Produces: `_viz_index_probe(self, notebook_id: str) -> dict` → `{"viz_indexed": bool, "viz_nodes": int, "viz_edges": int, "viz_stale": bool}`(**只读,绝不构建**);`unified_kg_status` 返回值合并这 4 键;`UnifiedKgStatus` pydantic 加同名字段。

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_viz_index_status.py
"""viz 索引状态探针:只读不构建 + 三态(未建/已就绪/待刷新)。"""
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
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _seed(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept", "payload": {"name": "gain", "section_path": ""}, "evidence": []},
    ], [
        {"source_local_id": "a", "target_local_id": "b", "edge_type": "relates", "evidence": []},
    ])
    return nb


def test_probe_never_builds(repo):
    nb = _seed(repo)
    repo.rebuild_unified_kg(nb.id)
    # 但 rebuild 会主动建;为测"未建"态,删掉 kg_viz 再探
    import shutil
    shutil.rmtree(repo._viz_index_dir(nb.id), ignore_errors=True)
    repo._viz_idx_cache.pop(nb.id, None)
    probe = repo._viz_index_probe(nb.id)
    assert probe["viz_indexed"] is False
    assert probe["viz_stale"] is False
    # 探针没有偷偷构建
    assert not os.path.exists(os.path.join(repo._viz_index_dir(nb.id), "manifest.json"))


def test_rebuild_refreshes_viz_index(repo):
    nb = _seed(repo)
    repo.rebuild_unified_kg(nb.id)
    probe = repo._viz_index_probe(nb.id)
    assert probe["viz_indexed"] is True
    assert probe["viz_nodes"] == 2
    assert probe["viz_stale"] is False


def test_stale_after_mutation(repo):
    nb = _seed(repo)
    repo.rebuild_unified_kg(nb.id)         # 建了新鲜索引
    repo._viz_idx_cache.pop(nb.id, None)
    # 变更 KG(加对象)→ version 变 → 磁盘旧索引变 stale
    repo.store_kg(nb.id, None, [
        {"local_id": "c", "object_type": "concept", "payload": {"name": "bias", "section_path": ""}, "evidence": []},
    ], [])
    probe = repo._viz_index_probe(nb.id)
    assert probe["viz_indexed"] is False
    assert probe["viz_stale"] is True


def test_unified_kg_status_carries_viz_fields(repo):
    nb = _seed(repo)
    repo.rebuild_unified_kg(nb.id)
    st = repo.unified_kg_status(nb.id)
    assert st["viz_indexed"] is True
    assert st["viz_nodes"] == 2
    assert "viz_edges" in st and "viz_stale" in st
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_viz_index_status.py -v`
Expected: FAIL — `AttributeError: ... '_viz_index_probe'`。

- [ ] **Step 3: Write minimal implementation**

在 `rebuild_unified_kg` 尾部(`backend/app/services/sqlite_repository.py:5037` 的 `_stage("DONE ...")` 之后、`return cluster_count` 之前)插入主动刷新:

```python
        # Proactively refresh the viz-only index so the next KG-view open doesn't
        # pay a lazy build (and to cover same-second in-place edits the version
        # tuple can miss). Fail-open: a viz build error must never break rebuild.
        try:
            self.build_viz_index(notebook_id)
        except Exception:
            self.event_log.logger.warning(
                "build_viz_index failed after rebuild for %s", notebook_id, exc_info=True)
```

新增 `_viz_index_probe`(放在 `_viz_index` 之后):

```python
    def _viz_index_probe(self, notebook_id: str) -> dict:
        """Read-only viz-index status — NEVER builds. Returns
        {viz_indexed, viz_nodes, viz_edges, viz_stale}."""
        cur = self._scale_index_version(notebook_id)
        scale = self._scale_index(notebook_id)
        if scale is not None and getattr(scale, "viz_ids", None) is not None:
            m = scale.manifest
            return {"viz_indexed": True,
                    "viz_nodes": int(m.get("n_viz_nodes", len(scale.viz_ids))),
                    "viz_edges": int(m.get("n_viz_edges", len(scale.viz_edges or []))),
                    "viz_stale": False}
        from app.services.kg import viz_index as vi
        idx = vi.load_viz_index(self._viz_index_dir(notebook_id))
        if idx is None:
            return {"viz_indexed": False, "viz_nodes": 0, "viz_edges": 0, "viz_stale": False}
        m = idx.manifest
        fresh = m.get("version") == cur
        return {"viz_indexed": fresh,
                "viz_nodes": int(m.get("n_viz_nodes", 0)),
                "viz_edges": int(m.get("n_viz_edges", 0)),
                "viz_stale": not fresh}
```

改 `unified_kg_status`(`backend/app/services/sqlite_repository.py:4403-4419`)——两个 return 都合并探针字段:

```python
    def unified_kg_status(self, notebook_id: str) -> dict:
        self.get_notebook(notebook_id)
        with self._connect() as db:
            row = db.execute("SELECT * FROM unified_kg_state WHERE notebook_id=?", (notebook_id,)).fetchone()
            clusters = db.execute(
                "SELECT COUNT(DISTINCT canonical_id) AS c FROM concept_clusters WHERE notebook_id=?",
                (notebook_id,),
            ).fetchone()["c"]
        viz = self._viz_index_probe(notebook_id)
        if row is None:
            return {"dirty": False, "last_rebuild_at": "", "objects": 0, "relations": 0,
                    "clusters": int(clusters), **viz}
        return {
            "dirty": bool(row["dirty"]),
            "last_rebuild_at": row["last_rebuild_at"],
            "objects": int(row["object_count"]),
            "relations": int(row["relation_count"]),
            "clusters": int(row["cluster_count"] or clusters),
            **viz,
        }
```

改 `UnifiedKgStatus`(`backend/app/models/schemas.py:494`)——加 4 字段:

```python
class UnifiedKgStatus(BaseModel):
    dirty: bool
    last_rebuild_at: str = ""
    objects: int = 0
    relations: int = 0
    clusters: int = 0
    viz_indexed: bool = False
    viz_nodes: int = 0
    viz_edges: int = 0
    viz_stale: bool = False
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_viz_index_status.py -v`
Expected: PASS (4 passed)。

回归 API:
Run: `cd backend && python -m pytest tests/test_unified_kg_api.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/app/models/schemas.py backend/tests/test_viz_index_status.py
git commit -m "feat(kg): 重合并主动刷新 viz 索引 + /unified-kg/status 只读探针"
```

---

### Task 5: 前端 — 「图谱处理」索引状态徽章(三态)

**Files:**
- Modify: `frontend/app/page.tsx`(`type UnifiedKgStatus`:305;状态标签行:3640-3649)

**Interfaces:**
- Consumes: `/unified-kg/status` 现返回 `viz_indexed/viz_nodes/viz_edges/viz_stale`(Task 4)。
- Produces: 无(纯展示)。

- [ ] **Step 1: 扩展类型**

`frontend/app/page.tsx:305` 把:

```tsx
type UnifiedKgStatus = { dirty: boolean; last_rebuild_at: string; objects: number; relations: number; clusters: number };
```

改为:

```tsx
type UnifiedKgStatus = { dirty: boolean; last_rebuild_at: string; objects: number; relations: number; clusters: number; viz_indexed: boolean; viz_nodes: number; viz_edges: number; viz_stale: boolean };
```

- [ ] **Step 2: 加徽章**

在 `frontend/app/page.tsx:3640` 的 `{unifiedKgStatus && ( ... )}` 状态标签行里,`last_rebuild_at` 那枚 `tag` 之后、`</div>` 之前,加一枚三态徽章:

```tsx
                    <span
                      className="tag"
                      title={
                        unifiedKgStatus.viz_indexed
                          ? `图谱索引已就绪 · ${unifiedKgStatus.viz_nodes} 节点 / ${unifiedKgStatus.viz_edges} 边`
                          : unifiedKgStatus.viz_stale
                            ? "图谱索引待刷新（重新合并后更新）"
                            : "图谱索引未构建（首次打开图谱将自动构建）"
                      }
                      style={{
                        color: unifiedKgStatus.viz_indexed
                          ? "var(--color-ok, #1a7f5a)"
                          : unifiedKgStatus.viz_stale
                            ? "var(--color-warn, #b97a00)"
                            : undefined,
                      }}
                    >
                      {unifiedKgStatus.viz_indexed
                        ? `图谱索引：已就绪 · ${unifiedKgStatus.viz_nodes} 节点`
                        : unifiedKgStatus.viz_stale
                          ? "图谱索引：待刷新"
                          : "图谱索引：未构建"}
                    </span>
```

- [ ] **Step 3: tsc 校验**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无错误。

- [ ] **Step 4: 现有前端测试**

Run: `cd frontend && npm test --silent 2>&1 | tail -5`
Expected: 全绿(无相关回归)。

- [ ] **Step 5: 视觉验证**

用 preview 打开某 notebook 的 KG 视图左栏「当前视图」小节,确认徽章三态之一渲染、与既有 `tag`/`tag-row` 对齐(符合 UI 对齐标准)。截图给用户。

- [ ] **Step 6: Commit**

```bash
git add frontend/app/page.tsx
git commit -m "feat(ui): KG 视图「图谱处理」索引状态徽章(三态)"
```

---

## 收尾

全部任务完成后:
- 后端全量回归:`cd backend && python -m pytest -q`(期望全绿)。
- 前端:`cd frontend && npx tsc --noEmit`(干净)。
- rebase 特性分支到 `origin/master` 保持线性 → push → `gh pr create --base master`(合并按钮走 Rebase and merge)。

## Self-Review 记录

- **Spec 覆盖**:组件 1(viz_index.py)=Task1;组件 2(build_viz_index + lite + 提速)=Task2;组件 3(_viz_index 访问器 + 改接)=Task3;组件 5(失效 + 重合并刷新)=Task4;组件 6(status 探针 + schema + 前端徽章)=Task4+Task5;数据流各分支(base 复用/空库回退/隔离/懒建)=Task3 测试;可观测三态=Task4+5。无遗漏。
- **类型一致**:`build_viz_index -> Optional[dict]`、`_viz_index -> 对象|None`、`_viz_index_probe -> dict(4 键)`、`VizIndex` 属性名与 `ScaleIndex.viz_*` 对齐、`UnifiedKgStatus` 前后端 4 字段同名——各 Task 间一致。
- **无占位符**:每步含完整代码与确切命令/预期。
