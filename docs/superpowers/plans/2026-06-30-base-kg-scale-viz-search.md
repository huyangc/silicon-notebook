# 基础 KG 可视化/搜索规模化(SP1)Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans. Steps use checkbox (`- [ ]`).

**Goal:** KG 可视化与节点搜索在 10^5–10^6 节点的基础 KG 上服务端有界:viz 从持久化折叠图取 top-N/邻域,search 走 FTS5 词法 + SP2 ANN 语义,前端不再拉全量。

**Architecture:** 构建期(`build_scale_index`)持久化折叠 viz 图;`unified_graph(limit=N)` 有索引时取折叠度数 top-N + 诱导边(弃 `_unified_graph_full`);新增 `/kg/search`(FTS5 ∪ ANN)、`/objects/{oid}/neighbors`;前端搜索改服务端 + 去「全部」+ 点击逐跳展开。

**Tech Stack:** SQLite FTS5(trigram)、scipy.sparse、hnswlib、numpy、Next.js/React。

参考 spec：`docs/superpowers/specs/2026-06-30-base-kg-scale-viz-search-design.md`。基于 master(含 SP2 scale 索引)。

## 并行分组
- **P-parallel(不同新文件,可并发)**:Task 1(`kg/search.py` 纯逻辑)与 Task 2(`scale_index.py` viz 纯函数)。
- **串行**(共改 `sqlite_repository.py`/`routes.py`):Task 3(search 集成)→ Task 4(viz 集成)→ Task 5(frontend)。Task 6 末尾。
- 执行:并发派 Task 1 + Task 2;回来后 Task 3 → 4 → 5 → 6。

## File Structure
- `backend/app/services/kg/search.py`(新)— `fts_search(db, notebook_id, q, k)` + `merge_search_hits(lexical, semantic, k)` 纯逻辑。
- `backend/app/services/kg/scale_index.py`(改)— `save_scale_index` 增持久化折叠 viz 图;`ScaleIndex` 加 viz 字段;`load_scale_index` 载入;新 `viz_core(idx, limit)`、`viz_neighbors(idx, node_id, cap)` 纯函数。
- `backend/app/services/sqlite_repository.py`(改)— FTS5 schema + `store_kg` 维护 + `backfill_kg_fts`;`build_scale_index` 产折叠 viz 图;`unified_graph` 有界分派;`kg_search`、`kg_neighbors` 包装。
- `backend/app/api/routes.py`(改)— `GET /kg/search`、`GET /objects/{oid}/neighbors`。
- `backend/app/models/schemas.py`(改)— `KgSearchHit`/`KgSearchResponse`。
- `frontend/app/page.tsx`(改)— 搜索改服务端 + neighbors 展开 + 去「全部」。
- 测试:`backend/tests/test_kg_search.py`、`test_viz_bounded.py`、`test_scale_index.py`、前端测试。

---

## Task 1: FTS 搜索纯逻辑 [P-parallel]

**Files:** Create `backend/app/services/kg/search.py`; Test `backend/tests/test_kg_search.py`.

- [ ] **Step 1: 失败测试** —
```python
import sqlite3
from app.services.kg.search import fts_search, merge_search_hits


def _fts_db():
    db = sqlite3.connect(":memory:"); db.row_factory = sqlite3.Row
    db.execute("CREATE VIRTUAL TABLE kg_objects_fts USING fts5(object_id UNINDEXED, notebook_id UNINDEXED, name, tokenize='trigram')")
    rows = [("o1","nb","current mirror"),("o2","nb","MOSFET"),("o3","nb","mirror symmetry"),("o4","other","current mirror")]
    db.executemany("INSERT INTO kg_objects_fts (object_id,notebook_id,name) VALUES (?,?,?)", rows)
    return db


def test_fts_search_substring_scoped_to_notebook():
    db = _fts_db()
    hits = fts_search(db, "nb", "mirror", k=10)
    ids = {h["object_id"] for h in hits}
    assert ids == {"o1", "o3"}            # substring 'mirror', notebook-scoped (o4 excluded)


def test_merge_dedup_prefers_lexical():
    lex = [{"object_id":"a","score":1.0,"match":"lexical"}]
    sem = [{"object_id":"a","score":0.9,"match":"semantic"},{"object_id":"b","score":0.8,"match":"semantic"}]
    out = merge_search_hits(lex, sem, k=10)
    by = {h["object_id"]: h for h in out}
    assert by["a"]["match"] == "lexical"   # dedup: lexical wins for 'a'
    assert "b" in by and by["b"]["match"] == "semantic"
    assert out == sorted(out, key=lambda h: -h["score"])  # sorted desc
```
Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_kg_search.py -q` → FAIL.

- [ ] **Step 2: 实现** `backend/app/services/kg/search.py`:
```python
"""KG 节点搜索纯逻辑:FTS5 词法查询 + 词法/语义结果合并。DB/ANN 由调用方提供。"""
from __future__ import annotations
from typing import List, Dict


def fts_search(db, notebook_id: str, q: str, k: int = 30) -> List[Dict]:
    """FTS5 MATCH 查询(已建 kg_objects_fts,trigram)。notebook 维度过滤。返回
    [{object_id, name, score, match:'lexical'}]。q 为空或全空白 → []。"""
    needle = (q or "").strip()
    if not needle:
        return []
    # trigram MATCH:用引号包裹做子串匹配;bm25 排序(越小越相关 → 取负作 score)
    rows = db.execute(
        "SELECT object_id, name, bm25(kg_objects_fts) AS rank "
        "FROM kg_objects_fts WHERE notebook_id=? AND kg_objects_fts MATCH ? "
        "ORDER BY rank LIMIT ?",
        (notebook_id, '"' + needle.replace('"', '""') + '"', k)).fetchall()
    return [{"object_id": r["object_id"], "name": r["name"],
             "score": -float(r["rank"]), "match": "lexical"} for r in rows]


def merge_search_hits(lexical: List[Dict], semantic: List[Dict], k: int = 30) -> List[Dict]:
    """合并词法 ∪ 语义,按 object_id 去重(词法优先保留),按 score 降序,截断 k。"""
    by: Dict[str, Dict] = {}
    for h in lexical:
        by[h["object_id"]] = h
    for h in semantic:
        by.setdefault(h["object_id"], h)
    out = sorted(by.values(), key=lambda h: -h["score"])
    return out[:k]
```
- [ ] **Step 3: PASS** — `pytest tests/test_kg_search.py -q`.
- [ ] **Step 4: 提交** — `git add backend/app/services/kg/search.py backend/tests/test_kg_search.py && git commit -m "feat(kg-viz): FTS5 搜索纯逻辑(fts_search + merge_search_hits)"`

---

## Task 2: viz 折叠图纯函数(top-N / 邻域)[P-parallel]

**Files:** Modify `backend/app/services/kg/scale_index.py`; Test `backend/tests/test_scale_index.py`.

**先读** `scale_index.py` 的 `ScaleIndex`、`save_scale_index`、`load_scale_index`(SP2)。

- [ ] **Step 1: 失败测试**(append `test_scale_index.py`):
```python
import numpy as np, scipy.sparse as sp
from app.services.kg.scale_index import viz_core, viz_neighbors


def _viz():
    # 折叠图:hub h 连 a,b,c;iso 无边。对称邻接(无向)。
    ids = ["h", "a", "b", "c", "iso"]
    idx = {n: i for i, n in enumerate(ids)}
    rows, cols = [], []
    for s, t in [("h","a"),("h","b"),("h","c")]:
        rows += [idx[s], idx[t]]; cols += [idx[t], idx[s]]
    adj = sp.csr_matrix((np.ones(len(rows)), (rows, cols)), shape=(5,5))
    deg = np.asarray(adj.getnnz(axis=1)).ravel().astype(np.int32)   # 度数
    types = ["concept"]*5
    return {"viz_ids": ids, "viz_adj": adj, "viz_deg": deg, "viz_types": types}


def test_viz_core_top_n_by_degree_with_induced_edges():
    v = _viz()
    out = viz_core(v, limit=2)
    ids = {n["id"] for n in out["nodes"]}
    assert "h" in ids and len(ids) == 2                 # hub(deg3) + 一个邻居
    assert all({e["source"], e["target"]} <= ids for e in out["edges"])  # 诱导边端点都在内
    assert out["total_nodes"] == 5 and out["truncated"] is True


def test_viz_neighbors_one_hop_bounded():
    v = _viz()
    out = viz_neighbors(v, "h", cap=2)
    nb = {n["id"] for n in out["nodes"]}
    assert "h" in nb and len(out["nodes"]) <= 3          # h + ≤cap 邻居
    assert all(e["source"]=="h" or e["target"]=="h" for e in out["edges"])
```
Run → FAIL.

- [ ] **Step 2: 实现** `viz_core` / `viz_neighbors`(纯函数,操作内存中的折叠图字典;`ScaleIndex`/load 的 viz 字段在 Task 4 接上):
```python
def viz_core(viz: dict, limit: int) -> dict:
    """折叠 viz 图取度数 top-N + 诱导边。viz: {viz_ids, viz_adj(csr), viz_deg, viz_types}。
    返回 {nodes:[{id,type,degree}], edges:[{source,target}], total_nodes, total_edges, truncated}。"""
    import numpy as np
    ids, adj, deg, types = viz["viz_ids"], viz["viz_adj"], viz["viz_deg"], viz["viz_types"]
    total = len(ids)
    n = total if (not limit or limit <= 0) else min(limit, total)
    top = np.argsort(-deg)[:n]
    keep = set(int(i) for i in top)
    nodes = [{"id": ids[i], "type": types[i], "degree": int(deg[i])} for i in top]
    coo = adj.tocoo()
    seen = set(); edges = []
    for r, c in zip(coo.row.tolist(), coo.col.tolist()):
        if r in keep and c in keep and r < c:
            key = (r, c)
            if key not in seen:
                seen.add(key); edges.append({"source": ids[r], "target": ids[c]})
    return {"nodes": nodes, "edges": edges, "total_nodes": total,
            "total_edges": int(adj.nnz // 2), "truncated": n < total}


def viz_neighbors(viz: dict, node_id: str, cap: int = 50) -> dict:
    """折叠图中 node_id 的 1-hop 邻域(≤cap),返回 {nodes,edges} 同 viz 形。未知 id → 空。"""
    import numpy as np
    ids, adj, deg, types = viz["viz_ids"], viz["viz_adj"], viz["viz_deg"], viz["viz_types"]
    index = {nid: i for i, nid in enumerate(ids)}
    i = index.get(node_id)
    if i is None:
        return {"nodes": [], "edges": []}
    row = adj.getrow(i)
    nbr = [int(j) for j in row.indices][:max(0, cap)]
    keep = [i] + nbr
    nodes = [{"id": ids[j], "type": types[j], "degree": int(deg[j])} for j in keep]
    edges = [{"source": node_id, "target": ids[j]} for j in nbr]
    return {"nodes": nodes, "edges": edges}
```
- [ ] **Step 3: PASS** — `pytest tests/test_scale_index.py -q`.
- [ ] **Step 4: 提交** — `git add backend/app/services/kg/scale_index.py backend/tests/test_scale_index.py && git commit -m "feat(kg-viz): viz_core/viz_neighbors 折叠图 top-N+邻域 纯函数"`

---

## Task 3: search 集成(FTS5 schema + 维护 + backfill + /kg/search)[串行,依赖 Task 1]

**Files:** Modify `sqlite_repository.py`、`routes.py`、`schemas.py`; Test `backend/tests/test_kg_search_api.py`(新).

**先读**:`store_kg`(~2782,INSERT knowledge_objects 块)、`_migrate`/schema、`search_notebook`(~5047)、`build_scale_index`(ANN 载入处,供语义)、route 注册风格(routes.py:502/796)。

- [ ] **Step 1: 失败测试**(repo + API):建库存几个 concept(`store_kg`),断言 `repo.kg_search(nb, "mirror", k=10)` 返回含名的命中(词法);`GET /notebooks/{nb}/kg/search?q=mirror` 200 且结构正确。
- [ ] **Step 2: FTS5 schema** — `_migrate` 加 `CREATE VIRTUAL TABLE IF NOT EXISTS kg_objects_fts USING fts5(object_id UNINDEXED, notebook_id UNINDEXED, name, tokenize='trigram')`。
- [ ] **Step 3: 维护** — `store_kg` 写 knowledge_objects 后,同批 `INSERT INTO kg_objects_fts(object_id,notebook_id,name) VALUES(?,?,?)`(name 取 payload.name;空名跳过);KG 删除处(`delete_notebook_kg`/对象删除)`DELETE FROM kg_objects_fts WHERE object_id=?` 或 `notebook_id=?`。加 `backfill_kg_fts(notebook_id)`:清该 nb 的 FTS 行后从 knowledge_objects 重灌(供存量)。
- [ ] **Step 4: `kg_search(notebook_id, q, k=30)`** — 词法:`from app.services.kg.search import fts_search, merge_search_hits`;`lex = fts_search(db, nb, q, k)`。语义:若 `self._scale_index(nb)`,embed q → 载入 hnswlib(`idx.ann_path`)→ `knn_query(k)` → 映射 `ann_labels` → `[{object_id,name(待 hydrate),score=1-dist,match:'semantic'}]`(只保留 KG 对象、非 chunk/hub)。`merge_search_hits(lex, sem, k)` → hydrate 名/类型(按 object_id 回 DB) → 返回。无索引 → 只词法;无 FTS(老库未 backfill)→ 回退 `search_notebook`。
- [ ] **Step 5: route + schema** — `schemas.py` 加 `KgSearchHit{object_id,name,object_type,score,match}` / `KgSearchResponse{query,hits}`;`routes.py` 加 `GET /notebooks/{id}/kg/search?q=&k=`(`require_notebook_access`)调 `repo.kg_search`。
- [ ] **Step 6:** `pytest tests/test_kg_search_api.py tests/test_kg_search.py -q` + 相关回归 → all pass。
- [ ] **Step 7: 提交** — `git commit -m "feat(kg-viz): FTS5 表+维护+backfill + kg_search(词法∪语义)+ /kg/search 端点"`

---

## Task 4: viz 集成(构建期持久化折叠图 + 有界 unified_graph + /neighbors)[串行,依赖 Task 2、3]

**Files:** Modify `scale_index.py`(save/load viz 字段)、`sqlite_repository.py`、`routes.py`; Test `backend/tests/test_viz_bounded.py`(新)+ `test_unified_kg_repository.py`(回归).

**先读**:`build_scale_index`(已 derive 折叠图?否——它建 PPR 图;viz 折叠图需 `derive_unified_graph(nodes,edges,cluster_map)` 的 concept-level 输出)、`unified_graph`/`_unified_graph_full`/`limit_graph_by_degree`、`concept_detail`(folded 邻域兜底参考)。

- [ ] **Step 1: 等价失败测试**(`test_viz_bounded.py`,fixture 同 `test_unified_kg_repository`):小库 `build_scale_index` 后,`unified_graph(nb, level="object", limit=2)` 的 nodes/edges == 旧路径同 limit 结果(顺序无关比较 id 集合 + 边集合)。
- [ ] **Step 2: 持久化 viz 折叠图** — `build_scale_index` 末尾额外:用 `derive_unified_graph` 得折叠 concept-level 图(nodes/edges),算每节点度数 + 邻接 CSR + types,`np.savez` 到 `{out_dir}/viz.npz`(viz_ids/viz_deg/viz_types + adj 经 `sp.save_npz` 到 `viz_adj.npz`);manifest 记 viz 存在。`save_scale_index`/`load_scale_index` 加 viz 字段(load 惰性可选)。`ScaleIndex` 加 `viz_ids/viz_adj/viz_deg/viz_types`(load 时填充或 None)。
- [ ] **Step 3: 有界 `unified_graph` 分派** — 在 `unified_graph` 开头:若 `idx=self._scale_index(nb)` 且其 viz 图有效且 `limit` 指定 → `core = viz_core({viz_*}, limit)`;按 `core` 节点 id(canonical)回 `knowledge_objects`/`concept_clusters` hydrate 展示名,组装与现 `unified_graph` 同形返回。否则走现 `_unified_graph_full` 路径(小库不回归)。
- [ ] **Step 4: `/objects/{oid}/neighbors`** — `repo.kg_neighbors(nb, oid, cap)`:有 viz 索引 → `viz_neighbors(...)` + hydrate 名;否则 `knowledge_relations` 邻接索引兜底(1-hop,cap)。`routes.py` 加 `GET /notebooks/{id}/objects/{object_id}/neighbors?cap=`。
- [ ] **Step 5:** `pytest tests/test_viz_bounded.py tests/test_unified_kg_repository.py tests/test_scale_index.py -q` → all pass(等价守护)。
- [ ] **Step 6: 提交** — `git commit -m "feat(kg-viz): 构建期持久化折叠viz图 + unified_graph 有界分派 + /neighbors 端点"`

---

## Task 5: 前端(搜索改服务端 + 去「全部」+ 点击展开)[依赖 Task 3、4]

**Files:** Modify `frontend/app/page.tsx`; Test 前端测试.

**先读**:`fetchUnifiedGraph`(595)、`openKgView`(1986)、`ensureFullGraph`(2006)、fgData 搜索 useMemo(~1193)、`KG_RANGE_STEPS`(599)、`base_kg_available` 字段。

- [ ] **Step 1:** 加 `fetchKgSearch(nb,q)` → `GET /kg/search`;`fetchNeighbors(nb,oid)` → `/objects/{oid}/neighbors`。
- [ ] **Step 2:** 搜索改服务端:`kgSearch` 非空 → 调 `fetchKgSearch`,结果作为命中节点渲染(替换 `uGraphFull` 懒加载 + 客户端过滤);删除 `ensureFullGraph`/`uGraphFull` 的全量拉取(`fetchUnifiedGraph(nb,0)`)。点命中/节点 → `fetchNeighbors` 并入当前视图。
- [ ] **Step 3:** 「全部」档位:`KG_RANGE_STEPS` 据 `currentNotebook?.base_kg_available`(或新标志)动态——index-backed 大库去掉 `{value:0,label:"全部"}`,小库保留。
- [ ] **Step 4:** tsc + 前端测试(`npm run lint` + `npm test`,用主 checkout node_modules 软链);断言搜索不再发 `limit=0`(若有相关测试)。
- [ ] **Step 5: 视觉验证** — show_widget/preview 还原命中列表 + 展开交互(按 [[ui-polish-bar]]);给用户截图。
- [ ] **Step 6: 提交** — `git commit -m "feat(kg-viz): 前端搜索改服务端 + 去「全部」+ 点击逐跳展开"`

---

## Task 6: gated 规模慢测 + 全量回归

**Files:** `backend/tests/test_viz_bounded.py`(slow);全量.

- [ ] **Step 1:** `@pytest.mark.slow` 合成大库(如 5万 concept + 边),`build_scale_index` 后断言 `unified_graph(limit=80)` 与 `kg_search` 有界完成(记录耗时,断言不物化全量——无 `_unified_graph_full` 调用,可 monkeypatch 监视)。
- [ ] **Step 2:** 跑 slow,记录耗时。
- [ ] **Step 3:** 全量(非 slow)`pytest -q -m "not slow"` + 前端 → all pass。
- [ ] **Step 4: 提交** — `git commit -m "test(kg-viz): viz/search 规模 gated 慢测 + 全量回归"`

---

## 收尾
- [ ] rebase 到 origin/master 线性 → push → `gh pr create --base master`。PR 附实测耗时、等价测试结论、FTS5/ANN 双路与「全部」UX 说明。
