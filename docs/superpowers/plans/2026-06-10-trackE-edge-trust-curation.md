# Track E — Edge Trust & Curation Tooling

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Date:** 2026-06-10
**Branch:** wave2 (off `994dd14`, Wave 1 A/B/C merged to master)
**Goal:** Make auto-extracted edges trustworthy enough for strict multi-hop reasoning without human-reviewing every edge. Trust is computed from signals already present on existing edges (no re-extraction). A curator review queue surfaces the highest-risk edges first; a feedback loop lets reviewers persist verdicts that the reasoning engine honours.

**Invariants (do not touch):**
- `[0,1]/tau` scoring path and `dual-index best-of` — `classify_evidence` / `evidence_tau_low/high` / `_fuse` / `W_KEYWORD`/`W_SEMANTIC` must not change.
- `multihop_subgraph` fan-out sort by `confidence` and `verify_chain_edges` demotion to `0.05` remain unchanged; this plan ADDS a separate `trust_score` signal computed from existing data.

---

## Code facts confirmed by reading merged files

### Edge storage (`sqlite_repository.py:322-331`)
`knowledge_relations` columns (as defined in the DDL):
```
id, notebook_id, source_id, source_object_id, target_object_id, edge_type, evidence, created_at
```
- **No `confidence` or `review_status` column** — `confidence` is a runtime default of `1.0` set in `build_rx_graph` (`graph_reason.py:67`).
- `source_id` is a FK to `sources(id)` (`sqlite_repository.py:325`) — it is the source document that produced the edge. This is the corroboration key.
- `evidence` is a JSON blob (`list[dict]`) with keys `file/char_start/char_end/line_start/line_end/quote`.

### Typed edge constraints (`extract.py:15-35`)
Typed constraints (source node type → target node type) embedded in the LLM prompt:
```
defines:         Claim  → Concept
about:           Claim|Formula → Concept
supports:        Claim|Formula → Claim
derived_from:    Formula → Formula
used_in:         Formula → Procedure
part_of, composed_of, contrasts_with, kind_of: Concept → Concept
depends_on, prerequisite_of, precedes: unconstrained (any → any)
```
`EDGE_TYPES` set at `extract.py:15`; `NODE_TYPES` set at `extract.py:14`.

### Rustworkx centrality APIs (verified by import)
- `rx.digraph_betweenness_centrality(G)` — returns `{node_idx: float}`
- `rx.digraph_edge_betweenness_centrality(G)` — returns `EdgeCentralityMapping {edge_idx: float}`
- `rx.pagerank(G, alpha=0.85)` — returns `{node_idx: float}` for `PyDiGraph`
- Edge index is obtainable via `G.edge_index_map()` → `{edge_idx: (src, tgt, payload)}`

### Graph build (`graph_reason.py:23-75`)
`build_rx_graph(nodes, relations)` produces `(PyDiGraph, idx_to_oid, oid_to_idx)`. Edge payload keys: `rel_id, edge_type, evidence, confidence, tier`. The version-cached graph is obtained via `SqliteRepository._rx_graph(notebook_id)` (`sqlite_repository.py:3128`).

### Existing merge/review pattern
`concept_merge_candidates` table uses `ALTER TABLE ... ADD COLUMN review_status/confidence/rationale/reviewed_by` pattern (`sqlite_repository.py:506-511`) — same pattern will be used for `knowledge_relations.review_status`.

---

## Trust score formula (documented here, enforced by tests)

```
trust_score(edge) =
    w_ev    * evidence_anchor_score    # 0 or 1: evidence list non-empty and quote non-empty
  + w_corr  * corroboration_score      # 0..1: min(distinct_source_count / CORR_CAP, 1.0)
  + w_type  * type_validity_score      # 0 or 1: edge_type constraint respected

where:
  w_ev   = 0.4
  w_corr = 0.3
  w_type = 0.3
  CORR_CAP = 3   (3 independent sources → full corroboration score)
```

`trust_score` lives in `app/services/kg/edge_trust.py` (new file). It is a **computed signal**, not stored in the DB. It is NOT the `[0,1]/tau` relevance score; it is only used by the review-prioritization surface and the reasoning path when `review_status` is `'rejected'`.

---

## Files

| File | Action | Responsibility |
|---|---|---|
| `backend/app/services/kg/edge_trust.py` | **Create** | `compute_trust_score`, `TYPE_CONSTRAINTS`, corroboration grouping |
| `backend/app/services/kg/graph_reason.py` | **Modify** (additive only) | Add `compute_centrality(G)` returning node betweenness + pagerank dicts; `compute_edge_centrality(G)` returning edge betweenness |
| `backend/app/services/sqlite_repository.py` | **Modify** | Schema migration (ADD COLUMN `review_status`); `review_queue(notebook_id)` method; `set_edge_review(notebook_id, rel_id, status)` method; honour `rejected` edges in `_rx_graph` |
| `backend/app/api/routes.py` | **Modify** | `GET /notebooks/{id}/edge-review-queue`; `POST /notebooks/{id}/relations/{rel_id}/review` |
| `backend/app/models/schemas.py` | **Modify** | `EdgeReviewItem`, `EdgeReviewRequest` Pydantic models |
| `backend/tests/test_edge_trust.py` | **Create** | All trust-signal unit tests (pure function, synthetic fixture) |
| `backend/tests/test_edge_centrality.py` | **Create** | Centrality computation tests over synthetic PyDiGraph |
| `backend/tests/test_edge_review_queue.py` | **Create** | Review queue ranking + feedback-loop integration tests |

---

## Task 1: `edge_trust.py` — evidence anchoring + type-constraint validity

**Files:**
- Create: `backend/app/services/kg/edge_trust.py`
- Create: `backend/tests/test_edge_trust.py`

### Step 1: Write the failing tests

Create `backend/tests/test_edge_trust.py`:

```python
# backend/tests/test_edge_trust.py
"""Unit tests for edge trust signals. All pure-function, no DB/IO."""
import pytest


# ── fixtures ─────────────────────────────────────────────────────────────────

def _rel(edge_type, src_type, tgt_type, evidence=None):
    """Build a minimal relation dict as used by build_rx_graph / relations_for_notebook."""
    return {
        "id": "r1",
        "edge_type": edge_type,
        "evidence": evidence if evidence is not None else [],
        "source_object_id": "src",
        "target_object_id": "tgt",
        "_src_type": src_type,  # extra key used by tests only
        "_tgt_type": tgt_type,
    }


def _ev(quote="some quote"):
    return [{"file": "f1", "char_start": 0, "char_end": 10,
             "line_start": 1, "line_end": 1, "quote": quote}]


# ── evidence anchoring ────────────────────────────────────────────────────────

def test_evidence_anchor_present():
    from app.services.kg.edge_trust import evidence_anchor_score
    assert evidence_anchor_score(_rel("supports", "Claim", "Claim", _ev())) == 1.0


def test_evidence_anchor_empty_list():
    from app.services.kg.edge_trust import evidence_anchor_score
    assert evidence_anchor_score(_rel("supports", "Claim", "Claim", [])) == 0.0


def test_evidence_anchor_empty_quote():
    from app.services.kg.edge_trust import evidence_anchor_score
    ev = [{"file": "f1", "char_start": 0, "char_end": 0, "line_start": 1, "line_end": 1, "quote": ""}]
    assert evidence_anchor_score(_rel("supports", "Claim", "Claim", ev)) == 0.0


def test_evidence_anchor_string_blob_decoded():
    """evidence stored as JSON string (as loaded from DB) is decoded."""
    import json
    from app.services.kg.edge_trust import evidence_anchor_score
    rel = _rel("supports", "Claim", "Claim")
    rel["evidence"] = json.dumps(_ev())
    assert evidence_anchor_score(rel) == 1.0


# ── type-constraint validity ───────────────────────────────────────────────────

def test_type_validity_defines_claim_concept():
    from app.services.kg.edge_trust import type_validity_score
    node_types = {"src": "Claim", "tgt": "Concept"}
    assert type_validity_score(_rel("defines", "Claim", "Concept"), node_types) == 1.0


def test_type_validity_defines_wrong_src():
    from app.services.kg.edge_trust import type_validity_score
    node_types = {"src": "Concept", "tgt": "Concept"}
    assert type_validity_score(_rel("defines", "Concept", "Concept"), node_types) == 0.0


def test_type_validity_used_in_formula_procedure():
    from app.services.kg.edge_trust import type_validity_score
    node_types = {"src": "Formula", "tgt": "Procedure"}
    assert type_validity_score(_rel("used_in", "Formula", "Procedure"), node_types) == 1.0


def test_type_validity_used_in_wrong():
    from app.services.kg.edge_trust import type_validity_score
    node_types = {"src": "Claim", "tgt": "Procedure"}
    assert type_validity_score(_rel("used_in", "Claim", "Procedure"), node_types) == 0.0


def test_type_validity_unconstrained_edge_passes():
    """depends_on / precedes have no type constraint → always 1.0."""
    from app.services.kg.edge_trust import type_validity_score
    node_types = {"src": "Concept", "tgt": "Claim"}
    assert type_validity_score(_rel("depends_on", "Concept", "Claim"), node_types) == 1.0
    assert type_validity_score(_rel("precedes", "Formula", "Claim"), node_types) == 1.0


def test_type_validity_unknown_edge_type_fails():
    """An edge_type not in EDGE_TYPES is invalid."""
    from app.services.kg.edge_trust import type_validity_score
    node_types = {"src": "Claim", "tgt": "Concept"}
    assert type_validity_score(_rel("made_up", "Claim", "Concept"), node_types) == 0.0


def test_type_validity_missing_node_type_fails():
    """If a node's type is not in node_types dict, conservative → 0.0."""
    from app.services.kg.edge_trust import type_validity_score
    assert type_validity_score(_rel("defines", "Claim", "Concept"), {}) == 0.0


# ── combined trust score ───────────────────────────────────────────────────────

def test_trust_score_full():
    from app.services.kg.edge_trust import compute_trust_score
    rel = _rel("defines", "Claim", "Concept", _ev())
    node_types = {"src": "Claim", "tgt": "Concept"}
    # ev=1, corr=1 (corr_score injected as 1.0), type=1 → 0.4+0.3+0.3 = 1.0
    score = compute_trust_score(rel, node_types, corroboration_score=1.0)
    assert abs(score - 1.0) < 1e-9


def test_trust_score_no_evidence_no_corroboration():
    from app.services.kg.edge_trust import compute_trust_score
    rel = _rel("defines", "Claim", "Concept", [])
    node_types = {"src": "Claim", "tgt": "Concept"}
    # ev=0, corr=0, type=1 → 0 + 0 + 0.3 = 0.3
    score = compute_trust_score(rel, node_types, corroboration_score=0.0)
    assert abs(score - 0.3) < 1e-9


def test_trust_score_bounds():
    from app.services.kg.edge_trust import compute_trust_score
    rel = _rel("supports", "Claim", "Claim", _ev())
    node_types = {"src": "Claim", "tgt": "Claim"}
    s = compute_trust_score(rel, node_types, corroboration_score=0.5)
    assert 0.0 <= s <= 1.0


# ── cross-doc corroboration grouping ─────────────────────────────────────────

def test_corroboration_count_distinct_sources():
    """Two edges with same (norm_src_name, edge_type, norm_tgt_name) but different
    source_id → corroboration_count = 2."""
    from app.services.kg.edge_trust import corroboration_counts
    rels = [
        {"id": "r1", "source_object_id": "A", "target_object_id": "B",
         "edge_type": "supports", "source_id": "src1",
         "_src_name": "Claim Alpha", "_tgt_name": "Concept Beta"},
        {"id": "r2", "source_object_id": "A2", "target_object_id": "B2",
         "edge_type": "supports", "source_id": "src2",
         "_src_name": "Claim Alpha", "_tgt_name": "Concept Beta"},
    ]
    counts = corroboration_counts(rels)
    assert counts["r1"] == 2
    assert counts["r2"] == 2


def test_corroboration_same_source_not_counted_twice():
    """Same source_id asserting same triple → still counts as 1."""
    from app.services.kg.edge_trust import corroboration_counts
    rels = [
        {"id": "r1", "source_object_id": "A", "target_object_id": "B",
         "edge_type": "supports", "source_id": "src1",
         "_src_name": "Alpha", "_tgt_name": "Beta"},
        {"id": "r2", "source_object_id": "A2", "target_object_id": "B2",
         "edge_type": "supports", "source_id": "src1",
         "_src_name": "Alpha", "_tgt_name": "Beta"},
    ]
    counts = corroboration_counts(rels)
    assert counts["r1"] == 1
    assert counts["r2"] == 1


def test_corroboration_cap_at_corr_cap():
    from app.services.kg.edge_trust import corroboration_score_from_count, CORR_CAP
    assert corroboration_score_from_count(CORR_CAP) == 1.0
    assert corroboration_score_from_count(CORR_CAP + 5) == 1.0
    assert corroboration_score_from_count(0) == 0.0
    assert abs(corroboration_score_from_count(1) - 1.0 / CORR_CAP) < 1e-9
```

### Step 2: Run the test to verify it fails

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/wave2/backend && \
  python -m pytest tests/test_edge_trust.py -v 2>&1 | head -20
```
Expected: FAIL — `ModuleNotFoundError: app.services.kg.edge_trust`

### Step 3: Write minimal implementation

Create `backend/app/services/kg/edge_trust.py`:

```python
"""Edge trust signals: evidence anchoring, cross-doc corroboration, type-constraint validity.

compute_trust_score is a pure function — no I/O, easily unit-tested.
It produces a SEPARATE computed signal (not the [0,1]/tau relevance score).
Do NOT use trust_score in classify_evidence / _fuse / W_KEYWORD / W_SEMANTIC paths.

Formula (weights documented in plan 2026-06-10-trackE-edge-trust-curation.md):
    trust_score = 0.4 * evidence_anchor + 0.3 * corroboration + 0.3 * type_validity
"""
from __future__ import annotations

import json
import re
from collections import defaultdict
from typing import Dict, List

# ── Typed edge constraints (mirrored from extract.py:32-35) ──────────────────
# Maps edge_type → frozenset of valid (src_type, tgt_type) pairs.
# None in the frozenset means "unconstrained" (any type allowed).
# Extract.py prompt text: defines(Claim->Concept), about(Claim|Formula->Concept),
# supports(Claim|Formula->Claim), derived_from(Formula->Formula),
# used_in(Formula->Procedure), part_of/composed_of/contrasts_with/kind_of(Concept->Concept),
# depends_on/prerequisite_of/precedes: unconstrained.
TYPE_CONSTRAINTS: Dict[str, frozenset] = {
    "defines":       frozenset({("Claim", "Concept")}),
    "about":         frozenset({("Claim", "Concept"), ("Formula", "Concept")}),
    "supports":      frozenset({("Claim", "Claim"), ("Formula", "Claim")}),
    "derived_from":  frozenset({("Formula", "Formula")}),
    "used_in":       frozenset({("Formula", "Procedure")}),
    "part_of":       frozenset({("Concept", "Concept")}),
    "composed_of":   frozenset({("Concept", "Concept")}),
    "contrasts_with":frozenset({("Concept", "Concept")}),
    "kind_of":       frozenset({("Concept", "Concept")}),
    # Unconstrained: depends_on, prerequisite_of, precedes
}

VALID_EDGE_TYPES = frozenset({
    "defines", "part_of", "composed_of", "contrasts_with", "kind_of",
    "about", "supports", "derived_from", "depends_on", "prerequisite_of",
    "used_in", "precedes",
})

CORR_CAP = 3  # 3 independent sources → full corroboration score (= 1.0)

# Weight constants
W_EVIDENCE  = 0.4
W_CORR      = 0.3
W_TYPE      = 0.3


def _decode_evidence(rel: dict) -> list:
    ev = rel.get("evidence", [])
    if isinstance(ev, str):
        try:
            ev = json.loads(ev)
        except Exception:
            ev = []
    return ev if isinstance(ev, list) else []


def evidence_anchor_score(rel: dict) -> float:
    """1.0 if the edge has at least one evidence entry with a non-empty quote; 0.0 otherwise."""
    for entry in _decode_evidence(rel):
        if isinstance(entry, dict) and entry.get("quote", "").strip():
            return 1.0
    return 0.0


def type_validity_score(rel: dict, node_types: Dict[str, str]) -> float:
    """1.0 if the edge_type is known AND the (src, tgt) node-type pair respects the constraint.

    node_types: {object_id: object_type} — caller provides the type of each endpoint.
    Unconstrained edge types (depends_on, prerequisite_of, precedes) always return 1.0
    provided the edge_type is in VALID_EDGE_TYPES.
    Returns 0.0 for unknown edge types, missing node types, or type-constraint violations.
    """
    edge_type = rel.get("edge_type", "")
    if edge_type not in VALID_EDGE_TYPES:
        return 0.0

    src_oid = rel.get("source_object_id", "")
    tgt_oid = rel.get("target_object_id", "")
    src_type = node_types.get(src_oid)
    tgt_type = node_types.get(tgt_oid)
    if not src_type or not tgt_type:
        return 0.0

    constraints = TYPE_CONSTRAINTS.get(edge_type)
    if constraints is None:
        # Unconstrained edge type
        return 1.0
    return 1.0 if (src_type, tgt_type) in constraints else 0.0


def corroboration_score_from_count(count: int) -> float:
    """Normalise a raw distinct-source count to [0, 1] with cap CORR_CAP."""
    if count <= 0:
        return 0.0
    return min(count / CORR_CAP, 1.0)


def _norm(name: str) -> str:
    """Lightweight name normaliser for triple-identity matching (case + punctuation)."""
    cleaned = re.sub(r"[^a-z0-9 ]+", " ", (name or "").lower())
    return re.sub(r"\s+", " ", cleaned).strip()


def corroboration_counts(
    relations: List[dict],
    node_names: Dict[str, str] | None = None,
) -> Dict[str, int]:
    """Return {rel_id: distinct_source_count} for each relation.

    Triple identity = (norm(src_name), edge_type, norm(tgt_name)).
    Corroboration counts DISTINCT source_ids asserting the same triple.
    If a rel's source_id is None/empty it is treated as its own unique source.

    node_names: {object_id: name} — when provided, triple keys use name-normalised
    values (more robust cross-doc matching).  When absent, raw object IDs are used.
    """
    if node_names is None:
        node_names = {}
    # Two-pass: first collect (triple_key → set of source_ids); then map rel_id → count.
    triple_sources: Dict[str, set] = defaultdict(set)
    rel_triple: Dict[str, str] = {}

    for rel in relations:
        rid = rel.get("id", "")
        src_oid = rel.get("source_object_id", "")
        tgt_oid = rel.get("target_object_id", "")
        src_name = _norm(node_names.get(src_oid) or rel.get("_src_name") or src_oid)
        tgt_name = _norm(node_names.get(tgt_oid) or rel.get("_tgt_name") or tgt_oid)
        edge_type = rel.get("edge_type", "")
        triple_key = f"{src_name}|{edge_type}|{tgt_name}"
        source_id = rel.get("source_id") or f"__unique_{rid}"
        triple_sources[triple_key].add(source_id)
        rel_triple[rid] = triple_key

    return {rid: len(triple_sources[triple_key]) for rid, triple_key in rel_triple.items()}


def compute_trust_score(
    rel: dict,
    node_types: Dict[str, str],
    corroboration_score: float,
) -> float:
    """Combine the three signals into a single edge trust score in [0, 1].

    corroboration_score is pre-computed by the caller (use corroboration_counts +
    corroboration_score_from_count so the caller can batch-compute it once over all edges).
    """
    ev_score = evidence_anchor_score(rel)
    tv_score = type_validity_score(rel, node_types)
    score = W_EVIDENCE * ev_score + W_CORR * corroboration_score + W_TYPE * tv_score
    return max(0.0, min(1.0, score))
```

### Step 4: Run the test to verify it passes

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/wave2/backend && \
  python -m pytest tests/test_edge_trust.py -v
```
Expected: PASS (all tests green)

### Step 5: Gate

All `tests/test_edge_trust.py` tests pass. Trust-score formula produces values in `[0,1]`. `corroboration_counts` counts distinct source IDs correctly.

### Step 6: Commit

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/wave2 && \
git add backend/app/services/kg/edge_trust.py backend/tests/test_edge_trust.py && \
git commit -m "feat(trackE): edge trust signals — evidence anchoring, type-constraint validity, corroboration (T1)"
```

---

## Task 2: Centrality from rustworkx graph (`graph_reason.py` additive additions)

**Files:**
- Modify: `backend/app/services/kg/graph_reason.py` (add two functions at end of file; no changes to existing functions)
- Create: `backend/tests/test_edge_centrality.py`

**Note:** A parallel track (storage decoupling) may also edit `graph_reason.py`. Keep additions strictly additive — append two new top-level functions after `verify_chain_edges`.

### Step 1: Write the failing tests

Create `backend/tests/test_edge_centrality.py`:

```python
# backend/tests/test_edge_centrality.py
"""Tests for centrality computation in graph_reason.py.
All tests use synthetic PyDiGraph fixtures — no DB/IO.
"""
import pytest
import rustworkx as rx


# ── Synthetic graph fixture ──────────────────────────────────────────────────
# Linear chain A→B→C→D; B is on every A→D path (max betweenness).
# D has no out-edges (low pagerank contribution as source).

NODES = {
    "A": {"type": "Formula",  "name": "Node A"},
    "B": {"type": "Claim",    "name": "Node B"},
    "C": {"type": "Claim",    "name": "Node C"},
    "D": {"type": "Concept",  "name": "Node D"},
}
RELATIONS = [
    {"id": "r1", "source_object_id": "A", "target_object_id": "B",
     "edge_type": "derived_from", "evidence": [{"quote": "A→B"}]},
    {"id": "r2", "source_object_id": "B", "target_object_id": "C",
     "edge_type": "supports",     "evidence": [{"quote": "B→C"}]},
    {"id": "r3", "source_object_id": "C", "target_object_id": "D",
     "edge_type": "depends_on",   "evidence": [{"quote": "C→D"}]},
]


@pytest.fixture
def chain_graph():
    from app.services.kg.graph_reason import build_rx_graph
    return build_rx_graph(NODES, RELATIONS)


# ── compute_centrality ────────────────────────────────────────────────────────

def test_compute_centrality_returns_node_dicts(chain_graph):
    from app.services.kg.graph_reason import compute_centrality
    G, idx_to_oid, oid_to_idx = chain_graph
    result = compute_centrality(G, idx_to_oid)
    assert "betweenness" in result and "pagerank" in result
    assert set(result["betweenness"].keys()) == {"A", "B", "C", "D"}
    assert set(result["pagerank"].keys()) == {"A", "B", "C", "D"}


def test_compute_centrality_middle_node_highest_betweenness(chain_graph):
    """In a 4-node linear chain, B and C are on all shortest paths between
    pairs that span the chain, so they should have higher betweenness than A or D."""
    from app.services.kg.graph_reason import compute_centrality
    G, idx_to_oid, oid_to_idx = chain_graph
    result = compute_centrality(G, idx_to_oid)
    btwn = result["betweenness"]
    # A is a source (betweenness=0 for normalized digraph betweenness when no paths pass through it)
    # D is a sink (same reason). B and C are strictly in the middle.
    assert btwn["B"] > btwn["A"]
    assert btwn["C"] > btwn["D"]


def test_compute_centrality_pagerank_all_positive(chain_graph):
    from app.services.kg.graph_reason import compute_centrality
    G, idx_to_oid, oid_to_idx = chain_graph
    result = compute_centrality(G, idx_to_oid)
    for v in result["pagerank"].values():
        assert v > 0.0


def test_compute_centrality_pagerank_sums_to_approx_one(chain_graph):
    """Pagerank of all nodes in a connected graph should sum to ~1.0."""
    from app.services.kg.graph_reason import compute_centrality
    G, idx_to_oid, oid_to_idx = chain_graph
    result = compute_centrality(G, idx_to_oid)
    assert abs(sum(result["pagerank"].values()) - 1.0) < 0.01


def test_compute_centrality_empty_graph():
    """Empty graph (no nodes) must return empty dicts without error."""
    from app.services.kg.graph_reason import compute_centrality
    G = rx.PyDiGraph()
    result = compute_centrality(G, {})
    assert result == {"betweenness": {}, "pagerank": {}}


# ── compute_edge_centrality ───────────────────────────────────────────────────

def test_compute_edge_centrality_returns_rel_id_dict(chain_graph):
    from app.services.kg.graph_reason import compute_edge_centrality
    G, idx_to_oid, oid_to_idx = chain_graph
    result = compute_edge_centrality(G)
    # One entry per edge (3 edges in fixture)
    assert len(result) == 3
    # Keys are rel_id strings from the edge payload
    assert all(isinstance(k, str) for k in result)
    assert all(isinstance(v, float) for v in result.values())


def test_compute_edge_centrality_middle_edge_highest(chain_graph):
    """In a linear 4-node chain, the middle edges r2 (B→C) carries all paths
    between A and C, A and D; r1 (A→B) only carries paths starting from A.
    r2 should have >= betweenness of r1."""
    from app.services.kg.graph_reason import compute_edge_centrality
    G, idx_to_oid, oid_to_idx = chain_graph
    result = compute_edge_centrality(G)
    # r2 (B→C) connects the chain at the middle — it's on more shortest paths
    assert result.get("r2", 0) >= result.get("r1", 0)


def test_compute_edge_centrality_empty_graph():
    from app.services.kg.graph_reason import compute_edge_centrality
    G = rx.PyDiGraph()
    assert compute_edge_centrality(G) == {}


def test_compute_edge_centrality_no_rel_id_falls_back(chain_graph):
    """Edges without a rel_id key in the payload are keyed by their edge index (int→str)."""
    from app.services.kg.graph_reason import build_rx_graph, compute_edge_centrality
    rels_no_id = [
        {"source_object_id": "A", "target_object_id": "B",
         "edge_type": "supports", "evidence": []},
    ]
    G2, _, _ = build_rx_graph(NODES, rels_no_id)
    result = compute_edge_centrality(G2)
    assert len(result) == 1
    # Key is a string (fallback to str(edge_index))
    assert all(isinstance(k, str) for k in result)
```

### Step 2: Run the test to verify it fails

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/wave2/backend && \
  python -m pytest tests/test_edge_centrality.py -v 2>&1 | head -20
```
Expected: FAIL — `ImportError: cannot import name 'compute_centrality' from 'app.services.kg.graph_reason'`

### Step 3: Write minimal implementation

Append to `backend/app/services/kg/graph_reason.py` (after `verify_chain_edges`, no changes to existing code):

```python


# ---------------------------------------------------------------------------
# Centrality helpers (Track E — edge trust & curation tooling)
# These are ADDITIVE functions; they do not modify any existing function.
# ---------------------------------------------------------------------------

def compute_centrality(
    G: rx.PyDiGraph,
    idx_to_oid: Dict[int, str],
) -> dict:
    """Compute node-level betweenness and pagerank over the graph.

    Returns:
        {
          "betweenness": {object_id: float, ...},   # digraph betweenness centrality
          "pagerank":    {object_id: float, ...},   # PageRank (alpha=0.85)
        }

    Both metrics are keyed by object_id (string), not node index.
    Empty graph returns {"betweenness": {}, "pagerank": {}}.
    """
    if G.num_nodes() == 0:
        return {"betweenness": {}, "pagerank": {}}

    btwn_raw: Dict[int, float] = rx.digraph_betweenness_centrality(G, normalized=True)
    pr_raw: Dict[int, float] = rx.pagerank(G, alpha=0.85)

    betweenness = {idx_to_oid[idx]: v for idx, v in btwn_raw.items() if idx in idx_to_oid}
    pagerank    = {idx_to_oid[idx]: v for idx, v in pr_raw.items()   if idx in idx_to_oid}
    return {"betweenness": betweenness, "pagerank": pagerank}


def compute_edge_centrality(G: rx.PyDiGraph) -> Dict[str, float]:
    """Compute edge betweenness centrality for each edge in the graph.

    Returns {rel_id: float} where rel_id is taken from the edge payload key
    'rel_id'.  If a payload has no rel_id, falls back to str(edge_index).

    Empty graph returns {}.
    """
    if G.num_edges() == 0:
        return {}

    ec_raw: Dict[int, float] = rx.digraph_edge_betweenness_centrality(G, normalized=True)
    edge_idx_map = G.edge_index_map()   # {edge_idx: (src_idx, tgt_idx, payload)}

    result: Dict[str, float] = {}
    for edge_idx, score in ec_raw.items():
        entry = edge_idx_map.get(edge_idx)
        if entry is None:
            continue
        _, _, payload = entry
        rel_id = payload.get("rel_id") if isinstance(payload, dict) else None
        key = str(rel_id) if rel_id else str(edge_idx)
        result[key] = float(score)
    return result
```

### Step 4: Run the test to verify it passes

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/wave2/backend && \
  python -m pytest tests/test_edge_centrality.py -v
```
Expected: PASS (all tests green)

### Step 5: Gate

`compute_centrality` and `compute_edge_centrality` are importable; existing `test_graph_reason.py` still passes unchanged.

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/wave2/backend && \
  python -m pytest tests/test_graph_reason.py tests/test_edge_centrality.py -v
```

### Step 6: Commit

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/wave2 && \
git add backend/app/services/kg/graph_reason.py backend/tests/test_edge_centrality.py && \
git commit -m "feat(trackE): centrality helpers (node betweenness+pagerank, edge betweenness) added to graph_reason (T2)"
```

---

## Task 3: Schema migration + `review_queue` + `set_edge_review`

**Files:**
- Modify: `backend/app/services/sqlite_repository.py`
  - Schema migration: `ALTER TABLE knowledge_relations ADD COLUMN review_status TEXT NOT NULL DEFAULT 'pending'`
  - New method `review_queue(notebook_id, limit)` — computes trust + centrality, returns ranked list
  - New method `set_edge_review(notebook_id, rel_id, status)` — persists `review_status`
  - `_rx_graph`: filter out `review_status='rejected'` edges when building the cached graph
- Modify: `backend/app/models/schemas.py` — add `EdgeReviewItem`, `EdgeReviewRequest`
- Create: `backend/tests/test_edge_review_queue.py`

### Step 1: Write the failing tests

Create `backend/tests/test_edge_review_queue.py`:

```python
# backend/tests/test_edge_review_queue.py
"""Integration tests for edge review queue and feedback loop.
Uses a real SQLiteRepository (in-memory / tmp_path) with FakeEmbedder.
Synthetic graph with 4 nodes and 3 edges.
"""
import json
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL",  f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_PROVIDER",  "dashscope")
    monkeypatch.setenv("EMBED_BASE_URL",  "https://embedding.example.test")
    monkeypatch.setenv("EMBED_API_KEY",   "test-key")
    monkeypatch.setenv("EMBED_MODEL",     "test-model")
    monkeypatch.setenv("EMBED_DIM",       "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _seed_graph(repo) -> str:
    """Insert 4 KG nodes + 3 typed edges. Returns notebook_id."""
    nb = repo.create_notebook(NotebookCreate(name="test-nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "C1", "object_type": "Claim",
         "payload": {"name": "Claim Alpha"}, "evidence": []},
        {"local_id": "C2", "object_type": "Concept",
         "payload": {"name": "Concept Beta"}, "evidence": []},
        {"local_id": "F1", "object_type": "Formula",
         "payload": {"name": "Formula Gamma"}, "evidence": []},
        {"local_id": "P1", "object_type": "Procedure",
         "payload": {"name": "Procedure Delta"}, "evidence": []},
    ], [
        # Valid typed edge with evidence
        {"source_local_id": "C1", "target_local_id": "C2",
         "edge_type": "defines",
         "evidence": [{"file": "f1", "char_start": 0, "char_end": 10,
                       "line_start": 1, "line_end": 1, "quote": "alpha defines beta"}]},
        # Valid typed edge, NO evidence
        {"source_local_id": "F1", "target_local_id": "P1",
         "edge_type": "used_in", "evidence": []},
        # Type-violating edge (Claim→Procedure is not a valid pair for used_in)
        {"source_local_id": "C1", "target_local_id": "P1",
         "edge_type": "used_in", "evidence": []},
    ])
    return nb.id


# ── Schema migration ──────────────────────────────────────────────────────────

def test_review_status_column_exists(repo):
    """knowledge_relations must have a review_status column after migration."""
    nb_id = _seed_graph(repo)
    with repo._connect() as db:
        cols = [r["name"] for r in db.execute(
            "PRAGMA table_info(knowledge_relations)").fetchall()]
    assert "review_status" in cols


# ── review_queue ──────────────────────────────────────────────────────────────

def test_review_queue_returns_list(repo):
    nb_id = _seed_graph(repo)
    q = repo.review_queue(nb_id)
    assert isinstance(q, list)
    assert len(q) >= 1


def test_review_queue_items_have_required_fields(repo):
    nb_id = _seed_graph(repo)
    q = repo.review_queue(nb_id)
    for item in q:
        assert "rel_id" in item
        assert "trust_score" in item
        assert "edge_centrality" in item
        assert "review_priority" in item
        assert "review_status" in item
        assert "edge_type" in item
        assert 0.0 <= item["trust_score"] <= 1.0
        assert item["review_priority"] >= 0.0


def test_review_queue_type_violating_edge_lower_trust(repo):
    """The type-violating edge (Claim→Procedure used_in) should have lower
    trust_score than the correctly-typed, evidenced edge (Claim→Concept defines)."""
    nb_id = _seed_graph(repo)
    q = repo.review_queue(nb_id)
    by_type = {item["edge_type"] + "|" + item.get("source_name", "") + "|" + item.get("target_name", ""): item
               for item in q}
    # Find the defines edge (valid + evidence) and the invalid used_in edge
    defines_item = next((i for i in q if i["edge_type"] == "defines"), None)
    # Both used_in edges — pick the one from Claim (type-violating)
    invalid_used_in = next(
        (i for i in q if i["edge_type"] == "used_in" and
         i.get("source_type") == "Claim"), None)
    if defines_item and invalid_used_in:
        assert defines_item["trust_score"] > invalid_used_in["trust_score"]


def test_review_queue_sorted_by_priority_desc(repo):
    """Items are sorted by review_priority descending (highest-risk first)."""
    nb_id = _seed_graph(repo)
    q = repo.review_queue(nb_id)
    priorities = [item["review_priority"] for item in q]
    assert priorities == sorted(priorities, reverse=True)


def test_review_queue_excludes_rejected(repo):
    """After marking an edge rejected, it must not appear in the review queue."""
    nb_id = _seed_graph(repo)
    q_before = repo.review_queue(nb_id)
    assert q_before, "need at least one edge"
    rel_id = q_before[0]["rel_id"]
    repo.set_edge_review(nb_id, rel_id, "rejected")
    q_after = repo.review_queue(nb_id)
    assert all(item["rel_id"] != rel_id for item in q_after)


# ── set_edge_review ───────────────────────────────────────────────────────────

def test_set_edge_review_persists_status(repo):
    nb_id = _seed_graph(repo)
    q = repo.review_queue(nb_id)
    rel_id = q[0]["rel_id"]
    repo.set_edge_review(nb_id, rel_id, "verified")
    with repo._connect() as db:
        row = db.execute(
            "SELECT review_status FROM knowledge_relations WHERE id=?", (rel_id,)
        ).fetchone()
    assert row["review_status"] == "verified"


def test_set_edge_review_invalid_status_raises(repo):
    nb_id = _seed_graph(repo)
    q = repo.review_queue(nb_id)
    rel_id = q[0]["rel_id"]
    with pytest.raises(ValueError, match="review_status"):
        repo.set_edge_review(nb_id, rel_id, "bogus_status")


# ── Feedback loop: rejected edges demoted in graph ───────────────────────────

def test_rejected_edge_excluded_from_rx_graph(repo):
    """A rejected edge must not appear in the version-cached PyDiGraph used by reasoning."""
    nb_id = _seed_graph(repo)
    q = repo.review_queue(nb_id)
    rel_id = q[0]["rel_id"]
    repo.set_edge_review(nb_id, rel_id, "rejected")
    G, idx_to_oid, oid_to_idx = repo._rx_graph(nb_id)
    # Collect all rel_ids from the live graph
    edge_rel_ids = set()
    for src_idx in range(G.num_nodes()):
        for tgt_idx in G.successor_indices(src_idx):
            payload = G.get_edge_data(src_idx, tgt_idx)
            if isinstance(payload, dict):
                edge_rel_ids.add(payload.get("rel_id", ""))
    assert rel_id not in edge_rel_ids, (
        f"rejected edge {rel_id} must not appear in the reasoning graph")


def test_verified_edge_remains_in_rx_graph(repo):
    """A verified edge must still appear in the reasoning graph."""
    nb_id = _seed_graph(repo)
    q = repo.review_queue(nb_id)
    rel_id = q[0]["rel_id"]
    repo.set_edge_review(nb_id, rel_id, "verified")
    G, idx_to_oid, oid_to_idx = repo._rx_graph(nb_id)
    edge_rel_ids = set()
    for src_idx in range(G.num_nodes()):
        for tgt_idx in G.successor_indices(src_idx):
            payload = G.get_edge_data(src_idx, tgt_idx)
            if isinstance(payload, dict):
                edge_rel_ids.add(payload.get("rel_id", ""))
    assert rel_id in edge_rel_ids


def test_verify_chain_edges_skips_rejected(repo):
    """verify_chain_edges in ask_graph: a subgraph traversal on a graph where a
    rejected edge has been excluded should not include that edge at all."""
    nb_id = _seed_graph(repo)
    q = repo.review_queue(nb_id)
    # Reject the first edge
    rel_id = q[0]["rel_id"]
    repo.set_edge_review(nb_id, rel_id, "rejected")
    # Traverse the graph — rejected edge should not appear in any subgraph
    from app.services.kg.graph_reason import multihop_subgraph, DEFAULT_REASONING_EDGES
    G, idx_to_oid, oid_to_idx = repo._rx_graph(nb_id)
    all_oids = list(oid_to_idx.keys())
    sub = multihop_subgraph(G, oid_to_idx, idx_to_oid,
                            seed_ids=all_oids[:1],
                            edge_types=DEFAULT_REASONING_EDGES,
                            max_depth=3, max_fan_out=10)
    sub_rel_ids = {e["rel_id"] for _, e, _ in sub if e and "rel_id" in e}
    assert rel_id not in sub_rel_ids
```

### Step 2: Run the test to verify it fails

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/wave2/backend && \
  python -m pytest tests/test_edge_review_queue.py -v 2>&1 | head -30
```
Expected: FAIL — `review_status` column missing, `review_queue` / `set_edge_review` not found.

### Step 3: Write minimal implementation

**3a. `backend/app/models/schemas.py`** — append after the `KnowledgeGraph` class (around line 350):

```python
class EdgeReviewItem(BaseModel):
    """One item in the edge curation review queue."""
    rel_id: str
    notebook_id: str
    edge_type: str
    source_object_id: str
    target_object_id: str
    source_name: str = ""
    target_name: str = ""
    source_type: str = ""
    target_type: str = ""
    trust_score: float
    edge_centrality: float
    review_priority: float
    review_status: str = "pending"


class EdgeReviewRequest(BaseModel):
    """Payload for POST /relations/{rel_id}/review."""
    status: str   # "verified" | "rejected" | "pending"
```

**3b. `backend/app/services/sqlite_repository.py`**

In the `__init__` / schema-init block, add after the existing `ALTER TABLE` migration guards (around line 520):

```python
            # Track E: edge review_status column
            kr_cols = {r["name"] for r in db.execute(
                "PRAGMA table_info(knowledge_relations)").fetchall()}
            if "review_status" not in kr_cols:
                db.execute(
                    "ALTER TABLE knowledge_relations "
                    "ADD COLUMN review_status TEXT NOT NULL DEFAULT 'pending'"
                )
```

Add two new methods to `SQLiteRepository` (place near `relations_for_notebook`, around line 1985):

```python
    _REVIEW_STATUSES = frozenset({"pending", "verified", "rejected"})

    def review_queue(self, notebook_id: str, limit: int = 200) -> List[dict]:
        """Return edges ranked by review priority = edge_centrality * (1 - trust_score).

        Only edges with review_status != 'rejected' are included (rejected edges are
        excluded from reasoning and need no further review).
        Centrality is computed over the FULL graph (including non-rejected edges).
        trust_score combines evidence anchoring + cross-doc corroboration + type validity.
        """
        import json as _json
        from app.services.kg.edge_trust import (
            compute_trust_score, corroboration_counts,
            corroboration_score_from_count,
        )
        from app.services.kg.graph_reason import build_rx_graph, compute_edge_centrality

        self.get_notebook(notebook_id)
        with self._connect() as db:
            rel_rows = db.execute(
                "SELECT kr.id, kr.source_object_id, kr.target_object_id, "
                "kr.edge_type, kr.evidence, kr.source_id, kr.review_status, "
                "ko_s.object_type AS src_type, ko_s.payload AS src_payload, "
                "ko_t.object_type AS tgt_type, ko_t.payload AS tgt_payload "
                "FROM knowledge_relations kr "
                "LEFT JOIN knowledge_objects ko_s ON ko_s.id = kr.source_object_id "
                "LEFT JOIN knowledge_objects ko_t ON ko_t.id = kr.target_object_id "
                "WHERE kr.notebook_id = ? AND kr.review_status != 'rejected'",
                (notebook_id,),
            ).fetchall()
            # Build node types + names for trust signals
            obj_rows = db.execute(
                "SELECT id, object_type, payload FROM knowledge_objects "
                "WHERE notebook_id = ?", (notebook_id,)
            ).fetchall()

        node_types: dict = {}
        node_names: dict = {}
        for r in obj_rows:
            node_types[r["id"]] = r["object_type"]
            p = _json.loads(r["payload"] or "{}")
            node_names[r["id"]] = p.get("name", "")

        rels = []
        for r in rel_rows:
            rels.append({
                "id": r["id"],
                "source_object_id": r["source_object_id"],
                "target_object_id": r["target_object_id"],
                "edge_type": r["edge_type"],
                "evidence": _json.loads(r["evidence"] or "[]"),
                "source_id": r["source_id"],
                "review_status": r["review_status"],
                "_src_type": r["src_type"] or "",
                "_tgt_type": r["tgt_type"] or "",
                "_src_name": node_names.get(r["source_object_id"], ""),
                "_tgt_name": node_names.get(r["target_object_id"], ""),
            })

        # Corroboration counts (batched over all edges)
        corr_counts = corroboration_counts(rels, node_names)

        # Edge centrality from the live graph (non-rejected edges only)
        G, idx_to_oid, oid_to_idx = build_rx_graph(
            {oid: {"type": t, "name": node_names.get(oid, "")}
             for oid, t in node_types.items()},
            rels,
        )
        edge_centrality = compute_edge_centrality(G)

        items = []
        for rel in rels:
            rid = rel["id"]
            corr_score = corroboration_score_from_count(corr_counts.get(rid, 1))
            trust = compute_trust_score(rel, node_types, corr_score)
            ec = edge_centrality.get(rid, 0.0)
            # review_priority = high centrality × low trust
            priority = ec * (1.0 - trust)
            src_payload = {}
            tgt_payload = {}
            items.append({
                "rel_id": rid,
                "notebook_id": notebook_id,
                "edge_type": rel["edge_type"],
                "source_object_id": rel["source_object_id"],
                "target_object_id": rel["target_object_id"],
                "source_name": rel["_src_name"],
                "target_name": rel["_tgt_name"],
                "source_type": rel["_src_type"],
                "target_type": rel["_tgt_type"],
                "trust_score": trust,
                "edge_centrality": ec,
                "review_priority": priority,
                "review_status": rel["review_status"],
            })

        items.sort(key=lambda x: x["review_priority"], reverse=True)
        return items[:limit]

    def set_edge_review(self, notebook_id: str, rel_id: str, status: str) -> None:
        """Persist review_status on a knowledge_relation.

        Allowed statuses: 'pending', 'verified', 'rejected'.
        Raises ValueError for unknown statuses.
        Raises KeyError if the relation does not exist in this notebook.
        Invalidates the _rx_graph cache so the next graph-reasoning call sees
        the updated set of active edges.
        """
        if status not in self._REVIEW_STATUSES:
            raise ValueError(
                f"review_status must be one of {sorted(self._REVIEW_STATUSES)}, got {status!r}")
        with self._write() as db:
            cur = db.execute(
                "UPDATE knowledge_relations SET review_status=? "
                "WHERE id=? AND notebook_id=?",
                (status, rel_id, notebook_id),
            )
            if cur.rowcount == 0:
                raise KeyError(f"relation {rel_id!r} not found in notebook {notebook_id!r}")
        # Invalidate cached graph so _rx_graph rebuilds on next access
        self._invalidate_unified_cache(notebook_id)
```

Modify `_rx_graph` (`sqlite_repository.py:3157-3162`) to filter rejected edges:

The existing query is:
```python
rel_rows = db.execute(
    "SELECT id, source_object_id, target_object_id, edge_type, evidence "
    "FROM knowledge_relations WHERE notebook_id = ?",
    (notebook_id,),
).fetchall()
```
Replace with (add `AND review_status != 'rejected'` if the column exists — guard via the migration):
```python
rel_rows = db.execute(
    "SELECT id, source_object_id, target_object_id, edge_type, evidence "
    "FROM knowledge_relations "
    "WHERE notebook_id = ? AND COALESCE(review_status, 'pending') != 'rejected'",
    (notebook_id,),
).fetchall()
```

Also update the version key at `sqlite_repository.py:3139-3144` so that a status change invalidates the cached graph:
```python
ver = db.execute(
    "SELECT COUNT(*) AS c, COALESCE(MAX(created_at), '') AS ts, "
    "COALESCE(MAX(review_status), '') AS rs "
    "FROM knowledge_relations WHERE notebook_id = ?",
    (notebook_id,),
).fetchone()
version = ("rxgraph", ver["c"], ver["ts"], ver["rs"])
```

### Step 4: Run the test to verify it passes

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/wave2/backend && \
  python -m pytest tests/test_edge_review_queue.py -v
```
Expected: PASS (all tests green)

### Step 5: Gate

All `test_edge_review_queue.py` tests pass. Existing graph-reason and edge-trust tests still pass.

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/wave2/backend && \
  python -m pytest tests/test_edge_trust.py tests/test_edge_centrality.py \
    tests/test_edge_review_queue.py tests/test_graph_reason.py -v
```

### Step 6: Commit

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/wave2 && \
git add \
  backend/app/models/schemas.py \
  backend/app/services/sqlite_repository.py \
  backend/tests/test_edge_review_queue.py && \
git commit -m "feat(trackE): review_queue + set_edge_review + rejected-edge demotion in _rx_graph (T3)"
```

---

## Task 4: API endpoints — review queue surface + feedback

**Files:**
- Modify: `backend/app/api/routes.py`

No new tests needed beyond what Task 3 already covers (the API is a thin wrapper). Add a smoke-test assertion in Task 3's integration test if convenient, but is not gated.

### Step 1: Add routes

In `backend/app/api/routes.py`, after the existing `/notebooks/{notebook_id}/derived-rules` block (around line 521):

```python
# ---------------------------------------------------------------------------
# Edge trust & curation (Track E)
# ---------------------------------------------------------------------------

@router.get("/notebooks/{notebook_id}/edge-review-queue")
def edge_review_queue(notebook_id: str, limit: int = 100) -> list:
    """Return edges ranked by review priority (high centrality × low trust) desc.
    Excludes already-rejected edges.
    """
    try:
        return repository().review_queue(notebook_id, limit=limit)
    except KeyError:
        raise HTTPException(status_code=404, detail="Notebook not found")


@router.post("/notebooks/{notebook_id}/relations/{rel_id}/review", status_code=200)
def review_relation(notebook_id: str, rel_id: str,
                    payload: "EdgeReviewRequest") -> dict:
    """Mark an edge as 'verified', 'rejected', or 'pending'.
    Rejected edges are excluded from all future graph-reasoning traversals.
    """
    try:
        repository().set_edge_review(notebook_id, rel_id, payload.status)
        return {"rel_id": rel_id, "review_status": payload.status}
    except KeyError as exc:
        raise HTTPException(status_code=404, detail=str(exc))
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
```

Add the import to the `from app.models.schemas import (...)` block at the top of `routes.py`:
```python
    EdgeReviewItem,
    EdgeReviewRequest,
```

### Step 2: Verify routes resolve

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/wave2/backend && \
  python -c "from app.api.routes import router; print('routes OK')"
```
Expected: `routes OK`

### Step 3: Gate

No new test failures. All four test files pass.

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/wave2/backend && \
  python -m pytest tests/test_edge_trust.py tests/test_edge_centrality.py \
    tests/test_edge_review_queue.py tests/test_graph_reason.py -q
```

### Step 4: Commit

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/wave2 && \
git add backend/app/api/routes.py backend/app/models/schemas.py && \
git commit -m "feat(trackE): API endpoints — edge-review-queue + relation review (T4)"
```

---

## Final Phase Gate

Run the full test suite from `backend/`:

```bash
cd /Users/hzf/workspace/silicon_notebook/.claude/worktrees/wave2/backend && \
  python -m pytest -q
```

**Gate criteria (all must be green):**

1. `trust_score` is computed from all three signals (evidence anchoring, cross-doc corroboration, type-constraint validity); the formula in `edge_trust.py` matches the documented weights `0.4/0.3/0.3`.
2. `compute_centrality` and `compute_edge_centrality` correctly rank backbone edges (middle edges in a linear chain have higher betweenness than leaf edges).
3. `review_queue` returns edges sorted by `review_priority = edge_centrality × (1 - trust_score)` descending; type-violating/unevidenced edges sort higher than correctly-typed evidenced edges.
4. A `set_edge_review(…, 'rejected')` call causes the edge to be absent from the next `_rx_graph` result and from any `multihop_subgraph` traversal on that graph.
5. Existing `test_graph_reason.py` (Track C tests) all pass unchanged — no regression in `build_rx_graph`, `multihop_subgraph`, `verify_chain_edges`, or the cache-isolation invariants.
6. Full `pytest -q` green (zero failures, zero errors).
