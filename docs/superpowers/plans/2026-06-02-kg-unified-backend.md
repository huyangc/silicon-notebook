# Notebook Unified KG (Backend) — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax.

**Goal:** Add a swappable embedding interface, a non-destructive cross-document Concept-merge layer (notebook-level unified KG), and the API that serves it — performant at notebook scale (thousands of nodes).

**Architecture:** An `Embedder` abstraction (local BGE for dev / dashscope `text-embedding-v4` for prod) produces node vectors **in batch** at ingestion. A per-notebook `rebuild` clusters Concepts (normalized-name seed + **vectorized** cosine over cluster *representative* vectors; auto ≥0.90, pending [0.82,0.90)), persisting a non-destructive `concept_clusters` map + `concept_merge_candidates`. The unified graph is **derived** by rewiring edges to canonical ids in one O(E) pass, cached in-process and invalidated on rebuild.

**Tech Stack:** Python/FastAPI, SQLite (`sqlite_repository.py`), numpy (vectorized cosine), `sentence-transformers` (local BGE, optional/lazy), pytest.

**Scope:** Backend only (Phases 1–3 of the spec). The **frontend visualization view** is a separate follow-up plan. Spec: `docs/superpowers/specs/2026-06-02-kg-unified-and-viz-design.md`.

---

## Performance budget & techniques (the user's explicit ask — applied throughout)

| Concern | Technique | Where |
|---|---|---|
| Embedding many nodes | **Batch** `embed_texts` (not per-node loops); dashscope batched requests; fail-fast timeout (`max_retries=0`, reuse the llm.py fix) | Task 2, 4 |
| Local model load cost | **Lazy singleton** load; reuse across calls | Task 3 |
| Clustering O(N²) pairwise | Name-seed buckets first; **vectorized numpy** cosine over **one representative vector per seed-cluster** (not all member pairs); **documented N cap** with a logged fallback | Task 6 |
| Vector load | Load notebook vectors into a single **numpy matrix once**, cache per notebook (invalidate on rebuild/ingest) | Task 7, 8 |
| Edge rewiring | Single **O(E)** pass with a `member→canonical` dict + set-dedup | Task 8 |
| Derived graph reads | **In-process cache** of the derived unified graph per notebook | Task 8 |
| DB access | **Indexes** on new tables; bounded, indexed endpoint queries | Task 5, 11 |
| Regression | A **perf test** asserting clustering of 2000 representative vectors completes < 2s | Task 6 |

Targets: `rebuild` for a notebook with ≤3000 concepts < ~3s (excluding the one-time ingestion embedding); `/unified-kg?level=concept` < 150ms warm (cache hit) / < 500ms cold.

---

## File Structure

**Create**
- `backend/app/services/embedding.py` — `Embedder` protocol, `make_embedder(settings)`, `FakeEmbedder`, `DashscopeEmbedder`, `LocalBGEEmbedder`.
- `backend/app/services/kg_merge.py` — pure clustering core: `cluster_concepts(...)`, `derive_unified_graph(...)` (no DB/IO).
- `backend/tests/test_embedding.py`, `backend/tests/test_kg_merge.py`, `backend/tests/test_unified_kg_repository.py`.

**Modify**
- `backend/app/core/config.py` — add `EMBED_*` settings + `embedder_configured`.
- `backend/app/services/sqlite_repository.py` — `concept_clusters`/`concept_merge_candidates` tables + indexes; `self.embedder`; replace `llm_client.embed` usages; batch node embedding in `store_kg`; `rebuild_unified_kg`, `unified_graph`, pending-merge methods; trigger rebuild after extraction.
- `backend/app/api/routes.py` — unified-KG endpoints.
- `backend/app/models/schemas.py` — response models for unified graph / concept detail / pending merges.

---

## Task 1: `Embedder` protocol + config + `FakeEmbedder` + factory

**Files:**
- Create: `backend/app/services/embedding.py`
- Modify: `backend/app/core/config.py`
- Test: `backend/tests/test_embedding.py`

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_embedding.py
from app.services.embedding import FakeEmbedder, make_embedder
from app.core.config import Settings

def test_fake_embedder_is_deterministic_and_batched():
    e = FakeEmbedder(dim=8)
    v1 = e.embed_query("MOSFET")
    v2 = e.embed_query("MOSFET")
    assert v1 == v2 and len(v1) == 8           # deterministic, right dim
    batch = e.embed_texts(["a", "b", "MOSFET"])
    assert len(batch) == 3 and batch[2] == v1  # batch matches single

def test_factory_defaults_to_fake_when_unconfigured(monkeypatch):
    monkeypatch.delenv("EMBED_PROVIDER", raising=False)
    e = make_embedder(Settings())
    assert e.__class__.__name__ in ("FakeEmbedder", "LocalBGEEmbedder", "DashscopeEmbedder")
    # with no provider set, factory returns a FakeEmbedder (safe default, no network)
    monkeypatch.setenv("EMBED_PROVIDER", "")
    assert make_embedder(Settings()).__class__.__name__ == "FakeEmbedder"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_embedding.py -v`
Expected: FAIL — `ModuleNotFoundError: app.services.embedding`.

- [ ] **Step 3: Add config fields**

In `backend/app/core/config.py`, add near the other `openai_compat_*` fields:

```python
    embed_provider: str = Field("", env="EMBED_PROVIDER")          # ""|fake|local|dashscope
    embed_model: str = Field("", env="EMBED_MODEL")
    embed_base_url: str = Field("", env="EMBED_BASE_URL")
    embed_api_key: str = Field("", env="EMBED_API_KEY")
    embed_dim: int = Field(1024, env="EMBED_DIM")
```

And a property (near `embedding_configured`):

```python
    @property
    def embedder_configured(self) -> bool:
        return self.embed_provider in ("local", "dashscope")
```

- [ ] **Step 4: Implement the protocol, FakeEmbedder, and factory**

```python
# backend/app/services/embedding.py
"""Swappable embedding backends. Dev default: local BGE; prod: dashscope
text-embedding-v4. Tests use FakeEmbedder (deterministic, no network)."""
from __future__ import annotations

import hashlib
import struct
from typing import List, Protocol

from app.core.config import Settings


class Embedder(Protocol):
    dim: int
    def embed_texts(self, texts: List[str]) -> List[List[float]]: ...
    def embed_query(self, text: str) -> List[float]: ...


class FakeEmbedder:
    """Deterministic hash-based vectors for tests — no network, stable."""
    def __init__(self, dim: int = 1024):
        self.dim = dim

    def _vec(self, text: str) -> List[float]:
        out: List[float] = []
        i = 0
        while len(out) < self.dim:
            h = hashlib.sha256(f"{i}:{text}".encode("utf-8")).digest()
            for j in range(0, len(h), 4):
                out.append(struct.unpack("<I", h[j:j + 4])[0] / 2**32)
                if len(out) >= self.dim:
                    break
            i += 1
        return out

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        return [self._vec(t) for t in texts]

    def embed_query(self, text: str) -> List[float]:
        return self._vec(text)


def make_embedder(settings: Settings) -> Embedder:
    provider = (settings.embed_provider or "").strip()
    if provider == "dashscope":
        from app.services.embedding_dashscope import DashscopeEmbedder
        return DashscopeEmbedder(settings)
    if provider == "local":
        from app.services.embedding_local import LocalBGEEmbedder
        return LocalBGEEmbedder(settings)
    return FakeEmbedder(dim=settings.embed_dim)
```

- [ ] **Step 5: Run test to verify it passes**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_embedding.py -v`
Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/embedding.py backend/app/core/config.py backend/tests/test_embedding.py
git commit -m "feat(embed): Embedder protocol + FakeEmbedder + config + factory"
```

---

## Task 2: `DashscopeEmbedder` (batched, fail-fast)

**Files:**
- Create: `backend/app/services/embedding_dashscope.py`
- Test: `backend/tests/test_embedding.py`

- [ ] **Step 1: Write the failing test (mock the OpenAI client)**

```python
# add to backend/tests/test_embedding.py
def test_dashscope_embedder_batches_and_no_retries(monkeypatch):
    import app.services.embedding_dashscope as mod
    captured = {}
    class _Emb:
        def create(self, model, input):
            captured["input"] = input
            data = [type("D", (), {"embedding": [0.1, 0.2]})() for _ in input]
            return type("R", (), {"data": data})()
    class _Client:
        embeddings = _Emb()
    def fake_openai(**kw):
        captured["kwargs"] = kw
        return _Client()
    monkeypatch.setattr(mod, "OpenAI", fake_openai)
    monkeypatch.setenv("EMBED_PROVIDER", "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL", "https://x"); monkeypatch.setenv("EMBED_API_KEY", "k")
    monkeypatch.setenv("EMBED_MODEL", "text-embedding-v4")
    from app.core.config import Settings
    e = mod.DashscopeEmbedder(Settings())
    out = e.embed_texts(["a", "b", "c"])
    assert len(out) == 3 and captured["input"] == ["a", "b", "c"]   # ONE batched call
    assert captured["kwargs"].get("max_retries") == 0               # fail-fast
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_embedding.py::test_dashscope_embedder_batches_and_no_retries -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# backend/app/services/embedding_dashscope.py
"""dashscope (or any OpenAI-compatible) embeddings — batched + fail-fast."""
from __future__ import annotations
from typing import List
from openai import OpenAI
from app.core.config import Settings

_BATCH = 25  # dashscope text-embedding caps batch size; keep requests small


class DashscopeEmbedder:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.dim = settings.embed_dim
        self.model = settings.embed_model
        self._client = None

    def _ensure(self):
        if self._client is None:
            self._client = OpenAI(
                api_key=self.settings.embed_api_key,
                base_url=self.settings.embed_base_url,
                timeout=self.settings.openai_compat_timeout_seconds,
                max_retries=0,  # don't amplify a network stall (see llm.py fix)
            )
        return self._client

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        out: List[List[float]] = []
        for i in range(0, len(texts), _BATCH):
            chunk = [t[:2000] for t in texts[i:i + _BATCH]]
            resp = self._ensure().embeddings.create(model=self.model, input=chunk)
            out.extend(list(d.embedding) for d in resp.data)
        return out

    def embed_query(self, text: str) -> List[float]:
        return self.embed_texts([text])[0]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_embedding.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/embedding_dashscope.py backend/tests/test_embedding.py
git commit -m "feat(embed): DashscopeEmbedder (batched, fail-fast)"
```

---

## Task 3: `LocalBGEEmbedder` (lazy singleton)

**Files:**
- Create: `backend/app/services/embedding_local.py`
- Test: `backend/tests/test_embedding.py`

> The real model needs `sentence-transformers` + a model download (no network in CI/subagents). Test only the lazy-load wiring with a monkeypatched loader; the real model runs in the main-session smoke (Task 13).

- [ ] **Step 1: Write the failing test**

```python
# add to backend/tests/test_embedding.py
def test_local_bge_lazy_loads_once(monkeypatch):
    import app.services.embedding_local as mod
    loads = {"n": 0}
    class _Model:
        def encode(self, texts, **kw):
            return [[0.0] * 4 for _ in texts]
    def fake_loader(name):
        loads["n"] += 1
        return _Model()
    monkeypatch.setattr(mod, "_load_model", fake_loader)
    monkeypatch.setenv("EMBED_MODEL", "BAAI/bge-m3"); monkeypatch.setenv("EMBED_DIM", "4")
    from app.core.config import Settings
    e = mod.LocalBGEEmbedder(Settings())
    e.embed_query("a"); e.embed_texts(["b", "c"])
    assert loads["n"] == 1  # model loaded once, reused
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_embedding.py::test_local_bge_lazy_loads_once -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement**

```python
# backend/app/services/embedding_local.py
"""Local BGE via sentence-transformers — lazy singleton load, batch encode."""
from __future__ import annotations
from typing import List
from app.core.config import Settings


def _load_model(name: str):
    from sentence_transformers import SentenceTransformer  # heavy import, deferred
    return SentenceTransformer(name)


class LocalBGEEmbedder:
    _model = None  # process-wide singleton (model load is expensive)

    def __init__(self, settings: Settings):
        self.dim = settings.embed_dim
        self.model_name = settings.embed_model or "BAAI/bge-m3"

    def _model_or_load(self):
        if LocalBGEEmbedder._model is None:
            LocalBGEEmbedder._model = _load_model(self.model_name)
        return LocalBGEEmbedder._model

    def embed_texts(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        vecs = self._model_or_load().encode(
            [t[:2000] for t in texts], normalize_embeddings=True, batch_size=64
        )
        return [list(map(float, v)) for v in vecs]

    def embed_query(self, text: str) -> List[float]:
        return self.embed_texts([text])[0]
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_embedding.py -v`
Expected: PASS (3+ tests).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/embedding_local.py backend/tests/test_embedding.py
git commit -m "feat(embed): LocalBGEEmbedder (lazy singleton, batch encode)"
```

---

## Task 4: Wire `Embedder` into the repo + batch node embedding in `store_kg`

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`
- Test: `backend/tests/test_unified_kg_repository.py` (Create)

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_unified_kg_repository.py
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
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)   # inject deterministic embedder
    return r

def test_store_kg_batch_embeds_nodes(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    objs = [
        {"local_id": "C1", "object_type": "concept",
         "payload": {"name": "MOSFET", "section_path": "1"}, "evidence": []},
        {"local_id": "C2", "object_type": "concept",
         "payload": {"name": "current mirror", "section_path": "1"}, "evidence": []},
    ]
    repo.store_kg(nb.id, None, objs, [])
    with repo._connect() as db:
        rows = db.execute("SELECT object_id, vector FROM knowledge_embeddings WHERE notebook_id=?", (nb.id,)).fetchall()
    assert len(rows) == 2                      # both nodes embedded
    assert len(json.loads(rows[0]["vector"])) == 16
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_unified_kg_repository.py::test_store_kg_batch_embeds_nodes -v`
Expected: FAIL — `store_kg` currently embeds one-by-one via `llm_client.embed` (needs `embedding_configured`, which is false), so no rows written.

- [ ] **Step 3: Implement**

In `sqlite_repository.py`:
1. In `__init__`, after `self.llm_client = ...`, add:
   ```python
   from app.services.embedding import make_embedder
   self.embedder = make_embedder(self.settings)
   ```
2. Add a batch helper:
   ```python
   def _embed_objects_batch(self, notebook_id: str, items: List[dict]) -> None:
       """Batch-embed object payloads (name first) into knowledge_embeddings."""
       texts, ids = [], []
       for it in items:
           name = (it["payload"].get("name") or "").strip()
           if not name:
               continue
           ids.append(it["_oid"]); texts.append(name[:2000])
       if not texts:
           return
       try:
           vectors = self.embedder.embed_texts(texts)
       except Exception:
           return  # embedding best-effort; never block ingestion
       now = _now()
       with self._connect() as db:
           for oid, vec in zip(ids, vectors):
               db.execute(
                   "INSERT OR REPLACE INTO knowledge_embeddings (object_id, notebook_id, vector, created_at) VALUES (?,?,?,?)",
                   (oid, notebook_id, json.dumps(vec), now))
   ```
3. In `store_kg`, where it assigns `oid` per object, record `obj["_oid"] = oid`. Replace the per-object `self._embed_knowledge(...)` loop at the end with a single batch call:
   ```python
   self._embed_objects_batch(notebook_id, objects)
   ```
   (Delete the old per-object embed loop. `objects` dicts now carry `_oid`.)
4. Update `_embed_query` to use the embedder: `return self.embedder.embed_query(query[:2000])` and gate on `self.settings.embedder_configured or isinstance(self.embedder, FakeEmbedder)` — simplest: try/except returning None. Leave `_embed_source`/`_knowledge_vectors` reading as-is (they read `knowledge_embeddings`).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_unified_kg_repository.py tests/kg tests/test_kg_repository.py -q`
Expected: PASS, no regressions.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_unified_kg_repository.py
git commit -m "feat(embed): wire Embedder into repo; batch node embedding in store_kg"
```

---

## Task 5: `concept_clusters` + `concept_merge_candidates` tables + indexes + CRUD

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`
- Test: `backend/tests/test_unified_kg_repository.py`

- [ ] **Step 1: Write the failing test**

```python
# add to backend/tests/test_unified_kg_repository.py
def test_cluster_and_candidate_crud(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.write_clusters(nb.id, [
        {"canonical_id": "K1", "member_object_id": "o1", "canonical_name": "MOSFET"},
        {"canonical_id": "K1", "member_object_id": "o2", "canonical_name": "MOSFET"},
    ])
    assert repo.cluster_map(nb.id) == {"o1": "K1", "o2": "K1"}
    repo.write_merge_candidate(nb.id, "K1", "K2", 0.85)
    pend = repo.pending_merges(nb.id)
    assert len(pend) == 1 and pend[0]["status"] == "pending"
    repo.set_merge_decision(nb.id, pend[0]["id"], "rejected")
    assert repo.pending_merges(nb.id) == []
    assert repo.decided_pairs(nb.id) == {("K1", "K2"): "rejected"}
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_unified_kg_repository.py::test_cluster_and_candidate_crud -v`
Expected: FAIL — methods/tables missing.

- [ ] **Step 3: Implement tables + indexes + CRUD**

Add to the schema-creation block:
```sql
CREATE TABLE IF NOT EXISTS concept_clusters (
  id TEXT PRIMARY KEY,
  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
  canonical_id TEXT NOT NULL,
  member_object_id TEXT NOT NULL,
  canonical_name TEXT NOT NULL,
  created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_clusters_nb ON concept_clusters(notebook_id);
CREATE INDEX IF NOT EXISTS idx_clusters_member ON concept_clusters(member_object_id);
CREATE TABLE IF NOT EXISTS concept_merge_candidates (
  id TEXT PRIMARY KEY,
  notebook_id TEXT NOT NULL REFERENCES notebooks(id) ON DELETE CASCADE,
  canonical_a TEXT NOT NULL, canonical_b TEXT NOT NULL,
  score REAL NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
  created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_candidates_nb_status ON concept_merge_candidates(notebook_id, status);
```

CRUD methods:
```python
def write_clusters(self, notebook_id: str, rows: List[dict]) -> None:
    now = _now()
    with self._connect() as db:
        db.execute("DELETE FROM concept_clusters WHERE notebook_id=?", (notebook_id,))
        for r in rows:
            db.execute(
                "INSERT INTO concept_clusters (id,notebook_id,canonical_id,member_object_id,canonical_name,created_at) VALUES (?,?,?,?,?,?)",
                (f"cc-{uuid4().hex[:10]}", notebook_id, r["canonical_id"], r["member_object_id"], r["canonical_name"], now))

def cluster_map(self, notebook_id: str) -> Dict[str, str]:
    with self._connect() as db:
        rows = db.execute("SELECT member_object_id, canonical_id FROM concept_clusters WHERE notebook_id=?", (notebook_id,)).fetchall()
    return {r["member_object_id"]: r["canonical_id"] for r in rows}

def write_merge_candidate(self, notebook_id: str, a: str, b: str, score: float) -> None:
    now = _now()
    with self._connect() as db:
        db.execute(
            "INSERT INTO concept_merge_candidates (id,notebook_id,canonical_a,canonical_b,score,status,created_at,updated_at) VALUES (?,?,?,?,?, 'pending', ?, ?)",
            (f"mc-{uuid4().hex[:10]}", notebook_id, a, b, score, now, now))

def pending_merges(self, notebook_id: str) -> List[dict]:
    with self._connect() as db:
        rows = db.execute("SELECT * FROM concept_merge_candidates WHERE notebook_id=? AND status='pending'", (notebook_id,)).fetchall()
    return [{"id": r["id"], "canonical_a": r["canonical_a"], "canonical_b": r["canonical_b"], "score": r["score"], "status": r["status"]} for r in rows]

def set_merge_decision(self, notebook_id: str, candidate_id: str, status: str) -> None:
    with self._connect() as db:
        db.execute("UPDATE concept_merge_candidates SET status=?, updated_at=? WHERE id=? AND notebook_id=?", (status, _now(), candidate_id, notebook_id))

def decided_pairs(self, notebook_id: str) -> Dict[tuple, str]:
    with self._connect() as db:
        rows = db.execute("SELECT canonical_a, canonical_b, status FROM concept_merge_candidates WHERE notebook_id=? AND status IN ('confirmed','rejected')", (notebook_id,)).fetchall()
    return {(r["canonical_a"], r["canonical_b"]): r["status"] for r in rows}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_unified_kg_repository.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_unified_kg_repository.py
git commit -m "feat(kg): concept_clusters + merge_candidates tables + CRUD"
```

---

## Task 6: Clustering core `cluster_concepts` (pure, vectorized) + perf test

**Files:**
- Create: `backend/app/services/kg_merge.py`
- Test: `backend/tests/test_kg_merge.py`

- [ ] **Step 1: Write the failing tests (correctness + perf)**

```python
# backend/tests/test_kg_merge.py
import time
from app.services.kg_merge import cluster_concepts, _norm

def _concept(oid, name): return {"object_id": oid, "name": name}

def test_name_seed_auto_merge():
    concepts = [_concept("o1", "MOSFET"), _concept("o2", "mosfet "), _concept("o3", "BJT")]
    vecs = {"o1": [1.0, 0], "o2": [1.0, 0], "o3": [0, 1.0]}
    res = cluster_concepts(concepts, vecs, confirmed=set(), rejected=set(), hi=0.9, lo=0.82)
    cmap = res["cluster_map"]
    assert cmap["o1"] == cmap["o2"] and cmap["o1"] != cmap["o3"]   # normalized-name merge

def test_vector_threshold_and_pending():
    concepts = [_concept("o1", "current mirror"), _concept("o2", "current-mirror circuit"), _concept("o3", "slew rate")]
    vecs = {"o1": [1.0, 0.0], "o2": [0.97, 0.24], "o3": [0.0, 1.0]}  # o1·o2 high, o3 orthogonal
    res = cluster_concepts(concepts, vecs, confirmed=set(), rejected=set(), hi=0.9, lo=0.82)
    assert res["cluster_map"]["o1"] == res["cluster_map"]["o2"]      # >=0.9 auto-merge
    assert all(res["cluster_map"]["o3"] != res["cluster_map"][o] for o in ("o1", "o2"))

def test_rejected_pair_not_merged_confirmed_forced():
    concepts = [_concept("o1", "A"), _concept("o2", "B")]
    vecs = {"o1": [1.0, 0.0], "o2": [0.99, 0.14]}  # would auto-merge by vector
    # rejected by canonical names that map to seed clusters "A"/"B" -> identify by name seeds:
    res_r = cluster_concepts(concepts, vecs, confirmed=set(), rejected={frozenset(("A", "B"))}, hi=0.9, lo=0.82)
    assert res_r["cluster_map"]["o1"] != res_r["cluster_map"]["o2"]  # blocked
    res_c = cluster_concepts([_concept("o1","A"), _concept("o2","B")], {"o1":[1.0,0.0],"o2":[0.0,1.0]},
                             confirmed={frozenset(("A","B"))}, rejected=set(), hi=0.9, lo=0.82)
    assert res_c["cluster_map"]["o1"] == res_c["cluster_map"]["o2"]  # forced union

def test_perf_2000_reps_under_2s():
    concepts = [_concept(f"o{i}", f"concept {i}") for i in range(2000)]
    vecs = {f"o{i}": [float((i % 7) == k) for k in range(8)] for i in range(2000)}
    t = time.perf_counter()
    cluster_concepts(concepts, vecs, confirmed=set(), rejected=set(), hi=0.9, lo=0.82)
    assert time.perf_counter() - t < 2.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_kg_merge.py -v`
Expected: FAIL — module missing.

- [ ] **Step 3: Implement (vectorized, representative-vector clustering)**

```python
# backend/app/services/kg_merge.py
"""Pure cross-document Concept clustering. No DB/IO. Vectorized cosine over
one representative vector per name-seed cluster (keeps it well under O(N^2) of
members). confirmed pairs force-union; rejected pairs block."""
from __future__ import annotations
import re
from typing import Dict, List, Set, FrozenSet

import numpy as np

_MAX_REPS = 4000  # above this, skip the vector tier (log + name-seed only)

def _norm(name: str) -> str:
    return re.sub(r"[\s\-_]+", " ", (name or "").strip().lower())

class _UF:
    def __init__(self, items): self.p = {x: x for x in items}
    def find(self, x):
        while self.p[x] != x:
            self.p[x] = self.p[self.p[x]]; x = self.p[x]
        return x
    def union(self, a, b): self.p[self.find(a)] = self.find(b)

def cluster_concepts(concepts: List[dict], vectors: Dict[str, List[float]],
                     confirmed: Set[FrozenSet[str]], rejected: Set[FrozenSet[str]],
                     hi: float = 0.90, lo: float = 0.82) -> dict:
    # 1. name-seed clusters: canonical key = normalized name
    seed_of = {c["object_id"]: _norm(c["name"]) for c in concepts}
    seeds = sorted(set(seed_of.values()))
    uf = _UF(seeds)
    # confirmed/rejected are keyed by seed name
    for pair in confirmed:
        a, b = tuple(pair)
        if a in uf.p and b in uf.p:
            uf.union(a, b)
    rej = {frozenset(p) for p in rejected}

    # 2. representative vector per seed (mean of members), vectorized cosine
    members: Dict[str, List[str]] = {}
    for c in concepts:
        members.setdefault(seed_of[c["object_id"]], []).append(c["object_id"])
    pending: List[tuple] = []
    if len(seeds) <= _MAX_REPS:
        reps = []
        for s in seeds:
            vs = [vectors[o] for o in members[s] if o in vectors]
            reps.append(np.mean(np.asarray(vs, dtype=np.float32), axis=0) if vs else None)
        idx = [i for i, r in enumerate(reps) if r is not None]
        if idx:
            M = np.asarray([reps[i] for i in idx], dtype=np.float32)
            M /= (np.linalg.norm(M, axis=1, keepdims=True) + 1e-8)
            sims = M @ M.T  # vectorized all-pairs cosine over reps (BLAS)
            for a in range(len(idx)):
                for b in range(a + 1, len(idx)):
                    sa, sb = seeds[idx[a]], seeds[idx[b]]
                    if frozenset((sa, sb)) in rej:
                        continue
                    s = float(sims[a, b])
                    if s >= hi:
                        uf.union(sa, sb)
                    elif s >= lo:
                        pending.append((sa, sb, s))
    # 3. assign canonical ids; canonical_name = largest member-count seed in the group
    groups: Dict[str, List[str]] = {}
    for s in seeds:
        groups.setdefault(uf.find(s), []).append(s)
    canon_id, canon_name = {}, {}
    for root, grp in groups.items():
        best = max(grp, key=lambda s: len(members[s]))
        cid = f"K-{root}"
        for s in grp:
            canon_id[s] = cid
            # display name: pick a real member name (not the normalized key)
        # representative real name = first concept whose seed == best
        canon_name[cid] = next(c["name"] for c in concepts if seed_of[c["object_id"]] == best)
    cluster_map = {c["object_id"]: canon_id[seed_of[c["object_id"]]] for c in concepts}
    names = {c["object_id"]: canon_name[cluster_map[c["object_id"]]] for c in concepts}
    pend_out = [(canon_id[a], canon_id[b], s) for a, b, s in pending if canon_id[a] != canon_id[b]]
    return {"cluster_map": cluster_map, "canonical_names": names, "pending": pend_out,
            "capped": len(seeds) > _MAX_REPS}
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_kg_merge.py -v`
Expected: PASS (incl. perf test < 2s).

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/kg_merge.py backend/tests/test_kg_merge.py
git commit -m "feat(kg): vectorized cross-doc Concept clustering core + perf test"
```

---

## Task 7: `rebuild_unified_kg` repo method (load vectors once, cache)

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`
- Test: `backend/tests/test_unified_kg_repository.py`

- [ ] **Step 1: Write the failing test**

```python
# add to backend/tests/test_unified_kg_repository.py
def test_rebuild_merges_same_concept_across_sources(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    # two sources each contributing a "MOSFET" concept (same normalized name)
    repo.store_kg(nb.id, None, [{"local_id":"a","object_type":"concept","payload":{"name":"MOSFET","section_path":""},"evidence":[]}], [])
    repo.store_kg(nb.id, None, [{"local_id":"b","object_type":"concept","payload":{"name":"mosfet","section_path":""},"evidence":[]}], [])
    repo.rebuild_unified_kg(nb.id)
    cmap = repo.cluster_map(nb.id)
    assert len(set(cmap.values())) == 1 and len(cmap) == 2   # both MOSFET nodes in one cluster

def test_rebuild_is_idempotent(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [{"local_id":"a","object_type":"concept","payload":{"name":"X","section_path":""},"evidence":[]}], [])
    repo.rebuild_unified_kg(nb.id); first = repo.cluster_map(nb.id)
    repo.rebuild_unified_kg(nb.id); assert repo.cluster_map(nb.id).keys() == first.keys()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_unified_kg_repository.py::test_rebuild_merges_same_concept_across_sources -v`
Expected: FAIL — `rebuild_unified_kg` missing.

- [ ] **Step 3: Implement**

```python
def rebuild_unified_kg(self, notebook_id: str) -> int:
    """Cluster the notebook's Concepts; persist concept_clusters + refresh
    pending candidates (preserving confirmed/rejected). Returns #clusters."""
    from app.services.kg_merge import cluster_concepts, _norm
    with self._connect() as db:
        crows = db.execute(
            "SELECT id, payload FROM knowledge_objects WHERE notebook_id=? AND object_type='concept' AND status!='deprecated'",
            (notebook_id,)).fetchall()
        vrows = db.execute("SELECT object_id, vector FROM knowledge_embeddings WHERE notebook_id=?", (notebook_id,)).fetchall()
    concepts = [{"object_id": r["id"], "name": json.loads(r["payload"] or "{}").get("name", "")} for r in crows]
    vectors = {r["object_id"]: json.loads(r["vector"]) for r in vrows}
    decided = self.decided_pairs(notebook_id)              # {(a,b): status} keyed by prior canonical ids
    # Translate prior decisions to seed-name pairs via current names (best-effort by name).
    confirmed = {frozenset((_norm(a), _norm(b))) for (a, b), s in decided.items() if s == "confirmed"}
    rejected = {frozenset((_norm(a), _norm(b))) for (a, b), s in decided.items() if s == "rejected"}
    res = cluster_concepts(concepts, vectors, confirmed, rejected)
    rows = [{"canonical_id": res["cluster_map"][c["object_id"]], "member_object_id": c["object_id"],
             "canonical_name": res["canonical_names"][c["object_id"]]} for c in concepts]
    self.write_clusters(notebook_id, rows)
    # refresh pending candidates: clear old 'pending', keep confirmed/rejected
    with self._connect() as db:
        db.execute("DELETE FROM concept_merge_candidates WHERE notebook_id=? AND status='pending'", (notebook_id,))
    for a, b, score in res["pending"]:
        self.write_merge_candidate(notebook_id, a, b, score)
    self._invalidate_unified_cache(notebook_id)
    return len(set(res["cluster_map"].values()))
```

Add a tiny cache holder in `__init__`: `self._unified_cache: Dict[str, Any] = {}` and `def _invalidate_unified_cache(self, nb): self._unified_cache.pop(nb, None)`.

> Note on decision keys: this v1 keys confirmed/rejected by normalized canonical *name* (stable across rebuilds). If two different concepts ever share a normalized name this is a known simplification — acceptable for v1; revisit if it bites.

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_unified_kg_repository.py -v`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_unified_kg_repository.py
git commit -m "feat(kg): rebuild_unified_kg (cluster concepts, persist, refresh pending)"
```

---

## Task 8: Derive the unified graph (rewire edges, cached)

**Files:**
- Modify: `backend/app/services/kg_merge.py` (pure derive), `backend/app/services/sqlite_repository.py` (cached read)
- Test: `backend/tests/test_kg_merge.py`, `backend/tests/test_unified_kg_repository.py`

- [ ] **Step 1: Write the failing tests**

```python
# add to backend/tests/test_kg_merge.py
from app.services.kg_merge import derive_unified_graph

def test_derive_rewires_and_dedups_edges():
    cluster_map = {"o1": "K1", "o2": "K1"}   # o1,o2 are the same canonical concept
    nodes = [{"id":"o1","object_type":"concept","payload":{"name":"MOSFET"}},
             {"id":"o2","object_type":"concept","payload":{"name":"mosfet"}},
             {"id":"k1","object_type":"claim","payload":{"name":"claim A"}}]
    edges = [{"source_object_id":"k1","target_object_id":"o1","edge_type":"about"},
             {"source_object_id":"k1","target_object_id":"o2","edge_type":"about"}]
    g = derive_unified_graph(nodes, edges, cluster_map)
    concept_ids = {n["id"] for n in g["nodes"] if n["object_type"]=="concept"}
    assert concept_ids == {"K1"}                       # two MOSFET nodes -> one canonical
    about = [e for e in g["edges"] if e["edge_type"]=="about"]
    assert len(about) == 1 and about[0]["target_object_id"]=="K1"   # rewired + deduped
```

```python
# add to backend/tests/test_unified_kg_repository.py
def test_unified_graph_concept_level_cached(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id":"a","object_type":"concept","payload":{"name":"MOSFET","section_path":""},"evidence":[]},
        {"local_id":"b","object_type":"concept","payload":{"name":"current mirror","section_path":""},"evidence":[]},
    ], [{"source_local_id":"b","target_local_id":"a","edge_type":"depends_on","evidence":[]}])
    repo.rebuild_unified_kg(nb.id)
    g = repo.unified_graph(nb.id, level="concept")
    assert len(g["nodes"]) == 2 and len(g["edges"]) == 1
    assert repo.unified_graph(nb.id, level="concept") is repo._unified_cache[(nb.id,"concept")]  # cache hit
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_kg_merge.py::test_derive_rewires_and_dedups_edges tests/test_unified_kg_repository.py::test_unified_graph_concept_level_cached -v`
Expected: FAIL.

- [ ] **Step 3: Implement `derive_unified_graph` (pure) + `unified_graph` (cached)**

```python
# add to backend/app/services/kg_merge.py
def derive_unified_graph(nodes: List[dict], edges: List[dict], cluster_map: Dict[str, str]) -> dict:
    """Rewire member-Concept endpoints to canonical ids; dedup edges. O(V+E)."""
    def canon(oid): return cluster_map.get(oid, oid)
    seen_concept, out_nodes = set(), []
    for n in nodes:
        if n["object_type"] == "concept":
            cid = canon(n["id"])
            if cid in seen_concept:
                continue
            seen_concept.add(cid)
            out_nodes.append({**n, "id": cid})
        else:
            out_nodes.append(n)
    seen_edge, out_edges = set(), []
    for e in edges:
        s, t = canon(e["source_object_id"]), canon(e["target_object_id"])
        if s == t:
            continue
        key = (s, t, e["edge_type"])
        if key in seen_edge:
            continue
        seen_edge.add(key)
        out_edges.append({"source_object_id": s, "target_object_id": t, "edge_type": e["edge_type"]})
    return {"nodes": out_nodes, "edges": out_edges}
```

```python
# in sqlite_repository.py
def unified_graph(self, notebook_id: str, level: str = "concept") -> dict:
    cached = self._unified_cache.get((notebook_id, level))
    if cached is not None:
        return cached
    from app.services.kg_merge import derive_unified_graph
    with self._connect() as db:
        nrows = db.execute("SELECT id, object_type, payload, status FROM knowledge_objects WHERE notebook_id=? AND status!='deprecated'", (notebook_id,)).fetchall()
    nodes = [{"id": r["id"], "object_type": r["object_type"], "payload": json.loads(r["payload"] or "{}")} for r in nrows]
    edges = [{"source_object_id": r["source_object_id"], "target_object_id": r["target_object_id"], "edge_type": r["edge_type"]}
             for r in self.relations_for_notebook(notebook_id)]
    g = derive_unified_graph(nodes, edges, self.cluster_map(notebook_id))
    if level == "concept":
        cids = {n["id"] for n in g["nodes"] if n["object_type"] == "concept"}
        g = {"nodes": [n for n in g["nodes"] if n["object_type"] == "concept"],
             "edges": [e for e in g["edges"] if e["source_object_id"] in cids and e["target_object_id"] in cids]}
    self._unified_cache[(notebook_id, level)] = g
    return g
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_kg_merge.py tests/test_unified_kg_repository.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/kg_merge.py backend/app/services/sqlite_repository.py backend/tests/test_kg_merge.py backend/tests/test_unified_kg_repository.py
git commit -m "feat(kg): derive unified graph (rewire+dedup edges), cached read"
```

---

## Task 9: Trigger rebuild after extraction + concept detail

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`
- Test: `backend/tests/test_unified_kg_repository.py`

- [ ] **Step 1: Write the failing test**

```python
# add to backend/tests/test_unified_kg_repository.py
def test_concept_detail_lists_members_and_attached(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id":"a","object_type":"concept","payload":{"name":"MOSFET","section_path":""},
         "evidence":[{"source_id":"s","source_title":"D","element_id":"e","element_type":"p","location_label":"1","quoted_span":"MOSFET","confidence":1.0}]},
        {"local_id":"k","object_type":"claim","payload":{"name":"MOSFET has threshold","section_path":""},"evidence":[]},
    ], [{"source_local_id":"k","target_local_id":"a","edge_type":"about","evidence":[]}])
    repo.rebuild_unified_kg(nb.id)
    cid = list(repo.cluster_map(nb.id).values())[0]
    detail = repo.concept_detail(nb.id, cid)
    assert detail["canonical_name"] == "MOSFET"
    assert any(x["object_type"]=="claim" for x in detail["attached"])   # the claim attached via 'about'
    assert detail["evidence"]                                            # member evidence surfaced
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_unified_kg_repository.py::test_concept_detail_lists_members_and_attached -v`
Expected: FAIL — `concept_detail` missing.

- [ ] **Step 3: Implement `concept_detail` + trigger**

```python
def concept_detail(self, notebook_id: str, canonical_id: str) -> dict:
    cmap = self.cluster_map(notebook_id)
    members = [oid for oid, cid in cmap.items() if cid == canonical_id]
    mset = set(members)
    with self._connect() as db:
        rows = db.execute("SELECT id, object_type, payload, evidence FROM knowledge_objects WHERE notebook_id=? AND status!='deprecated'", (notebook_id,)).fetchall()
    by_id = {r["id"]: {"id": r["id"], "object_type": r["object_type"], "payload": json.loads(r["payload"] or "{}"),
                       "evidence": json.loads(r["evidence"] or "[]")} for r in rows}
    name = next((self.cluster_map_name(notebook_id, canonical_id)), "")
    attached = []
    for rel in self.relations_for_notebook(notebook_id):
        s, t = rel["source_object_id"], rel["target_object_id"]
        other = None
        if s in mset and t not in mset: other = t
        elif t in mset and s not in mset: other = s
        if other and other in by_id and by_id[other]["object_type"] != "concept":
            attached.append({**by_id[other], "edge_type": rel["edge_type"]})
    evidence = [ev for oid in members for ev in by_id.get(oid, {}).get("evidence", [])]
    return {"canonical_id": canonical_id, "canonical_name": name,
            "members": [by_id[o] for o in members if o in by_id], "attached": attached, "evidence": evidence}

def cluster_map_name(self, notebook_id: str, canonical_id: str):
    with self._connect() as db:
        row = db.execute("SELECT canonical_name FROM concept_clusters WHERE notebook_id=? AND canonical_id=? LIMIT 1", (notebook_id, canonical_id)).fetchone()
    yield row["canonical_name"] if row else ""
```

In `process_source` (after `_run_extraction(source_id)` succeeds), add:
```python
try:
    self.rebuild_unified_kg(self.get_source(source_id).notebook_id)
except Exception:
    pass  # rebuild best-effort; never fail ingestion
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_unified_kg_repository.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_unified_kg_repository.py
git commit -m "feat(kg): concept_detail + auto-rebuild unified KG after extraction"
```

---

## Task 10: Confirm/reject merge + re-cluster

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`
- Test: `backend/tests/test_unified_kg_repository.py`

- [ ] **Step 1: Write the failing test**

```python
# add to backend/tests/test_unified_kg_repository.py
def test_confirm_merge_unions_clusters_on_rebuild(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    # two distinct-name concepts that won't auto-merge
    repo.store_kg(nb.id, None, [{"local_id":"a","object_type":"concept","payload":{"name":"current mirror","section_path":""},"evidence":[]}], [])
    repo.store_kg(nb.id, None, [{"local_id":"b","object_type":"concept","payload":{"name":"current source","section_path":""},"evidence":[]}], [])
    repo.rebuild_unified_kg(nb.id)
    cmap = repo.cluster_map(nb.id); a_cid, b_cid = (cmap[list(cmap)[0]], cmap[list(cmap)[1]])
    repo.write_merge_candidate(nb.id, a_cid, b_cid, 0.84)
    cand = repo.pending_merges(nb.id)[0]
    repo.confirm_merge(nb.id, cand["id"])
    repo.rebuild_unified_kg(nb.id)
    assert len(set(repo.cluster_map(nb.id).values())) == 1   # forced union held across rebuild
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_unified_kg_repository.py::test_confirm_merge_unions_clusters_on_rebuild -v`
Expected: FAIL — `confirm_merge` missing.

- [ ] **Step 3: Implement**

```python
def confirm_merge(self, notebook_id: str, candidate_id: str) -> None:
    self.set_merge_decision(notebook_id, candidate_id, "confirmed")
    self._invalidate_unified_cache(notebook_id)

def reject_merge(self, notebook_id: str, candidate_id: str) -> None:
    self.set_merge_decision(notebook_id, candidate_id, "rejected")
    self._invalidate_unified_cache(notebook_id)
```

> The forced-union/blocking is already honored by `rebuild_unified_kg` (it reads `decided_pairs` → confirmed/rejected). This test confirms the end-to-end path. The decided pair is keyed by normalized canonical name (see Task 7 note).

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_unified_kg_repository.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_unified_kg_repository.py
git commit -m "feat(kg): confirm/reject merge decisions honored on rebuild"
```

---

## Task 11: API endpoints

**Files:**
- Modify: `backend/app/api/routes.py`, `backend/app/models/schemas.py`
- Test: `backend/tests/test_unified_kg_api.py` (Create — use FastAPI `TestClient`)

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_unified_kg_api.py
from fastapi.testclient import TestClient
import pytest
from app.main import app

@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return TestClient(app)

def test_unified_kg_endpoints(client):
    nb = client.post("/api/notebooks", json={"name": "nb"}).json()["id"]
    # (seed via the repo behind the app or an upload; minimal: rebuild on empty is 200)
    assert client.post(f"/api/notebooks/{nb}/unified-kg/rebuild").status_code == 200
    g = client.get(f"/api/notebooks/{nb}/unified-kg?level=concept")
    assert g.status_code == 200 and "nodes" in g.json() and "edges" in g.json()
    assert client.get(f"/api/notebooks/{nb}/unified-kg/pending-merges").status_code == 200
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_unified_kg_api.py -v`
Expected: FAIL — endpoints 404.

- [ ] **Step 3: Implement endpoints**

In `routes.py` (follow the existing endpoint style; `repo` is the shared repository dependency):
```python
@router.post("/notebooks/{notebook_id}/unified-kg/rebuild")
def rebuild_unified_kg(notebook_id: str):
    n = repo().rebuild_unified_kg(notebook_id)
    return {"clusters": n}

@router.get("/notebooks/{notebook_id}/unified-kg")
def unified_kg(notebook_id: str, level: str = "concept"):
    return repo().unified_graph(notebook_id, level=level)

@router.get("/notebooks/{notebook_id}/concepts/{canonical_id}/detail")
def concept_detail(notebook_id: str, canonical_id: str):
    return repo().concept_detail(notebook_id, canonical_id)

@router.get("/notebooks/{notebook_id}/unified-kg/pending-merges")
def pending_merges(notebook_id: str):
    return repo().pending_merges(notebook_id)

@router.post("/notebooks/{notebook_id}/unified-kg/merges/{candidate_id}/confirm")
def confirm_merge(notebook_id: str, candidate_id: str):
    repo().confirm_merge(notebook_id, candidate_id); return {"ok": True}

@router.post("/notebooks/{notebook_id}/unified-kg/merges/{candidate_id}/reject")
def reject_merge(notebook_id: str, candidate_id: str):
    repo().reject_merge(notebook_id, candidate_id); return {"ok": True}
```
(Match the real `repo` accessor used by the other routes — grep an existing route to see whether it's a module-level `repo()` or a dependency injection, and follow it.)

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/test_unified_kg_api.py -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add backend/app/api/routes.py backend/app/models/schemas.py backend/tests/test_unified_kg_api.py
git commit -m "feat(kg): unified-KG + concept-detail + merge-review API"
```

---

## Task 12: Full suite + import gate

**Files:** none (verification)

- [ ] **Step 1: Run full suite**

Run: `cd backend && PYTHONPATH=. python -m pytest tests/ -q`
Expected: all pass.

- [ ] **Step 2: App import**

Run: `cd backend && PYTHONPATH=. python -c "from app.main import app; print('ok')"`
Expected: `ok`.

- [ ] **Step 3: Commit (if any fixups)**

```bash
git add -A && git commit -m "test(kg): full suite green for unified-KG backend" || echo "nothing to commit"
```

---

## Task 13: Real-embedder smoke + perf baseline (MAIN SESSION ONLY)

> Subagents have no network / can't load BGE. Run in the main session.

- [ ] **Step 1: Configure local BGE in `.env`** (`EMBED_PROVIDER=local`, `EMBED_MODEL=BAAI/bge-m3`, `EMBED_DIM=1024`); ensure `sentence-transformers` installed.

- [ ] **Step 2: Drive a 2-source notebook end-to-end** (reuse `scripts/kg_product_smoke.py` style): ingest two docs sharing concepts → `rebuild_unified_kg` → `unified_graph(level="concept")`. Print: #concepts before vs #canonical after (expect merging), #pending candidates, and **timings** for rebuild + unified_graph.

- [ ] **Step 3: Assert perf budget** — rebuild < ~3s for the corpus; unified_graph warm < 150ms. Record numbers in `fangan_todo.md` under the unified-KG entry.

- [ ] **Step 4: Commit**

```bash
git add fangan_todo.md && git commit -m "chore(kg): unified-KG real-embedder smoke + perf baseline"
```

---

## Self-Review notes (for the implementer)
- **`repo()` accessor** in Task 11 is illustrative — match the real one the existing routes use.
- **Decision keying** (Task 7/10) uses normalized canonical *name*; documented simplification for v1.
- **`_MAX_REPS` cap** (Task 6): above 4000 seed-clusters the vector tier is skipped (name-seed only) and `capped=True` is returned — surface that in logs so silent under-merging is visible.
- **Embedding is best-effort** (Task 4): ingestion never fails on embed errors; nodes without vectors simply don't participate in the vector merge tier (still merge by name).
