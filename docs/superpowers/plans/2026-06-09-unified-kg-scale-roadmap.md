# Unified-KG-at-Scale Roadmap (10k+ academic docs → one KG)

> **For agentic workers:** This is the **scale master plan**. P0 is done; P1/P2 task-level detail lives in the sibling plan `2026-06-09-storage-decouple-retrieval-ports.md`. P2.5–P8 here are **phase-specs** — expand each into its own dated plan (with bite-sized TDD tasks) when greenlit. Use superpowers:subagent-driven-development per phase.

**Goal:** Support **one unified KG over 10k+ (→100k) academic documents, queried globally**, while preserving sentence-level `[k]` citations + tau grounding, cross-document merge/dedup, and community/global QA.

**Architecture:** Decouple storage/index behind capability ports (P1/P2), then move the vector/text/object layer onto **managed Postgres + pgvector + Postgres FTS**, and replace per-query "score-the-world" with **two-stage retrieval** (indexed candidate generation → bounded app-side KG scoring). Cross-doc merge & communities become **batch-recomputed** jobs behind their existing seams. All scoring/grounding/`[k]` stays app-side; the engine returns neutral primitives only.

**Tech stack:** Python/FastAPI · today hand-written `sqlite3` (no ORM, **no Neo4j**) → target managed Postgres+pgvector+FTS · numpy (small/bounded only) · pytest.

---

## 0. Locked requirements (from the user, 2026-06-09)

- **Scale:** start ~1万 docs (~1–3M objects / 2–6M vectors); interfaces & index choices must scale **smoothly to ~10万 docs (~10–30M objects)** without a rewrite. Doc count may exceed 10k.
- **Deployment:** managed/cloud services **allowed** (pgvector / dedicated vector DB / ES-class full-text all on the table).
- **Ingestion:** **batch-primary** with occasional incremental (merge/communities/indexes may be batch-recomputed; incremental may be coarse).
- **Must keep at scale (hard):** (1) `[k]` citations + tau tiers; (2) cross-document unified KG (merge/dedup); (3) community detection + global (GraphRAG) QA.
- **Negotiable:** the fixed 4-type ontology. **Decision: KEEP it** — see §3. Relaxing types is **not** a scale lever.

---

## 1. Named trigger scenario — "Unified KG over 10k+ academic docs"

**The condition:** documents are merged into **one globally-queried KG** (not per-doc shards). This collapses the entire corpus into a single retrieval scope, so every per-notebook operation inherits whole-corpus cost.

**Scale math (band: ~100–500 KG objects per ~10-page paper; anchor: a textbook source ≈ 4k–11k objects):**

| docs | objects | vectors (knowledge+element) | in-RAM float32 matrix (×1024×4B, both indexes) |
|---|---|---|---|
| 10k | ~1–5M (central ~2.5M) | ~2–6M | **~18–30 GB** (low ~6–9 / high ~60+) |
| 30k | ~3–15M | ~6–18M | ~55–90 GB |
| 100k | ~10–30M | ~20–60M+ | ~150–400 GB (impossible in-process) |

**What this triggers (the order things break as scale grows):**
1. **Ingestion/extraction throughput** — 10k docs × multiple LLM windows (+ refine + gleaning) ≈ 10⁵ LLM calls; 100k ≈ 10⁶. This is the **first real wall** (days of wall-clock + cost), and it's a one-time batch — but must be durable/resumable/budgeted (track P7).
2. **In-RAM vector matrix** — ~18–30 GB at 10k is the first thing that **physically can't load** on a normal box (`vector_index.build_matrix`/`query_sims`; built in `ask()` `sqlite_repository.py:3253`). Must move to an external ANN index.
3. **Per-query full-scan** — `score_knowledge` loops every object (`retrieval.py:307`) and `bm25_scores` recomputes **full-corpus IDF per query** (`retrieval.py:377-391`): 75ms@36k → **seconds–tens-of-seconds** at millions. Must bound candidates via an index first.
4. **Per-notebook model** — `notebook_id` was the unit of *isolation*; as one giant KG it becomes the unit of *unbounded cost*. Needs a corpus partition (P3).
5. **Batch merge & communities** — `cluster_objects` (needs real blocking) and `networkx` louvain (won't hold a multi-million-node graph) need scale engines behind their seams (P6).

> **Honest framing:** at every tier the **answer LLM (~9.3s, ~91% of single-ask wall-clock)** still dominates a *single* ask. The scale work buys **feasibility, throughput, and concurrency** — not a faster single ask. Past ~30k objects/query the full-scan *inverts* this (scan overtakes the LLM), which is exactly what two-stage retrieval fixes.

---

## 2. Substrate decision — Postgres + pgvector + Postgres FTS

**One managed Postgres serves multiple ports:** tables for `ObjectStore`/`RelationStore`/`GraphTraversal`, **pgvector** (two separate tables `knowledge_embeddings` / `element_embeddings`, each its own HNSW index) for `VectorIndex`, **pg FTS / pg_trgm** as a **recall-only** candidate generator for `TextIndex`.

**Why pgvector (not a dedicated vector DB) for the 1万→10万 path:**
- It's the only option that natively serves **both** ANN top-k (`ORDER BY vec <=> q LIMIT k`, HNSW) **and exact gather-by-id** (`WHERE id = ANY(:ids)`) — and the dual-index best-of *requires* exact gather over arbitrary evidence-element ids.
- `1 - (vec <=> q)` = **raw cosine in [-1,1]**, feeding `_fuse`'s `max(0.0, cosine)` unchanged → **the [0,1]/tau invariant survives**; the engine returns neutral cosine, the app owns all fusion/grounding.
- One store = no cross-store read-your-writes problem (objects+evidence+vectors are read together every ask).
- Scales: comfortable at 1万; toward 10万 use `halfvec`/scalar-quantization + bigger instance **before** reaching for a dedicated DB.

**Escalation triggers (do NOT pre-build):** swap pgvector → Qdrant/Milvus **only if** a measured 10万 profile shows ANN build/query (not the LLM round-trips) is the bottleneck. Add ES/Tantivy **only if** app-side BM25 over a pg-bounded candidate set becomes the bottleneck — and even then only as a **rank producer into `rrf_fuse`**, never into the calibrated [0,1] slot.

**CJK tokenizer stays app-side** (the single source of truth, `retrieval.py:161-197`). pg FTS / pg_trgm returns a **recall superset of candidate ids only**; `bm25_scores` re-scores app-side. The pre-filter must be eval-gated on a **Chinese top-n identity test** so it never drops CJK hits.

---

## 3. Invariants, must-keeps, and the typed-ontology decision

**Two invariants (CI-guarded at every phase, via `test_port_conformance`):**
1. **[0,1]/tau calibration:** `VectorIndex` returns RAW cosine, unclamped/unrebranded; the engine never invents `relevance` (the `0ca8f1a` regression class).
2. **Dual-index best-of:** DENSE id→cosine over **two separate, never-merged** indexes + **exact gather-by-id** for arbitrary evidence-element ids.

**Must-keeps, all preserved in the design:** `[k]`+tau (app-side scoring over the bounded candidate set); cross-doc merge (batch, P6); communities+global (batch + hierarchical map-reduce, P6).

**Typed ontology — DECISION: KEEP.** Relaxing the 4 types is **not** a throughput lever: retrieval cost is driven by object/vector count and the per-query scan + LLM, none of which is a function of type count (types are a tiny categorical column + a fixed per-type weight table, `retrieval.py:65-79`). Element-level evidence + grounding are orthogonal to type count. Keep types for clarity; optionally **extend** later only if an extraction-quality eval shows a real gap. **Never flatten "for scale," and never bundle a type change into a scale refactor.**

---

## 4. Revised phase map (what changed vs the original P0–P4)

| Phase | Effort | Role change from original |
|---|---|---|
| **P0** Quick wins + baseline | done | unchanged (done; baseline confirmed LLM-bound at small scale) |
| **P1** Interface hardening | M | **promoted**: now the *precondition* for swapping substrate (+ folds in the RRF-relevance & few present-day fixes) |
| **P2** Capability ports | L | **promoted**: from "testability nicety" to **load-bearing substrate seam** |
| **P2.5** Scale eval harness + ground truth | L | **NEW**: every later mitigation needs a scale-correct recall/grounding eval *first* |
| **P3** Corpus model migration (`corpus_id` + sticky canonical ids) | L | **NEW**: per-notebook scope → durable corpus partition; no "all objects of a corpus" path |
| **P4** Managed substrate adoption (Postgres+pgvector+FTS), shadow+parity | XL | **NEW** (was "optional default-off ANN"); now required, gated on P2.5 parity |
| **P5** Two-stage retrieval | L | **NEW**: index-backed candidate gen → bounded rescoring; retires the full-scan wall |
| **P6** Batch merge & community at scale | XL | **NEW**: real blocking for entity resolution; Leiden/igraph behind the community seam; hierarchical global |
| **P7** Ingestion scaling & cost budget (parallel track) | L | **NEW**: durable resumable budgeted extraction; turn ON the C8 LLM cache; remove single-writer at ingest |
| **P8** Escalation (gated on measured 100k profile) / packaged engine | L | dedicated vector DB / ES only if measured; packaged engine still DEFER |

---

## 5. Phase details

### P0 — Quick wins & per-stage baseline — **DONE** (PR #24)
Cache-invalidation fix, `keyword_score_tokens` + token cache, `ask_latency` baseline. Baseline: `answer_llm` ~91% of single-ask wall-clock; matrix not the bottleneck *at small scale*.

### P1 — Interface hardening (precondition for substrate swap) — `M`
Detail in sibling doc. Scope here: close every `repo._connect`/raw-SQL leak (`eval/speed.py`, `eval/db.py`) behind public readonly methods; extract `CacheBackend` (inject into the LLM client). **Fold in present-day bug-fixes** flagged by review: the **RRF path `_rrf_scored` computes `relevance` from knowledge sims only, never `element_sims`** (`sqlite_repository.py:~3595`) — fix so the RRF path's relevance also honors the element best-of (it currently half-breaks the dual-index invariant). **Gate:** full eval through public API, zero `_connect` outside the repo; grounded/overview/inferred distribution unchanged.

### P2 — Capability ports (load-bearing substrate seam) — `L`
Detail in sibling doc. Define `ObjectStore/RelationStore/GraphTraversal/VectorIndex/TextIndex/EvidenceEnrichment/ClusterStore/CommunityStore` Protocols + DTOs with numeric invariants in docstrings. **`VectorIndex` exposes BOTH `vector_topk` AND `topk_for_ids` (exact gather)**; two named never-merged indexes; `TextIndex.candidates` returns ids-only. SQLiteRepository satisfies them unchanged. **Gate:** recall@k/MRR + tier distribution identical before/after; conformance suite green; no repo internals imported by the retrieval service.

### P2.5 — Scale eval harness + ground truth (prerequisite gate for ALL scale work) — `L`
**Why:** every later mitigation (ANN, two-stage, batch merge) is only trustworthy against a scale-correct eval, and today's is not: `run_recall` calls the broken full-scan path and only 1/30 questions carries gold.
**Scope:** (1) make `run_recall` use the **same two-stage candidate path** it measures and report **two numbers** — *ANN-recall-of-gold* (did the candidate set contain gold?) vs *end-to-end recall@k*; (2) build a **sampled-oracle** ground truth: run the exact dense engine on fixed slices as gold, including dedicated **long-tail / CJK / global** question classes; (3) version tau against the corpus.
**Gate:** recall eval runs on the two-stage path, reports ANN-recall vs end-to-end separately, on ≥N gold questions across the hard classes. **Start in parallel with P2; must pass before P4 parity is trusted.**

### P3 — Corpus model migration (`corpus_id` partition + sticky canonical ids) — `L`  · depends: P2
`corpus_id` becomes the durable partition key (logical KG identity); **no code path returns "all objects of a corpus"** — the per-query candidate set is the retrieval scope. Product "notebook" becomes a view over a corpus. **Land sticky canonical ids BEFORE any bulk load**: today `canonical_id = id_prefix + min(seed)` (`kg_merge.py:243`) ⇒ a batch re-run reassigns ids and **breaks citations**. Make canonical ids stable across re-runs (content/identity-based, not min-of-membership). **Gate:** a simulated batch re-run on a fixed corpus reassigns **zero** canonical ids for unchanged-membership clusters; sticky-id conformance test passes.

### P4 — Managed substrate adoption (Postgres + pgvector + FTS), shadow + parity — `XL`  · depends: P3 + P2.5
Implement the port bundle on Postgres+pgvector+FTS (see §2 port mapping). `vector_topk` = `ORDER BY vec<=>q LIMIT K` returning `1-(vec<=>q)` raw cosine **unclamped/unrebranded**; `topk_for_ids` = exact gather; `TextIndex` = pg FTS/pg_trgm ids-only recall superset; **persist BM25 df/IDF** (no per-query full-corpus recompute). Provide a **migration**: one-time ETL of the live **SQLite** KG (~1.3 GB today) → Postgres, **shadow-run** (reads compared, writes still to SQLite) before cutover; keep the `.db` as rollback. **Gate:** conformance suite green on the Postgres engine; read-compare parity (recall@k/MRR/tier distribution within an agreed bound) on the P2.5 slices; CJK identity eval passes; migration is resumable with a defined rollback.

### P5 — Two-stage retrieval (index-driven candidates → bounded rescoring) — `L`  · depends: P4
Replace score-the-world with: **Stage 1** vector ANN per index (knowledge + element, generous `K_v` tuned by the P2.5 recall gate) → id→raw cosine; element hits map to owning object_id; `TextIndex` recall superset (ids only); union → bounded candidate set. **Stage 2** hydrate candidates (`WHERE id = ANY(...)`) + run the **existing byte-identical** `score_knowledge`/`_fuse`/tau/best-of/`[k]` over the **bounded** set (with `topk_for_ids` to recover exact cosine for evidence elements not in the ANN top-K — the long-tail guard). **This retires breakage #2/#3.** **Gate (red-team must-address):** P2.5 sampled-oracle shows ANN-recall-of-gold AND end-to-end recall@k within an agreed bound vs the exact engine on the same slice — *specifically* validating the **dual-index best-of long tail** (objects that win only on a mid-ranked evidence-element vector); re-run each time `K_v`/`ef_search` changes.

### P6 — Batch merge & community at scale — `XL`  · depends: P3 + P4
**Entity resolution:** implement real **blocking** (normalized seed-key / bigram-band LSH) so ANN candidate generation runs **within bounded blocks**, not one global million-seed `hnswlib` build (today `rebuild_unified_kg` loads ALL vectors). Keep the 3-tier shape (exact-key blocking → ANN candidates → guarded canonicalization). Address **transitive-reject** and **giant-cluster (hub concept)** blowups. **Communities:** swap `networkx` louvain → **Leiden (igraph/leidenalg)** behind the `rebuild_communities` seam; **hierarchical levels**; cap giant-community LLM context. **Global:** hierarchical/tiered map-reduce (pick community LEVEL by query breadth; bound map fan-out). All **batch-recomputed**; incremental adds do cheap local-only updates with bounded staleness. **Gate:** dedup precision/recall (over-/under-merge per tier) on a labeled merge set measured **at scale**, not the 36k thresholds; global-mode coverage validated.

### P7 — Ingestion scaling & cost budget (parallel TRACK) — `L`  · ledger/cache independent; write-path move lands with P4
Make extraction the durable, resumable, budgeted pipeline it must be at 10⁵–10⁶ LLM calls: **per-window extraction ledger** (doc_id, window_idx, content_hash, status) written **before** KG so a failed window is recorded (today failures just increment a counter and nodes vanish — silent loss); **idempotent content-hash resume**; concurrency budgeted to provider **TPM** (not a fixed 16); **turn ON the C8 LLM cache** (built, default-off) so re-runs are near-free; remove the single-writer SQLite bottleneck at ingest (subsumed by P4's Postgres). **Gate:** kill-a-worker-mid-doc test leaves no half-ingested retrievable object; resume re-runs only missing windows; idempotent re-ingest is deterministic.

### P8 — Escalation (gated on a measured 100k profile) / packaged engine (DEFER) — `L`  · depends: P5 + P6
Dedicated vector DB (Qdrant/Milvus) or ES **only if** a measured 100k profile names ANN/full-text — not LLM — as the bottleneck (and the new engine returns raw cosine + gather-by-id, passes the CJK eval). Standalone packaged engine: still **DEFER** unless a real external consumer exists — managed substrate (P4) already delivers swappability.

---

## 6. Red-team must-address (baked into the gates above)

- **Dual-index best-of long tail is NOT fully closed by gather-by-id alone** — gather-by-id needs the *candidate ids first*; an object that wins only on an evidence element absent from both the ANN top-K *and* the object-candidate set is invisible. Mitigation: generous `K_v` + element→object expansion + the P5 long-tail recall gate; accept a measured, bounded recall delta, don't claim zero.
- **Persist BM25 IDF** (P4) — never recompute full-corpus IDF per query.
- **tau recalibration at scale** — a single corpus-versioned tau may not suffice across 10M heterogeneous docs; P2.5 must check whether per-query-margin/relative grounding is needed.
- **Eval/ground-truth must exist before substrate swap** (P2.5 is a hard gate before P4/P5).
- **Canonical-id churn is structurally guaranteed today** → P3 sticky ids before any bulk load (citations depend on it).
- **Extraction magnitude + tail-failure** under-modeled → P7 ledger/resume/budget.
- **Live KG migration** (SQLite→Postgres, **not** Neo4j) needs shadow + rollback (P4).

---

## 7. Open questions (decide / spike before committing the dependent phase)

1. **Substrate validation:** does pgvector HNSW hold recall@`K_v` on the long-tail/CJK classes at 1万 (and at 10万 with halfvec/quantization)? → P2.5 eval.
2. **Ground truth at scale:** is the sampled-oracle (exact dense on 36k slices = gold) representative of the full-corpus long-tail/CJK/global cases?
3. **Extraction cost budget:** actual $/wall-clock for 10⁵ (1万) and 10⁶ (10万) extraction calls at the provider's TPM, with refine+gleaning?
4. **tau recalibration:** corpus-versioned tau vs per-query-margin at 10M heterogeneous docs?
5. **Text-recall bound:** what selectivity/LIMIT bounds the pg_trgm/FTS candidate side for common CJK bigrams without dropping tail recall?
6. **Community engine:** does igraph/leidenalg hold a multi-million-node graph in acceptable RAM/time, or is a heavier engine needed?
7. **Hierarchical global:** how is "query breadth" detected to pick the community LEVEL, and how is map fan-out validated for coverage?
8. **Migration cutover:** acceptable downtime/rollback window for SQLite→Postgres on a live system?

---

## 8. Sequencing rationale

Interface/ports **first** (P1→P2) so the substrate is swappable behind a proven seam; the **scale eval harness (P2.5)** before any substrate swap so parity is measurable; **sticky corpus/canonical ids (P3)** before any bulk load so citations survive batch re-runs; **substrate (P4)** before **two-stage retrieval (P5)** (P5 needs pgvector `vector_topk`/`topk_for_ids`/persisted-df); **batch merge/community (P6)** overlaps P5 (it's offline); **ingestion (P7)** is a parallel track that can start now (ledger/cache) and lands its write-path move with P4. Two invariants are CI-guarded at every gate; every phase is independently shippable and eval-gated.

**The honest bottom line:** single-ask latency stays **LLM-bound** at every scale — this roadmap buys **feasibility, throughput, and concurrency** for a 10k→100k unified KG, not a faster single ask. The first real walls are **extraction throughput** (P7) and the **in-RAM matrix + per-query full-scan** (P4+P5); everything else is in service of keeping the grounding/citation/merge/community guarantees intact while crossing those walls.
