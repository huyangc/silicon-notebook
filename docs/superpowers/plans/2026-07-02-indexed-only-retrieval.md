# 大库检索统一「只检索已索引部分」原则 — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 PR#158 在 chunk 侧确立的「已索引的库默认只检索已索引部分,delta 参与检索是 opt-in(SCALE_SEARCH_INCLUDE_DELTA),delta 靠 auto-fold 最终一致收进索引」原则推广到全部检索路径(KG 对象/关系/PPR 图基底),并补齐让该原则成立的基础设施(delta SQL 全部 IN 分批防 SQLite 变量上限、大 delta 时 fold 自动升级 full、大库无 ANN 时拒绝全量暴力改 FTS 词法兜底、element 侧大库守卫),前端徽章语义同步。

**Architecture:** 检索热路径上所有「⊕ delta 暴力」块统一包在 `settings.scale_search_include_delta`(默认 False)之后,与 chunk 侧 `_retrieve_chunks_ann` 字节同构;PPR 组合图的 self-delta splice 同守此门(未索引的 active 小库整库 splice 是 two-tier 联邦语义,不属 delta,不受门控)。所有把 id 列表内联成 SQL IN 占位符的 delta 位点改走新的 `_in_batches` 分批helper(镜像既有 `_delta_vector_matrix`/`_relations_with_names` 的 `_IN_CHUNK` 范式)。大库(沿用 `notebook_copy_stats()["copyable"] is False` 的既有「大」定义)在拿不到 ANN 候选时不再全量暴力,改 `fts_search`(kg_objects_fts,PR#118 已建)有界词法兜底+事件。

**Tech Stack:** Python 3.13 / FastAPI / SQLite(FTS5+hnswlib+scipy CSR) / pytest;前端 Next.js+TS。

## Global Constraints

- 后端测试从 `backend/` 目录跑:`python -m pytest tests/<file> -q`;收尾全量 `python -m pytest tests/ -q` 必须全绿(现状基线 ~1000+ passed)。
- 本机后端解释器用系统共享 conda(直接 `python` 即可,勿建 venv)。
- 新配置项的环境变量映射必须用 `validation_alias=`(pydantic-settings v2,`Field(env=...)` 无效——仓库已知坑)。
- 「大库」判定一律复用 `self.notebook_copy_stats(notebook_id)["copyable"] is False`,不新造阈值。
- 检索不变量:relevance 守 [0,1]/tau;降权/守卫只影响候选集与 score,绝不改 relevance 语义。
- 事件观测:每个新守卫/跳过位点发 `self.event_log.emit({...})` 事件,kind 命名跟随既有 `relation_scoring_skipped`/`graph_walk_refused` 风格。
- 前端 page.tsx 内已有中文文案的弯引号“”是合法 JSX 文本,严禁全文件批量替换引号。
- 每个任务:测试先行(先写失败测试再实现),完成即 commit(中文 conventional message,尾行 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`)。
- 所有被改的行为必须保持:小库(copyable=True)路径字节不变;`scale_search_include_delta=True` 时恢复今日强一致行为。

**当前分支说明:** 工作分支 `feat/indexed-only-retrieval` 从 `claude/sleepy-lalande-dd6608`(= master + PR#176 的 diag_slow.py 扩展)切出,PR 时注明 stacked on #176。scripts/diag_slow.py 的 INTEREST_KINDS 更新以本分支文件为准。

---

### Task 1: `_in_batches` helper + 全部 delta SQL 位点 IN 分批

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(7 处,行号基于 5b1045b:`_IN_CHUNK` 定义在 ~9682;`_index_delta` ~8762;`_gather_kg_graph` ~8066-8099;fold `_delta_vecs` ~8652 与 relation-id 拉取 ~8688;`_kg_object_candidates` delta ~9921;`_relation_ann_candidates` delta ~9756;`_retrieve_chunks_ann` delta ~10214)
- Test: `backend/tests/test_in_batching.py`(新建)

**Interfaces:**
- Produces: `SQLiteRepository._in_batches(ids: Iterable[str]) -> Iterator[List[str]]`(去重保序,每批 ≤ `self._IN_CHUNK`)。后续任务的 delta 查询一律经它。

- [ ] **Step 1: 写失败测试**

```python
"""IN 分批:所有 delta id 列表内联 SQL 的位点在超过 _IN_CHUNK 时结果不变、不抛
"too many SQL variables"(生产 48,739 delta source 实测打爆 SQLite 32,766 上限)。"""
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
    for k, v in {"EMBED_PROVIDER": "dashscope", "EMBED_BASE_URL": "https://e.test",
                 "EMBED_API_KEY": "k", "EMBED_MODEL": "m", "EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _insert_source_with_object(repo, nb_id, i):
    """一个 source + 一个 chunk + 一个 KG 对象(带 embedding)+ 一条自环外关系。"""
    sid, cid, oid = f"s{i}", f"c{i}", f"o{i}"
    now = "2026-07-01T00:00:00"
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                   (sid, nb_id, "t", "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) VALUES (?,?,?,?,?,?,?)",
                   (cid, nb_id, sid, f"text {i}", "", "[]", now))
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,source_id,object_type,payload,evidence,status,owner,last_reviewed,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (oid, nb_id, sid, "claim", json.dumps({"name": f"obj {i}"}), "[]",
                    "approved", "", "", now, now))
        v = repo.embedder.embed_query(f"obj {i}")
        db.execute("INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                   (oid, nb_id, json.dumps(v), now))
    return sid, cid, oid


def test_in_batches_dedup_and_order(repo):
    batches = list(repo._in_batches(["a", "b", "a", "c"]))
    assert [x for b in batches for x in b] == ["a", "b", "c"]


def test_delta_sites_equivalent_when_batched(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="n"))
    sids = []
    for i in range(5):
        sid, _, _ = _insert_source_with_object(repo, nb.id, i)
        sids.append(sid)

    big = {
        "index_delta": repo._index_delta(nb.id),
        "gather": repo._gather_kg_graph(nb.id, source_ids=sids),
    }
    monkeypatch.setattr(SQLiteRepository, "_IN_CHUNK", 2)
    small = {
        "index_delta": repo._index_delta(nb.id),
        "gather": repo._gather_kg_graph(nb.id, source_ids=sids),
    }
    assert small["index_delta"] == big["index_delta"]
    assert sorted(small["gather"][0]) == sorted(big["gather"][0])   # node_ids
    assert sorted(small["gather"][2]) == sorted(big["gather"][2])   # chunk_ids
    assert small["index_delta"]["delta_chunks"] == 5
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_in_batching.py -q`
Expected: FAIL(`_in_batches` 不存在,AttributeError)

- [ ] **Step 3: 实现**

在 `_IN_CHUNK = 900` 定义(~9682)之后加 helper:

```python
    def _in_batches(self, ids):
        """把 id 列表切成 ≤_IN_CHUNK 的批(去重保序)。所有把 id 列表内联成
        SQL IN 占位符的 delta 位点必须经它——SQLite 3.32+ 变量上限 32,766,
        生产 48,739 delta source 已真实打爆(too many SQL variables)。"""
        ids = list(dict.fromkeys(ids))
        for i in range(0, len(ids), self._IN_CHUNK):
            yield ids[i:i + self._IN_CHUNK]
```

改 7 个位点(全部保持结果语义;批间无重复:每行属于唯一 source_id,批是不相交集合):

(a) `_index_delta`(~8760-8765)的 delta chunk COUNT:

```python
            nchunks = 0
            for batch in self._in_batches(delta_sources):
                ph = ",".join("?" for _ in batch)
                nchunks += db.execute(
                    f"SELECT COUNT(*) c FROM chunks WHERE notebook_id=? AND source_id IN ({ph})",
                    (notebook_id, *batch)).fetchone()["c"]
        return {"delta_sources": delta_sources, "delta_chunks": int(nchunks), "indexed": True}
```

(b) `_gather_kg_graph`(~8073-8099):把单一 `src_clause/src_params` 改为批列表,三个用它的查询逐批执行合并(objects 入 dict 天然去重;relations/chunks 逐批 extend,批不相交无重复;cluster 查询不变):

```python
        clauses = [("", ())]
        if scoped:
            clauses = [
                (f" AND source_id IN ({','.join('?' for _ in b)})", tuple(b))
                for b in self._in_batches(source_ids)
            ]
        ...
        with self._connect() as db:
            for src_clause, src_params in clauses:
                for r in db.execute(
                        f"SELECT id, object_type, payload FROM knowledge_objects "
                        f"WHERE notebook_id=? AND status IN ({ph}){src_clause}",
                        (notebook_id, *USABLE_STATUSES, *src_params)).fetchall():
                    kg_nodes[r["id"]] = {
                        "type": r["object_type"],
                        "name": json.loads(r["payload"] or "{}").get("name", ""),
                    }
            for src_clause, src_params in clauses:
                for r in db.execute(
                        f"SELECT source_object_id, target_object_id FROM knowledge_relations "
                        f"WHERE notebook_id=?{src_clause}", (notebook_id, *src_params)).fetchall():
                    relations.append(dict(r))
            for src_clause, src_params in clauses:
                for r in db.execute(
                        f"SELECT id FROM chunks WHERE notebook_id=?{src_clause}",
                        (notebook_id, *src_params)).fetchall():
                    chunk_ids.append(r["id"])
```

(注意:原 `src_clause, src_params = "", ()` 两个变量删除,scoped 空列表的早退分支保留不动。)

(c) `_kg_object_candidates` delta 块(~9923-9931)内层查询:

```python
            delta = self._index_delta(notebook_id)
            if delta["delta_sources"] and query_vector is not None:
                drows = []
                with self._connect() as db:
                    for batch in self._in_batches(delta["delta_sources"]):
                        ph_s = ",".join("?" for _ in batch)
                        drows.extend(db.execute(
                            f"SELECT object_id AS vid, vector FROM knowledge_embeddings "
                            f"WHERE notebook_id=? AND object_id IN "
                            f"(SELECT id FROM knowledge_objects WHERE notebook_id=? AND source_id IN ({ph_s}))",
                            (notebook_id, notebook_id, *batch)).fetchall())
                d_ids, d_mat = build_matrix((r["vid"], r["vector"]) for r in drows)
```

(d) `_relation_ann_candidates` delta 块(~9757-9766)同款:

```python
            delta = self._index_delta(notebook_id)
            if delta["delta_sources"] and query_vector is not None:
                drows = []
                with self._connect() as db:
                    for batch in self._in_batches(delta["delta_sources"]):
                        ph_s = ",".join("?" for _ in batch)
                        drows.extend(db.execute(
                            f"SELECT relation_id AS vid, vector FROM relation_embeddings "
                            f"WHERE notebook_id=? AND relation_id IN "
                            f"(SELECT id FROM knowledge_relations WHERE notebook_id=? AND source_id IN ({ph_s}))",
                            (notebook_id, notebook_id, *batch)).fetchall())
                d_ids, d_mat = build_matrix((r["vid"], r["vector"]) for r in drows)
```

(e) `_retrieve_chunks_ann` delta 块(~10214-10224)同款(chunk_embeddings/chunks 表)。

(f) `fold_scale_index_delta` 内 `_delta_vecs`(~8652-8661):

```python
            def _delta_vecs(table, col, ids):
                if not ids:
                    return [], []

                def _rows():
                    with self._connect() as db:
                        for batch in self._in_batches(ids):
                            ph = ",".join("?" for _ in batch)
                            for r in db.execute(
                                    f"SELECT {col} AS vid, vector FROM {table} "
                                    f"WHERE notebook_id=? AND {col} IN ({ph})",
                                    (notebook_id, *batch)).fetchall():
                                yield r["vid"], r["vector"]
                return build_matrix(_rows())
```

(g) fold 内 relation-id 拉取(~8688-8694):

```python
                d_relation_ids = []
                with self._connect() as db:
                    for batch in self._in_batches(delta["delta_sources"]):
                        ph_s = ",".join("?" for _ in batch)
                        d_relation_ids.extend(r["id"] for r in db.execute(
                            f"SELECT id FROM knowledge_relations "
                            f"WHERE notebook_id=? AND source_id IN ({ph_s})",
                            (notebook_id, *batch)).fetchall())
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_in_batching.py tests/test_scale_delta_policy.py tests/test_scale_index_repo.py tests/test_relation_ann.py -q`
Expected: 全 PASS(既有 delta/scale 测试同样必须绿——分批是纯等价改写)

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_in_batching.py
git commit -m "fix(scale): delta SQL 全部 IN 分批,防 48k delta source 打爆 SQLite 变量上限

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 2: KG 对象侧 delta 门控(SCALE_SEARCH_INCLUDE_DELTA)

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(`_kg_object_candidates` delta 块,Task 1 改后的位置)
- Modify: `backend/app/core/config.py:163`(注释扩写)
- Test: `backend/tests/test_indexed_only_principle.py`(新建)

**Interfaces:**
- Consumes: Task 1 的分批版 delta 块。
- Produces: flag 关时 `_kg_object_candidates` 返回纯 ANN 核候选(不触 `_index_delta`);flag 开时行为与今日相同。

- [ ] **Step 1: 写失败测试**

新建 `backend/tests/test_indexed_only_principle.py`(fixture 复制 Task 1 的 repo fixture 与 `_insert_source_with_object`;另加一个「建好索引再加 delta」的构造函数——镜像 `tests/test_scale_delta_policy.py` 的 `_build_indexed_nb_with_delta` 手法):

```python
def _build_indexed_nb_with_delta_object(repo):
    """source A 进水位;source B 在 build 之后插入(其 KG 对象 embedding 与
    查询词 'bravo' 最匹配,payload 名字与查询无词法重叠)→ B 的对象只可能经
    delta 语义暴力被检回,FTS/关键词都救不了它。"""
    nb = repo.create_notebook(NotebookCreate(name="base"))
    _insert_source_with_object(repo, nb.id, 0)          # sA: 'obj 0'
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)                        # watermark = {s0}
    sid, cid, oid = "sB", "cB", "oB"
    now = "2026-07-02T00:00:00"
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) VALUES (?,?,?,?,?,?,?)",
                   (sid, nb.id, "t", "md", "ready", now, now))
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,source_id,object_type,payload,evidence,status,owner,last_reviewed,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (oid, nb.id, sid, "claim", json.dumps({"name": "zzz"}), "[]",
                    "approved", "", "", now, now))
        v = repo.embedder.embed_query("bravo")
        db.execute("INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                   (oid, nb.id, json.dumps(v), now))
    return nb, oid


def test_object_delta_excluded_by_default(repo):
    nb, oid = _build_indexed_nb_delta = _build_indexed_nb_with_delta_object(repo)
    assert repo.settings.scale_search_include_delta is False
    hits = repo._retrieve_scored(nb.id, "bravo")
    assert oid not in {h.object_id for h in hits}


def test_object_delta_included_when_opted_in(repo, monkeypatch):
    nb, oid = _build_indexed_nb_with_delta_object(repo)
    monkeypatch.setattr(repo.settings, "scale_search_include_delta", True)
    hits = repo._retrieve_scored(nb.id, "bravo")
    assert oid in {h.object_id for h in hits}
```

(注意第一处笔误 `_build_indexed_nb_delta =` 别照抄——直接 `nb, oid = _build_indexed_nb_with_delta_object(repo)`。)

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_indexed_only_principle.py -q`
Expected: `test_object_delta_excluded_by_default` FAIL(delta 对象当前无条件可见)

- [ ] **Step 3: 实现**

`_kg_object_candidates` 的 delta 块整体包进 flag(与 `_retrieve_chunks_ann` 10213 行的门控字节同构),flag 关时不调用 `_index_delta`(热路径省掉水位盘点):

```python
        # ⊕ delta 对象(水位后 source)暴力 —— opt-in(scale_search_include_delta,
        # 默认关):与 chunk 侧同一原则「已索引的库只检索已索引部分」,delta 由
        # scale_auto_fold_on_add 的增量 fold 收进索引(最终一致)。True 时保持
        # 强一致暴力(慢,且量级随 delta 无界增长)。
        if self.settings.scale_search_include_delta:
            try:
                ...(Task 1 改后的既有 delta 块原样内移一层)...
            except Exception as exc:  # noqa: BLE001 — delta 失败不拖垮
                self._note_model_error("kg_obj_delta", self.settings.embed_model, exc)
        return sims
```

`config.py:163` 注释改为:

```python
    scale_search_include_delta: bool = Field(False, validation_alias="SCALE_SEARCH_INCLUDE_DELTA")  # 已索引库检索是否含水位后 delta:统一门控 chunk(_retrieve_chunks_ann)/KG对象(_kg_object_candidates)/关系(_relation_ann_candidates)/PPR图基底(_active_kg_delta) 四处;默认关=只检索已索引部分(delta 靠 auto-fold 收进,最终一致);开=强一致 delta 暴力(大 delta 不可扩展)
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_indexed_only_principle.py tests/test_scale_delta_policy.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/app/core/config.py backend/tests/test_indexed_only_principle.py
git commit -m "feat(scale): KG 对象侧 delta 检索改 opt-in,对齐 chunk 侧「只检索已索引部分」原则

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 3: 关系侧 delta 门控

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(`_relation_ann_candidates` delta 块)
- Test: `backend/tests/test_indexed_only_principle.py`(追加)

**Interfaces:**
- Consumes: Task 1 分批版 relation delta 块;Task 2 的测试 fixture。
- Produces: flag 关时 `_relation_ann_candidates` 返回纯 ANN 核候选。

- [ ] **Step 1: 写失败测试(追加到 test_indexed_only_principle.py)**

构造:建索引时已有一条带 embedding 的关系(使 manifest has_relation_ann=True——参照 `tests/test_relation_ann.py` 的现成构造手法,插 knowledge_relations + relation_embeddings 后 build_scale_index);之后再插一条 delta 关系(embedding 匹配查询 'bravo'):

```python
def test_relation_delta_excluded_by_default(repo):
    nb = _build_indexed_nb_with_delta_relation(repo)   # 返回 (nb, delta_rel_id)
    nb, rid = nb
    sims = repo._relation_ann_candidates(
        nb.id, repo.embedder.embed_query("bravo"),
        repo._scale_index(nb.id, allow_stale=True), 10)
    assert rid not in sims


def test_relation_delta_included_when_opted_in(repo, monkeypatch):
    nb, rid = _build_indexed_nb_with_delta_relation(repo)
    monkeypatch.setattr(repo.settings, "scale_search_include_delta", True)
    sims = repo._relation_ann_candidates(
        nb.id, repo.embedder.embed_query("bravo"),
        repo._scale_index(nb.id, allow_stale=True), 10)
    assert rid in sims
```

`_build_indexed_nb_with_delta_relation` 的插行 SQL 抄 `tests/test_relation_ann.py` 现有 fixture(关系表列名以该文件为准),delta 关系挂在新 source `sB` 上。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_indexed_only_principle.py -q`
Expected: `test_relation_delta_excluded_by_default` FAIL

- [ ] **Step 3: 实现**

`_relation_ann_candidates` 的 delta 块同样整体包进 `if self.settings.scale_search_include_delta:`,注释同 Task 2 风格,原 try/except(`relation_ann_delta` model_error)保留在门内。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_indexed_only_principle.py tests/test_relation_ann.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_indexed_only_principle.py
git commit -m "feat(scale): 关系侧 delta 检索改 opt-in(同一原则第三处)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 4: PPR 图基底 self-delta splice 门控

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(`_active_kg_delta` ~9102;`_scale_combined_graph` 的 version 元组与 splice 调用 ~9285/~9326)
- Test: `backend/tests/test_indexed_only_principle.py`(追加)

**Interfaces:**
- Consumes: `_index_delta`(Task 1 分批版)。
- Produces: flag 关且 self 已索引时 `_active_kg_delta` 返回 `([], [], [])`;组合图缓存 version 元组含 flag 值。

- [ ] **Step 1: 写失败测试(追加)**

```python
def test_ppr_splice_excludes_self_delta_by_default(repo):
    """已索引库的 PPR 图基底默认不 splice 水位后 delta:delta chunk 不出现在
    scale_ppr 排名里;开 flag 后出现。用 test_scale_delta_policy 同款构造:
    delta source 的 chunk embedding 与查询最匹配。"""
    nb = _build_indexed_nb_with_delta_chunk(repo)   # (nb, delta_chunk_id)
    nb, d_cid = nb
    ranked = dict(repo.scale_ppr(nb.id, "bravo"))
    assert d_cid not in ranked


def test_ppr_splice_includes_self_delta_when_opted_in(repo, monkeypatch):
    nb, d_cid = _build_indexed_nb_with_delta_chunk(repo)
    monkeypatch.setattr(repo.settings, "scale_search_include_delta", True)
    ranked = dict(repo.scale_ppr(nb.id, "bravo"))
    assert d_cid in ranked
```

`_build_indexed_nb_with_delta_chunk`:抄 `tests/test_scale_delta_policy.py` 的 `_insert_source_chunk` + `_build_indexed_nb_with_delta`(source A 'alpha' 进水位 build,source B 'bravo' 之后插入)。两个测试都在同一 repo 实例上跑时注意组合图缓存——flag 翻转必须自然使缓存失效(这正是 version 元组含 flag 的验收点,两个用例共用一个 repo fixture 实例时顺序执行必须都过)。

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_indexed_only_principle.py -q`
Expected: `test_ppr_splice_excludes_self_delta_by_default` FAIL(delta 今日无条件 splice)

- [ ] **Step 3: 实现**

`_active_kg_delta`:

```python
        delta = self._index_delta(notebook_id)
        if delta["indexed"] and not self.settings.scale_search_include_delta:
            # 一个原则:已索引库的图基底只含已索引部分(核心 CSR 本身),水位后
            # delta 由 auto-fold 收进索引后自然可达。未索引的 active 小库(下方
            # src=None 整库 gather)是 two-tier 联邦的 active 层,不是 delta,
            # 不受此门控。
            return [], [], []
        src = delta["delta_sources"] if delta["indexed"] else None
```

`_scale_combined_graph`:version 元组追加 flag(翻转开关即缓存失效,不动 manifest 版本——绝不能把 flag 塞进 `_scale_index_version` 的 settings_tail,那会让所有存量索引 manifest 失配变 stale):

```python
        version = ("scale_combined", base_ver, active_ver,
                   bool(self.settings.scale_search_include_delta))
```

`_load` 内 splice 调用加空集守卫(空 delta 时跳过 splice,组合图=纯 base CSR):

```python
            if active_node_ids or active_edges:
                combined_ids, combined_A = si.splice_active(
                    combined_ids, combined_A, active_node_ids, active_edges)
```

(`combined_index`/`combined_chunk_ids.update(active_chunk_ids)` 等后续行不动;`_scale_xlayer_bridge_edges` 对 `active_node_ids=[]` 已自然短路——`_need_delta` 为 False。)

⚠️ 既有测试语义更新:`tests/test_scale_delta_policy.py`/`tests/test_scale_index_repo.py` 若有断言「delta 内容默认出现在 scale_ppr/组合图」的用例,属旧语义,改为在用例内 `monkeypatch.setattr(repo.settings, "scale_search_include_delta", True)` 后再断言(逐个看失败用例,只改语义相关断言,不动无关用例)。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_indexed_only_principle.py tests/test_scale_delta_policy.py tests/test_scale_index_repo.py tests/test_scale_index.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/
git commit -m "feat(scale): PPR 图基底 self-delta splice 改 opt-in(同一原则第四处,组合图缓存键含开关)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 5: 大 delta 时 fold 自动升级 full

**Files:**
- Modify: `backend/app/core/config.py`(新增 setting,放在 ~165 scale_auto_fold_on_add 附近)
- Modify: `backend/app/services/sqlite_repository.py`(`_resolve_scale_mode` ~8843)
- Test: `backend/tests/test_indexed_only_principle.py`(追加)

**Interfaces:**
- Produces: `settings.scale_fold_max_delta_sources: int = 500`(env `SCALE_FOLD_MAX_DELTA_SOURCES`,必须 validation_alias);`_resolve_scale_mode` 对 fold(显式或 auto 解析出)在 delta 源数超限时返回 "full"。

- [ ] **Step 1: 写失败测试(追加)**

```python
def test_resolve_scale_mode_upgrades_big_delta_fold_to_full(repo, monkeypatch):
    nb, _oid = _build_indexed_nb_with_delta_object(repo)   # 1 个 delta source
    monkeypatch.setattr(repo.settings, "scale_fold_max_delta_sources", 0)
    assert repo._resolve_scale_mode(nb.id, "fold") == "full"
    assert repo._resolve_scale_mode(nb.id, "auto") == "full"
    monkeypatch.setattr(repo.settings, "scale_fold_max_delta_sources", 500)
    assert repo._resolve_scale_mode(nb.id, "fold") == "fold"


def test_fold_threshold_env_alias(monkeypatch, tmp_path):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("SCALE_FOLD_MAX_DELTA_SOURCES", "7")
    from app.core.config import Settings
    assert Settings().scale_fold_max_delta_sources == 7
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_indexed_only_principle.py -q`
Expected: FAIL(setting 不存在)

- [ ] **Step 3: 实现**

config.py(scale_auto_fold_on_add 行后):

```python
    scale_fold_max_delta_sources: int = Field(500, validation_alias="SCALE_FOLD_MAX_DELTA_SOURCES")  # fold(含 auto 解析出的)在 delta 源数超此值时升级 full 全量重建:fold 逐源 incremental_fuse 是 O(delta) 但常数大(生产 48k 源 delta 实测不可行),大 delta 全量重建反而有界
```

`_resolve_scale_mode`:

```python
    def _resolve_scale_mode(self, notebook_id: str, mode: str) -> str:
        """把 mode 解析为具体操作:fold|full。
        auto = 有(含 stale)索引 → fold,否则 → full。
        fold(显式或 auto 解析出)在 delta 源数超 scale_fold_max_delta_sources 时
        升级 full:fold 的逐源 incremental_fuse + 增量 splice 常数大(生产 48k 源
        delta 实测十几小时不可行),大 delta 全量重建反而有界(~1h)。"""
        if mode not in ("fold", "full"):
            mode = "fold" if self._scale_index(notebook_id, allow_stale=True) is not None else "full"
        if mode == "fold":
            try:
                delta = self._index_delta(notebook_id)
                if len(delta["delta_sources"]) > self.settings.scale_fold_max_delta_sources:
                    return "full"
            except Exception:  # noqa: BLE001 — 探测失败不挡操作,维持 fold
                pass
        return mode
```

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_indexed_only_principle.py tests/test_auto_scale_index.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/core/config.py backend/app/services/sqlite_repository.py backend/tests/test_indexed_only_principle.py
git commit -m "feat(scale): fold 在 delta 超阈值(默认500源)时自动升级 full 全量重建

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 6: 大库无 ANN 候选时拒绝全量暴力(FTS 词法兜底)

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(`_retrieve_scored`,ANN 尝试之后、全量加载之前 ~9964)
- Modify: `scripts/diag_slow.py`(INTEREST_KINDS + 守卫拒绝分组各加 "kg_bruteforce_refused")
- Test: `backend/tests/test_indexed_only_principle.py`(追加)

**Interfaces:**
- Consumes: `app.services.kg.search.fts_search(db, notebook_id, q, k)`(kg_objects_fts,返回 [{object_id, name, score, match}])。
- Produces: 大库(copyable=False)且 `cand_sims is None` 时,`_retrieve_scored` 不做全量加载:FTS top-`chunk_recall` 词法候选 → 有界打分;FTS 空则返回 [];发 `kg_bruteforce_refused` 事件。小库路径字节不变。

- [ ] **Step 1: 写失败测试(追加)**

```python
def test_big_unindexed_lib_refuses_bruteforce(repo, monkeypatch):
    """大库 + 无索引:不做全量矩阵加载,FTS 词法兜底 + kg_bruteforce_refused 事件。"""
    nb = repo.create_notebook(NotebookCreate(name="big"))
    _sid, _cid, oid = _insert_source_with_object(repo, nb.id, 1)   # payload 名 'obj 1'
    with repo._write() as db:
        db.execute("INSERT INTO kg_objects_fts (notebook_id, object_id, name) VALUES (?,?,?)",
                   (nb.id, oid, "obj 1"))
    monkeypatch.setattr(repo.settings, "notebook_copy_max_rows", 0)   # 一切皆「大」
    events = []
    monkeypatch.setattr(repo.event_log, "emit", lambda e: events.append(e))

    def _boom(*a, **k):
        raise AssertionError("大库不得触发全量向量矩阵加载")
    monkeypatch.setattr(repo, "_vector_matrix", _boom)

    hits = repo._retrieve_scored(nb.id, "obj 1")
    assert oid in {h.object_id for h in hits}          # FTS 词法兜底仍可命中
    assert any(e.get("kind") == "kg_bruteforce_refused" for e in events)


def test_small_lib_bruteforce_unchanged(repo):
    nb = repo.create_notebook(NotebookCreate(name="small"))
    _sid, _cid, oid = _insert_source_with_object(repo, nb.id, 2)
    hits = repo._retrieve_scored(nb.id, "obj 2")       # 小库全量路径不受影响
    assert oid in {h.object_id for h in hits}
```

(kg_objects_fts 的列名以 `app/services/kg/search.py::fts_search` 的 SELECT 为准——object_id/name/notebook_id;若建表还有别的列,INSERT 按实际 DDL 调整,grep `kg_objects_fts` 找 CREATE。)

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_indexed_only_principle.py -q`
Expected: `test_big_unindexed_lib_refuses_bruteforce` FAIL(_boom 触发)

- [ ] **Step 3: 实现**

`_retrieve_scored` 里 ANN 尝试块(`if query_vector is not None: ...`)之后、`from app.services.vector_index import query_sims, build_matrix` 之前插入:

```python
        if cand_sims is None and not self.notebook_copy_stats(notebook_id)["copyable"]:
            # 大库拿不到 ANN 候选(未建索引/ANN 打不开/维度失配/embed 失败)——
            # 一个原则:绝不全量暴力(全表 json 解析 + 全量分词 + GB 级矩阵,
            # 49 万对象生产实测数十分钟)。FTS 词法有界兜底:kg_objects_fts 覆盖
            # 全部对象(含 delta),候选的语义分仍由下方按候选 evidence 元素向量
            # 有界补充。FTS 空 → [](与 relation 侧冷矩阵守卫同一 fail-open 出口)。
            from app.services.kg.search import fts_search
            with self._connect() as db:
                lex = fts_search(db, notebook_id, query, k=self.settings.chunk_recall)
            self.event_log.emit({
                "kind": "kg_bruteforce_refused", "notebook_id": notebook_id,
                "site": "_retrieve_scored", "lexical_candidates": len(lex),
            })
            if not lex:
                return []
            cand_sims = {h["object_id"]: 0.0 for h in lex}
```

(此后 `id_filter = set(cand_sims)` 走既有有界分支:对象加载/分词/边查询/element_sims 全部有界。`knowledge_sims` 里词法候选的 0.0 语义分只压低融合分,不破坏 [0,1]。)

`scripts/diag_slow.py`:`INTEREST_KINDS` 元组加 `"kg_bruteforce_refused"`;`report_events` 中守卫拒绝分组的 `elif k in (...)` 元组同步加。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_indexed_only_principle.py tests/test_p4_kg_shrink.py -q && python -m py_compile scripts/../../scripts/diag_slow.py`
(diag 编译检查在仓库根:`python -m py_compile scripts/diag_slow.py`)
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_indexed_only_principle.py scripts/diag_slow.py
git commit -m "feat(retrieval): 大库无 ANN 候选拒绝全量暴力,FTS 词法有界兜底+事件

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 7: element 侧大库守卫

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(`_retrieve_elements` ~10154)
- Modify: `scripts/diag_slow.py`(两处元组加 "element_scoring_skipped")
- Test: `backend/tests/test_indexed_only_principle.py`(追加)

**Interfaces:**
- Produces: 大库时 `_retrieve_elements` 返回 [] + `element_scoring_skipped` 事件;调用方(reasoning `search_elements` 动作、chunk 模式兜底层)对空结果已天然容错。

- [ ] **Step 1: 写失败测试(追加)**

```python
def test_big_lib_element_search_skipped(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="big"))
    _insert_source_with_object(repo, nb.id, 3)
    monkeypatch.setattr(repo.settings, "notebook_copy_max_rows", 0)
    events = []
    monkeypatch.setattr(repo.event_log, "emit", lambda e: events.append(e))
    assert repo._retrieve_elements(nb.id, "anything") == []
    assert any(e.get("kind") == "element_scoring_skipped" for e in events)
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_indexed_only_principle.py::test_big_lib_element_search_skipped -q`
Expected: FAIL(当前会全表扫返回打分结果或空但无事件)

- [ ] **Step 3: 实现**

`_retrieve_elements` 开头加:

```python
        if not self.notebook_copy_stats(notebook_id)["copyable"]:
            # source_elements 没有索引模态,本方法=全表扫+逐行向量解码(生产
            # 17 万元素×4096 维=数 GB/次)。大库跳过并发事件,返回 [] ——
            # 调用方(reasoning search_elements、chunk 兜底层)均容错空结果。
            self.event_log.emit({
                "kind": "element_scoring_skipped", "notebook_id": notebook_id,
                "site": "_retrieve_elements", "reason": "large_notebook",
            })
            return []
```

diag_slow.py 两处元组加 `"element_scoring_skipped"`。

- [ ] **Step 4: 跑测试确认通过**

Run: `cd backend && python -m pytest tests/test_indexed_only_principle.py -q`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_indexed_only_principle.py scripts/diag_slow.py
git commit -m "feat(retrieval): element 检索大库守卫(无索引模态不做全表扫,事件+空结果)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 8: 前端徽章语义同步

**Files:**
- Modify: `frontend/app/page.tsx`(~3112 与 ~4162 两处「N 源待索引」徽章)

**Interfaces:**
- Consumes: 既有 `scale-index/status` 字段(`unindexed_sources`/`delta_searchable`),无后端改动。

- [ ] **Step 1: 改文案**

两处徽章 span(条件 `s.exists && !s.delta_searchable && (s.unindexed_sources ?? 0) > 0` 保持不变)加 `title` 提示,让「待索引=全检索面不可见」的新语义可发现。先读上下文找到确切 JSX(两处结构相同):

```tsx
<span title="未索引部分不参与检索与推理（chunk/KG对象/关系/图谱漫游）；点「重建索引」或等待自动增量收进后可见">
  {` · ${s.unindexed_sources} 源待索引`}
</span>
```

若该文案已在带样式的 span 内,只加 `title` 属性,不动样式与既有弯引号文案。

- [ ] **Step 2: 类型检查**

Run: `cd frontend && npx tsc --noEmit`
Expected: 0 errors

- [ ] **Step 3: 弯引号防呆自查**

Run: `git diff frontend/app/page.tsx | grep -c '^-.*[“”]'`
Expected: `0`(没有删掉任何既有弯引号文案)

- [ ] **Step 4: Commit**

```bash
git add frontend/app/page.tsx
git commit -m "feat(ui): 「N 源待索引」徽章补全检索面语义提示(检索/推理均不含未索引部分)

Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>"
```

---

### Task 9: 全量验证 + PR

- [ ] **Step 1: 后端全量测试**

Run: `cd backend && python -m pytest tests/ -q`
Expected: 全绿(0 failed;若有 Task 4 提到的旧语义断言漏网,按该任务的更新规则修)

- [ ] **Step 2: 仓库检查脚本**

Run: `bash scripts/check.sh`
Expected: 绿(该脚本含 lint/测试合集)

- [ ] **Step 3: rebase + push + PR**

```bash
git fetch origin && git rebase origin/master || true
# 若 rebase 冲突在 scripts/diag_slow.py 且 #176 已合并:以本分支版本为准解决后 continue
git push -u origin feat/indexed-only-retrieval
gh pr create --base master --title "feat(scale): 大库检索统一「只检索已索引部分」原则" --body "$(cat <<'EOF'
## 原则
已索引的库,所有检索路径默认只检索已索引部分;水位后 delta 参与检索是 opt-in(SCALE_SEARCH_INCLUDE_DELTA,默认关);delta 靠 auto-fold/重建收进索引(最终一致)。此前该原则只落在 chunk 侧(PR#158),本 PR 推广到其余全部路径,并修掉让「最终一致」失效的基础设施 bug。

## 变更
- KG 对象/关系/PPR 图基底(self-delta splice)三处 delta 消费点统一门控(组合图缓存键含开关)
- delta SQL 全部 IN 分批(_in_batches):生产 48,739 delta source 打爆 SQLite 32,766 变量上限,连锁炸掉检索 delta/状态端点/auto-fold 评估——auto-fold 因此从未跑成,delta 才积成山
- fold 在 delta 超阈值(SCALE_FOLD_MAX_DELTA_SOURCES=500)时自动升级 full 全量重建
- 大库无 ANN 候选(未建索引/ANN 失效/维度失配/embed 失败)拒绝全量暴力:FTS 词法有界兜底 + kg_bruteforce_refused 事件(49 万对象全量路径生产实测数十分钟)
- element 检索大库守卫(无索引模态,事件+空结果)
- 前端「N 源待索引」徽章补全检索面语义;diag_slow.py 关注事件同步
- 不变量:小库路径字节不变;开关开=恢复强一致暴力;relevance [0,1]/tau 不动

Stacked on #176(分支含其 diag_slow.py 扩展提交;#176 先合则 rebase 自然去重)。

🤖 Generated with [Claude Code](https://claude.com/claude-code)
EOF
)"
```

---

## Self-Review 结论

- 覆盖检查:四处 delta 门控(chunk 既有+对象 T2+关系 T3+PPR T4)、IN 分批 T1、fold 升级 T5、大库暴力拒绝 T6、element 守卫 T7、前端 T8 —— 与 spec 的六项逐一对应 ✓
- 无占位符:每步含真实代码/命令;涉及既有 fixture 的两处(关系表列名、kg_objects_fts DDL)明确指了核对来源文件 ✓
- 类型一致:`_in_batches` 签名在 T1 定义,T1(c-g) 用法一致;`scale_fold_max_delta_sources` 名称 T5 定义与 `_resolve_scale_mode` 引用一致;事件 kind 两个新值与 diag_slow.py 增补一致 ✓
- 已知风险:T4 可能翻出旧语义断言(计划内列了处理规则);T6 的 kg_objects_fts INSERT 列名需按 DDL 微调(已标注)。
