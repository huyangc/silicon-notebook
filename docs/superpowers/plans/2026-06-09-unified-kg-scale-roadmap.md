# Unified Two-Tier KG-at-Scale Roadmap (v2)

> **v2 (2026-06-09)** supersedes v1 (which was scale-only). It folds in the **two-tier KB**, **governance/promotion**, **strict multi-hop reasoning**, and the **edge-trust** model decided in design discussion. Schema is locked separately in `schema/kg-schema.yaml` (v1.0.0).
>
> **For agentic workers:** master plan. P0 is done; P1/P2 task detail in sibling `2026-06-09-storage-decouple-retrieval-ports.md`. Everything else (P2.5–P8, T1–T4) is a **phase-spec** — expand each into its own dated plan (bite-sized TDD) when greenlit. Use superpowers:subagent-driven-development per phase.

**Goal:** A **two-tier knowledge base** for analog-circuit design: a shared, committee-curated **base KG over 10k+ (→100k) documents** (anti-hallucination grounding, authoritative) plus per-owner **personal KGs** (scenario experience), supporting **strict multi-hop reasoning** with sentence-level `[k]` citations + tau grounding, cross-document merge/dedup, and community/global QA. POC is **single-user** (multi-user/ACL deferred).

**Architecture:** Decouple storage/index behind capability ports (P1/P2); move objects/edges/vectors/text onto **managed Postgres + pgvector + Postgres FTS**; replace per-query "score-the-world" with **two-stage retrieval** (indexed candidate gen → bounded app-side scoring) that is **tier-aware** (federate base ∪ personal, base authoritative on conflict). A derived **in-memory graph (rustworkx)** — not a graph DB — serves multi-hop reasoning, community detection, and centrality. Cross-doc merge & communities are **batch-recomputed**. All scoring/grounding/`[k]`/conflict-precedence stays app-side; the engine returns neutral primitives.

**Tech stack:** Python/FastAPI · today hand-written `sqlite3` (no ORM, **no Neo4j**) → managed Postgres+pgvector+FTS · **rustworkx** (in-memory graph) · numpy (bounded only) · pytest.

---

## 0. Locked requirements & decisions (2026-06-09)

**Scale/infra:** start ~1万 docs (~1–3M objects / 2–6M vectors), scale **smoothly to ~10万 (~10–30M objects)** without rewrite · managed/cloud services **allowed** · ingestion **batch-primary + occasional incremental**.

**Must keep at scale (hard):** `[k]` citations + tau tiers · cross-document unified KG (merge/dedup) · community + global (GraphRAG) QA.

**Two-tier model (NEW):**
- **Base KG** — vertical-domain (all analog-circuit knowledge: textbooks + papers), committee-maintained, **authoritative (wins on conflict)**, **build-once + rare gated additions (strong human review)**, shared-read.
- **Personal KG** — per-owner, scenario-specific experience, lightweight, **no auto admission filtering**. Promotion into base is **owner-triggered → committee final review**, at the **node** level.
- **Reasoning chains may span both tiers** (personal experience is allowed in a chain); each hop is tier-tagged.

**Decisions locked this round:**
- **Schema locked at v1.0.0** (`schema/kg-schema.yaml`): 4 node types (Concept/Claim/Formula/Procedure, **atomic**) + **`validity_scope`** attribute on Claim/Formula + **reasoning edges broadened to connect Claim/Formula** + element-anchored evidence + edge-trust fields. Typed ontology **KEPT** (not a scale lever).
- **POC = single-user**; multi-user/ACL **deferred**.
- **Graph = in-memory rustworkx**, not a graph DB (topology is MB-scale even at 100k; only vectors are GB-scale).
- **Committee review spans front + back end** (review/promotion queue UX).
- **Edges auto-extracted**; trust via layered signals + targeted review, not blanket human review (T3).

---

## 1. Trigger scenario — the BASE KG ("unified KG over 10k+ academic docs")

The base tier is **one globally-queried KG**, collapsing the corpus into a single retrieval scope, so every per-notebook op inherits whole-corpus cost.

**Scale math (band ~100–500 objects/paper; textbook anchor ≈ 4k–11k objects):**

| docs | objects | vectors | in-RAM float32 matrix (×1024×4B, both indexes) |
|---|---|---|---|
| 10k | ~1–5M (central ~2.5M) | ~2–6M | **~18–30 GB** |
| 30k | ~3–15M | ~6–18M | ~55–90 GB |
| 100k | ~10–30M | ~20–60M+ | ~150–400 GB (impossible in-process) |

**Order things break:** (1) **ingestion throughput** (10⁵→10⁶ LLM calls — first real wall, P7); (2) **in-RAM matrix** (~18–30GB at 10k can't load — `vector_index.build_matrix`/`query_sims`, built in `ask()` `sqlite_repository.py:3253` — P4); (3) **per-query full-scan** (`score_knowledge` loops every object `retrieval.py:307`; `bm25_scores` recomputes full-corpus IDF per query `:377-391` — P5); (4) **per-notebook model** → corpus partition (P3); (5) **batch merge/communities** at millions → blocking + rustworkx (P6).

> **Honest framing:** at every tier the **answer LLM (~9.3s, ~91% of single-ask wall-clock)** dominates a *single* ask. The scale work buys **feasibility/throughput/concurrency**, not a faster ask. Past ~30k objects/query the full-scan inverts this — that's what two-stage retrieval (P5) fixes.

---

## 2. Two-tier KB model, conflict authority & POC scope (NEW)

- **Tier as a corpus property:** `corpus_id` carries `tier ∈ {base, personal}` + `authority` + `provenance`. Base = one big static corpus; personal = many small dynamic corpora. The **scale machinery (P3–P7) applies to the base**; personal KGs are small and run on the existing lightweight per-notebook pipeline.
- **Federated, tier-aware retrieval:** a query retrieves from **base ∪ the active personal corpus**; every candidate is **tier-tagged**; tier flows through the answer context and `[k]` citations; tau/grounding is **tier-weighted** (base-grounded > personal-only).
- **Conflict authority — hybrid enforcement:**
  1. *Build-time:* when a personal KB is built, run cross-tier conflict detection (reuse existing conflict detection + `contrasts_with`); tag personal claims `superseded_by_base` where they contradict base.
  2. *Answer-time:* the answer prompt encodes "base is authoritative; if personal contradicts base, **defer to base and surface the conflict**"; `[k]` shows the tier.
- **Anti-hallucination = the product thesis**, realized by element-level evidence + `[k]` + tau, now strengthened: answers must ground in the **authoritative base**; personal supplies scenario framing only.
- **POC:** single-user — one base corpus + one personal corpus, no ACL/auth. Multi-user is a later, separable axis (the biggest deferred scope).

---

## 3. Substrate — Postgres + pgvector + Postgres FTS

One managed Postgres serves multiple ports: tables for `ObjectStore`/`RelationStore`/`GraphTraversal`; **pgvector** (two separate tables `knowledge_embeddings`/`element_embeddings`, each its own HNSW index) for `VectorIndex`; **pg FTS/pg_trgm** as a **recall-only** candidate generator for `TextIndex`.

**Why pgvector for 1万→10万:** only option serving **both** ANN top-k (`ORDER BY vec <=> q LIMIT k`) **and exact gather-by-id** (`WHERE id = ANY(:ids)`) — the dual-index best-of needs exact gather over arbitrary evidence-element ids. `1-(vec<=>q)` = **raw cosine in [-1,1]** → `_fuse`'s `max(0.0,cos)` unchanged → **[0,1]/tau invariant survives**. One store = no cross-store read-your-writes. Toward 10万 use `halfvec`/quantization before a dedicated DB.

**Escalation (don't pre-build):** dedicated vector DB / ES only if a measured 100k profile shows ANN/full-text — not the LLM — is the bottleneck (P8). **CJK tokenizer stays app-side** (`retrieval.py:161-197`); FTS/pg_trgm returns a **recall superset of ids only**, eval-gated on a Chinese top-n identity test; `bm25_scores` re-scores app-side.

---

## 4. Invariants, must-keeps, schema (locked)

**Two invariants (CI-guarded every phase, `test_port_conformance`):** (1) **[0,1]/tau** — `VectorIndex` returns RAW cosine, engine never invents `relevance` (the `0ca8f1a` class); (2) **dual-index best-of** — DENSE id→cosine over two never-merged indexes + exact gather-by-id.

**Schema (LOCKED v1.0.0, `schema/kg-schema.yaml`):** 4 atomic node types; `validity_scope` on Claim/Formula (condition/region/assumptions — prevents out-of-scope application, a key hallucination source); reasoning edges broadened to connect Claim/Formula (fixes the thin `contrasts_with`/`derived_from`); edge fields `evidence/confidence/tier/corroboration`. Extraction must be **aligned** to this before the base build (see "Schema-alignment & calibration" below).

---

## 5. Strict reasoning & the in-memory graph (NEW)

- **Graph engine = rustworkx in-memory**, derived from the relations table. Topology is MB-scale even at 100k (~3M edges @10k ≈ 48MB; ~38M @100k ≈ 600MB) — **no graph-DB server**. Base graph is **precomputed once** (read-mostly) + personal overlaid; serves **multi-hop traversal, pathfinding, community detection (Leiden), and centrality**.
- **Multi-hop spans base ∪ personal.** Each edge carries `tier + confidence + evidence`; a chain's trust = **weakest link**, surfaced in the answer ("step 2 rests on your unverified note `[k·personal]`").
- **LLM integration (two-stage subgraph → synthesis):** retrieve a query-relevant **subgraph** (deterministic traversal, relevance-pruned per hop), serialize nodes+typed-edges+evidence into the context budget with `[k]` ids, and the LLM synthesizes a **citable reasoning chain**. Pushing hops into the graph (vs LLM-driven hop-by-hop) is both a quality and latency lever (one synthesis call vs N round-trips).
- **Answer-time chain verification:** adversarially verify only the **few edges in the chain about to be asserted** (LLM checks each edge against its evidence; majority-vote) — makes strict reasoning trustworthy without pre-verifying the whole graph.

---

## 6. Phase map

**A. Scale infrastructure**

| Phase | Effort | Role |
|---|---|---|
| **P0** Quick wins + baseline | ✅done | PR #24; baseline confirmed LLM-bound at small scale |
| **P1** Interface hardening | M | precondition for substrate swap (+ folds in the RRF-relevance fix) |
| **P2** Capability ports | L | load-bearing substrate seam |
| **P2.5** Scale eval harness + ground truth | L | hard gate before any substrate swap |
| **P3** Corpus + **tier** model (`corpus_id` + tier + sticky canonical ids) | L | per-notebook → durable tiered corpus partition |
| **P4** Substrate (Postgres+pgvector+FTS), shadow+parity | XL | the matrix/scan wall fix; gated on P2.5 |
| **P5** Two-stage **+ federated tier-aware** retrieval + conflict precedence | L | retires full-scan; base∪personal; base wins |
| **P6** Batch merge & community at scale (blocking + **rustworkx Leiden** + hierarchical global) | XL | offline; overlaps P5 |
| **P7** Ingestion scaling & cost budget (parallel track) | L | durable/resumable/budgeted; turn on C8 cache |
| **P8** Escalation (gated on measured 100k) / packaged engine (defer) | L | only if measured |

**B. Two-tier & strict-reasoning capability layer**

| Phase | Effort | Role · depends |
|---|---|---|
| **SA** Schema-alignment & calibration | M | align `extract.py` to schema v1.0.0 + 50–100-doc calibration gate · before base build |
| **T1** Personal tier + federated retrieval + conflict | M | personal corpus on existing pipeline; tier-aware merge; base precedence · P3, P5 |
| **T2** Deep graph reasoning (rustworkx, multi-hop, chain verify) | L | in-memory graph; subgraph→synthesis; answer-time verification · P6 |
| **T3** Edge trust & curation tooling | L | auto signals + centrality-prioritized review + feedback · P6 |
| **T4** Governance / promotion workflow (FE+BE) | L | owner→committee node-level promotion; base strong-review gate · T1, T3 |

---

## 7. Phase details

### P0 — Quick wins & baseline — **DONE** (PR #24, merged)
Cache-invalidation fix, `keyword_score_tokens` + token cache, `ask_latency` baseline (`answer_llm` ~91% of single-ask wall-clock).

### P1 — Interface hardening — `M`
Close every `repo._connect`/raw-SQL leak (`eval/speed.py`, `eval/db.py`) behind public readonly methods; extract `CacheBackend`. **Fold in the present-day bug:** `_rrf_scored` computes `relevance` from knowledge sims only, never `element_sims` (`sqlite_repository.py:~3595`) — half-breaks the dual-index invariant; fix it. **Gate:** full eval through public API, zero `_connect` outside repo; tier/grounding distribution unchanged. (Detail in sibling doc.)

### P2 — Capability ports — `L`
`ObjectStore/RelationStore/GraphTraversal/VectorIndex/TextIndex/EvidenceEnrichment/ClusterStore/CommunityStore` Protocols + DTOs with invariants in docstrings. `VectorIndex` exposes **`vector_topk` + `topk_for_ids`**; two never-merged indexes; `TextIndex.candidates` ids-only. SQLiteRepository satisfies unchanged. **Gate:** recall@k/MRR + tier distribution identical; conformance suite green.

### P2.5 — Scale eval harness + ground truth — `L`
Today's `run_recall` uses the broken full-scan path and 1/30 gold. Fix: run it on the **two-stage candidate path**, report **ANN-recall-of-gold vs end-to-end recall@k** separately; build a **sampled-oracle** gold (exact dense engine on fixed slices) incl. **long-tail / CJK / global** classes; version tau vs corpus. **Gate:** runs on two-stage path, reports both numbers on ≥N gold across hard classes. **Must pass before P4 parity is trusted.**

### P3 — Corpus + tier model — `L` · depends P2
`corpus_id` = durable partition with `tier{base,personal}` + authority + provenance; **no code path returns "all objects of a corpus"** (candidate set is the scope). Product "notebook" = a view over a corpus. **Sticky canonical ids before any bulk load:** today `canonical_id = id_prefix + min(seed)` (`kg_merge.py:243`) ⇒ batch re-run reassigns ids and **breaks citations** → make ids content/identity-based. **Gate:** simulated re-run reassigns **zero** canonical ids for unchanged-membership clusters.

### P4 — Substrate (Postgres+pgvector+FTS), shadow+parity — `XL` · depends P3 + P2.5
Port impls on Postgres+pgvector+FTS (§3). `vector_topk` raw cosine unclamped; `topk_for_ids` exact gather; **persist BM25 df/IDF**; one-time ETL of the live **SQLite** KG (~1.3GB) → Postgres; **shadow-run** (reads compared, writes to SQLite) before cutover; `.db` as rollback. **Gate:** conformance green on Postgres; read-compare parity on P2.5 slices; CJK identity eval passes; migration resumable + rollback defined.

### P5 — Two-stage + federated tier-aware retrieval + conflict — `L` · depends P4 (+ T1 for federation)
**Stage 1** vector ANN per index (generous `K_v`) + `TextIndex` recall superset → bounded candidate set, **across base ∪ active personal corpus**, tier-tagged. **Stage 2** hydrate + run **byte-identical** `score_knowledge`/`_fuse`/tau/best-of/`[k]` over the bounded set (`topk_for_ids` recovers exact cosine for evidence elements outside the ANN top-K — the long-tail guard); **tier-weighted** relevance; **base-precedence** conflict handling. **Gate:** P2.5 sampled-oracle shows ANN-recall-of-gold AND end-to-end recall@k within bound — specifically validating the **dual-index best-of long tail**; re-run on `K_v`/`ef_search` change.

### P6 — Batch merge & community at scale — `XL` · depends P3 + P4
**Entity resolution:** real **blocking** (normalized seed-key / bigram-band LSH) so ANN candidate gen runs within bounded blocks (not one global million-seed build); keep the 3-tier shape; handle transitive-reject + giant-cluster (hub concept). **Communities:** `networkx` → **rustworkx/leidenalg Leiden** behind the `rebuild_communities` seam; **hierarchical levels**; cap giant-community LLM context. **Global:** hierarchical map-reduce (pick LEVEL by query breadth; bound fan-out). All **batch**; incremental adds = cheap local updates, bounded staleness. **Gate:** dedup precision/recall at scale (not 36k); global coverage validated.

### P7 — Ingestion scaling & cost budget (parallel TRACK) — `L`
**Per-window extraction ledger** (doc_id, window_idx, content_hash, status) written **before** KG (today failures silently drop nodes); idempotent content-hash resume; concurrency budgeted to provider **TPM**; **turn ON the C8 LLM cache** (built, default-off) so re-runs are near-free; remove single-writer (subsumed by P4 Postgres). **Gate:** kill-a-worker-mid-doc leaves no half-ingested retrievable object; resume re-runs only missing windows; idempotent re-ingest deterministic.

### P8 — Escalation (gated) / packaged engine (DEFER) — `L` · depends P5 + P6
Dedicated vector DB / ES only if a measured 100k profile names ANN/full-text — not LLM — as bottleneck (raw cosine + gather-by-id + CJK eval required). Packaged engine **DEFER** unless a real external consumer exists.

### SA — Schema-alignment & calibration — `M` · before the base build
Align `extract.py` + `extraction_profiles.py` to schema v1.0.0: add **`validity_scope`** field; **broaden reasoning-edge constraints** to Claim/Formula; **explicitly hunt the sparse reasoning edges** (`depends_on`/`contrasts_with`/`prerequisite_of` — today 791/556/68); enforce **atomic claims**; **base-tier meta-text filter**. Then the **calibration gate**: 50–100 docs → measure per-relation edge density, atomicity, token cost/doc → extrapolate the 100k cost. **Gate:** reasoning-edge density adequate AND cost within budget AND atomicity acceptable; **do NOT run the full 100k on an unvalidated prompt.**

### T1 — Personal tier + federated retrieval + conflict — `M` · depends P3, P5
Personal corpus on the existing lightweight pipeline (small). Federated candidate gen base ∪ personal (P5 hook); tier tags through context + `[k]`; tier-weighted tau; build-time cross-tier conflict tagging (`contrasts_with` + conflict detection) + answer-time base-precedence prompt. **Gate:** a base↔personal contradiction is detected and the answer defers to base + surfaces it; tier shown in citations.

### T2 — Deep graph reasoning — `L` · depends P6
rustworkx in-memory graph (base precomputed + personal overlay). Subgraph retrieval (relevance-pruned multi-hop across tiers) → serialized into context with `[k]` → LLM synthesizes a citable chain. Per-edge tier+confidence; chain trust = weakest link, surfaced. **Answer-time chain verification** (adversarial-verify the chain's edges; majority vote). Read-mostly base → **precompute** common closures/chains. **Gate:** on a labeled multi-hop question set, verified chains beat 1-hop/vector baseline on correctness; every asserted edge carries evidence + tier; wrong-edge demotion works.

### T3 — Edge trust & curation tooling — `L` · depends P6
Automatic signals: extraction confidence (self-rate × cross-window agreement × endpoint confidence), evidence-anchoring, **cross-doc corroboration count**, type-constraint validation. Targeted human review (committee): **centrality-prioritized backbone edges** (from the rustworkx graph) + **traversed-in-answers** (review-on-use) + **flagged conflicts** + **low-confidence-high-impact**. Feedback loop: wrong-chain reports demote the edge. **Gate:** review queue prioritizes by centrality/usage/conflict; demotion reflected in retrieval/reasoning.

### T4 — Governance / promotion workflow (FE+BE) — `L` · depends T1, T3
**Owner-triggered** promotion of personal nodes → **committee final review** → on approval, cross-corpus merge into base (dedup vs base) + status flip. Base additions go through the **strong-review** gate. Front end: review/promotion queue UX; back end: promotion state machine (extends existing status lifecycle + merge-candidate machinery). **Gate:** a personal claim promoted end-to-end appears in base (deduped), with audit trail; rejected promotions don't leak into base retrieval.

---

## 8. Red-team must-address (baked into the gates)

- **Dual-index best-of long tail not fully closed by gather-by-id alone** — needs candidate ids first; generous `K_v` + element→object expansion + the P5 recall gate; accept a measured bounded delta.
- **Persist BM25 IDF** (P4); never recompute full-corpus IDF per query.
- **tau recalibration at scale** — corpus-versioned tau may not suffice across 10M heterogeneous docs + two tiers; P2.5 checks per-query-margin/relative grounding.
- **Eval/ground-truth before substrate swap** (P2.5 hard gate before P4/P5).
- **Canonical-id churn structurally guaranteed today** → P3 sticky ids before bulk load.
- **Extraction magnitude + tail-failure** → P7 ledger/resume/budget; **SA calibration before the 100k spend.**
- **Live KG migration** SQLite→Postgres (**not** Neo4j) needs shadow + rollback (P4).
- **Reasoning over mixed-tier chains** — a chain mixing curated base edges + noisy personal edges is only as trustworthy as its weakest edge; T2 must label + answer-time-verify, not silently assert.
- **Reasoning-edge sparsity** (`depends_on`/`contrasts_with`/`prerequisite_of`) — SA must measurably increase these or deep reasoning has no edges to walk.

---

## 9. Open questions (decide / spike before the dependent phase)

**Resolved this round:** multi-user → deferred (single-user POC); reasoning chains include personal experience → yes; committee review → FE+BE; graph → rustworkx in-memory; typed ontology → keep; schema → locked v1.0.0.

**Still open:**
1. **Substrate validation:** pgvector HNSW recall@`K_v` on long-tail/CJK at 1万 (and 10万 w/ halfvec)? → P2.5.
2. **Ground-truth representativeness:** sampled-oracle (exact dense on slices) vs full-corpus long-tail/CJK/global?
3. **Extraction cost budget:** actual $/wall-clock for 10⁵ (1万) and 10⁶ (10万) calls w/ refine+gleaning? → SA calibration.
4. **tau recalibration:** corpus-versioned vs per-query-margin at 10M + two tiers?
5. **Text-recall bound:** pg_trgm/FTS selectivity/LIMIT for common CJK bigrams without dropping tail recall?
6. **Community engine:** does rustworkx/leidenalg hold a multi-million-node graph in acceptable RAM/time?
7. **Hierarchical global:** how is query breadth detected to pick the community LEVEL; fan-out coverage?
8. **Migration cutover:** acceptable downtime/rollback window for SQLite→Postgres on a live system?
9. **Conflict mechanism (two-tier):** exact precedence enforcement — build-time tagging coverage vs answer-time prompt reliance; how are *ambiguous* (non-`contrasts_with`) conflicts handled?
10. **Chain-trust aggregation:** weakest-link vs confidence-product vs verified-only — which best matches "strict"?
11. **Committee workflow detail:** roles, queue SLAs, what's reviewed at promotion vs base-add.

---

## 10. Sequencing rationale

**Foundation first:** P1 (hardening) → P2 (ports) so the substrate is swappable behind a proven seam. **SA (schema-alignment + calibration)** can start now in parallel (it gates the base build, and the calibration cost-check de-risks the 100k spend). **P2.5 (scale eval)** before any substrate swap so parity is measurable. **P3 (tiered corpus + sticky ids)** before any bulk load so citations survive batch re-runs. **P4 (substrate)** before **P5 (two-stage)**. **P6 (merge/community)** overlaps P5 (offline). **P7 (ingestion)** is a parallel track (ledger/cache now; write-path move with P4). The **T-layer** rides on top: **T1** (personal+federation) needs P3/P5; **T2** (deep reasoning) needs P6's rustworkx graph; **T3** (edge trust) needs P6 centrality; **T4** (governance) needs T1+T3. Two invariants are CI-guarded at every gate; every phase is independently shippable and eval-gated.

**Near-term POC slice (single-user):** SA (align + calibrate) → P1 → P2 → P2.5, then P3+P4 to stand up the base substrate, P5+T1 for tier-aware retrieval, T2 for the strict-reasoning demo. P6/P7 scale the base build; T3/T4 add curation+governance.

**Honest bottom line:** single-ask latency stays **LLM-bound** at every scale — this roadmap buys **feasibility, throughput, concurrency, and trustworthy strict reasoning** for a two-tier 10k→100k KG, not a faster single ask. The first real walls are **extraction throughput** (P7/SA) and the **in-RAM matrix + per-query full-scan** (P4+P5); the two-tier/reasoning/governance layer (T1–T4) is what turns "a big KG" into "the anti-hallucination, citable, strict-reasoning product."
