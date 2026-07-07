# 跨文档社区层 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 建一个持久、通用、领域无关的跨文档"社区层"，让对比/广度类问题能从焦点实体走到其跨文档同社区兄弟（在 ask 与深度报告两路生效），取代已否决的 find_siblings/词典方案。

**Architecture:** 三层。L1 社区资产：在 canonical 实体图（关系两端经 `cluster_map` 映射）上跑 Louvain，持久化到 `communities` + 新增反向索引表 `community_members`，接进 KG 重建，大库无 CSR 时硬拒 networkx（fail-open + 事件）。L2 检索集成：`community_peers(base_nb, focal_name)` 取焦点社区成员按 (关键词×中心度) 排序，由 reasoning 的 `expand_community` reflect 动作（模型驱动）+ chunk 的 `comparison` 字段 + 报告 STORM 提示触发。L3（本 plan 外）：按需 community report、PPR 社区 hub、分层、igraph-on-CSR scale 路径。

**Tech Stack:** Python 3.13、FastAPI、SQLite（自研 `SQLiteRepository`）、networkx（Louvain，已有依赖）、pytest。设计文档：`docs/superpowers/specs/2026-07-07-cross-document-community-layer-design.md`。

## Global Constraints

- **运行效率一等约束**：不新增每问 LLM/embed 调用；社区检测是离线/增量资产（纯图、无 LLM）；`expand_community` 仅模型触发才 fan-out。
- **大库绝不暴力**：scale-tier（`notebook_copy_stats(nb)["copyable"]==False`）无持久化 CSR（`_scale_index(nb, allow_stale=True) is None`）时，`rebuild_communities` 拒 networkx，emit 事件，返回 0。绝不 OOM/hang。
- **fail-open + 可观测**：社区层任何缺失 → 退回"无兄弟扩展"现状 + `event_log.emit`，查询侧永不 crash/变慢/静默零召回。
- **config 用 `validation_alias`**（pydantic-settings v2，`Field(env=...)` 对新字段失效）。
- **确定性**：Louvain `seed=42`；下游只认 `member_ids`/`community_members` 内容，不认社区 id（每次重建可能变）。
- **不堆 God 对象**：新原语放 `backend/app/services/communities.py`，被 reasoning/chunk 消费。
- 中文交互；提交信息尾加 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。

---

## Task 1: 配置项

**Files:**
- Modify: `backend/app/core/config.py`（Settings 字段区，紧邻 `report_section_top_n` 一带）
- Test: `backend/tests/test_community_config.py`

**Interfaces:**
- Produces: `settings.community_layer_enabled: bool`, `settings.community_min_size: int`, `settings.community_peers_topk: int`, `settings.community_rerank_candidates: int`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_community_config.py
from app.core.config import Settings

def test_community_defaults():
    s = Settings(_env_file=None)
    assert s.community_layer_enabled is True
    assert s.community_min_size == 3
    assert s.community_peers_topk == 8
    assert s.community_rerank_candidates == 200

def test_community_env_alias(monkeypatch):
    monkeypatch.setenv("COMMUNITY_PEERS_TOPK", "5")
    assert Settings(_env_file=None).community_peers_topk == 5
```

- [ ] **Step 2: 跑，确认失败**

Run: `cd backend && python -m pytest tests/test_community_config.py -q`
Expected: FAIL（`AttributeError`/字段不存在）

- [ ] **Step 3: 加字段**

在 `backend/app/core/config.py` 的 Settings 类里加：

```python
    community_layer_enabled: bool = Field(True, validation_alias="COMMUNITY_LAYER_ENABLED")
    community_min_size: int = Field(3, validation_alias="COMMUNITY_MIN_SIZE")
    community_peers_topk: int = Field(8, validation_alias="COMMUNITY_PEERS_TOPK")
    community_rerank_candidates: int = Field(200, validation_alias="COMMUNITY_RERANK_CANDIDATES")
```

- [ ] **Step 4: 跑，确认通过**

Run: `cd backend && python -m pytest tests/test_community_config.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/core/config.py backend/tests/test_community_config.py
git commit -m "feat(community): 社区层配置项(validation_alias)"
```

---

## Task 2: 反向索引表 `community_members`

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`（schema 初始化，`communities` 建表附近 ~line 1017；及 `_add_column_if_missing` 迁移段 ~line 1127）
- Test: `backend/tests/test_community_members_schema.py`

**Interfaces:**
- Produces: 表 `community_members(canonical_id TEXT, notebook_id TEXT, level INT, community_id TEXT, canonical_name TEXT, centrality REAL)` + 索引 `(notebook_id, canonical_id)` 与 `(notebook_id, community_id)`。

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_community_members_schema.py
from tests.conftest import make_repo  # 复用现有 fixture 工厂;若无则用 SQLiteRepository(tmp)

def test_community_members_table(tmp_path):
    from app.services.sqlite_repository import SQLiteRepository
    from app.core.config import Settings
    repo = SQLiteRepository(str(tmp_path / "t.db"), Settings(_env_file=None))
    with repo._connect() as db:
        cols = {r["name"] for r in db.execute("PRAGMA table_info(community_members)")}
    assert {"canonical_id","notebook_id","level","community_id","canonical_name","centrality"} <= cols
```

（注：`SQLiteRepository` 构造签名以现有代码为准——若为 `SQLiteRepository(settings)` 且 db 路径走 settings，则按现有测试写法构造。实现者先看 `backend/tests/conftest.py` 的既有 repo fixture 并复用。）

- [ ] **Step 2: 跑，确认失败**

Run: `cd backend && python -m pytest tests/test_community_members_schema.py -q`
Expected: FAIL（无 `community_members` 表）

- [ ] **Step 3: 建表 + 索引**

在 `communities` 建表语句（`CREATE TABLE IF NOT EXISTS communities ...`, ~line 1017）之后追加：

```python
                CREATE TABLE IF NOT EXISTS community_members (
                  canonical_id TEXT NOT NULL,
                  notebook_id TEXT NOT NULL,
                  level INTEGER NOT NULL DEFAULT 0,
                  community_id TEXT NOT NULL,
                  canonical_name TEXT NOT NULL DEFAULT '',
                  centrality REAL NOT NULL DEFAULT 0
                );
                CREATE INDEX IF NOT EXISTS idx_commmem_nb_can ON community_members(notebook_id, canonical_id);
                CREATE INDEX IF NOT EXISTS idx_commmem_nb_comm ON community_members(notebook_id, community_id);
```

（跟随该处 `executescript` 的既有风格；若该处是逐句 `db.execute`，则逐句加。）

- [ ] **Step 4: 跑，确认通过**

Run: `cd backend && python -m pytest tests/test_community_members_schema.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_community_members_schema.py
git commit -m "feat(community): 反向索引表 community_members(canonical→社区,O(1)定位)"
```

---

## Task 3: `rebuild_communities` 改喂 canonical 图 + 大库守卫 + 反向索引

**Files:**
- Modify: `backend/app/services/sqlite_repository.py:6629`（`rebuild_communities`）
- Test: `backend/tests/test_rebuild_communities.py`

**Interfaces:**
- Consumes: `self.cluster_map(nb)`（member_object_id→canonical_id）、`self.notebook_copy_stats(nb)["copyable"]`、`self._scale_index(nb, allow_stale=True)`、`self.event_log.emit`、`self.settings.community_min_size`、`self.settings.community_layer_enabled`
- Produces: `rebuild_communities(notebook_id, level=0) -> int`（写 `communities` + `community_members`，member_ids/centrality=canonical 维度；返回入库社区数）

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_rebuild_communities.py
# 构造:两篇"文档"(source A/B)各自内部关系 + 一个共享 canonical(cluster 把 A/B 的同名对象并一起)
# 断言:A/B 通过共享 canonical 落进同一社区(跨文档);裸关系版会分裂(对照见 assert)。
def test_canonical_bridges_documents(repo_with_kg):
    repo = repo_with_kg  # fixture: 已插入 knowledge_objects/relations/concept_clusters 的 base nb
    n = repo.rebuild_communities("nb-test", level=0)
    assert n >= 1
    with repo._connect() as db:
        # 共享 canonical "shared-x" 应与 A、B 的对象同社区
        rows = db.execute("SELECT community_id FROM community_members WHERE notebook_id='nb-test' AND canonical_id IN ('canA','canB','shared-x')").fetchall()
    assert len({r["community_id"] for r in rows}) == 1  # 同一社区 => 跨文档桥成立

def test_large_no_index_refuses(repo_large_no_csr):
    repo = repo_large_no_csr  # fixture: notebook_copy_stats.copyable=False 且 _scale_index=None
    events = []
    repo.event_log.emit = lambda e: events.append(e)
    assert repo.rebuild_communities("nb-big", level=0) == 0
    assert any(e.get("kind") == "community_build_refused" for e in events)

def test_min_size_filter(repo_with_kg):
    repo = repo_with_kg
    repo.settings.community_min_size = 999  # 没有社区达标
    assert repo.rebuild_communities("nb-test", level=0) == 0
```

（实现者按 `backend/tests/conftest.py` 既有 fixture 风格补 `repo_with_kg`/`repo_large_no_csr`；若无现成，写最小 fixture：内存 repo + 直插 3 表。）

- [ ] **Step 2: 跑，确认失败**

Run: `cd backend && python -m pytest tests/test_rebuild_communities.py -q`
Expected: FAIL

- [ ] **Step 3: 重写 `rebuild_communities`**

替换 `backend/app/services/sqlite_repository.py:6629` 的方法体为：

```python
    def rebuild_communities(self, notebook_id: str, level: int = 0) -> int:
        """在 canonical 实体图(关系两端经 cluster_map 映射)上跑 Louvain 社区检测,
        持久化到 communities + community_members(反向索引,存 canonical_name/centrality)。
        无 LLM、确定性(seed=42)。大库(scale-tier)无持久化 CSR → 拒 networkx(避免 OOM),
        emit community_build_refused 返回 0。返回入库社区数。"""
        self.get_notebook(notebook_id)
        if not self.settings.community_layer_enabled:
            return 0
        # 大库守卫:绝不在 scale-tier 无 CSR 时用 networkx 暴力建图。
        if (not self.notebook_copy_stats(notebook_id)["copyable"]
                and self._scale_index(notebook_id, allow_stale=True) is None):
            self.event_log.emit({"kind": "community_build_refused",
                                 "notebook_id": notebook_id, "reason": "no_scale_index"})
            return 0
        import networkx as nx
        from networkx.algorithms.community import louvain_communities
        cmap = self.cluster_map(notebook_id)  # member_object_id -> canonical_id
        with self._connect() as db:
            rels = db.execute(
                "SELECT source_object_id, target_object_id FROM knowledge_relations WHERE notebook_id=?",
                (notebook_id,)).fetchall()
            names = {r["canonical_id"]: r["canonical_name"] for r in db.execute(
                "SELECT DISTINCT canonical_id, canonical_name FROM concept_clusters WHERE notebook_id=?",
                (notebook_id,))}
        g = nx.Graph()
        for r in rels:
            s = cmap.get(r["source_object_id"], r["source_object_id"])
            t = cmap.get(r["target_object_id"], r["target_object_id"])
            if not s or not t or s == t:
                continue
            if g.has_edge(s, t):
                g[s][t]["weight"] += 1
            else:
                g.add_edge(s, t, weight=1)
        comms = louvain_communities(g, weight="weight", seed=42) if g.number_of_nodes() else []
        now = _now()
        min_size = self.settings.community_min_size
        kept = 0
        with self._write() as db:
            db.execute("DELETE FROM communities WHERE notebook_id=? AND level=?", (notebook_id, level))
            db.execute("DELETE FROM community_members WHERE notebook_id=? AND level=?", (notebook_id, level))
            for comm in comms:
                if len(comm) < min_size:
                    continue
                cid = f"cm-{uuid4().hex[:10]}"
                members = sorted(comm)
                db.execute(
                    "INSERT INTO communities (id, notebook_id, level, member_ids, size, created_at) "
                    "VALUES (?,?,?,?,?,?)",
                    (cid, notebook_id, level, json.dumps(members), len(members), now))
                db.executemany(
                    "INSERT INTO community_members "
                    "(canonical_id, notebook_id, level, community_id, canonical_name, centrality) "
                    "VALUES (?,?,?,?,?,?)",
                    [(m, notebook_id, level, cid, names.get(m, m), float(g.degree(m))) for m in members])
                kept += 1
        self.event_log.emit({"kind": "communities_rebuilt", "notebook_id": notebook_id,
                             "level": level, "communities": kept, "nodes": g.number_of_nodes()})
        return kept
```

（`uuid4`、`json`、`_now` 该文件顶部已 import；核对无缺再改。）

- [ ] **Step 4: 跑，确认通过**

Run: `cd backend && python -m pytest tests/test_rebuild_communities.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_rebuild_communities.py
git commit -m "feat(community): rebuild_communities喂canonical图(跨文档)+大库拒暴力守卫+反向索引"
```

---

## Task 4: 接进 KG 重建

**Files:**
- Modify: `backend/app/services/sqlite_repository.py:6301`（`rebuild_unified_kg`，末尾聚类完成后）
- Test: `backend/tests/test_rebuild_wires_communities.py`

**Interfaces:**
- Consumes: Task 3 的 `rebuild_communities`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_rebuild_wires_communities.py
def test_rebuild_unified_kg_builds_communities(repo_with_kg, monkeypatch):
    repo = repo_with_kg
    called = {}
    orig = repo.rebuild_communities
    def spy(nb, level=0):
        called["nb"] = nb; return orig(nb, level)
    monkeypatch.setattr(repo, "rebuild_communities", spy)
    repo.rebuild_unified_kg("nb-test", force=True)
    assert called.get("nb") == "nb-test"
```

- [ ] **Step 2: 跑，确认失败**

Run: `cd backend && python -m pytest tests/test_rebuild_wires_communities.py -q`
Expected: FAIL

- [ ] **Step 3: 在 `rebuild_unified_kg` 末尾接入**

在 `rebuild_unified_kg` 的 `return`（成功路径）之前加（聚类已完成后）：

```python
        # 社区层:聚类稳定后重建(纯图、无 LLM、fail-open——绝不拖垮 KG 重建)。
        try:
            self.rebuild_communities(notebook_id, level=0)
        except Exception as exc:  # noqa: BLE001
            self.event_log.emit({"kind": "communities_rebuild_failed",
                                 "notebook_id": notebook_id, "error": str(exc)[:200]})
```

- [ ] **Step 4: 跑，确认通过**

Run: `cd backend && python -m pytest tests/test_rebuild_wires_communities.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_rebuild_wires_communities.py
git commit -m "feat(community): 社区检测接进 rebuild_unified_kg(fail-open,不拖垮重建)"
```

---

## Task 5: `community_peers` 原语（新模块 `communities.py`）

**Files:**
- Create: `backend/app/services/communities.py`
- Test: `backend/tests/test_community_peers.py`

**Interfaces:**
- Consumes: `repo._connect()`、`repo.event_log.emit`、`from app.services.retrieval import keyword_score`
- Produces:
  - `first_base_notebook_id(repo, active_nb) -> str | None`
  - `community_peers(repo, base_nb, focal_name, query, *, top_k, candidates) -> list[str]`（焦点社区兄弟实体名，按 (keyword_score(query,name) desc, centrality desc) 排序去重截断；缺失 fail-open 返回 [] + emit `community_unavailable`）

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_community_peers.py
from app.services.communities import community_peers, first_base_notebook_id

def test_peers_from_community(repo_with_communities):
    # fixture: base nb 已 rebuild_communities;焦点 "DeepSeek-V4" 与 "Qwen-X"/"GPT-Y" 同社区
    repo = repo_with_communities
    peers = community_peers(repo, "nb-base", "DeepSeek-V4", "efficiency", top_k=5, candidates=50)
    assert any("qwen" in p.lower() for p in peers)
    assert all("deepseek-v4" != p.lower() for p in peers)  # 排除焦点自身

def test_focal_unresolved_failopen(repo_with_communities):
    repo = repo_with_communities
    events = []; repo.event_log.emit = lambda e: events.append(e)
    assert community_peers(repo, "nb-base", "NoSuchModelXYZ", "q", top_k=5, candidates=50) == []
    assert any(e["kind"] == "community_unavailable" for e in events)

def test_not_built_failopen(repo_no_communities):
    repo = repo_no_communities
    events = []; repo.event_log.emit = lambda e: events.append(e)
    assert community_peers(repo, "nb-base", "DeepSeek-V4", "q", top_k=5, candidates=50) == []
    assert any(e["kind"] == "community_unavailable" for e in events)
```

- [ ] **Step 2: 跑，确认失败**

Run: `cd backend && python -m pytest tests/test_community_peers.py -q`
Expected: FAIL（模块不存在）

- [ ] **Step 3: 写 `communities.py`**

```python
# backend/app/services/communities.py
"""社区感知检索原语:焦点实体 → base 库所在社区 → 同社区兄弟实体名。
纯查表 + 廉价词法重排;任何缺失 fail-open 返回 [] 并 emit 事件(绝不静默零召回)。"""
from __future__ import annotations
from typing import List, Optional


def _norm(s: str) -> str:
    return " ".join((s or "").split()).lower()


def first_base_notebook_id(repo, active_nb: str) -> Optional[str]:
    with repo._connect() as db:
        row = db.execute(
            "SELECT id FROM notebooks WHERE tier='base' AND id != ? ORDER BY updated_at DESC LIMIT 1",
            (active_nb,)).fetchone()
    return row["id"] if row else None


def community_peers(repo, base_nb: str, focal_name: str, query: str, *,
                    top_k: int, candidates: int) -> List[str]:
    from app.services.retrieval import keyword_score
    key = _norm(focal_name)
    if not base_nb or not key:
        return []
    with repo._connect() as db:
        frow = db.execute(
            "SELECT canonical_id FROM concept_clusters WHERE notebook_id=? AND lower(canonical_name)=? "
            "GROUP BY canonical_id ORDER BY COUNT(*) DESC LIMIT 1", (base_nb, key)).fetchone()
        if not frow:
            repo.event_log.emit({"kind": "community_unavailable", "notebook_id": base_nb,
                                 "reason": "focal_unresolved", "focal": focal_name})
            return []
        focal_can = frow["canonical_id"]
        crow = db.execute(
            "SELECT community_id FROM community_members WHERE notebook_id=? AND canonical_id=? "
            "ORDER BY level DESC LIMIT 1", (base_nb, focal_can)).fetchone()
        if not crow:
            repo.event_log.emit({"kind": "community_unavailable", "notebook_id": base_nb,
                                 "reason": "not_built", "focal": focal_name})
            return []
        rows = db.execute(
            "SELECT canonical_name, centrality FROM community_members "
            "WHERE notebook_id=? AND community_id=? AND canonical_id!=? "
            "ORDER BY centrality DESC LIMIT ?", (base_nb, crow["community_id"], focal_can, candidates)
        ).fetchall()
    ranked = sorted(rows, key=lambda r: (keyword_score(query, r["canonical_name"] or ""),
                                         r["centrality"]), reverse=True)
    seen, out = set(), []
    for r in ranked:
        nm = (r["canonical_name"] or "").strip()
        k = _norm(nm)
        if nm and k not in seen:
            seen.add(k); out.append(nm)
        if len(out) >= top_k:
            break
    return out
```

- [ ] **Step 4: 跑，确认通过**

Run: `cd backend && python -m pytest tests/test_community_peers.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/communities.py backend/tests/test_community_peers.py
git commit -m "feat(community): community_peers原语(焦点→社区→兄弟名,fail-open+可观测)"
```

---

## Task 6: reasoning `expand_community` 动作 — prompt/schema/reflect 解析

**Files:**
- Modify: `backend/app/services/prompts.py:233`（`REFLECT_SCHEMA_HINT`）、`:241`（`reflect_prompt`）
- Modify: `backend/app/services/reasoning_retrieval.py:69`（`ReflectDecision`）、`:134`（`reflect()`）、`:150`（action 白名单）
- Test: `backend/tests/test_reflect_expand_community.py`

**Interfaces:**
- Produces: `ReflectDecision.community_focal: str`；`reflect()` 能解析 `next_action="expand_community"` + `community_focal`

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_reflect_expand_community.py
from app.services.reasoning_retrieval import ReasoningRetriever

class _StubLLM:
    configured = True
    def __init__(self, payload): self._p = payload
    def chat_json(self, *a, **k): return self._p

def test_reflect_parses_expand_community(monkeypatch):
    repo = type("R", (), {"reasoning_llm_client": _StubLLM(
        '{"next_action":"expand_community","community_focal":"DeepSeek-V4","reason":"need peers"}')})()
    rr = ReasoningRetriever(repo, type("S", (), {"reasoning_timeout_seconds":1,"reasoning_max_retries":0})())
    d = rr.reflect("q", "candidates")
    assert d.next_action == "expand_community"
    assert d.community_focal == "DeepSeek-V4"
```

- [ ] **Step 2: 跑，确认失败**

Run: `cd backend && python -m pytest tests/test_reflect_expand_community.py -q`
Expected: FAIL

- [ ] **Step 3: 改 schema + prompt + 解析**

`prompts.py` `REFLECT_SCHEMA_HINT`（`:233`）在 action 枚举加 `expand_community`，并加字段：
```python
REFLECT_SCHEMA_HINT = (
    '{"sufficient":false,"next_action":"answer|expand_graph|add_subquery|'
    'search_elements|ppr_retrieve|expand_community","expand":{"object_id":"","edge_type":null,'
    '"direction":"out|in|both"},"new_sub_query":{"query":"","types":[],'
    '"prefer":"balanced","reason":""},"community_focal":"","elements_query":"","ppr_query":"","reason":""}'
)
```
`reflect_prompt`（`:241`）在 `ppr_retrieve` 说明之后加一条动作说明：
```python
        "- expand_community: the question compares an entity with its peers / other "
        "of-its-kind, and those peers are missing from candidates; pull the entity's "
        "SEMANTIC COMMUNITY members across documents (set community_focal to the entity "
        "name, e.g. 'DeepSeek-V4'). Use for 'X vs other Y' questions.\n"
```
`reasoning_retrieval.py` `ReflectDecision`（`:69`）加字段：
```python
    community_focal: str = ""
```
`reflect()` action 白名单（`:150`）加 `"expand_community"`；并在解析块里（`ppr_query` 附近）加：
```python
            d.community_focal = str(data.get("community_focal", "")).strip()
```

- [ ] **Step 4: 跑，确认通过**

Run: `cd backend && python -m pytest tests/test_reflect_expand_community.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/prompts.py backend/app/services/reasoning_retrieval.py backend/tests/test_reflect_expand_community.py
git commit -m "feat(community): reflect新增expand_community动作(schema+prompt+解析)"
```

---

## Task 7: reasoning `run()` 的 `expand_community` 分支

**Files:**
- Modify: `backend/app/services/reasoning_retrieval.py`（`run()`：run-locals 初始化处加 `community_focals_done=set()`；action 分派 `elif decision.next_action == "ppr_retrieve"` 分支之后加新分支）
- Test: `backend/tests/test_run_expand_community.py`

**Interfaces:**
- Consumes: `community_peers`、`first_base_notebook_id`（Task 5）、`self.search`、run 内 `collected/attempted/used_queries/question`
- Produces: run 中 `expand_community` 触发 → 对焦点社区兄弟发子查询、折进 collected、同 focal 只一次

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_run_expand_community.py
# 用 stub:community_peers 返回 ["Qwen-X","GPT-Y"];断言这两个成为子查询、命中折进 collected、二次触发跳过。
def test_run_fans_out_community_peers(monkeypatch, reasoning_env):
    rr, notebook_id = reasoning_env  # fixture: ReasoningRetriever + 可被 search 命中的 base
    import app.services.communities as C
    monkeypatch.setattr(C, "community_peers", lambda *a, **k: ["Qwen-X", "GPT-Y"])
    monkeypatch.setattr(C, "first_base_notebook_id", lambda *a, **k: "nb-base")
    # 让 reflect 头一轮返回 expand_community,再返回 answer
    ...
    result = rr.run(notebook_id, "分析X相比其他Y")
    used = [a["query"] for a in result.attempted]
    assert any("Qwen-X" in q for q in used) and any("GPT-Y" in q for q in used)
```

（实现者按现有 `reasoning_retrieval` 测试的 stub 风格补 `reasoning_env`；参考仓库既有 reasoning 测试。）

- [ ] **Step 2: 跑，确认失败**

Run: `cd backend && python -m pytest tests/test_run_expand_community.py -q`
Expected: FAIL

- [ ] **Step 3: 加 run-local + 分支**

在 `run()` 初始化 run-local（`visited`/`seen_chunks` 一带）加：
```python
        community_focals_done: set = set()
```
在 `elif decision.next_action == "ppr_retrieve":` 整块之后、`else: break` 之前加：
```python
            elif decision.next_action == "expand_community":
                from app.services.communities import community_peers, first_base_notebook_id
                focal_name = decision.community_focal or (
                    max(collected.values(), key=lambda h: h.score).payload.get("name", "")
                    if collected else "")
                fkey = _norm_query(focal_name)
                if not focal_name or fkey in community_focals_done:
                    record(TraceStep(step_type="skip",
                                     summary="跳过 expand_community(无焦点或已扩展)",
                                     detail={"reason": "no_focal_or_done", "focal": focal_name}))
                else:
                    community_focals_done.add(fkey)
                    base_nb = first_base_notebook_id(self.repo, notebook_id)
                    peers = community_peers(
                        self.repo, base_nb, focal_name, question,
                        top_k=self.settings.community_peers_topk,
                        candidates=self.settings.community_rerank_candidates) if base_nb else []
                    added, names = 0, []
                    for pname in peers:
                        raise_if_cancelled(self.cancel_event)
                        key = _norm_query(pname)
                        if key in attempted:
                            continue
                        got = 0
                        for h in self.search(notebook_id, pname)[:_PER_QUERY_LIMIT]:
                            if h.object_id not in collected:
                                collected[h.object_id] = h; added += 1; got += 1
                        attempted[key] = _QueryAttempt(query=pname, new=got, tries=1)
                        if pname not in used_queries:
                            used_queries.append(pname)
                        names.append(pname)
                    record(TraceStep(step_type="expand_community",
                                     summary=f"横向对比:纳入 {len(names)} 个同社区实体,新增候选 {added}",
                                     detail={"focal": focal_name, "peers": names, "new": added}))
```

- [ ] **Step 4: 跑，确认通过**

Run: `cd backend && python -m pytest tests/test_run_expand_community.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/reasoning_retrieval.py backend/tests/test_run_expand_community.py
git commit -m "feat(community): run()新增expand_community分支(社区兄弟fan-out,同focal一次)"
```

---

## Task 8: chunk 模式 `comparison` 字段

**Files:**
- Modify: `backend/app/services/prompts.py:337`（`EXPAND_SCHEMA_HINT`）、`:341`（`expand_query_prompt`）
- Modify: `backend/app/services/query_rewrite.py:32`（`ExpandedQuery`）、`:40`（`expand_query` 解析）
- Modify: `backend/app/services/sqlite_repository.py:10988`（`ask_chunk` expand 调用点后消费）
- Test: `backend/tests/test_expand_comparison_field.py`

**Interfaces:**
- Produces: `ExpandedQuery.comparison: Optional[dict]`（`{"focal": str}` 或 None）；`ask_chunk` 命中时追加社区兄弟子查询

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_expand_comparison_field.py
from app.services.query_rewrite import expand_query

class _LLM:
    configured = True
    def chat_json(self, *a, **k):
        return '{"query":"q","sub_queries":[{"query":"a"}],"comparison":{"focal":"DeepSeek-V4"}}'

def test_expand_parses_comparison():
    ex = expand_query(_LLM(), "分析DeepSeek-V4相比其他llm")
    assert ex.comparison == {"focal": "DeepSeek-V4"}

def test_expand_comparison_absent_is_none():
    class L2:
        configured = True
        def chat_json(self, *a, **k): return '{"query":"q","sub_queries":[{"query":"a"}]}'
    assert expand_query(L2(), "介绍DeepSeek-V4").comparison is None
```

- [ ] **Step 2: 跑，确认失败**

Run: `cd backend && python -m pytest tests/test_expand_comparison_field.py -q`
Expected: FAIL

- [ ] **Step 3: 加字段 + 解析 + 消费**

`query_rewrite.py` `ExpandedQuery`（`:32`）加：
```python
    comparison: Optional[dict] = None
```
`expand_query` 成功分支（return `ExpandedQuery(...)` 处，`:99`）解析：
```python
        comp = data.get("comparison")
        comparison = None
        if isinstance(comp, dict) and str(comp.get("focal", "")).strip():
            comparison = {"focal": str(comp["focal"]).strip()}
        return ExpandedQuery(query=query, sub_queries=out,
                             high_level_keywords=hl, low_level_keywords=ll,
                             comparison=comparison)
```
`prompts.py` `EXPAND_SCHEMA_HINT`（`:337`）加 `,"comparison":{"focal":""}`；`expand_query_prompt`（`:341`）在 sub_queries 规则后加一句：
```python
        "If the question compares an entity with others of its kind (e.g. 'X vs "
        "other LLMs'), also set comparison.focal to that entity's canonical name; "
        "omit comparison otherwise.\n"
```
`sqlite_repository.py` `ask_chunk`（`:10988` expand 之后）加：
```python
                if ex.comparison and self.settings.community_layer_enabled:
                    from app.services.communities import community_peers, first_base_notebook_id
                    base_nb = first_base_notebook_id(self, notebook_id)
                    if base_nb:
                        for pname in community_peers(self, base_nb, ex.comparison["focal"],
                                                     retrieval_query,
                                                     top_k=self.settings.community_peers_topk,
                                                     candidates=self.settings.community_rerank_candidates):
                            ex.sub_queries.append(SubQuerySpec(query=pname))
```
（`SubQuerySpec` 从 `query_rewrite` import；`retrieval_query` 为该处上下文已有变量,核对名称。）

- [ ] **Step 4: 跑，确认通过**

Run: `cd backend && python -m pytest tests/test_expand_comparison_field.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/prompts.py backend/app/services/query_rewrite.py backend/app/services/sqlite_repository.py backend/tests/test_expand_comparison_field.py
git commit -m "feat(community): chunk模式comparison字段→追加社区兄弟子查询"
```

---

## Task 9: 报告 STORM 规划提示

**Files:**
- Modify: `backend/app/services/prompts.py:488`（`report_storm_outline_prompt`）
- Test: `backend/tests/test_storm_comparison_hint.py`

**Interfaces:**
- Produces: STORM prompt 含"对比题规划横向对比一节"的指令（该节深挖时由 Task 7 的 `expand_community` 落地）

- [ ] **Step 1: 写失败测试**

```python
# backend/tests/test_storm_comparison_hint.py
from app.services.prompts import report_storm_outline_prompt

def test_storm_prompt_mentions_comparison_section():
    p = report_storm_outline_prompt("分析X相比其他Y", "corpus", max_sections=6)
    assert "横向对比" in p or "cross-model" in p.lower() or "compare" in p.lower()
```

- [ ] **Step 2: 跑，确认失败**

Run: `cd backend && python -m pytest tests/test_storm_comparison_hint.py -q`
Expected: FAIL（除非现有 prompt 已含 compare 字样——若已含,调整断言为新增的横向对比指令关键词）

- [ ] **Step 3: 加提示**

在 `report_storm_outline_prompt` 的规则串里加一句：
```python
        "If the question compares an entity with its peers, plan ONE dedicated "
        "cross-model comparison section (横向对比) whose sub_queries target the peer "
        "entities' corresponding dimensions.\n"
```

- [ ] **Step 4: 跑，确认通过**

Run: `cd backend && python -m pytest tests/test_storm_comparison_hint.py -q`
Expected: PASS

- [ ] **Step 5: 提交**

```bash
git add backend/app/services/prompts.py backend/tests/test_storm_comparison_hint.py
git commit -m "feat(community): STORM规划提示横向对比节(深挖时expand_community落地)"
```

---

## Task 10: 前端来源分布徽章 + 手动重建入口

**Files:**
- Modify: 前端报告/ask 视图组件（实现者先 `grep -rn "references_json\|tier\|references" frontend/ web/ app/ 2>/dev/null` 定位渲染点；本仓前端为 Next.js）
- Modify: 后端若无"重建社区"入口，复用现有"刷新图谱"按钮（`rebuild_unified_kg` 已接社区，无需新端点）
- Test: 视觉验证（`preview_*`）

**Interfaces:**
- Consumes: 报告 `references_json[].tier`（已存在）、检索结果 tier 标记

- [ ] **Step 1: 定位渲染点**

Run: `grep -rn "references" frontend 2>/dev/null | head` 与 `grep -rn "tier" frontend 2>/dev/null | head`（路径以实际前端目录为准）
Expected: 找到报告引用列表 + ask 答案渲染组件

- [ ] **Step 2: 加徽章**

在报告整体/每节渲染处，按 `references` 的 `tier` 统计 `active N · base M` 显示；ask 答案区加"base 命中 M"小标（读检索结果 tier）。遵循 [[ui-polish-bar]]：对齐、省略号截断、不粗糙堆叠。

- [ ] **Step 3: 视觉验证**

用 `preview_start` + `preview_screenshot`/`preview_inspect` 确认徽章渲染、对齐、数值正确（对一份含 base 引用的报告）。

- [ ] **Step 4: 提交**

```bash
git add <前端文件>
git commit -m "feat(community): 报告/ask来源分布徽章(active/base可见)"
```

---

## Task 11（后续，本 plan 收尾后单独排期）: scale-tier igraph-on-CSR 路径

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`（`rebuild_communities` 加 scale 分支）、`backend/requirements.txt`（`python-igraph`/`leidenalg`）

**说明（design-level，需先读代码再写）**：
- Task 3 的 networkx 路径覆盖当前 base 库（4 万节点，秒级）与小库，并对"大库无 CSR"硬拒（安全）。本任务把"大库有 CSR"从"拒绝"升级为"用 igraph 在持久化 CSR 上跑 Leiden/Louvain"，使其在 10^6–10^7 也快。
- **实现者先读**：`_scale_index`（`:8100`，取 `graph.npz`/`node_ids`）、`_scale_combined_graph`（`:~9630` 的 `_load`，看 `combined_A`(CSR)/`combined_ids` 结构）——复用这张已建 CSR，勿重建 networkx。
- 步骤要点：scale-tier 且 CSR 可用时，从 CSR 构 igraph（`Graph(edges=..., n=...)`），`community_leiden`/`community_multilevel`，节点 id 经 `node_ids`/`combined_ids` 映射回 canonical，写 `communities`/`community_members`（centrality 用 igraph degree/PageRank）；`member_ids` 分批 executemany。
- 测试：大库 fixture（带 CSR）→ 秒级出社区、内存有界;与 networkx 小库结果在小图上一致性对照。

---

## Self-Review

- **Spec 覆盖**：L1 社区资产=Task 2-4;L2 检索=Task 5-9;规模化守卫=Task 3(拒暴力)+Task 11(igraph);兜底三态=Task 3(build_refused)+Task 5(unavailable/not_built);前端徽章=Task 10;配置=Task 1。✓（L3 report/PPR-hub/分层 明确留后续,与 spec §7 P3 一致。）
- **占位扫描**：无 TBD;各 code step 给真实代码;前端(Task 10)与 igraph(Task 11)因需先读现有代码/前端结构,明确标注"实现者先 grep/读"——非占位,是定位指令。
- **类型一致**：`community_peers(repo, base_nb, focal_name, query, *, top_k, candidates)->list[str]` 在 Task 5 定义,Task 7/8 同签名调用;`ExpandedQuery.comparison`/`ReflectDecision.community_focal`/`community_members(canonical_name,centrality)` 跨任务一致;事件 `community_build_refused`/`community_unavailable`/`communities_rebuilt` 命名统一。✓
- **风险点**（实现者注意）:`ask_chunk` 里 `retrieval_query`、`SubQuerySpec` 的实际变量/导入名以现场为准;`SQLiteRepository` 构造与 conftest fixture 以现有测试为准;`_write`/`_connect`/`event_log`/`notebook_copy_stats`/`_scale_index` 均为现有方法,改前核对签名。
