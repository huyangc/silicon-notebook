# Storage Decoupling & Retrieval Port Extraction — Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make the storage/index layer highly cohesive and swappable behind hardened capability-port interfaces — so a future standalone native-Python (accelerated) graph engine can plug in without touching any retrieval/scoring code — while taking the real, cheap latency wins first.

**Architecture:** Three-layer dependency inversion. **App/Domain** (FastAPI + all retrieval scoring/grounding/citation logic, stays Python) depends only on **Capability Ports** (thin Python `Protocol`s carrying documented numeric *invariants*), which are implemented by **Engine** packages (default = today's SQLite-backed repo; future = native accelerated engine). The load-bearing rule: the engine returns *neutral primitives* (candidate rows, `id→raw cosine`, neighbours, enrichment bundles) and **never invents `relevance`** — all `[0,1]` fusion, tau grounding tiers, and `[k]` citations live above the seam.

**Tech Stack:** Python 3 / FastAPI / hand-written `sqlite3` repository (no ORM) / numpy float32 matrices / `OpenAICompatibleClient` / pytest. No new runtime dependency is introduced before Phase 3 (and only behind a default-off flag).

---

## Background & the honest framing (read before executing)

A profiling + red-team analysis (2026-06-09) established two facts that shape this plan:

1. **The all-in-memory vector matrix is NOT the bottleneck.** At current scale (~19.3k element + ~19.9k knowledge vectors, 1024-dim float32) it is ~160 MB resident and the matmul is ~20–40 ms. It was itself the fix for an earlier 1.3 GB Python-list problem. The real per-query cost is the **O(N) Python keyword re-tokenization loop** (`score_knowledge` → `keyword_score`/`bm25_scores` re-tokenize the *same* unchanged docs every query ≈ 500 ms–1 s), and above that, **2–3 LLM round-trips dominate wall-clock (seconds)**. → Decompose for **testability/swappability**, not for latency. Take the token-cache + measurement wins *first*.

2. **Two invariants must be protected at every step** (a regression in either is silent — no crash, wrong answers):
   - **The `[0,1]` / tau calibration.** `_fuse` (`retrieval.py:266-276`) does `semantic = max(0.0, cosine)` as the *only* floor and the *only* `[0,1]` transform; tau tiers (0.18 / 0.35) are calibrated to that scale. Any port/engine that clamps, rescales, or returns a non-cosine "similarity" makes `_fuse` double-transform and silently flips grounding tiers — **the exact bug fixed in commit `0ca8f1a`** (RRF micro-scores in the `[0,1]` slot made everything `inferred`).
   - **The dual-index best-of.** `score_knowledge` (`retrieval.py:300-352`) computes `max(knowledge_sims[obj_id], max(element_sims[ev.element_id] for ev in evidence))`. This requires a **dense `id→cosine` dict** (not top-k) over **two separately-addressable indexes** (`element_embeddings`, `knowledge_embeddings`) that must never be pooled. A top-k-only port silently degrades the long tail to keyword-only.

**Scope note (per writing-plans scope-check):** This spans multiple subsystems. **Phases 0 and 1 are execution-ready and fully detailed below.** **Phases 2–4 are sequenced phase-specs** (concrete steps, files, interfaces, eval gates) to be expanded into their own plan documents when greenlit — Phase 3/4 are explicitly *gated on Phase 0 measurement* and may be deferred or dropped.

### Phase map

| Phase | Effort | What | Gate to start |
|---|---|---|---|
| **P0** Quick wins (no architecture) | M | Fix dead cache-invalidation; token/BM25 cache on the real bottleneck; capture P50/P95 baseline | none — do now |
| **P1** Interface hardening | M | Close all `_connect`/raw-SQL leaks; extract `CacheBackend` | none |
| **P2** Define capability ports; repo implements them in-process | L | `Protocol`s + DTOs + conformance suite; no behavior change, no 2nd engine | P1 done |
| **P3** Pluggable `VectorIndex` 2nd impl (sqlite-vec/hnswlib), default-off | L | Prove swappability behind the seam | P0 data shows vector layer on critical path |
| **P4** Standalone packaged engine (optional, likely DEFER) | XL | Publish ports contract + engine package | A real external consumer exists |

### Files this plan touches

- Modify: `backend/app/services/sqlite_repository.py` (cache invalidation, token-cache wiring, port methods later)
- Modify: `backend/app/services/retrieval.py` (pure pre-tokenized scoring variants)
- Modify: `backend/app/services/vector_cache.py` (reuse for token cache; optional LRU cap)
- Modify: `backend/app/eval/speed.py`, `backend/app/eval/db.py` (close `_connect` leaks)
- Modify: `backend/app/core/llm.py`, `backend/app/core/llm_cache.py` (extract `CacheBackend`)
- Modify: `backend/app/services/repository.py` (Protocol surface — P2)
- Create (P2+): `backend/app/services/ports.py` (capability Protocols + DTOs), `backend/tests/test_port_conformance.py`
- Test: `backend/tests/test_vector_cache_invalidation.py`, `backend/tests/test_keyword_token_cache.py`, `backend/tests/test_cache_backend.py`

---

## Phase 0 — Quick wins & correctness fixes (no architecture)

> Highest leverage, lowest risk, independent of any future engine. Delivers most of the latency value the user actually wants and tells us whether the matrix even matters.

### Task 1: Fix the dead vector-cache invalidation (and close the in-place re-embed staleness gap)

**Files:**
- Modify: `backend/app/services/sqlite_repository.py:2052-2055` (`_invalidate_unified_cache`)
- Test: `backend/tests/test_vector_cache_invalidation.py`

**Context:** `_invalidate_unified_cache` calls `self._vector_cache.invalidate(f"{notebook_id}:knowledge")`, but `_vector_matrix` (`:3039`) stores under `f"{notebook_id}:matrix:{table}"`. The keys never match → the explicit invalidation is a **no-op**, masked only by the `(table, count, max created_at)` version tuple. That version tuple can also miss an **in-place re-embed** (same row count, same-second `created_at`).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_vector_cache_invalidation.py
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository


def _repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    return SQLiteRepository(Settings())


def test_invalidate_clears_matrix_keys_for_both_tables(tmp_path, monkeypatch):
    repo = _repo(tmp_path, monkeypatch)
    nb = "nb-x"
    # Seed both matrix cache keys directly (loader returns an empty dict).
    repo._vector_cache.get(f"{nb}:matrix:knowledge_embeddings", ("knowledge_embeddings", 0, ""), lambda: {})
    repo._vector_cache.get(f"{nb}:matrix:element_embeddings", ("element_embeddings", 0, ""), lambda: {})
    assert f"{nb}:matrix:knowledge_embeddings" in repo._vector_cache._store
    repo._invalidate_unified_cache(nb)
    # Both real matrix keys must be gone (the old code popped a key that never existed).
    assert f"{nb}:matrix:knowledge_embeddings" not in repo._vector_cache._store
    assert f"{nb}:matrix:element_embeddings" not in repo._vector_cache._store
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_vector_cache_invalidation.py -v`
Expected: FAIL — `knowledge_embeddings` key still present (old code invalidated `{nb}:knowledge`).

- [ ] **Step 3: Implement the fix**

```python
# backend/app/services/sqlite_repository.py  (replace _invalidate_unified_cache body)
    def _invalidate_unified_cache(self, notebook_id: str) -> None:
        for key in [k for k in self._unified_cache if k[0] == notebook_id]:
            self._unified_cache.pop(key, None)
        # Matrices are stored under "{nb}:matrix:{table}" (see _vector_matrix). The old
        # "{nb}:knowledge" key never matched (dead no-op). Invalidate BOTH embedding
        # tables so an in-place re-embed (same row count + same-second created_at, i.e.
        # an unchanged version tuple) cannot serve a stale vector.
        for table in ("knowledge_embeddings", "element_embeddings"):
            self._vector_cache.invalidate(f"{notebook_id}:matrix:{table}")
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_vector_cache_invalidation.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_vector_cache_invalidation.py
git commit -m "fix: vector-cache invalidation was a no-op (wrong key); invalidate real matrix keys"
```

### Task 2: Pure pre-tokenized scoring variants (`keyword_score_tokens`)

**Files:**
- Modify: `backend/app/services/retrieval.py:243-254` (`keyword_score`)
- Test: `backend/tests/test_keyword_token_cache.py`

**Context:** Extract the token-set math from `keyword_score` so callers that already hold a tokenized haystack skip re-tokenization. `keyword_score` becomes a thin wrapper (zero behavior change for existing callers).

- [ ] **Step 1: Write the failing test**

```python
# backend/tests/test_keyword_token_cache.py
from app.services.retrieval import keyword_score, keyword_score_tokens, _tokens, _STOPWORDS


def test_keyword_score_tokens_matches_string_version():
    query, text = "cascode output resistance", "the cascode raises output resistance"
    q_tokens = {t for t in _tokens(query) if t not in _STOPWORDS}
    h_tokens = set(_tokens(text))
    assert abs(keyword_score_tokens(q_tokens, h_tokens) - keyword_score(query, text)) < 1e-12


def test_keyword_score_tokens_empty_query_is_zero():
    assert keyword_score_tokens(set(), {"a"}) == 0.0
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_keyword_token_cache.py -v`
Expected: FAIL — `cannot import name 'keyword_score_tokens'`.

- [ ] **Step 3: Implement**

```python
# backend/app/services/retrieval.py  (replace keyword_score)
def keyword_score_tokens(query_tokens: set, haystack_tokens: set) -> float:
    """Fraction of (content) query tokens present in a pre-tokenized haystack (0..1)."""
    if not query_tokens:
        return 0.0
    hits = sum(1 for token in query_tokens if token in haystack_tokens)
    return hits / len(query_tokens)


def keyword_score(query: str, text: str) -> float:
    """Fraction of (content) query tokens present in the text (0..1).

    Stopwords are dropped from the query basis so verbose phrasings aren't diluted.
    Thin wrapper over keyword_score_tokens for callers without a cached token set.
    """
    query_tokens = {t for t in _tokens(query) if t not in _STOPWORDS}
    return keyword_score_tokens(query_tokens, set(_tokens(text)))
```

- [ ] **Step 4: Run test to verify it passes**

Run: `cd backend && python -m pytest tests/test_keyword_token_cache.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add backend/app/services/retrieval.py backend/tests/test_keyword_token_cache.py
git commit -m "refactor: extract keyword_score_tokens (pre-tokenized) for a per-notebook token cache"
```

### Task 3: Per-notebook token-set cache wired into the score path

**Files:**
- Modify: `backend/app/services/retrieval.py` — `score_knowledge` signature (`:279-289`)
- Modify: `backend/app/services/sqlite_repository.py` — `_retrieve_scored` (`:3090-3118`) builds + passes the cache
- Test: `backend/tests/test_keyword_token_cache.py` (extend)

**Context:** `score_knowledge` re-derives `f"{text} {evidence_text}"` and re-tokenizes per object every query. Precompute `{object_id: frozenset(haystack_tokens)}` once per `(notebook_id, version-tuple)` (reuse the `VectorCache` machinery so invalidation is identical to the matrix cache) and pass it in. This is **additive** — when the param is absent, behavior is byte-identical.

- [ ] **Step 1: Write the failing test** (asserts the cached path equals the live path)

```python
# backend/tests/test_keyword_token_cache.py  (append)
from app.services.retrieval import score_knowledge, _payload_text, _tokens
from app.models.schemas import Evidence  # adjust import to the actual Evidence type


def _obj(oid, name):
    return {"id": oid, "payload": {"name": name, "section_path": "1"}, "evidence": []}


def test_score_knowledge_pretokenized_equals_live():
    objs = [_obj("a", "cascode output resistance"), _obj("b", "current mirror")]
    live = score_knowledge("cascode resistance", objs, "claim")
    pre = {o["id"]: frozenset(_tokens(_payload_text(o["payload"]))) for o in objs}
    cached = score_knowledge("cascode resistance", objs, "claim", keyword_token_sets=pre)
    assert [(h.object_id, round(h.relevance, 9)) for h in live] == \
           [(h.object_id, round(h.relevance, 9)) for h in cached]
```

- [ ] **Step 2: Run test to verify it fails**

Run: `cd backend && python -m pytest tests/test_keyword_token_cache.py -k pretokenized_equals_live -v`
Expected: FAIL — `score_knowledge() got an unexpected keyword argument 'keyword_token_sets'`.

- [ ] **Step 3: Implement in `score_knowledge`**

Add the optional parameter and use it when present:

```python
# retrieval.py — add to score_knowledge signature (after w_semantic):
    keyword_token_sets: Optional[Dict[str, "frozenset"]] = None,
```

Inside the per-object loop, replace the keyword line:

```python
        # BEFORE:
        # keyword = keyword_score(query, f"{text} {evidence_text}")
        # AFTER:
        if keyword_token_sets is not None and object_id in keyword_token_sets:
            keyword = keyword_score_tokens(query_basis_tokens, keyword_token_sets[object_id])
        else:
            keyword = keyword_score(query, f"{text} {evidence_text}")
```

Compute `query_basis_tokens = {t for t in _tokens(query) if t not in _STOPWORDS}` **once** before the loop (it is query-constant). Keep the `else` branch so callers that pass nothing are unaffected.

- [ ] **Step 4: Wire the cache in the repo**

In `sqlite_repository.py`, add a helper that memoizes the token sets per `(notebook, table-version)` using the existing `self._vector_cache` pattern, and pass it into the `score_knowledge` calls in `_retrieve_scored`:

```python
    def _keyword_token_sets(self, db, notebook_id, objects):
        # Version-keyed on the SAME (count, max created_at) tuple used for vectors so it
        # auto-invalidates after ingest/merge/delete. Cache value: {object_id: frozenset}.
        from app.services.retrieval import _tokens, _payload_text
        ver = db.execute(
            "SELECT COUNT(*) AS c, COALESCE(MAX(updated_at), '') AS ts "
            "FROM knowledge_objects WHERE notebook_id = ?", (notebook_id,)).fetchone()
        version = ("kwtok", ver["c"], ver["ts"])
        def _load():
            out = {}
            for o in objects:
                ev_text = " ".join(getattr(e, "quoted_span", "") for e in o.get("evidence", []))
                out[o["id"]] = frozenset(_tokens(f"{_payload_text(o['payload'])} {ev_text}"))
            return out
        return self._vector_cache.get(f"{notebook_id}:kwtok", version, _load)
```

Pass `keyword_token_sets=self._keyword_token_sets(db, notebook_id, objects)` into each `score_knowledge(...)` call in `_retrieve_scored`. (Note: the version uses `MAX(updated_at)` on `knowledge_objects` because a payload edit changes the haystack without changing embedding `created_at`. Add `_invalidate_unified_cache` to also drop `f"{nb}:kwtok"`.)

- [ ] **Step 5: Run tests + full retrieval eval to verify equivalence**

Run: `cd backend && python -m pytest tests/test_keyword_token_cache.py tests/test_bm25_rrf.py -v`
Run: `cd backend && python -m pytest tests/ -q` (full suite must stay green)
Expected: PASS; recall@k/MRR unchanged (cached tokens are byte-equivalent to live tokenization).

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/retrieval.py backend/app/services/sqlite_repository.py backend/tests/test_keyword_token_cache.py
git commit -m "perf: per-notebook token-set cache for keyword scoring (the real per-query hot path)"
```

### Task 4: Capture a real per-stage ask P50/P95 baseline

**Files:**
- Read: `backend/app/services/sqlite_repository.py:3189-3192` (`ask_stage` event emission)
- Create: `backend/app/eval/ask_latency.py` (small script aggregating `ask_stage` events)

- [ ] **Step 1:** Confirm `ask_stage` events record `latency_ms` per stage (rewrite-LLM / load_indexes / score / rerank-LLM / answer_llm) in `.local/logs/events.jsonl`.
- [ ] **Step 2:** Write `ask_latency.py` to read `events.jsonl`, group by stage, print P50/P95 over the last N asks (pure stdlib; no new dep).
- [ ] **Step 3:** Run a representative set of asks against a prod-DB copy (use the existing eval `.backup` workflow — never mutate the live DB), then run `ask_latency.py`.
- [ ] **Step 4:** Record the baseline numbers in this plan's "Measured baseline" appendix below. **This decides whether P3 (ANN) is ever justified.**
- [ ] **Step 5: Commit** the script.

```bash
git add backend/app/eval/ask_latency.py
git commit -m "chore(eval): per-stage ask P50/P95 from ask_stage events (baseline for retrieval work)"
```

**P0 exit gate:** full suite green; `retrieval_metrics.py` recall@k/MRR within noise of pre-P0; re-embed-in-place reflected in `topk`; baseline P50/P95 recorded. **Expectation:** the token cache yields a large drop in the `score` stage; LLM stages dominate the remainder — confirm before doing any port work.

---

## Phase 1 — Close SQL / connection leaks behind public API

> Pure interface hardening. Value regardless of any future engine, and a prerequisite for proving the port seam against the existing default.

### Task 5: Route `eval/speed.py` and `eval/db.py` through public methods

**Files:**
- Modify: `backend/app/eval/speed.py:79,98` (raw `repo._connect()` INSERT/DELETE)
- Modify: `backend/app/eval/db.py` (separate `_connect`, duplicated schema knowledge)
- Modify: `backend/app/services/sqlite_repository.py` (+ readonly eval query methods; verify/extend cascading delete)

- [ ] **Step 1:** Add readonly query methods to the repo: `objects_for_eval(notebook_id, object_type)`, `relation_degree_for_eval(notebook_id)`, `source_titles_for_eval(notebook_id)` (SELECT-only). Write a test asserting each returns the same rows `eval/db.py` produced via `_connect`.
- [ ] **Step 2:** Replace `EvalDB`'s `_connect` usage with those methods (or delete `EvalDB`).
- [ ] **Step 3:** Verify `delete_notebook` (`sqlite_repository.py:782`) actually cascades to embeddings/relations/objects/elements (FK `ON DELETE CASCADE`); if not, add `delete_notebook_deep()` and a test asserting **zero rows remain** across all KG tables. Route `speed.py`'s `_cleanup` through it and `_insert_source` through the public upload path.
- [ ] **Step 4:** `grep -rn "_connect(" app/eval app/services | grep -v sqlite_repository.py` → assert zero hits.
- [ ] **Step 5: Commit.**

### Task 6: Extract `CacheBackend` and inject it into the LLM client

**Files:**
- Modify: `backend/app/core/llm_cache.py` (define `CacheBackend` Protocol; `SQLiteCacheBackend`, `NoCacheBackend`)
- Modify: `backend/app/core/llm.py:52-63,119-130,203-207` (consume the injected backend instead of hardcoding `LLMCache`)
- Test: `backend/tests/test_cache_backend.py`

- [ ] **Step 1:** Define `class CacheBackend(Protocol): def get(self, key: str) -> Optional[str]: ...; def put(self, key: str, value: str) -> None: ...`. Make `LLMCache` satisfy it (rename to `SQLiteCacheBackend` or alias). Add `NoCacheBackend` (get→None, put→pass).
- [ ] **Step 2:** Failing test: a `OpenAICompatibleClient` constructed with `NoCacheBackend` never persists; with `SQLiteCacheBackend` get/put round-trips a key.
- [ ] **Step 3:** Add a `cache_backend` parameter to the client; default resolves to `SQLiteCacheBackend` when `llm_cache_enabled` else `NoCacheBackend`. Replace the inline `_get_cache()` sqlite wiring.
- [ ] **Step 4:** Run `tests/test_llm_cache.py` + the new test green. Cache hit/miss semantics identical to before.
- [ ] **Step 5: Commit.**

**P1 exit gate:** full eval suite runs end-to-end through public API with **zero** `repo._connect` outside the repo; LLM cache hit/miss parity test green.

---

## Phase 2 — Define capability ports; repo implements them in-process (phase-spec)

> Carve the retrieval-relevant methods into thin `Protocol`s and have today's `SQLiteRepository` satisfy them **unchanged** (no second engine yet). This is where the conformance suite locks the two invariants down. Expand into its own plan when starting.

**Ports to define** (`backend/app/services/ports.py`): `ObjectStore`, `RelationStore`, `GraphTraversal`, `VectorIndex`, `TextIndex`, `EvidenceEnrichment`, `ClusterStore`, `CommunityStore` (+ existing infra `EmbeddingBackend`, `LLMBackend`, `CacheBackend`).

**Minimal retrieval port operations** (the repo already maps ~1:1):
- `fetch_objects_by_type(nb, type, statuses) -> list[KnowledgeObject]` (full payload+evidence; never a score field) — from `_knowledge_objects`
- `vector_topk(nb, index_name, query_vector, limit=None) -> dict[id: float]` — from `_vector_matrix` + `query_sims`
- `fetch_neighbors(nb, id, edge_type, direction) -> list[KnowledgeObject]` — from `_retrieve_neighbors`
- `fetch_in_network_relations(nb, object_ids) -> list[Relation]` — both endpoints in set
- `fetch_node_enrichment(nb, object_ids[], type) -> dict[id: NodeEnrichment]` — **batched** form of `node_context` (kills the N+1 in the `_answer_context` loop)
- `fetch_cluster_map(nb) -> dict[member_id: canonical_id]` — from `cluster_map`
- `fetch_community_reports(nb, limit) -> list[Report]`

**Critical invariants encoded in the contract (and tested):**
- `vector_topk` returns **raw cosine in [-1, 1]**, unclamped/unrescaled; with `limit=None` it returns a **dense** `id→cosine` dict over the full named index (what `score_knowledge` best-of and `rrf_fuse` rank-completeness require).
- `element_embeddings` and `knowledge_embeddings` are **separate named indexes, never merged**.
- An object present in `fetch_objects_by_type` but absent from the sims dict ⇒ treated as `has_vector=False`, not an error.
- `TextIndex` returns **raw text only** — all CJK-bigram tokenization + BM25 IDF stay in the app (`bm25_scores`), the single source of truth.

**Steps (high level — detail at expansion time):**
1. Extract `retrieval.py` scoring as-is into an app-side `RetrievalService` taking ports as constructor deps (no logic change).
2. Define `Protocol`s + DTOs (`KnowledgeObject`, `Relation`, `NodeEnrichment`, `Report`) carrying the invariants as docstrings.
3. Make `SQLiteRepository` satisfy them; route the three full-scan call sites (`sqlite_repository.py:3102-3103`, `~3221-3224`, `retrieval_metrics.py:39`) through ports.
4. Lift caches behind clear ownership: matrix stays engine-private (only the per-query `id→float` dict crosses the seam); `_unified_cache` returns a copy/frozen structure on hit (add a "mutate-returned-graph doesn't corrupt next fetch" test).
5. Add `backend/tests/test_port_conformance.py`: orthogonal/antiparallel/identical vectors → `~0 / ~-1 / ~1` (catches distance-vs-similarity + rescaling); every `topk` value ∈ [-1,1]; a pure-keyword hit fuses to **exactly 0.4** and `tau_high < 0.4` (locks grounding-tier calibration); absent-id ⇒ `has_vector=False`.

**P2 exit gate:** `retrieval_metrics.py` recall@k/MRR **and** the grounded/overview/inferred distribution identical before/after; conformance suite green on `SQLiteRepository`; no repo internals (`_vector_matrix`, `query_sims`, table names) imported by `RetrievalService`; ask P50/P95 not worse than the P0 baseline.

---

## Phase 3 — Pluggable `VectorIndex` 2nd impl, default-off (phase-spec, GATED)

> Only proceed if the P0 baseline shows the vector layer is actually on the critical path. Proves the seam carries a genuinely different backend without touching app code.

**Steps:**
1. Add `make_engine(settings)` factory / `EngineRegistry` returning the port bundle; default binding = today's SQLite matrix engine.
2. Implement a sqlite-vec **or** hnswlib `VectorIndex` behind the same port, feature-flagged **off**. It MUST: return raw cosine ∈ [-1,1] (convert from inner-product/L2 if needed); keep the two indexes separate; **and provide `topk_for_ids(nb, index_name, query_vector, ids)` (exact gather-by-id)** — because best-of over arbitrary evidence-element ids and dense RRF cannot be served by top-k-only.
3. Equivalence eval gate: run keyword-strong, element-only-strong, and **Chinese-query** classes through both engines; assert recall@k/MRR + tier distribution match within a tight bound; RRF top-n stable. Diverge ⇒ stays off.
4. Do **not** add `topk_multi`/pooled-index convenience (optimizes the confirmed non-bottleneck, risks destroying best-of).
5. Keep `TextIndex` text-out only; forbid any engine pre-filter using a different tokenizer (a CJK-blind pre-filter destroys Chinese recall) — if ever added it must be a recall-only superset using the *identical* tokenizer, validated by a Chinese top-n identity eval.

**Risk:** ANN returns top-k-by-one-metric while this retriever needs exact cosine for arbitrary candidates + gather-by-id over evidence elements; a naive swap silently converts hybrid → keyword-only for the long tail. Mitigated by `topk_for_ids` + equivalence gate + default-off.

---

## Phase 4 — Standalone packaged engine (phase-spec, OPTIONAL / likely DEFER)

> Architecture/distribution play, **not** performance. Only if a concrete external consumer for the engine exists.

**Steps:**
1. Extract `Protocol`s + DTOs + invariants into a tiny `sn-engine-ports` contract package that imports nothing from `retrieval.py`.
2. Package the SQLite engine (and any validated ANN impl) as `sn-graph-engine` depending only on `sn-engine-ports`; enforce with an import-linter rule (engine must not reference `_fuse`/tau/RRF/keyword scoring/`RetrievedKnowledge`/`AskResponse`).
3. Formalize the contracts the cohesion review flagged as implicit: (a) version/invalidation as a published invariant with a conformance test (re-embed-in-place reflects in `topk`); (b) `store_objects`/`add_relations`/`upsert_vectors` as **separate** ops the app sequences — `store_kg` is today 5+ non-atomic transactions across threads, do **not** collapse to one atomic verb; (c) cross-index read-your-writes for the best-of; (d) the unified-KG dirty/rebuild state machine (document actual read-path behavior before formalizing a `KGRebuildCoordinator`); (e) explicit `WriteLockPolicy`/`TransactionPolicy` (no single-source write isolation).
4. **Do NOT build a from-scratch native-Python graph DB.** Pure Python loses to SQLite's C btree for indexed fetches and to numpy/BLAS for matmul, and the keyword bottleneck (CJK tokenizer) is forbidden from moving below the seam. If acceleration is needed, it is the validated sqlite-vec/hnswlib impl from Phase 3 inside this package.
5. Tests continue using the SQLite engine + `FakeEmbedder` + `NoCacheBackend`.

**Risk:** High, speculative ROI. An out-of-process boundary turns today's free in-process DTO sharing into per-ask serialization of tens of thousands of objects. **Recommend DEFER unless a real consumer exists.**

---

## Measured baseline (captured 2026-06-09, P0 Task 4)

Real run: notebook `nb-012fb94249` ("Analog CMOS IC Design", 35.8k objects / 9.1k knowledge embeddings), 30 questions through `repo.ask` with the production LLM + embedding endpoints, on an online-backup copy of the 1.3 GB prod DB. Aggregated from `ask_stage` events via `app/eval/ask_latency.py` (nearest-rank percentile). The **P0 token cache is active** in these numbers.

| Stage | P50 (ms) | P95 (ms) | max (ms) | share of total (P50) |
|---|---|---|---|---|
| load_indexes | 724 | 836 | 6983 | ~7% |
| score | 75 | 165 | 2457 | ~0.7% |
| expand (1-hop) | 2 | 4 | 5 | ~0.02% |
| **answer_llm** | **9302** | **21932** | 22665 | **~91%** |
| **total** | **10244** | **22807** | 23476 | 100% |

**Conclusion — Phase 3 (ANN) is NOT justified on latency grounds.** The LLM answer call is ~91% of wall-clock and dwarfs everything else by ~10×. The entire vector/score layer is ~8% of total, and most of `load_indexes` (724 ms P50) is the query-embedding network round-trip + object load, **not** the matmul — the matmul is buried inside `score`, which with the P0 token cache is only 75 ms P50. The `max` spikes (`load_indexes` 6983 ms, `score` 2457 ms) are the first ask's cold matrix build, paid once and cached. So: the all-in-memory matrix the user worried about is a third-order cost; **decompose the storage layer for testability/swappability (P1→P2), not for latency.** If end-to-end ask latency is the goal, the lever is the answer LLM (model/streaming/output-length), not retrieval.

---

## Self-review checklist (done at authoring)

- **Spec coverage:** matrix-cost answer → P0 framing + Task 3/4; decomposition → P2 ports; cohesion/hardening → P1 + P2 step 4; native-engine target → P4; the two invariants → guarded at every exit gate + P2 conformance suite. ✓
- **Two invariants:** `[0,1]`/tau and dual-index best-of are named in Background and enforced by the P2 conformance tests and every eval gate. ✓
- **No fabricated symbols:** P0/P1 use verified file:line and real signatures (`keyword_score`, `score_knowledge`, `_invalidate_unified_cache`, `_vector_matrix`, `vector_cache.VectorCache`). P2–P4 are intentionally phase-specs (per scope-check) — expand each into its own dated plan before coding. ✓
