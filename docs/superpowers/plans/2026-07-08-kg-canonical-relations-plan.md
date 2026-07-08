# P1 Canonical 关系层 + answer-context 联邦修复 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 持久化「canonical 端点折叠后的关系聚合」（support_count/source_count），供图谱边权与 ask relations 行消费；同 PR 修复 `_answer_context` 跨 tier 折叠与 base 关系盲区。

**Architecture:** 派生表 `canonical_relations` 随 `rebuild_unified_kg` 全量重写（communities 同款 seq 闸，fail-open）；读侧一个注解 helper 挂 `_vector_cache`；前端 `react-force-graph-2d` 边宽/悬停/侧栏标注。Spec: `docs/superpowers/specs/2026-07-08-kg-canonical-relations-design.md`（决策以 spec 为准）。

**Tech Stack:** Python/SQLite/pytest；Next.js + react-force-graph-2d + tsc + node --test。测试解释器 `/opt/homebrew/Caskroom/miniconda/base/bin/python`。

## Global Constraints

- **效率**：无新增 LLM/embed 调用；rebuild 新增一次 O(E) 流式折叠（seq 闸防重复）；ask 侧注解 dict 按 `canonical_rel_seq` 版本缓存；`_answer_context` 只多 ≤2-3 个 participant 的缓存 cluster_map 合并 + 各一条 IN 查询。
- **schema-migration-convention**：新表/新列必须「`_migration_1` baseline 双写 + 新增 `_migration_8` + `SCHEMA_VERSION = 8`」；已部署库升级路径必须有测试。
- **fail-open**：`rebuild_canonical_relations` 在 rebuild 尾部/跳过分支的调用必须 try/except（事件 `canonical_relations_rebuild_failed`），绝不拖垮 rebuild。
- **rejected 边排除**：聚合 `WHERE review_status!='rejected'`；折叠后自环丢弃；`source_id` 可为 NULL（source_count 数非 NULL 去重，下限 1）；方向保留。
- **注解键折叠**：查表键 = `(cmap.get(s,s), edge_type, cmap.get(t,t))`——unified 图只折叠 concept 端点，表按全类型折叠（spec §3）。
- **前后端同 PR**；前端校验 = `npm run lint`（tsc）+ `npm run test`；字段全部可选（表空/滞后优雅缺省）。
- **测试运行**：`cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest <file> -q`。提交规范 `feat(kg): 中文摘要`，每 Task 一个 commit。不碰真实 `.local/`。

---

### Task 1: Schema——`canonical_relations` 表 + `canonical_rel_seq` 列

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`（`SCHEMA_VERSION` line ~252；`_migration_1` 内 communities CREATE 附近 ~1041-1067 与 `_add_column_if_missing` 块 ~1156；文件 `_migration_7` 之后 ~1322 新增 `_migration_8`）
- Test: `backend/tests/test_canonical_relations.py`（新建）

**Interfaces:**
- Produces: 表 `canonical_relations(notebook_id, canonical_src, edge_type, canonical_tgt, support_count, source_count, sample_relation_ids, updated_at, PRIMARY KEY(notebook_id, canonical_src, edge_type, canonical_tgt))`；`unified_kg_state.canonical_rel_seq INTEGER NOT NULL DEFAULT -1`。Task 2/3 依赖两者。

- [ ] **Step 1: 写失败测试**（新建 `backend/tests/test_canonical_relations.py`）

```python
import sqlite3

import pytest
from app.core.config import Settings
from app.models.schemas import NotebookCreate
from app.services.embedding import FakeEmbedder
from app.services.sqlite_repository import SQLiteRepository, SCHEMA_VERSION


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _table_cols(repo, table):
    with repo._connect() as db:
        return {r["name"] for r in db.execute(f"PRAGMA table_info({table})").fetchall()}


def test_fresh_db_has_canonical_relations_table(repo):
    assert {"notebook_id", "canonical_src", "edge_type", "canonical_tgt",
            "support_count", "source_count", "sample_relation_ids",
            "updated_at"} <= _table_cols(repo, "canonical_relations")
    assert "canonical_rel_seq" in _table_cols(repo, "unified_kg_state")


def test_deployed_v7_db_gets_backfilled(tmp_path, monkeypatch):
    # 模拟已部署 user_version=7 的库:全新建库后删掉新表/新列、回拨版本号,
    # 再次实例化必须经 _migration_8 补齐(schema-migration-convention 教训用例)。
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'m.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    SQLiteRepository(Settings())  # 建全新库(version=SCHEMA_VERSION)
    raw = sqlite3.connect(tmp_path / "m.db")
    raw.execute("DROP TABLE canonical_relations")
    raw.execute("ALTER TABLE unified_kg_state DROP COLUMN canonical_rel_seq")
    raw.execute("PRAGMA user_version = 7")
    raw.commit(); raw.close()
    r2 = SQLiteRepository(Settings())  # 重新迁移:必须跑 _migration_8
    assert "canonical_src" in _table_cols(r2, "canonical_relations")
    assert "canonical_rel_seq" in _table_cols(r2, "unified_kg_state")


def test_schema_version_bumped():
    assert SCHEMA_VERSION == 8
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_canonical_relations.py -q`
Expected: 4 个全 FAIL（表不存在 / 列不存在 / SCHEMA_VERSION==7）。

- [ ] **Step 3: 最小实现**

`SCHEMA_VERSION = 7` → `8`（保留原注释）。

`_migration_1` 内、`communities` CREATE（~line 1041）之后追加同款 CREATE：

```python
        # canonical 关系层(P1):关系端点折叠到 canonical 空间后的聚合,随
        # rebuild_unified_kg 全量重写(seq 闸)。派生数据,可随时重建。
        db.execute(
            """
            CREATE TABLE IF NOT EXISTS canonical_relations (
                notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                canonical_src TEXT NOT NULL,
                edge_type TEXT NOT NULL,
                canonical_tgt TEXT NOT NULL,
                support_count INTEGER NOT NULL DEFAULT 1,
                source_count INTEGER NOT NULL DEFAULT 1,
                sample_relation_ids TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL,
                PRIMARY KEY (notebook_id, canonical_src, edge_type, canonical_tgt)
            )
            """
        )
```

`_migration_1` 的 `_add_column_if_missing` 块（`community_seq` 行 ~1156 之后）追加：

```python
            # canonical_rel_seq: canonical_relations 上次重建时的 kg_mutation_seq。
            # -1 默认 → 首次必建(同 community_seq 语义)。
            self._add_column_if_missing(db, "unified_kg_state", "canonical_rel_seq", "INTEGER NOT NULL DEFAULT -1")
```

`_migration_7`（~1322）之后新增：

```python
    def _migration_8(self) -> None:
        """canonical 关系层(P1):canonical_relations 表 + unified_kg_state.canonical_rel_seq。

        已部署库(user_version>=1 时 _migration_1 短路)靠本迁移补建——与
        _migration_3/_migration_4 同款两层写法(baseline 双写 + 独立迁移)。"""
        with self._write() as db:
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS canonical_relations (
                    notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
                    canonical_src TEXT NOT NULL,
                    edge_type TEXT NOT NULL,
                    canonical_tgt TEXT NOT NULL,
                    support_count INTEGER NOT NULL DEFAULT 1,
                    source_count INTEGER NOT NULL DEFAULT 1,
                    sample_relation_ids TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (notebook_id, canonical_src, edge_type, canonical_tgt)
                )
                """
            )
            self._add_column_if_missing(db, "unified_kg_state", "canonical_rel_seq", "INTEGER NOT NULL DEFAULT -1")
```

注意 `_add_column_if_missing` 是 `@staticmethod`（line ~519），签名 `(db, table, column, coldef)`——`_migration_8` 内用 `self._add_column_if_missing(db, ...)` 与 `_migration_4` 写法一致（先看 `_migration_4` 原文再落笔）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_canonical_relations.py tests/test_schema* -q`
Expected: 全 PASS（`tests/test_schema*` 若匹配不到文件则去掉该参数，用 `ls backend/tests | grep -i schema` 找到实际迁移测试文件一并跑）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_canonical_relations.py
git commit -m "feat(kg): canonical_relations 表 + canonical_rel_seq 列(_migration_8, SCHEMA_VERSION=8)"
```

---

### Task 2: `rebuild_canonical_relations` 聚合 + rebuild 接线

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`（新方法放 `rebuild_communities` 定义之前；接线两处——`rebuild_unified_kg` 跳过分支 ~6673 与全量尾部 ~6951）
- Test: `backend/tests/test_canonical_relations.py`（追加）

**Interfaces:**
- Consumes: Task 1 的表与列。
- Produces: `rebuild_canonical_relations(notebook_id: str, force: bool = False) -> int`（返回写入行数）。Task 3 读表。

- [ ] **Step 1: 写失败测试**（追加）

```python
def _mk_nb_with_relations(repo):
    """两源:s1/s2 各有 A--supports-->B 的关系(A/B 同名跨源,会折叠到同 canonical);
    另有 s1 内 rejected 边与自环候选。返回 (nb_id, ids)。"""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    for src, (a, b) in {"s1": ("A1", "B1"), "s2": ("A2", "B2")}.items():
        repo.store_kg(nb.id, src, [
            {"local_id": a, "object_type": "concept",
             "payload": {"name": "cascode", "section_path": "1"}, "evidence": []},
            {"local_id": b, "object_type": "concept",
             "payload": {"name": "gain", "section_path": "1"}, "evidence": []},
        ], [
            {"source_local_id": a, "target_local_id": b, "edge_type": "supports", "evidence": []},
        ])
    repo.rebuild_unified_kg(nb.id)
    return nb


def _canon_rows(repo, nb_id):
    with repo._connect() as db:
        return db.execute(
            "SELECT * FROM canonical_relations WHERE notebook_id=?", (nb_id,)).fetchall()


def test_rebuild_aggregates_cross_source_support(repo):
    nb = _mk_nb_with_relations(repo)
    rows = _canon_rows(repo, nb.id)
    assert len(rows) == 1                      # 两源同一逻辑边折叠成一行
    r = rows[0]
    assert r["canonical_src"] == "K-cascode" and r["canonical_tgt"] == "K-gain"
    assert r["edge_type"] == "supports"
    assert r["support_count"] == 2 and r["source_count"] == 2
    import json as _j
    assert 1 <= len(_j.loads(r["sample_relation_ids"])) <= 5


def test_rejected_edges_excluded(repo):
    nb = _mk_nb_with_relations(repo)
    with repo._write() as db:
        db.execute("UPDATE knowledge_relations SET review_status='rejected' "
                   "WHERE notebook_id=? AND source_id='s2'", (nb.id,))
    repo.rebuild_canonical_relations(nb.id, force=True)
    r = _canon_rows(repo, nb.id)[0]
    assert r["support_count"] == 1 and r["source_count"] == 1


def test_direction_preserved(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, "s1", [
        {"local_id": "X", "object_type": "concept",
         "payload": {"name": "a", "section_path": "1"}, "evidence": []},
        {"local_id": "Y", "object_type": "concept",
         "payload": {"name": "b", "section_path": "1"}, "evidence": []},
    ], [
        {"source_local_id": "X", "target_local_id": "Y", "edge_type": "supports", "evidence": []},
        {"source_local_id": "Y", "target_local_id": "X", "edge_type": "supports", "evidence": []},
    ])
    repo.rebuild_unified_kg(nb.id)
    assert len(_canon_rows(repo, nb.id)) == 2   # A→B 与 B→A 不合并


def test_seq_gate_skips_then_force_recomputes(repo):
    nb = _mk_nb_with_relations(repo)
    with repo._connect() as db:
        seq0 = db.execute("SELECT canonical_rel_seq FROM unified_kg_state WHERE notebook_id=?",
                          (nb.id,)).fetchone()["canonical_rel_seq"]
    assert seq0 >= 0                           # rebuild 后闸已写
    assert repo.rebuild_canonical_relations(nb.id) >= 0   # 未变 → 跳过不炸
    assert repo.rebuild_canonical_relations(nb.id, force=True) == 1
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_canonical_relations.py -q`
Expected: 新增 4 个 FAIL（`AttributeError: rebuild_canonical_relations` / 表空）。

- [ ] **Step 3: 最小实现**

新方法（放在 `def rebuild_communities` 之前，模式对齐其 seq 闸与持久化写法）：

```python
    def rebuild_canonical_relations(self, notebook_id: str, force: bool = False) -> int:
        """把 knowledge_relations 端点经 concept_clusters 折叠到 canonical 空间,
        按 (canonical_src, edge_type, canonical_tgt) 聚合 support_count(原始行数)/
        source_count(非NULL source_id 去重,下限1)/sample_relation_ids(cap 5),全量
        重写 canonical_relations。方向保留;rejected 边排除;折叠后自环丢弃。

        seq 闸(同 rebuild_communities):canonical_rel_seq==kg_mutation_seq 且表非空
        → 跳过(force 绕过);重写后把入口捕获的 seq 写回。返回写入行数(跳过时返回
        现有行数)。派生数据,fail-open 由调用方负责。"""
        self.get_notebook(notebook_id)
        with self._connect() as db:
            st = db.execute(
                "SELECT kg_mutation_seq, canonical_rel_seq FROM unified_kg_state WHERE notebook_id=?",
                (notebook_id,)).fetchone()
            cnt = db.execute(
                "SELECT COUNT(*) AS c FROM canonical_relations WHERE notebook_id=?",
                (notebook_id,)).fetchone()["c"]
        seq = int(st["kg_mutation_seq"]) if st else 0
        if (not force and st is not None and st["canonical_rel_seq"] == seq and cnt > 0):
            return int(cnt)
        agg: Dict[tuple, dict] = {}
        with self._connect() as db:
            cur = db.execute(
                "SELECT kr.id AS rid, kr.source_id AS src_doc, kr.edge_type AS et, "
                "       COALESCE(cs.canonical_id, kr.source_object_id) AS s, "
                "       COALESCE(ct.canonical_id, kr.target_object_id) AS t "
                "FROM knowledge_relations kr "
                "LEFT JOIN concept_clusters cs ON cs.notebook_id=kr.notebook_id "
                "  AND cs.member_object_id=kr.source_object_id "
                "LEFT JOIN concept_clusters ct ON ct.notebook_id=kr.notebook_id "
                "  AND ct.member_object_id=kr.target_object_id "
                "WHERE kr.notebook_id=? AND kr.review_status!='rejected'",
                (notebook_id,))
            for r in cur:
                s, t = r["s"], r["t"]
                if not s or not t or s == t:
                    continue
                key = (s, r["et"], t)
                ent = agg.get(key)
                if ent is None:
                    ent = agg[key] = {"n": 0, "docs": set(), "samples": []}
                ent["n"] += 1
                if r["src_doc"]:
                    ent["docs"].add(r["src_doc"])
                if len(ent["samples"]) < 5:
                    ent["samples"].append(r["rid"])
        now = _now()
        rows = [(notebook_id, s, et, t, ent["n"], max(1, len(ent["docs"])),
                 json.dumps(ent["samples"]), now)
                for (s, et, t), ent in agg.items()]
        with self._write() as db:
            db.execute("DELETE FROM canonical_relations WHERE notebook_id=?", (notebook_id,))
            for i in range(0, len(rows), 1000):
                db.executemany(
                    "INSERT INTO canonical_relations "
                    "(notebook_id, canonical_src, edge_type, canonical_tgt, "
                    " support_count, source_count, sample_relation_ids, updated_at) "
                    "VALUES (?,?,?,?,?,?,?,?)", rows[i:i + 1000])
            db.execute(
                "UPDATE unified_kg_state SET canonical_rel_seq=? WHERE notebook_id=?",
                (seq, notebook_id))
        return len(rows)
```

接线①——`rebuild_unified_kg` 跳过分支（现有 `self.rebuild_communities(notebook_id, level=0)` 调用 ~6673 之前，同款 try/except）：

```python
                try:
                    self.rebuild_canonical_relations(notebook_id)
                except Exception as exc:  # noqa: BLE001
                    self.event_log.emit({"kind": "canonical_relations_rebuild_failed",
                                         "notebook_id": notebook_id, "error": str(exc)[:200]})
```

接线②——全量尾部：在 `self.build_viz_index(notebook_id)`（~6951）**之前**插入同款块，但 `force=True`（聚类刚重算，闸可能误跳）：

```python
        try:
            self.rebuild_canonical_relations(notebook_id, force=True)
        except Exception as exc:  # noqa: BLE001
            self.event_log.emit({"kind": "canonical_relations_rebuild_failed",
                                 "notebook_id": notebook_id, "error": str(exc)[:200]})
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_canonical_relations.py tests/test_cross_doc_merge.py tests/test_rebuild_cache.py -q`
Expected: 全 PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_canonical_relations.py
git commit -m "feat(kg): rebuild_canonical_relations 聚合(seq闸+fail-open)接入 rebuild 尾部与跳过分支"
```

---

### Task 3: 读侧注解 `_annotate_edge_support` + 三出口接线

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`（helper 放 `unified_graph` 定义之前；接线 `unified_graph` 全量返回 ~6246、`_unified_graph_bounded` 返回 ~6314、`kg_neighbors` 两条路径的 edges 出口）
- Test: `backend/tests/test_canonical_relations.py`（追加）

**Interfaces:**
- Consumes: Task 2 的表内容；`cluster_map(notebook_id)`。
- Produces: `_annotate_edge_support(notebook_id: str, edges: List[dict]) -> List[dict]`（原地/新列表均可，命中边多 `support_count`/`source_count` 两个 int 字段）。前端（Task 5）依赖字段名。

- [ ] **Step 1: 写失败测试**（追加）

```python
def test_unified_graph_edges_carry_support(repo):
    nb = _mk_nb_with_relations(repo)
    g = repo.unified_graph(nb.id, level="object")
    sup = [e for e in g["edges"] if e.get("source_count")]
    assert sup and sup[0]["support_count"] == 2 and sup[0]["source_count"] == 2


def test_neighbors_edges_carry_support(repo):
    nb = _mk_nb_with_relations(repo)
    g = repo.unified_graph(nb.id, level="object")
    nid = next(n["id"] for n in g["nodes"])
    nbres = repo.kg_neighbors(nb.id, nid)
    assert any(e.get("source_count") == 2 for e in nbres["edges"])


def test_empty_table_leaves_edges_bare(repo):
    nb = _mk_nb_with_relations(repo)
    with repo._write() as db:
        db.execute("DELETE FROM canonical_relations WHERE notebook_id=?", (nb.id,))
        db.execute("UPDATE unified_kg_state SET canonical_rel_seq=-1 WHERE notebook_id=?", (nb.id,))
    repo._unified_cache.clear()
    g = repo.unified_graph(nb.id, level="object")
    assert all("support_count" not in e for e in g["edges"])
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_canonical_relations.py -q -k "support or bare"`
Expected: 前两个 FAIL（边无字段），第三个 PASS（基线本就无字段）。

- [ ] **Step 3: 最小实现**

helper（放 `def unified_graph` 之前；缓存模式对齐 `cluster_map` ~5246 的 `_vector_cache.get`）：

```python
    def _edge_support_map(self, notebook_id: str) -> Dict[tuple, tuple]:
        """{(canonical_src, edge_type, canonical_tgt): (support_count, source_count)}。
        版本 = canonical_rel_seq(O(1) 行读),表重建后自动失效。"""
        with self._connect() as db:
            st = db.execute(
                "SELECT canonical_rel_seq FROM unified_kg_state WHERE notebook_id=?",
                (notebook_id,)).fetchone()
        seq = int(st["canonical_rel_seq"]) if st else -1

        def _load():
            out: Dict[tuple, tuple] = {}
            with self._connect() as db:
                for r in db.execute(
                        "SELECT canonical_src, edge_type, canonical_tgt, support_count, source_count "
                        "FROM canonical_relations WHERE notebook_id=?", (notebook_id,)):
                    out[(r["canonical_src"], r["edge_type"], r["canonical_tgt"])] = (
                        int(r["support_count"]), int(r["source_count"]))
            return out

        return self._vector_cache.get(f"{notebook_id}:edge_support", ("edge_support", seq), _load)

    def _annotate_edge_support(self, notebook_id: str, edges: List[dict]) -> List[dict]:
        """给 unified/neighbors 形状的边({source_object_id,target_object_id,edge_type})
        附 support_count/source_count。查表键先过 cluster_map 折叠:unified 图只折叠
        concept 端点,claim/formula/procedure 保原始 id,而 canonical_relations 按全
        类型折叠;concept 端点已是 canonical id、不在 cluster_map 键中,get(s,s) 恒等
        通过。未命中不加字段(表空/滞后 → 前端优雅缺省)。"""
        sup = self._edge_support_map(notebook_id)
        if not sup:
            return edges
        cmap = self.cluster_map(notebook_id)
        for e in edges:
            key = (cmap.get(e["source_object_id"], e["source_object_id"]),
                   e["edge_type"],
                   cmap.get(e["target_object_id"], e["target_object_id"]))
            hit = sup.get(key)
            if hit:
                e["support_count"], e["source_count"] = hit[0], hit[1]
        return edges
```

接线（三处，均在 return 前对 edges 列表调用；`_unified_graph_full` 的 `self._unified_cache` 缓存**不含**注解——注解在 `unified_graph` 出口做，避免缓存粘住旧计数）：

1. `unified_graph` 全量返回（~6246）：`"edges": self._annotate_edge_support(notebook_id, sliced["edges"]),`
2. `_unified_graph_bounded`（~6288）：签名加 `notebook_id`（两个调用点 ~6217/~6234 传入），返回前 `kept_edges = self._annotate_edge_support(notebook_id, kept_edges)`。
3. `kg_neighbors`（~6324）：两条路径（viz 快路与 fallback）拼好 edges 后、return 前统一 `edges = self._annotate_edge_support(notebook_id, edges)`（先读该函数两条路径的实际返回变量名再落笔）。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_canonical_relations.py tests/test_unified_kg_repository.py tests/test_kg_viz_index.py -q`
Expected: 全 PASS（viz 测试文件名以 `ls backend/tests | grep viz` 实际为准）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_canonical_relations.py
git commit -m "feat(kg): unified-kg/neighbors 边附 support_count/source_count(版本缓存注解,三出口)"
```

---

### Task 4: `_answer_context` 联邦修复（跨 tier 折叠 + base 关系 + ×N源 标注排序）

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`（新 helper `_participant_notebook_ids` 放 `_answer_context` 前；改 `_answer_context` ~12148-12232 两处）
- Test: `backend/tests/test_cross_doc_merge.py`（追加，复用 `repo` fixture）

**Interfaces:**
- Consumes: `cluster_map`、Task 2 表（标注可选命中）。
- Produces: `_participant_notebook_ids(notebook_id: str) -> List[str]`（active 在首位）。`_answer_context` 行为变化：跨 tier 同 canonical 折叠、relations 行含 participant 库边、行按 source_count 降序、`(×N源)` 后缀。

- [ ] **Step 1: 写失败测试**（追加到 `backend/tests/test_cross_doc_merge.py`）

```python
def _mark_base(repo, nb_id):
    with repo._write() as db:
        db.execute("UPDATE notebooks SET tier='base' WHERE id=?", (nb_id,))


def test_answer_context_folds_across_tiers(repo):
    # base 与 personal 各有同名 concept:命中两条 → 折叠成一行
    base = repo.create_notebook(NotebookCreate(name="base"))
    per = repo.create_notebook(NotebookCreate(name="per"))
    _mark_base(repo, base.id)
    for nb, lid in ((base, "B"), (per, "P")):
        repo.store_kg(nb.id, "s1", [{"local_id": lid, "object_type": "concept",
            "payload": {"name": "cascode", "section_path": "1"}, "evidence": []}], [])
        repo.rebuild_unified_kg(nb.id)
    ids = {}
    for nb in (base, per):
        with repo._connect() as db:
            ids[nb.id] = db.execute(
                "SELECT id FROM knowledge_objects WHERE notebook_id=?", (nb.id,)).fetchone()["id"]
    hits = [RetrievedKnowledge(object_id=ids[base.id], object_type="concept",
                               payload={"name": "cascode"}, evidence=[], notebook_id=base.id),
            RetrievedKnowledge(object_id=ids[per.id], object_type="concept",
                               payload={"name": "cascode"}, evidence=[], notebook_id=per.id)]
    block, id_map = repo._answer_context(per.id, hits)
    assert len(id_map) == 1     # 跨 tier 同名折叠(旧行为:2 行)


def test_answer_context_shows_base_relations(repo):
    base = repo.create_notebook(NotebookCreate(name="base"))
    _mark_base(repo, base.id)
    repo.store_kg(base.id, "s1", [
        {"local_id": "X", "object_type": "concept",
         "payload": {"name": "cascode", "section_path": "1"}, "evidence": []},
        {"local_id": "Y", "object_type": "concept",
         "payload": {"name": "gain", "section_path": "1"}, "evidence": []},
    ], [{"source_local_id": "X", "target_local_id": "Y", "edge_type": "supports", "evidence": []}])
    repo.rebuild_unified_kg(base.id)
    per = repo.create_notebook(NotebookCreate(name="per"))
    with repo._connect() as db:
        rows = db.execute("SELECT id, json_extract(payload,'$.name') nm FROM knowledge_objects "
                          "WHERE notebook_id=?", (base.id,)).fetchall()
    hits = [RetrievedKnowledge(object_id=r["id"], object_type="concept",
                               payload={"name": r["nm"]}, evidence=[], notebook_id=base.id)
            for r in rows]
    block, id_map = repo._answer_context(per.id, hits)   # active=per, 命中全在 base
    assert "relations:" in block and "supports" in block   # 旧行为:base 边不可见
```

注意：`RetrievedKnowledge` 若无 `notebook_id` 字段则看 `backend/app/services/retrieval.py` 中定义（`_answer_context` 已用 `getattr(hit, "notebook_id", ...)`——若构造不支持该 kwarg，改用 `setattr` 或现有字段）。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_cross_doc_merge.py -q -k "tiers or base_relations"`
Expected: 两个都 FAIL（id_map 有 2 行；relations 行缺失）。

- [ ] **Step 3: 最小实现**

helper：

```python
    def _participant_notebook_ids(self, notebook_id: str) -> List[str]:
        """联邦参与库:active 在首位 + 全部 base tier(与 _ppr_graph/federated_retrieve
        的内联谓词一致;此 helper v1 只供新代码使用,存量调用点不迁移)。"""
        with self._connect() as db:
            rows = db.execute(
                "SELECT id FROM notebooks WHERE tier='base' AND id != ?",
                (notebook_id,)).fetchall()
        return [notebook_id] + [r["id"] for r in rows]
```

`_answer_context` 两处修改：

1. 折叠 map（~12162 `cmap = self.cluster_map(notebook_id)`）改为：

```python
        cmap: Dict[str, str] = {}
        participants = self._participant_notebook_ids(notebook_id)
        for nb in participants:
            cmap.update(self.cluster_map(nb))
```

2. relations 块（~12210-12229）：外层查询循环 participants，且 rel 行带上来源库以便标注：

```python
        oid_to_key = {v["object_id"]: k for k, v in id_map.items()}
        if len(oid_to_key) >= 2:
            ids = list(oid_to_key)
            ph = ",".join("?" for _ in ids)
            rel_rows: List[tuple] = []   # (s_key, edge_type, t_key, src_nb, s_oid, t_oid)
            seen_rel = set()
            with self._connect() as db:
                for nb in participants:
                    for r in db.execute(
                            f"SELECT source_object_id, target_object_id, edge_type "
                            f"FROM knowledge_relations WHERE notebook_id=? "
                            f"AND source_object_id IN ({ph}) AND target_object_id IN ({ph})",
                            [nb, *ids, *ids]).fetchall():
                        s = oid_to_key.get(r["source_object_id"])
                        t = oid_to_key.get(r["target_object_id"])
                        if s and t and s != t and (s, r["edge_type"], t) not in seen_rel:
                            seen_rel.add((s, r["edge_type"], t))
                            rel_rows.append((s, r["edge_type"], t, nb,
                                             r["source_object_id"], r["target_object_id"]))
            if rel_rows:
                def _support(row):
                    s_key, et, t_key, nb, s_oid, t_oid = row
                    sup = self._edge_support_map(nb)
                    cm = self.cluster_map(nb)
                    hit = sup.get((cm.get(s_oid, s_oid), et, cm.get(t_oid, t_oid)))
                    return hit[1] if hit else 1
                rel_rows.sort(key=_support, reverse=True)
                rel_lines = []
                for row in rel_rows[:30]:
                    s_key, et, t_key = row[0], row[1], row[2]
                    n_src = _support(row)
                    suffix = f" (×{n_src}源)" if n_src >= 2 else ""
                    rel_lines.append(f"{s_key} -[{et}]-> {t_key}{suffix}")
                lines.append("relations: " + "; ".join(rel_lines))
```

（`_support` 内的 `_edge_support_map`/`cluster_map` 都是版本缓存命中，循环 ≤30 行零额外查询。保留原 cap 30 语义——排序后取前 30。）

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_cross_doc_merge.py tests/test_answer_context_budget.py tests/test_canonical_relations.py -q`
Expected: 全 PASS（尤其既有 `test_answer_context_dedups_merged_claims` 不回归）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_cross_doc_merge.py
git commit -m "feat(kg): _answer_context 联邦修复——跨tier canonical折叠+base关系可见+×N源标注按支持度排序"
```

---

### Task 5: 前端——边宽/悬停/中点 pill/侧栏 ×N源

**Files:**
- Modify: `frontend/app/page.tsx`（`UnifiedEdge` ~320、`FgLink` ~336、FgLink 构建 ~1665、`<ForceGraph2D>` props ~4546-4565、`drawKgLinkLabel` ~894、侧栏关系列表 ~4614-4623）

**Interfaces:**
- Consumes: 后端边字段 `support_count?/source_count?`（Task 3/4）。

- [ ] **Step 1: 类型与透传**

```ts
type UnifiedEdge = { source_object_id: string; target_object_id: string; edge_type: string;
  support_count?: number; source_count?: number };
...
type FgLink = { source: string | FgNode; target: string | FgNode; label: string; sourceCount?: number };
```

FgLink 构建（~1665）：`{ source: e.source_object_id, target: e.target_object_id, label: e.edge_type, sourceCount: e.source_count }`（展开邻居合并处若另有一处构建，一并透传——搜 `label: e.edge_type`）。

- [ ] **Step 2: 渲染**

`<ForceGraph2D>` props：

```tsx
linkWidth={(link: any) => 1.35 + Math.min(((link.sourceCount ?? 1) - 1), 4) * 0.5}
linkLabel={(link: any) => {
  const base = RELATION_LABELS[link.label] ?? link.label;
  return (link.sourceCount ?? 1) >= 2 ? `${base} · ${link.sourceCount} 源支持` : base;
}}
```

`drawKgLinkLabel`（~894）：拿到显示文本后 `if ((link.sourceCount ?? 1) >= 2) text += \` ×${link.sourceCount}\``（保持 pill 宽度计算用追加后的文本）。

侧栏关系行（`selectedKgEdges` 渲染 ~4614-4623）：`selectedKgEdges` useMemo 里保留 `source_count`，行内 `{e.source_count && e.source_count >= 2 ? <span className="tag">×{e.source_count}源</span> : null}`（对齐现有 `.tag` pill；先读该 useMemo 的实际字段再落笔）。

- [ ] **Step 3: 校验**

Run: `cd frontend && npm run lint && npm run test`
Expected: tsc 零错误；node --test 存量全绿。

- [ ] **Step 4: Commit**

```bash
git add frontend/app/page.tsx
git commit -m "feat(kg-ui): 图谱边按来源支持度加权——边宽/悬停/中点pill/侧栏 ×N源"
```

---

### Task 6: 全量回归 + 视觉验证 + PR

- [ ] **Step 1:** `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q | tail -3` → 全 PASS。
- [ ] **Step 2:** `bash scripts/check.sh` 若存在则跑（前后端一体检查）。
- [ ] **Step 3:** 视觉验证（controller 亲自做，preview 工具起前端截图 KG 视图——多源边更粗、悬停/pill/侧栏标注对齐）。
- [ ] **Step 4:** push + `gh pr create --base master`（PR body：spec 链接、基线数字 585/50519、生效方式=下一次刷新图谱自动、效率账、截图）。
