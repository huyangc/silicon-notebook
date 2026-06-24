# 增量 KG 融合(分层)+ KG 抽取 LLM 独立 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 上传新源后自动**增量**把其 KG 实体融合进 `concept_clusters`(Tier1 名种子 append → Tier2 向量桥接入队 → Tier3 手动全量逃生口),修复「新论文入库但未融合、PPR/概念漫游跨不到」;并给 KG 构建 LLM 独立的 `KG_LLM_*` 配置组。

**Architecture:** 复用 `cluster_objects` 已有的分层逻辑(精确名种子 force-union + 向量候选不自动并、入队),把它限定在「新实体 vs 已有簇」+ 追加写。`kg_llm_client` 照抄 `reasoning_llm_client` 的「配齐用独立、否则回退主」范式。只动 KG 构建侧,在线问答零改。

**Tech Stack:** Python / FastAPI / SQLite / numpy;pytest。

**Spec:** [docs/superpowers/specs/2026-06-24-incremental-kg-fusion-design.md](../specs/2026-06-24-incremental-kg-fusion-design.md)

**约束(记忆):** 中文;模型仅 URL 端点;守 [0,1]/tau;收尾 rebase→push→PR;commit 末署名 `Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>`。分支 `claude/incremental-kg-fusion`(off origin/master)。

---

## 文件结构
- `backend/app/core/config.py` — `KG_LLM_*`(3)+ `kg_llm_configured`;`KG_INCREMENTAL_FUSION_ENABLED`。
- `backend/app/services/sqlite_repository.py` — `__init__` 建 `self._kg_llm_client`;`kg_llm_client` 属性;KG 构建 LLM 调用切到它;`append_clusters`、`incremental_fuse_source`;`_run_extraction` 接线。
- `backend/app/services/kg_merge.py` — `place_new_concepts`(Tier1)、`detect_bridge_candidates`(Tier2)。
- 新建 `backend/tests/test_incremental_fusion.py`、`backend/tests/test_kg_llm_client.py`。

---

## Task 1: B —— KG 构建 LLM 独立配置 `KG_LLM_*`

**Files:** Modify `config.py`、`sqlite_repository.py`;Test `tests/test_kg_llm_client.py`(新建)

- [ ] **Step 1: 写失败测试**

`backend/tests/test_kg_llm_client.py`:
```python
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder


def test_kg_llm_configured_default_false(monkeypatch):
    for k in ("KG_LLM_BASE_URL", "KG_LLM_API_KEY", "KG_LLM_MODEL"):
        monkeypatch.delenv(k, raising=False)
    assert Settings(_env_file=None).kg_llm_configured is False
    monkeypatch.setenv("KG_LLM_BASE_URL", "https://kg.example")
    monkeypatch.setenv("KG_LLM_API_KEY", "k")
    monkeypatch.setenv("KG_LLM_MODEL", "kg-extract-fast")
    assert Settings(_env_file=None).kg_llm_configured is True


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings(_env_file=None)); r.embedder = FakeEmbedder(dim=16); return r


def test_kg_llm_client_falls_back_to_main_when_unset(repo):
    # KG_LLM_* 未配 → kg_llm_client 动态回退到当前 self.llm_client(含测试替身)
    sentinel = object()
    repo.llm_client = sentinel
    assert repo.kg_llm_client is sentinel
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_kg_llm_client.py -q`
Expected: FAIL — `Settings` 无 `kg_llm_configured`;`repo` 无 `kg_llm_client`。

- [ ] **Step 3: 实现 config**

`config.py` 在 `rewrite_llm_*`(~:47-49)之后加:
```python
    kg_llm_base_url: str = Field("", env="KG_LLM_BASE_URL")
    kg_llm_api_key: str = Field("", env="KG_LLM_API_KEY")
    kg_llm_model: str = Field("", env="KG_LLM_MODEL")
```
在 `reasoning_llm_configured` property 附近加:
```python
    @property
    def kg_llm_configured(self) -> bool:
        return bool(self.kg_llm_base_url and self.kg_llm_api_key and self.kg_llm_model)
```
并在 `kg_*` 区加增量开关(Task 2 也要,但本步一并加):
```python
    kg_incremental_fusion_enabled: bool = Field(True, env="KG_INCREMENTAL_FUSION_ENABLED")
```

- [ ] **Step 4: 实现 client(照抄 reasoning 范式)**

`sqlite_repository.py` `__init__` 中,`self._reasoning_llm_client = (...)` 块之后加(镜像其结构):
```python
        self._kg_llm_client = (
            OpenAICompatibleClient(
                settings,
                base_url=settings.kg_llm_base_url,
                api_key=settings.kg_llm_api_key,
                model=settings.kg_llm_model,
            )
            if settings.kg_llm_configured
            else None
        )
```
在 `reasoning_llm_client` property 之后加:
```python
    @property
    def kg_llm_client(self):
        """KG 构建/融合专用 LLM(批量离线)。配齐 KG_LLM_* → 独立模型;否则动态回退
        到当前 self.llm_client(含测试替身),未配置时与今天行为完全一致。"""
        if self._kg_llm_client is not None:
            return self._kg_llm_client
        return self.llm_client
```

- [ ] **Step 5: 把 KG 构建 LLM 调用切到 kg_llm_client**

五处(`self.llm_client` → `self.kg_llm_client`):
- `_run_extraction` 的**配置门控**([~:1598](backend/app/services/sqlite_repository.py:1598))`if not getattr(self.llm_client, "configured", False):` → `getattr(self.kg_llm_client, "configured", False)`(否则配了独立 KG 模型但主模型空时会被错跳过)。
- `_run_extraction`([:1614-1615](backend/app/services/sqlite_repository.py:1614))`extract_graph(self.llm_client, …)` → `extract_graph(self.kg_llm_client, …)`(refine/glean 经 extract_graph 跟随)。
- `rebuild_unified_kg` 簇描述门控([~:3300](backend/app/services/sqlite_repository.py:3300))`getattr(self.llm_client, "configured", …)` 与其下 `self.llm_client.chat_json(…)` → `self.kg_llm_client`。
- `rebuild_unified_kg` 的 `review_merge_candidates(self.llm_client, …)`([~:3287](backend/app/services/sqlite_repository.py:3287))→ `self.kg_llm_client`。
- 冲突复审 `review_conflict_candidates(self.llm_client, …)`([~:3044](backend/app/services/sqlite_repository.py:3044))→ `self.kg_llm_client`。
（在线问答 ask/reasoning/概念漫游答案**不动**。)

- [ ] **Step 6: 加「抽取把 kg_llm_client 传给 extract_graph」的断言测试**

追加到 `tests/test_kg_llm_client.py`(monkeypatch `extract_graph` 捕获 client 参数,插一行真实 source 让 `_run_extraction` 的 DB 操作不报错):
```python
def test_extraction_passes_kg_llm_client_to_extract_graph(repo, monkeypatch):
    import app.services.kg_ingest as kg_ingest
    captured = {}
    def _fake(client, *a, **k):
        captured["client"] = client
        return type("G", (), {"objects": [], "relations": []})()
    monkeypatch.setattr(kg_ingest, "extract_graph", _fake)
    kg_stub = type("KG", (), {"configured": True})()
    repo._kg_llm_client = kg_stub
    repo.llm_client = type("Main", (), {"configured": True})()   # 主 client 也 configured,证明门控/调用都走 kg
    from app.models.schemas import NotebookCreate
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    now = "2026-06-22T00:00:00"
    with repo._write() as db:
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?)", ("src-x", nb.id, "T", "md", "extracted", "extracted", now, now))
    monkeypatch.setattr(repo, "source_elements", lambda sid: [])
    monkeypatch.setattr(repo, "_source_raw_text", lambda s, e: "MoE 是一种架构。")
    repo._run_extraction("src-x")
    assert captured.get("client") is kg_stub        # 抽取用 kg_llm_client,非主 client
```
（若 `_run_extraction` 还依赖别的 source 字段/表致插入不足,实现期补齐该 source 行的必需列;核心断言不变。)

- [ ] **Step 7: 跑测试 + 提交**

Run: `cd backend && python -m pytest tests/test_kg_llm_client.py -q` → PASS。
```bash
git add backend/app/core/config.py backend/app/services/sqlite_repository.py backend/tests/test_kg_llm_client.py
git commit -m "$(cat <<'EOF'
feat(kg): dedicated KG_LLM_* config for KG construction LLM (falls back to main)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 2: A·Tier1 —— 名种子增量 append + 接线 + flag

**Files:** Modify `kg_merge.py`、`sqlite_repository.py`;Test `tests/test_incremental_fusion.py`(新建)

- [ ] **Step 1: 写失败测试**

`backend/tests/test_incremental_fusion.py`:
```python
import json
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings(_env_file=None)); r.embedder = FakeEmbedder(dim=16); return r


def test_place_new_concepts_name_seed(repo):
    from app.services.kg_merge import place_new_concepts, _norm
    existing_cmap = {"obj-old": "K-mixtureofexperts"}              # 已有「MoE」簇
    existing_names = {"K-mixtureofexperts": "Mixture-of-Experts"}
    new = [{"object_id": "obj-new", "name": "Mixture-of-Experts (MoE)"},
           {"object_id": "obj-x", "name": "Quantization"}]
    rows = place_new_concepts(new, existing_cmap, existing_names,
                              seed_fn=lambda o: _norm(o["name"]), id_prefix="K-")
    by_oid = {r["member_object_id"]: r for r in rows}
    # 同名归一 → 命中已有簇(canonical_id 一致、复用簇名)
    assert by_oid["obj-new"]["canonical_id"] == "K-" + _norm("Mixture-of-Experts (MoE)")
    assert by_oid["obj-new"]["canonical_id"] in existing_names or \
           by_oid["obj-new"]["canonical_id"] == "K-mixtureofexperts"
    # 新名 → 新簇(用自身名)
    assert by_oid["obj-x"]["canonical_name"] == "Quantization"


def _seed_one_doc_moe(repo, sid, cid_chunk, obj_id, name="Mixture-of-Experts (MoE)"):
    """直插一个源 + 一个 concept(+evidence) + 已入簇,模拟『已有 KG+簇』。"""
    nb_existing = None
    return None  # 占位,见下方端到端测试用真实抽取或直插


def test_incremental_fuse_appends_same_name_cross_doc(repo):
    """已有 doc 的『MoE』簇 + 新源也抽到『MoE』concept → 增量后新 concept 进同簇。"""
    nb = repo.create_notebook(NotebookCreate(name="kb"))
    now = "2026-06-22T00:00:00"
    with repo._write() as db:
        # 已有源 A 的 concept(已入簇 K-...)
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("ko-A", nb.id, "concept", "approved", "", json.dumps({"name":"Mixture-of-Experts (MoE)"}), "[]", "src-A", now, now))
        from app.services.kg_merge import _norm
        cid = "K-" + _norm("Mixture-of-Experts (MoE)")
        db.execute("INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,canonical_description,created_at) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   ("cc-A", nb.id, cid, "ko-A", "Mixture-of-Experts (MoE)", "concept", "", now))
        # 新源 B 抽到的 concept(尚未入簇)
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("ko-B", nb.id, "concept", "approved", "", json.dumps({"name":"Mixture-of-Experts (MoE)"}), "[]", "src-B", now, now))
    repo.invalidate_cluster_cache(nb.id) if hasattr(repo, "invalidate_cluster_cache") else None
    repo.incremental_fuse_source(nb.id, "src-B")
    cmap = repo.cluster_map(nb.id)
    assert cmap.get("ko-B") == cmap.get("ko-A")   # 新 concept 进了同一跨文档簇


def test_incremental_fuse_flag_off(repo, monkeypatch):
    monkeypatch.setattr(repo.settings, "kg_incremental_fusion_enabled", False)
    nb = repo.create_notebook(NotebookCreate(name="kb"))
    now = "2026-06-22T00:00:00"
    with repo._write() as db:
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("ko-B", nb.id, "concept", "approved", "", json.dumps({"name":"X"}), "[]", "src-B", now, now))
    repo.incremental_fuse_source(nb.id, "src-B")
    assert repo.cluster_map(nb.id).get("ko-B") is None   # flag 关 → 不融合
```

- [ ] **Step 2: 跑测试确认失败**

Run: `cd backend && python -m pytest tests/test_incremental_fusion.py -q -k "place_new_concepts or appends_same_name or flag_off"`
Expected: FAIL — `place_new_concepts`/`incremental_fuse_source` 未定义。

- [ ] **Step 3: `kg_merge.place_new_concepts`(Tier1)**

`kg_merge.py` 末尾加(复用本文件的 `_norm`):
```python
def place_new_concepts(new_objects, existing_cluster_map, existing_canon_names,
                       *, seed_fn, id_prefix="K-"):
    """Tier-1 名种子放置:每个新对象按 seed_fn → canonical_id 追加到已有簇或建新簇。
    不重排任何已有对象(已有 canonical_id 不变 → 无簇分布漂移)。
    existing_cluster_map: {existing_object_id: canonical_id};existing_canon_names: {canonical_id: name}。
    返回 [{canonical_id, member_object_id, canonical_name}]。"""
    existing_cids = set(existing_cluster_map.values())
    rows = []
    for o in new_objects:
        cid = f"{id_prefix}{seed_fn(o)}"
        name = o.get("name", "")
        canon_name = existing_canon_names.get(cid, name) if cid in existing_cids else name
        rows.append({"canonical_id": cid, "member_object_id": o["object_id"],
                     "canonical_name": canon_name})
    return rows
```

- [ ] **Step 4: `append_clusters` + `incremental_fuse_source`**

`sqlite_repository.py`(`write_clusters` 附近)加追加写:
```python
    def append_clusters(self, notebook_id: str, rows: list, object_type: str = "concept") -> int:
        """追加写 concept_clusters(不 DELETE);member_object_id 幂等(已在则跳过)。返回新增数。"""
        now = _now()
        added = 0
        with self._write() as db:
            existing = {r["member_object_id"] for r in db.execute(
                "SELECT member_object_id FROM concept_clusters WHERE notebook_id=? AND object_type=?",
                (notebook_id, object_type)).fetchall()}
            for r in rows:
                if r["member_object_id"] in existing:
                    continue
                db.execute(
                    "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,canonical_description,created_at) "
                    "VALUES (?,?,?,?,?,?,?,?)",
                    (f"cc-{uuid4().hex[:10]}", notebook_id, r["canonical_id"], r["member_object_id"],
                     r["canonical_name"], object_type, "", now))
                added += 1
        return added
```
加增量融合入口(Tier1;Tier2 在 Task 3 追加到标注处):
```python
    def incremental_fuse_source(self, notebook_id: str, source_id: str) -> None:
        """上传后增量融合该源 concept 进 concept_clusters。Tier1 名种子 append(无 LLM)。"""
        if not self.settings.kg_incremental_fusion_enabled:
            return
        from app.services.kg_merge import place_new_concepts, _norm
        with self._connect() as db:
            new = db.execute(
                "SELECT id, payload FROM knowledge_objects WHERE notebook_id=? AND source_id=? "
                "AND object_type='concept' AND status!='deprecated'",
                (notebook_id, source_id)).fetchall()
            cn = db.execute(
                "SELECT DISTINCT canonical_id, canonical_name FROM concept_clusters "
                "WHERE notebook_id=? AND object_type='concept'", (notebook_id,)).fetchall()
        new_objs = [{"object_id": r["id"],
                     "name": json.loads(r["payload"] or "{}").get("name", "")} for r in new]
        if not new_objs:
            return
        cmap = self.cluster_map(notebook_id)
        canon_names = {r["canonical_id"]: r["canonical_name"] for r in cn}
        rows = place_new_concepts(new_objs, cmap, canon_names,
                                  seed_fn=lambda o: _norm(o["name"]), id_prefix="K-")
        self.append_clusters(notebook_id, rows, object_type="concept")
        # >>> Tier2(Task 3)在此插入桥接检测 + 入队 <<<
        self._invalidate_unified_cache(notebook_id)
```

- [ ] **Step 5: 接进 `_run_extraction`(store_kg 之后,fail-open)**

`_run_extraction` 中 `n_obj, n_rel = self.store_kg(...)`([~:1631](backend/app/services/sqlite_repository.py:1631))之后、`extraction_runs ... completed` UPDATE 之前插:
```python
            try:
                self.incremental_fuse_source(source.notebook_id, source.id)
            except Exception:
                self.event_log.logger.exception("incremental_fuse_source failed for %s", source_id)
```

- [ ] **Step 6: 跑测试 + 提交**

Run: `cd backend && python -m pytest tests/test_incremental_fusion.py -q` → PASS。
```bash
git add backend/app/services/kg_merge.py backend/app/services/sqlite_repository.py backend/app/core/config.py backend/tests/test_incremental_fusion.py
git commit -m "$(cat <<'EOF'
feat(kg): Tier1 incremental fusion — name-seed append on upload

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 3: A·Tier2 —— 向量桥接检测 → 入队(不自动并)

**Files:** Modify `kg_merge.py`、`sqlite_repository.py`;Test `tests/test_incremental_fusion.py`

- [ ] **Step 1: 写失败测试**

追加:
```python
def test_tier2_bridge_enqueues_candidate_not_merge(repo):
    """新 concept 向量近一个异名异簇已有 concept → 入 concept_merge_candidates,
    且不自动并簇。"""
    nb = repo.create_notebook(NotebookCreate(name="kb"))
    now = "2026-06-22T00:00:00"
    v_old = json.dumps([1.0] + [0.0]*15)
    v_new = json.dumps([0.99] + [0.0]*15)   # 与 old 余弦≈1
    with repo._write() as db:
        for oid, nm, src in [("ko-old", "Expert Routing", "src-A"), ("ko-new", "MoE Gating", "src-B")]:
            db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                       "VALUES (?,?,?,?,?,?,?,?,?,?)",
                       (oid, nb.id, "concept", "approved", "", json.dumps({"name":nm}), "[]", src, now, now))
        from app.services.kg_merge import _norm
        db.execute("INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,canonical_description,created_at) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   ("cc-old", nb.id, "K-"+_norm("Expert Routing"), "ko-old", "Expert Routing", "concept", "", now))
        for oid, vec in [("ko-old", v_old), ("ko-new", v_new)]:
            db.execute("INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) VALUES (?,?,?,?)",
                       (oid, nb.id, vec, now))
    repo.incremental_fuse_source(nb.id, "src-B")
    with repo._connect() as db:
        n = db.execute("SELECT count(*) c FROM concept_merge_candidates WHERE notebook_id=?", (nb.id,)).fetchone()["c"]
        cmap = repo.cluster_map(nb.id)
    assert n >= 1                                   # 桥接候选入队
    assert cmap["ko-new"] != cmap["ko-old"]         # 未自动并(各属自己名种子簇)
```

- [ ] **Step 2: 跑确认失败** —— `concept_merge_candidates` 为 0(Tier2 未实现)。

- [ ] **Step 3: `kg_merge.detect_bridge_candidates`(numpy 余弦,new-vs-existing)**

```python
def detect_bridge_candidates(new_items, new_vectors, existing_items, existing_vectors,
                             existing_cluster_map, rejected, *, hi=0.94, lo=0.82, top_k=5):
    """新对象 embedding 对已有对象做余弦,命中 ≥lo 且落在不同 canonical 簇、且对未被 rejected →
    桥接候选。返回 [{canonical_a, canonical_b, score}](a<b 去序)。new-vs-existing,O(N_new × N_exist)
    分块 numpy,不动已有。"""
    import numpy as np
    if not new_vectors or not existing_vectors:
        return []
    ex_ids = [i["object_id"] for i in existing_items if i["object_id"] in existing_vectors]
    if not ex_ids:
        return []
    EX = np.asarray([existing_vectors[i] for i in ex_ids], dtype="float32")
    EX /= (np.linalg.norm(EX, axis=1, keepdims=True) + 1e-9)
    ex_cid = {i["object_id"]: existing_cluster_map.get(i["object_id"]) for i in existing_items}
    from app.services.kg_merge import _norm
    out, seen = [], set()
    for it in new_items:
        v = new_vectors.get(it["object_id"])
        if v is None:
            continue
        q = np.asarray(v, dtype="float32"); q /= (np.linalg.norm(q) + 1e-9)
        sims = EX @ q
        idx = np.argsort(-sims)[:top_k]
        my_cid = "K-" + _norm(it.get("name", ""))
        for j in idx:
            s = float(sims[j])
            if s < lo:
                break
            other_cid = ex_cid.get(ex_ids[j])
            if not other_cid or other_cid == my_cid:
                continue
            a, b = sorted((my_cid, other_cid))
            if frozenset((a, b)) in rejected or (a, b) in seen:
                continue
            seen.add((a, b))
            out.append({"canonical_a": a, "canonical_b": b, "score": s})
    return out
```

- [ ] **Step 4: `incremental_fuse_source` 接 Tier2(在 Step4 标注处)**

把 `# >>> Tier2 <<<` 替换为:
```python
        with self._connect() as db:
            ex = db.execute(
                "SELECT id, payload FROM knowledge_objects WHERE notebook_id=? "
                "AND object_type='concept' AND status!='deprecated' AND source_id!=?",
                (notebook_id, source_id)).fetchall()
            vrows = db.execute("SELECT object_id, vector FROM knowledge_embeddings WHERE notebook_id=?",
                               (notebook_id,)).fetchall()
        dim = self.settings.embed_dim
        vecs = {r["object_id"]: json.loads(r["vector"]) for r in vrows
                if len(json.loads(r["vector"])) == dim}
        existing_items = [{"object_id": r["id"]} for r in ex]
        new_vecs = {o["object_id"]: vecs[o["object_id"]] for o in new_objs if o["object_id"] in vecs}
        decided = self.decided_pairs(notebook_id)   # 键 (canonical_a, canonical_b),concept 为 "K-<seed>" 形式(已核)
        rejected = {frozenset((a, b)) for (a, b), s in decided.items() if s == "rejected"}
        from app.services.kg_merge import detect_bridge_candidates
        cands = detect_bridge_candidates(new_objs, new_vecs, existing_items, vecs, cmap, rejected)
        if cands:
            now = _now()
            with self._write() as db:
                for c in cands:
                    db.execute(
                        "INSERT INTO concept_merge_candidates (id,notebook_id,canonical_a,canonical_b,score,status,created_at,updated_at) "
                        "VALUES (?,?,?,?,?, 'pending', ?, ?)",
                        (f"cm-{uuid4().hex[:10]}", notebook_id, c["canonical_a"], c["canonical_b"], c["score"], now, now))
```
（注:`decided_pairs` 返回键是 canonical 对;若其归一格式与 `K-<seed>` 不同,实现期对齐——见 `rebuild_unified_kg` 的 `_seed` 用法。)

- [ ] **Step 5: 跑测试 + 提交**

Run: `cd backend && python -m pytest tests/test_incremental_fusion.py -q` → PASS。
```bash
git add backend/app/services/kg_merge.py backend/app/services/sqlite_repository.py backend/tests/test_incremental_fusion.py
git commit -m "$(cat <<'EOF'
feat(kg): Tier2 incremental fusion — vector bridge detection enqueued (no auto-merge)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 4: claim/formula/procedure 增量(名种子,无向量层)

**Files:** Modify `sqlite_repository.py`;Test `tests/test_incremental_fusion.py`

- [ ] **Step 1: 写失败测试**
```python
def test_incremental_fuse_claim_by_name(repo):
    nb = repo.create_notebook(NotebookCreate(name="kb"))
    now = "2026-06-22T00:00:00"
    from app.services.kg_merge import seed_claim
    with repo._write() as db:
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("kl-A", nb.id, "claim", "approved", "", json.dumps({"name":"MoE raises capacity"}), "[]", "src-A", now, now))
        db.execute("INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,canonical_description,created_at) "
                   "VALUES (?,?,?,?,?,?,?,?)",
                   ("ccl-A", nb.id, "KL-"+seed_claim({"name":"MoE raises capacity"}), "kl-A", "MoE raises capacity", "claim", "", now))
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("kl-B", nb.id, "claim", "approved", "", json.dumps({"name":"MoE raises capacity"}), "[]", "src-B", now, now))
    repo.incremental_fuse_source(nb.id, "src-B")
    cmap = repo.cluster_map(nb.id)
    assert cmap.get("kl-B") == cmap.get("kl-A")
```

- [ ] **Step 2: 跑确认失败**(claim 未被增量处理)。

- [ ] **Step 3: `incremental_fuse_source` 扩到三类型**

在 concept 处理之后,对 claim/formula/procedure 各跑名种子 append(复用 `seed_claim/seed_formula/seed_procedure` + `KL-/KF-/KP-` 前缀,**不做向量层**):
```python
        from app.services.kg_merge import seed_claim, seed_formula, seed_procedure
        _TYPES = {"claim": (seed_claim, "KL-"), "formula": (seed_formula, "KF-"),
                  "procedure": (seed_procedure, "KP-")}
        for t, (sfn, prefix) in _TYPES.items():
            with self._connect() as db:
                trows = db.execute(
                    "SELECT id, payload FROM knowledge_objects WHERE notebook_id=? AND source_id=? "
                    "AND object_type=? AND status!='deprecated'", (notebook_id, source_id, t)).fetchall()
                tcn = db.execute("SELECT DISTINCT canonical_id, canonical_name FROM concept_clusters "
                                 "WHERE notebook_id=? AND object_type=?", (notebook_id, t)).fetchall()
            tnew = [{"object_id": r["id"], "payload": json.loads(r["payload"] or "{}"),
                     "name": json.loads(r["payload"] or "{}").get("name", "")} for r in trows]
            if not tnew:
                continue
            tcanon = {r["canonical_id"]: r["canonical_name"] for r in tcn}
            trows_w = place_new_concepts(tnew, self.cluster_map(notebook_id), tcanon,
                                         seed_fn=lambda o, _s=sfn: _s(o["payload"]), id_prefix=prefix)
            self.append_clusters(notebook_id, trows_w, object_type=t)
```
（`seed_claim` 等签名以 `kg_merge.py` 实际为准:若它们取 `payload` 而非 obj,Step 用 `_s(o["payload"])`;实现期核对。）

- [ ] **Step 4: 跑测试 + 提交**

Run: `cd backend && python -m pytest tests/test_incremental_fusion.py -q` → PASS。
```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_incremental_fusion.py
git commit -m "$(cat <<'EOF'
feat(kg): incremental fusion for claim/formula/procedure (name-seed)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## Task 5: 集成 + 隔离 + 全量

**Files:** Test `tests/test_incremental_fusion.py`

- [ ] **Step 1: 写隔离 + 幂等测试**
```python
def test_incremental_fuse_idempotent(repo):
    nb = repo.create_notebook(NotebookCreate(name="kb"))
    now = "2026-06-22T00:00:00"
    with repo._write() as db:
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("ko-B", nb.id, "concept", "approved", "", json.dumps({"name":"X"}), "[]", "src-B", now, now))
    repo.incremental_fuse_source(nb.id, "src-B")
    repo.incremental_fuse_source(nb.id, "src-B")   # 二次
    with repo._connect() as db:
        n = db.execute("SELECT count(*) c FROM concept_clusters WHERE notebook_id=? AND member_object_id='ko-B'", (nb.id,)).fetchone()["c"]
    assert n == 1                                   # 幂等,不重复成员


def test_rebuild_unified_kg_still_works(repo):
    """全量逃生口不受影响:rebuild 后所有 concept 入簇。"""
    nb = repo.create_notebook(NotebookCreate(name="kb"))
    now = "2026-06-22T00:00:00"
    with repo._write() as db:
        db.execute("INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("ko-A", nb.id, "concept", "approved", "", json.dumps({"name":"MoE"}), "[]", "src-A", now, now))
    repo.rebuild_unified_kg(nb.id)
    assert "ko-A" in repo.cluster_map(nb.id)
```

- [ ] **Step 2: 跑新文件** —— `cd backend && python -m pytest tests/test_incremental_fusion.py tests/test_kg_llm_client.py -q` → PASS。

- [ ] **Step 3: 隔离套件** —— `cd backend && python -m pytest tests/test_ppr_retrieve.py tests/test_ppr.py tests/test_reasoning_ppr.py tests/test_mix_overlay.py tests/test_kg_scheduler.py -q` → PASS(融合/抽取改动不破 PPR/聚类/抽取调度)。

- [ ] **Step 4: 全量** —— `cd backend && python -m pytest -q` → 0 failed(记 passed 数;注意 default-on 的增量融合是否影响既有抽取/KG 测试,若有断言 KG 计数的用例需复核)。

- [ ] **Step 5: 提交**
```bash
git add backend/tests/test_incremental_fusion.py
git commit -m "$(cat <<'EOF'
test(kg): incremental fusion idempotency + isolation + rebuild intact

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
EOF
)"
```

---

## 收尾:提 PR
```bash
cd backend && python -m pytest -q
git -C .. fetch origin && git -C .. rebase origin/master
git -C .. push -u origin claude/incremental-kg-fusion
gh pr create --base master --head claude/incremental-kg-fusion \
  --title "feat(kg): incremental KG fusion (tiered) + dedicated KG LLM" \
  --body "见 spec/plan。A:上传自动增量融合(Tier1 名种子 append → Tier2 桥接入队 → Tier3 手动全量逃生口),修复新论文未融合;B:KG 构建 LLM 独立 KG_LLM_*(缺省回退主)。在线问答零改。"
```
待真机:对 nb-b37185f4ae 下次上传(或手动跑 incremental_fuse_source / rebuild)验证新源进簇 + PPR 对比跨到。

---

## 自审清单(写计划后已核)
- **Spec 覆盖:** A1 Tier1→T2;A1 Tier2→T3;claim/formula/procedure→T4;Tier3 逃生口(rebuild 不变)→T5 隔离;B→T1。✓
- **类型一致:** `place_new_concepts(new_objects, existing_cluster_map, existing_canon_names, *, seed_fn, id_prefix)`、`append_clusters(notebook_id, rows, object_type)`、`detect_bridge_candidates(...)`、`incremental_fuse_source(notebook_id, source_id)`、`kg_llm_client` 跨任务一致。✓
- **无占位:** 每步真实代码 + 命令 + 预期。两处「实现期核对」(建源 API、`seed_claim` 签名、`decided_pairs` canonical 格式)已显式标注、给了对齐依据。
- **风险:** 增量融合 default-on 可能影响断言 KG/簇计数的既有测试 → T5 Step4 显式复核。
