# 合并审阅队列止血 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让「待确认合并」队列不再永远顶在 ~1000：unsure 落终态 `deferred` 不回流、拒绝/deferred 用稳定 seed 名对去重不复活、并提供后台「全部预审」把整队列一次跑完。

**Architecture:** 三处协同,都在概念合并审阅子系统内。给 `concept_merge_candidates` 加 `seed_a/seed_b` 稳定身份列 + `deferred` 状态；`cluster_seeds` 额外产出 `pending_seeds`(带 seed 对)；`rebuild_unified_kg` 用 seed 键排除 confirmed/rejected/deferred；`review_pending_merges` 把 unsure 落 `deferred`；新增后台 job 分批跑完整队列 + 进度轮询。

**Tech Stack:** Python 3.13 / FastAPI / SQLite(PRAGMA 迁移、JSON1)/ numpy / pytest；前端 Next.js + React + TypeScript;后台任务 = `contextvars.copy_context()` + daemon `threading.Thread`(与既有 KG job 同款)。

## Global Constraints

- **队列不变量**:队列只显示 `status='pending'`;`rebuild_unified_kg` 只 `DELETE ... status='pending'`;`confirmed/rejected/deferred` 三态**存活且不再被重新提出**。
- **稳定去重键**:排除已决定对必须按 **seed 名对**(`seed_a/seed_b`),非按 canonical id;存量行 `seed_a/seed_b=''` 时回退 `strip-"K-"(canonical)`。
- **deferred 语义**:`review_pending_merges` 中一切非"高置信 merge/keep_separate"的判定(含 unsure、低置信)→ `status='deferred'`。
- **fail-open**:后台 job 每批异常不终止整 job;`review_pending_merges` 已 fail-open(LLM 失败→decisions=[]);job 须防"连续无进展"死循环(stall 计数中止)。
- **单飞**:同 notebook 只允许一个 review job `running`。
- **后台线程**:走 LLM 的 job 用 `contextvars.copy_context()` 传播 per-user 模型配置(镜像 [routes.py:616-617](../../../backend/app/api/routes.py))。
- **不改**:相似度阈值 hi=0.94/lo=0.82、`auto_candidates` LLM 兜底逻辑、`pending`(3 元组)现有返回键(新增 `pending_seeds` 而非改形状)。
- 测试从 `backend/` 跑:`cd backend && python -m pytest ...`;`python`=共享 conda base,无 venv。

---

### Task 1: schema 迁移(seed_a/seed_b)+ `decided_seed_pairs`

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`(`_migrate` 加两列;新增 `decided_seed_pairs`)
- Test: `backend/tests/test_merge_seed_pairs.py`

**Interfaces:**
- Produces: `decided_seed_pairs(self, notebook_id: str) -> Dict[frozenset, str]` — 每个已决定对的 `frozenset({seed_a, seed_b}) -> status`,status ∈ {confirmed, rejected, deferred};`seed_a/seed_b` 空则回退 `strip-"K-"(canonical_a/_b)`。

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_merge_seed_pairs.py
"""seed_a/seed_b 迁移 + decided_seed_pairs(稳定键 + 空值回退 + 含 deferred)。"""
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
    monkeypatch.setenv("EMBED_API_KEY", "k")
    monkeypatch.setenv("EMBED_MODEL", "m")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _mk(repo, nb, cid, ca, cb, sa, sb, status):
    with repo._write() as db:
        db.execute(
            "INSERT INTO concept_merge_candidates "
            "(id,notebook_id,canonical_a,canonical_b,seed_a,seed_b,score,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?, '', '')",
            (cid, nb, ca, cb, sa, sb, 0.9, status))


def test_columns_exist_and_decided_seed_pairs(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    # 精确 seed 行(confirmed / rejected / deferred)
    _mk(repo, nb, "m1", "K-x", "K-y", "x", "y", "confirmed")
    _mk(repo, nb, "m2", "K-p", "K-q", "p", "q", "rejected")
    _mk(repo, nb, "m3", "K-u", "K-v", "u", "v", "deferred")
    # pending 不计入
    _mk(repo, nb, "m4", "K-a", "K-b", "a", "b", "pending")
    dsp = repo.decided_seed_pairs(nb)
    assert dsp[frozenset(("x", "y"))] == "confirmed"
    assert dsp[frozenset(("p", "q"))] == "rejected"
    assert dsp[frozenset(("u", "v"))] == "deferred"
    assert frozenset(("a", "b")) not in dsp


def test_decided_seed_pairs_falls_back_to_canonical(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    # 存量行:seed_a/seed_b 为空 → 回退 strip-"K-"
    _mk(repo, nb, "m1", "K-foo", "K-bar", "", "", "rejected")
    dsp = repo.decided_seed_pairs(nb)
    assert dsp[frozenset(("foo", "bar"))] == "rejected"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_merge_seed_pairs.py -v`
Expected: FAIL — `sqlite3.OperationalError: table concept_merge_candidates has no column named seed_a`(迁移未加列)。

- [ ] **Step 3: Write minimal implementation**

在 `_migrate` 里、`concept_merge_candidates` 的 `reviewed_by` 迁移块之后([sqlite_repository.py:812](backend/app/services/sqlite_repository.py) 那三个 `ADD COLUMN` 之后)加:

```python
            if "seed_a" not in cm_cols:
                db.execute("ALTER TABLE concept_merge_candidates ADD COLUMN seed_a TEXT NOT NULL DEFAULT ''")
            if "seed_b" not in cm_cols:
                db.execute("ALTER TABLE concept_merge_candidates ADD COLUMN seed_b TEXT NOT NULL DEFAULT ''")
```

新增方法(放在 `decided_pairs` 之后,约 [sqlite_repository.py:4475](backend/app/services/sqlite_repository.py)):

```python
    def decided_seed_pairs(self, notebook_id: str) -> Dict[frozenset, str]:
        """{frozenset({seed_a, seed_b}): status} for confirmed/rejected/deferred.

        Seed-name keys are STABLE across rebuilds (canonical ids shift when a
        cluster's min-member changes; seed names don't). Legacy rows written
        before the seed_a/seed_b columns existed carry '' → fall back to
        strip-"K-"(canonical), matching the old decided_pairs key derivation."""
        with self._connect() as db:
            rows = db.execute(
                "SELECT canonical_a, canonical_b, seed_a, seed_b, status "
                "FROM concept_merge_candidates WHERE notebook_id=? "
                "AND status IN ('confirmed','rejected','deferred')",
                (notebook_id,),
            ).fetchall()
        def _strip(cid: str) -> str:
            return cid[2:] if cid.startswith("K-") else cid
        out: Dict[frozenset, str] = {}
        for r in rows:
            a = r["seed_a"] or _strip(r["canonical_a"])
            b = r["seed_b"] or _strip(r["canonical_b"])
            out[frozenset((a, b))] = r["status"]
        return out
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_merge_seed_pairs.py -v`
Expected: PASS (2 passed)。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_merge_seed_pairs.py
git commit -m "feat(kg): concept_merge_candidates 加 seed_a/seed_b + decided_seed_pairs(稳定键)"
```

---

### Task 2: `cluster_seeds` 产出 `pending_seeds`(带 seed 对)

**Files:**
- Modify: `backend/app/services/kg_merge.py`（`cluster_seeds` 返回加 `pending_seeds`；`cluster_objects`/`cluster_concepts` 透传）
- Test: `backend/tests/test_kg_merge.py`（新增用例）

**Interfaces:**
- Produces: `cluster_seeds(...)` 返回 dict 新增键 `"pending_seeds": List[Tuple[str,str,str,str,float]]`，元素 `(seed_a, seed_b, canon_a, canon_b, sim)`，与 `pending`（`(canon_a,canon_b,sim)`）同对同序同 cap。`cluster_objects`/`cluster_concepts` 透传该键。

- [ ] **Step 1: Write the failing test**

追加到 `backend/tests/test_kg_merge.py` 末尾:

```python
def test_cluster_seeds_emits_pending_seeds():
    import numpy as np
    from app.services.kg_merge import cluster_seeds
    # 两个高相似但 <hi 的 seed → 一条 pending
    seeds = ["deepseek v2", "deepseek v2 series", "unrelated topic"]
    v = {"deepseek v2": np.array([1.0, 0.0], dtype=np.float32),
         "deepseek v2 series": np.array([0.96, 0.28], dtype=np.float32),
         "unrelated topic": np.array([0.0, 1.0], dtype=np.float32)}
    mc = {s: 1 for s in seeds}
    sfn = {s: s for s in seeds}
    res = cluster_seeds(seeds, v, mc, sfn, set(), set(), hi=0.999, lo=0.5)
    assert "pending_seeds" in res
    # pending_seeds 与 pending 一一对应(同对同序)
    assert len(res["pending_seeds"]) == len(res["pending"])
    for (sa, sb, ca, cb, sim), (pa, pb, psim) in zip(res["pending_seeds"], res["pending"]):
        assert (ca, cb, sim) == (pa, pb, psim)
        # seed 名对能还原(strip-"K-" == seed)
        assert {ca[2:], cb[2:]} == {sa, sb}
    # 至少命中那条版本变体对
    assert any({sa, sb} == {"deepseek v2", "deepseek v2 series"}
               for sa, sb, *_ in res["pending_seeds"])
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_kg_merge.py::test_cluster_seeds_emits_pending_seeds -v`
Expected: FAIL — `KeyError: 'pending_seeds'`。

- [ ] **Step 3: Write minimal implementation**

在 `cluster_seeds`（[kg_merge.py:331-336](backend/app/services/kg_merge.py)）把 `pending`/return 改为同时算 `pending_seeds`:

```python
    pending = [(canon_id[a], canon_id[b], sim) for a, b, sim in cand
               if sim < hi and canon_id[a] != canon_id[b]]
    pending.sort(key=lambda t: t[2], reverse=True)
    pending_seeds = [(a, b, canon_id[a], canon_id[b], sim) for a, b, sim in cand
                     if sim < hi and canon_id[a] != canon_id[b]]
    pending_seeds.sort(key=lambda t: t[4], reverse=True)
    was_capped = len(pending) > max_pending
    return {"seed_to_canonical": canon_id, "canonical_names": canon_name,
            "auto_candidates": auto_candidates, "pending": pending[:max_pending],
            "pending_seeds": pending_seeds[:max_pending], "capped": was_capped}
```

在 `cluster_objects` 的 return（[kg_merge.py:391](backend/app/services/kg_merge.py)）透传:

```python
            "auto_candidates": sd["auto_candidates"], "pending": sd["pending"],
            "pending_seeds": sd["pending_seeds"], "capped": sd["capped"]}
```

`cluster_concepts` 委托 `cluster_objects`（同文件),其 return 若也显式列键则同样加 `"pending_seeds": sd["pending_seeds"]`;若直接返回 `sd` 则无需改（实现时按现状核对）。

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_kg_merge.py -q`
Expected: PASS（新用例 + 既有全绿；既有断言仍用 3 元组 `pending`,不受影响）。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/kg_merge.py backend/tests/test_kg_merge.py
git commit -m "feat(kg): cluster_seeds 增 pending_seeds(带 seed 对,供稳定去重)"
```

---

### Task 3: rebuild 接线 —— seed 键排除 + 写 seed_a/seed_b

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`（`rebuild_unified_kg` concept 段的 decided 构造 + pending 插入）
- Test: `backend/tests/test_merge_rebuild_exclude.py`

**Interfaces:**
- Consumes: `decided_seed_pairs`（Task 1）、`cluster_seeds` 的 `pending_seeds`（Task 2）。
- Produces: rebuild 后 pending 行带 `seed_a/seed_b`；confirmed/rejected/deferred 对不再出现在 pending。

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_merge_rebuild_exclude.py
"""rebuild 用 seed 键排除 confirmed/rejected/deferred;pending 行写 seed_a/seed_b;
canonical id 漂移后按 seed 键仍排除。"""
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
    monkeypatch.setenv("EMBED_API_KEY", "k")
    monkeypatch.setenv("EMBED_MODEL", "m")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _pending_pairs(repo, nb):
    with repo._connect() as db:
        rows = db.execute(
            "SELECT seed_a, seed_b FROM concept_merge_candidates "
            "WHERE notebook_id=? AND status='pending'", (nb,)).fetchall()
    return {frozenset((r["seed_a"], r["seed_b"])) for r in rows}


def test_rebuild_writes_seed_cols_and_excludes_decided(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    # 一批相似概念名,rebuild 会产生 pending 候选
    kg = [{"local_id": f"c{i}", "object_type": "concept",
           "payload": {"name": n, "section_path": ""}, "evidence": []}
          for i, n in enumerate(["gpt four", "gpt four turbo", "llama three", "llama three point one"])]
    repo.store_kg(nb, None, kg, [])
    repo.rebuild_unified_kg(nb)
    p1 = _pending_pairs(repo, nb)
    assert p1, "应产生若干 pending 候选"
    # pending 行的 seed 列非空
    with repo._connect() as db:
        empties = db.execute(
            "SELECT COUNT(*) c FROM concept_merge_candidates "
            "WHERE notebook_id=? AND status='pending' AND (seed_a='' OR seed_b='')", (nb,)).fetchone()["c"]
    assert empties == 0
    # 取一条 pending 标为 deferred,rebuild 后它不应再回到 pending
    pair = next(iter(p1))
    sa, sb = tuple(pair)
    with repo._write() as db:
        db.execute("UPDATE concept_merge_candidates SET status='deferred' "
                   "WHERE notebook_id=? AND status='pending' AND seed_a=? AND seed_b=?",
                   (nb, sa, sb))
    repo.rebuild_unified_kg(nb)
    assert pair not in _pending_pairs(repo, nb)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_merge_rebuild_exclude.py -v`
Expected: FAIL —— rebuild 仍写空 seed 列(`empties>0`)且 deferred 对回流(`pair` 仍在 pending)。

- [ ] **Step 3: Write minimal implementation**

改 rebuild concept 段的 decided 构造（[sqlite_repository.py:5054-5060](backend/app/services/sqlite_repository.py)）为按 seed 键、含 deferred:

```python
        decided = self.decided_seed_pairs(notebook_id)
        confirmed = {p for p, s in decided.items() if s == "confirmed"}
        rejected = {p for p, s in decided.items() if s in ("rejected", "deferred")}
```

（删除原 `def _seed(cid)` + `decided_pairs` 两行的 confirmed/rejected 构造。注意:auto-candidate 兜底那段 [sqlite_repository.py:5089](backend/app/services/sqlite_repository.py) 内联的 `a[2:] if a.startswith("K-")` 保持不变——它作用于 canonical 形态的 auto_candidates。)

改 pending 插入（[sqlite_repository.py:5243-5247](backend/app/services/sqlite_repository.py)）用 `pending_seeds`、写 seed 列:

```python
            db.execute("DELETE FROM concept_merge_candidates WHERE notebook_id=? AND status='pending'", (notebook_id,))
            db.executemany(
                "INSERT INTO concept_merge_candidates "
                "(id,notebook_id,canonical_a,canonical_b,seed_a,seed_b,score,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?, 'pending', ?, ?)",
                [(f"mc-{uuid4().hex[:10]}", notebook_id, ca, cb, sa, sb, score, now, now)
                 for sa, sb, ca, cb, score in sd["pending_seeds"]])
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_merge_rebuild_exclude.py -v`
Expected: PASS (1 passed)。

回归:`cd backend && python -m pytest tests/test_unified_kg_repository.py tests/test_kg_merge.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_merge_rebuild_exclude.py
git commit -m "feat(kg): rebuild 按 seed 键排除 confirmed/rejected/deferred + pending 写 seed 列"
```

---

### Task 4: `review_pending_merges` —— unsure → `deferred`

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`（`review_pending_merges` 的 else 分支）
- Test: `backend/tests/test_merge_review_deferred.py`

**Interfaces:**
- Consumes: 无新增。
- Produces: 低置信/unsure 判定 → `status='deferred'`（离开 pending 队列）。

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_merge_review_deferred.py
"""review_pending_merges:unsure/低置信 → deferred(离队),不再是 pending。"""
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
import app.services.sqlite_repository as repomod


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "k")
    monkeypatch.setenv("EMBED_MODEL", "m")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _mk(repo, nb, cid, status="pending"):
    with repo._write() as db:
        db.execute(
            "INSERT INTO concept_merge_candidates "
            "(id,notebook_id,canonical_a,canonical_b,seed_a,seed_b,score,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?, '', '')",
            (cid, nb, "K-x", "K-y", "x", "y", 0.9, status))


def test_unsure_becomes_deferred(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    _mk(repo, nb, "m1")
    # stub 复审:返回 unsure
    monkeypatch.setattr(repomod, "review_merge_candidates" if hasattr(repomod, "review_merge_candidates") else "_noop", lambda *a, **k: None, raising=False)
    import app.services.concept_merge_review as cmr
    monkeypatch.setattr(cmr, "review_merge_candidates",
                        lambda client, pending, **k: [{"candidate_id": "m1", "decision": "unsure",
                                                       "confidence": 0.4, "rationale": "unclear"}])
    summary = repo.review_pending_merges(nb, limit=50)
    assert summary["unsure"] == 1
    with repo._connect() as db:
        row = db.execute("SELECT status FROM concept_merge_candidates WHERE id='m1'").fetchone()
    assert row["status"] == "deferred"
    assert repo.pending_merges(nb) == []
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_merge_review_deferred.py -v`
Expected: FAIL — `assert row["status"] == "deferred"`（当前仍是 `'pending'`）。

- [ ] **Step 3: Write minimal implementation**

改 `review_pending_merges` 的 else 分支（[sqlite_repository.py:4457-4458](backend/app/services/sqlite_repository.py)）:

```python
                else:
                    status = "deferred"
                    unsure += 1
```

（其余不变;confirmed/rejected 分支与 `if confirmed or rejected: _mark_unified_kg_dirty` 保持——deferred 不改图故不触发。）

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_merge_review_deferred.py -v`
Expected: PASS (1 passed)。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_merge_review_deferred.py
git commit -m "feat(kg): 预审 unsure/低置信 → deferred 终态(离开 pending 队列)"
```

---

### Task 5: 后台「全部预审」job + 状态表 + 两端点

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`（`_migrate` 建表；`run_merge_review_job`、`merge_review_job_status`）
- Modify: `backend/app/models/schemas.py`（`MergeReviewJob`）
- Modify: `backend/app/api/routes.py`（两端点）
- Test: `backend/tests/test_merge_review_job.py`

**Interfaces:**
- Consumes: `review_pending_merges`（Task 4）、`pending_merges`。
- Produces:
  - `run_merge_review_job(self, notebook_id: str, *, batch: int = 100) -> dict` — 循环跑到 pending 清空/上限;返回 `{"status","total","done","error"}`;`running` 时二次调用返回 `{"status":"running","already":True}`。
  - `merge_review_job_status(self, notebook_id: str) -> dict` — `{"status","total","done","error"}`（无记录→`status="idle"`）。
  - `POST /notebooks/{id}/unified-kg/merges/review-all` → `{"status": "started"|"running"}`；`GET /notebooks/{id}/unified-kg/merges/review-job` → `MergeReviewJob`。
  - `class MergeReviewJob(BaseModel): status: str; total: int = 0; done: int = 0; error: str = ""`。

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_merge_review_job.py
"""后台全量预审 job:分批清空 pending;进度;fail-open 不死循环;单飞。"""
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
import app.services.concept_merge_review as cmr


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY", "k")
    monkeypatch.setenv("EMBED_MODEL", "m")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _seed_pending(repo, nb, n):
    with repo._write() as db:
        for i in range(n):
            db.execute(
                "INSERT INTO concept_merge_candidates "
                "(id,notebook_id,canonical_a,canonical_b,seed_a,seed_b,score,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?, 'pending', '', '')",
                (f"m{i}", nb, f"K-a{i}", f"K-b{i}", f"a{i}", f"b{i}", 0.9))


def test_job_drains_all_pending(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    _seed_pending(repo, nb, 250)
    # 复审:一律 keep_separate 高置信 → rejected(离队)
    monkeypatch.setattr(cmr, "review_merge_candidates",
                        lambda client, pending, **k: [{"candidate_id": c["id"], "decision": "keep_separate",
                                                       "confidence": 0.95, "rationale": ""} for c in pending])
    res = repo.run_merge_review_job(nb, batch=100)
    assert res["status"] == "done"
    assert res["total"] == 250
    assert repo.pending_merges(nb) == []
    st = repo.merge_review_job_status(nb)
    assert st["status"] == "done" and st["done"] == 250


def test_job_failopen_no_infinite_loop(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    _seed_pending(repo, nb, 30)
    # 复审始终返回空(LLM 失败) → reviewed=0 每批 → stall 中止,不死循环
    monkeypatch.setattr(cmr, "review_merge_candidates", lambda client, pending, **k: [])
    res = repo.run_merge_review_job(nb, batch=10)
    assert res["status"] == "failed"
    # pending 未被清(没有决定),但 job 已中止
    assert len(repo.pending_merges(nb)) == 30


def test_status_idle_when_never_run(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb")).id
    assert repo.merge_review_job_status(nb)["status"] == "idle"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_merge_review_job.py -v`
Expected: FAIL — `AttributeError: 'SQLiteRepository' object has no attribute 'run_merge_review_job'`。

- [ ] **Step 3: Write minimal implementation**

`_migrate` 里、`concept_merge_candidates` 的 CREATE 附近（[sqlite_repository.py:624](backend/app/services/sqlite_repository.py) 那条 CREATE INDEX 之后）加建表:

```python
            db.execute(
                """
                CREATE TABLE IF NOT EXISTS merge_review_jobs (
                  notebook_id TEXT PRIMARY KEY REFERENCES notebooks(id) ON DELETE CASCADE,
                  status TEXT NOT NULL DEFAULT 'idle',
                  total INTEGER NOT NULL DEFAULT 0,
                  done INTEGER NOT NULL DEFAULT 0,
                  started_at TEXT NOT NULL DEFAULT '',
                  updated_at TEXT NOT NULL DEFAULT '',
                  error TEXT NOT NULL DEFAULT ''
                )
                """
            )
```

新增两方法（放在 `review_pending_merges` 之后）:

```python
    def merge_review_job_status(self, notebook_id: str) -> dict:
        with self._connect() as db:
            row = db.execute(
                "SELECT status,total,done,error FROM merge_review_jobs WHERE notebook_id=?",
                (notebook_id,)).fetchone()
        if row is None:
            return {"status": "idle", "total": 0, "done": 0, "error": ""}
        return {"status": row["status"], "total": int(row["total"]),
                "done": int(row["done"]), "error": row["error"]}

    def run_merge_review_job(self, notebook_id: str, *, batch: int = 100) -> dict:
        """Drain the whole pending merge queue in batches (each batch = one
        review_pending_merges call). Single-flight per notebook. Fail-open per
        batch; a batch that reviews 0 (LLM down) counts as a stall — abort after
        2 consecutive stalls so a persistent failure can't loop forever. Since
        Task 4 makes unsure→deferred, every reviewed candidate leaves pending, so
        a healthy run strictly shrinks the queue and terminates."""
        self.get_notebook(notebook_id)
        with self._write() as db:
            row = db.execute("SELECT status FROM merge_review_jobs WHERE notebook_id=?",
                             (notebook_id,)).fetchone()
            if row is not None and row["status"] == "running":
                return {"status": "running", "already": True}
            total = db.execute(
                "SELECT COUNT(*) c FROM concept_merge_candidates "
                "WHERE notebook_id=? AND status='pending'", (notebook_id,)).fetchone()["c"]
            now = _now()
            db.execute(
                """
                INSERT INTO merge_review_jobs (notebook_id,status,total,done,started_at,updated_at,error)
                VALUES (?, 'running', ?, 0, ?, ?, '')
                ON CONFLICT(notebook_id) DO UPDATE SET
                  status='running', total=excluded.total, done=0,
                  started_at=excluded.started_at, updated_at=excluded.updated_at, error=''
                """,
                (notebook_id, total, now, now))
        done, stalls, error, final = 0, 0, "", "done"
        max_batches = (total // max(1, batch)) + 3
        try:
            for _ in range(max_batches):
                if not self.pending_merges(notebook_id):
                    break
                summary = self.review_pending_merges(notebook_id, limit=batch)
                reviewed = int(summary.get("reviewed", 0))
                done += reviewed
                with self._write() as db:
                    db.execute("UPDATE merge_review_jobs SET done=?, updated_at=? WHERE notebook_id=?",
                               (done, _now(), notebook_id))
                if reviewed == 0:
                    stalls += 1
                    if stalls >= 2:
                        error, final = "LLM 预审连续无进展,已中止", "failed"
                        break
                else:
                    stalls = 0
        except Exception as exc:  # noqa: BLE001
            error, final = f"{type(exc).__name__}: {exc}", "failed"
            self.event_log.logger.exception("merge review job failed for %s", notebook_id)
        with self._write() as db:
            db.execute("UPDATE merge_review_jobs SET status=?, error=?, updated_at=? WHERE notebook_id=?",
                       (final, error, _now(), notebook_id))
        return {"status": final, "total": total, "done": done, "error": error}
```

`schemas.py` 加（放在 `UnifiedKgStatus` 附近）:

```python
class MergeReviewJob(BaseModel):
    status: str
    total: int = 0
    done: int = 0
    error: str = ""
```

`routes.py` 加两端点（在 `review_unified_kg_merges` 之后,约 [routes.py:1021](backend/app/api/routes.py);文件已 import `contextvars`、`threading`）:

```python
@router.post("/notebooks/{notebook_id}/unified-kg/merges/review-all", dependencies=[Depends(require_notebook_access)])
def review_all_unified_kg_merges(notebook_id: str) -> dict:
    repo = repository()
    try:
        repo.get_notebook(notebook_id)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
    if repo.merge_review_job_status(notebook_id)["status"] == "running":
        return {"status": "running"}
    ctx = contextvars.copy_context()
    threading.Thread(target=lambda: ctx.run(repo.run_merge_review_job, notebook_id),
                     name=f"mergereview-{notebook_id}", daemon=True).start()
    return {"status": "started"}


@router.get("/notebooks/{notebook_id}/unified-kg/merges/review-job", dependencies=[Depends(require_notebook_access)])
def merge_review_job(notebook_id: str) -> MergeReviewJob:
    try:
        return MergeReviewJob(**repository().merge_review_job_status(notebook_id))
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")
```

并在 `routes.py` 顶部的 schemas import 里加入 `MergeReviewJob`（实现时按现有 import 行补上）。

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_merge_review_job.py -v`
Expected: PASS (3 passed)。

回归:`cd backend && python -m pytest tests/test_unified_kg_api.py -q`
Expected: PASS。

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/app/models/schemas.py backend/app/api/routes.py backend/tests/test_merge_review_job.py
git commit -m "feat(kg): 后台全量预审 job + merge_review_jobs 进度表 + 两端点"
```

---

### Task 6: 前端 —— 「全部预审」钮 + 进度轮询

**Files:**
- Modify: `frontend/app/page.tsx`
- (无独立前端单测;以 tsc + 现有测试 + 视觉验证为准)

**Interfaces:**
- Consumes: `POST /unified-kg/merges/review-all`、`GET /unified-kg/merges/review-job`（Task 5）。

- [ ] **Step 1: 加类型 + API**

`frontend/app/page.tsx`:在 `MergeReviewSummary` 类型附近加:

```tsx
type MergeReviewJob = { status: string; total: number; done: number; error: string };
```

在 `reviewPendingMergesApi`（[page.tsx:648](frontend/app/page.tsx)）附近加:

```tsx
const reviewAllMergesApi = (nb: string) =>
  api<{ status: string }>(`/notebooks/${nb}/unified-kg/merges/review-all`, { method: "POST" });
const fetchMergeReviewJob = (nb: string) =>
  api<MergeReviewJob>(`/notebooks/${nb}/unified-kg/merges/review-job`);
```

- [ ] **Step 2: 加状态 + handler**

在 `kgReviewBusy` state（[page.tsx:1034](frontend/app/page.tsx)）附近加:

```tsx
  const [reviewAllJob, setReviewAllJob] = useState<MergeReviewJob | null>(null);
```

在 `reviewPendingMerges` 函数（[page.tsx:2239](frontend/app/page.tsx)）之后加后台驱动 handler（复用现有 6s 轮询范式）:

```tsx
  async function reviewAllMerges() {
    if (!currentNotebookId) return;
    const nb = currentNotebookId;
    try {
      await reviewAllMergesApi(nb);
      setReviewAllJob({ status: "running", total: pendingMerges.length, done: 0, error: "" });
      const poll = window.setInterval(async () => {
        try {
          const job = await fetchMergeReviewJob(nb);
          setReviewAllJob(job);
          if (job.status !== "running") {
            window.clearInterval(poll);
            const [pend, status] = await Promise.all([
              fetchPendingMerges(nb),
              fetchUnifiedKgStatus(nb),
            ]);
            setPendingMerges(pend);
            setUnifiedKgStatus(status);
            setToast(job.status === "failed"
              ? `全部预审中止:${job.error || "未知错误"}(已处理 ${job.done})`
              : `全部预审完成:已处理 ${job.done} 项`);
          }
        } catch { /* transient; keep polling */ }
      }, 6000);
    } catch (err) { reportError(err); }
  }
```

- [ ] **Step 3: 加按钮 + 进度**

在「待确认合并」标题行（[page.tsx:3998-4000](frontend/app/page.tsx)）的「LLM 预审」按钮旁加「全部预审」+ 进度文案:

```tsx
                <button
                  className="ghost-button"
                  onClick={reviewAllMerges}
                  disabled={!pendingMerges.length || reviewAllJob?.status === "running"}
                >
                  {reviewAllJob?.status === "running"
                    ? `全部预审中… ${reviewAllJob.done}/${reviewAllJob.total}`
                    : "全部预审"}
                </button>
```

（保留原「LLM 预审(50)」按钮不动。按钮对齐现有 `ghost-button`/工具行样式,符合 UI 对齐标准。）

- [ ] **Step 4: tsc + 现有测试**

Run: `cd frontend && npx tsc --noEmit`
Expected: 无错误。

Run: `cd frontend && npm test --silent 2>&1 | tail -5`
Expected: 全绿。

- [ ] **Step 5: 视觉验证**

preview 打开 KG 视图「待确认合并」区,确认「全部预审」钮与「LLM 预审」并排对齐、点击后显示「全部预审中… X/Y」。截图给用户。

- [ ] **Step 6: Commit**

```bash
git add frontend/app/page.tsx
git commit -m "feat(ui): 待确认合并区加「全部预审」钮 + 进度轮询"
```

---

## 收尾

- 后端全量回归:`cd backend && python -m pytest -q`。
- 前端:`cd frontend && npx tsc --noEmit`。
- rebase 到 `origin/master` 线性 → push → `gh pr create --base master`（Rebase and merge）。

## Self-Review 记录

- **Spec 覆盖**:A=Task4;C=Task1(seed 列+decided_seed_pairs)+Task2(pending_seeds)+Task3(rebuild 接线);B=Task5(job+表+端点)+Task6(前端)。数据流各分支(排除集含 deferred、pending 写 seed、fail-open stall、单飞)均有对应测试。无遗漏。
- **类型一致**:`decided_seed_pairs -> Dict[frozenset,str]`、`pending_seeds` 5 元组 `(seed_a,seed_b,canon_a,canon_b,sim)`、`run_merge_review_job/merge_review_job_status` 返回 `{status,total,done,error}`、`MergeReviewJob` 前后端同字段——跨 Task 一致。
- **无占位符**:每步含完整代码 + 确切命令/预期。
- **YAGNI**:`pending`(3 元组)保留不改形状(避免动既有读者/测试),新增 `pending_seeds`;不加 job×rebuild 重锁(fail-open + 影响 0 行兜底)。
