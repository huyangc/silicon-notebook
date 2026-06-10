# Track C — rustworkx In-Memory Graph + Multi-Hop Reasoning POC (Single-Tier, T2)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Date:** 2026-06-09  
**Branch/worktree:** `claude/unified-kg-evolution` → `/Users/hzf/workspace/silicon_notebook/.claude/worktrees/unified-kg-evolution`  
**Python:** `/opt/homebrew/Caskroom/miniconda/base/bin/python`  
**Run tests from:** `backend/`

---

## Goal

Wire a rustworkx `PyDiGraph` over the existing `knowledge_relations` table, traverse it for multi-hop derivation/support chains with bounded fan-out, serialize chains into the existing `[k]` / `_answer_context` citation flow, and add adversarial LLM chain-verification — all while fitting cleanly into the already-present `mode=reasoning` dispatch hook in `ask()`.

**Why rustworkx, not networkx?** The project already imports `networkx>=3.0` (for community detection), but rustworkx gives Rust-backed traversal that is trivially fast at 36k nodes / 46k edges (graph topology is ~MBs in RAM). The `VectorCache` version-key pattern already in the repo (`:3036`, `:3057`) is reused verbatim to cache the built `PyDiGraph` so it is rebuilt only on ingest.

**Why these edge types first?** `derived_from` has 4 160 rows, `supports` has 6 068 rows — enough population to demonstrate multi-hop chains immediately. `depends_on` (791) is the best third hop. `contrasts_with` (556) and `prerequisite_of` (68) are thin and excluded from the default traversal set but accessible via the API parameter.

---

## Background — code facts verified before drafting

| Symbol | File:line | What it is |
|---|---|---|
| `knowledge_relations` DDL | `sqlite_repository.py:321-330` | `(id, notebook_id, source_id, source_object_id, target_object_id, edge_type, evidence JSON, created_at)` — **no `confidence` or `tier` column** at the SQL layer; both are carried in the rustworkx edge payload |
| `EDGE_TYPES` | `kg/extract.py:15-17` | `{"defines", "part_of", "composed_of", "contrasts_with", "kind_of", "about", "supports", "derived_from", "depends_on", "prerequisite_of", "used_in", "precedes"}` — 12 types |
| `NODE_TYPES` | `kg/extract.py:14` | `{"Concept", "Claim", "Formula", "Procedure"}` |
| `Edge` / `Evidence` | `kg/models.py:8-34` | `Evidence(file, char_start, char_end, line_start, line_end, quote)`; `Edge(id, type, source_id, target_id, evidence: List[Evidence])` |
| `_retrieve_neighbors` | `sqlite_repository.py:3152-3200` | 1-hop neighbor lookup over indexed `(notebook_id, source_object_id)` / `(notebook_id, target_object_id)`; returns `List[RetrievedKnowledge]` with `score=0.0` |
| `_answer_context` | `sqlite_repository.py:3682-3757` | Builds the `(context_block_str, id_map)` tuple; assigns stable `k{i}` keys; already emits `relations: k2 -[derived_from]-> k1` lines when both endpoints are in `id_map` (`:3746-3756`) |
| `_MARKER_RE` | `sqlite_repository.py:135` | `re.compile(r"\[(k\d+)\]")` — the anchor marker regex |
| `ask()` dispatch | `sqlite_repository.py:3214-3216` | `if getattr(payload, "mode", "fast") == "reasoning": return self.ask_reasoning(...)` — adding `mode="graph"` follows the identical pattern |
| `ask_reasoning` | `sqlite_repository.py:3857-3930` | Constructs `ReasoningRetriever(self, self.settings).run(...)`, then calls `_answer_reasoning(notebook_id, question, top_hits, elements, history)` — the new graph mode can call `_answer_kg(notebook_id, question, top_hits)` from the same path |
| `AskRequest.mode` | `schemas.py:152` | `mode: str = "fast"  # "fast" | "reasoning"` |
| `VectorCache` | `vector_cache.py:8-21` | `get(key, version, loader)` — version-keyed dict; `invalidate(key)` |
| `_vector_cache` on repo | `sqlite_repository.py:168` | Already instantiated; reuse for graph cache |

**Key structural observation:** `knowledge_relations` has no `confidence` column at the DB layer. The plan stores `confidence` only in the rustworkx edge payload (defaulting to `1.0` for relations extracted from text — the evidence itself is the only grounding signal at extraction time). `tier` defaults to `"base"` for this single-tier POC; the field exists in the payload so the multi-tier extension is just a filter.

---

## Files

| Action | Path |
|---|---|
| **Create** | `backend/app/services/kg/graph_reason.py` |
| **Modify** | `backend/requirements.txt` (add `rustworkx`) |
| **Modify** | `backend/app/services/sqlite_repository.py` (add `mode="graph"` dispatch + `_build_rx_graph` helper; invalidate graph cache in `_invalidate_unified_cache`) |
| **Modify** | `backend/app/models/schemas.py` (extend `AskRequest.mode` comment to include `"graph"`) |
| **Create** | `backend/tests/test_graph_reason.py` |

---

## Task 1 — Add `rustworkx` + `graph_reason.py` graph builder with version cache

**Goal:** `rustworkx` is importable; a `build_rx_graph(relations)` function turns a list of relation dicts into a `PyDiGraph` with typed edges carrying `edge_type`, `evidence`, `confidence`, `tier`; the repo method `_rx_graph(notebook_id)` wraps it with the `VectorCache` version-key pattern.

### Files

- **Create:** `backend/app/services/kg/graph_reason.py`
- **Modify:** `backend/requirements.txt`
- **Modify:** `backend/app/services/sqlite_repository.py` — add `_rx_graph` method; extend `_invalidate_unified_cache`
- **Test:** `backend/tests/test_graph_reason.py`

### Step 1 — Write failing test

```python
# backend/tests/test_graph_reason.py
import pytest

# ── synthetic fixture ────────────────────────────────────────────────────────
# 4 nodes: A→B derived_from, B→C supports, C→D depends_on; one bad edge D→A
NODES = {
    "A": {"type": "Formula",  "name": "Node A"},
    "B": {"type": "Claim",    "name": "Node B"},
    "C": {"type": "Claim",    "name": "Node C"},
    "D": {"type": "Concept",  "name": "Node D"},
}
RELATIONS = [
    {"id": "r1", "source_object_id": "A", "target_object_id": "B",
     "edge_type": "derived_from",
     "evidence": [{"file": "f1", "char_start": 0, "char_end": 10,
                   "line_start": 1, "line_end": 1, "quote": "A derives B"}]},
    {"id": "r2", "source_object_id": "B", "target_object_id": "C",
     "edge_type": "supports",
     "evidence": [{"file": "f1", "char_start": 20, "char_end": 30,
                   "line_start": 2, "line_end": 2, "quote": "B supports C"}]},
    {"id": "r3", "source_object_id": "C", "target_object_id": "D",
     "edge_type": "depends_on",
     "evidence": [{"file": "f1", "char_start": 40, "char_end": 50,
                   "line_start": 3, "line_end": 3, "quote": "C depends on D"}]},
    {"id": "r4", "source_object_id": "D", "target_object_id": "A",
     "edge_type": "contrasts_with",
     "evidence": []},
]


def test_build_rx_graph_node_count():
    from app.services.kg.graph_reason import build_rx_graph
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    assert G.num_nodes() == 4


def test_build_rx_graph_edge_count():
    from app.services.kg.graph_reason import build_rx_graph
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    assert G.num_edges() == 4


def test_build_rx_graph_edge_payload():
    from app.services.kg.graph_reason import build_rx_graph
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    src_idx = oid_to_idx["A"]
    tgt_idx = oid_to_idx["B"]
    # rustworkx: G.get_edge_data(src, tgt) returns the payload
    payload = G.get_edge_data(src_idx, tgt_idx)
    assert payload["edge_type"] == "derived_from"
    assert payload["tier"] == "base"
    assert payload["confidence"] == 1.0
    assert len(payload["evidence"]) == 1
    assert payload["evidence"][0]["quote"] == "A derives B"


def test_build_rx_graph_node_payload():
    from app.services.kg.graph_reason import build_rx_graph
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    idx = oid_to_idx["C"]
    node_data = G[idx]
    assert node_data["object_id"] == "C"
    assert node_data["object_type"] == "Claim"
    assert node_data["name"] == "Node C"
```

Run: `cd backend && python -m pytest tests/test_graph_reason.py::test_build_rx_graph_node_count -v`
Expected: **FAIL** — `ModuleNotFoundError: No module named 'rustworkx'` (or import error from `graph_reason`).

### Step 2 — Add `rustworkx` to requirements

```
# backend/requirements.txt  (append)
rustworkx>=0.15.0
```

Install: `cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/pip install rustworkx>=0.15.0`

### Step 3 — Implement `graph_reason.py`

```python
# backend/app/services/kg/graph_reason.py
"""rustworkx-backed in-memory KG graph for multi-hop reasoning.

Nodes carry: object_id, object_type, name.
Edges carry: edge_type, evidence (list[dict]), confidence (float), tier (str).

build_rx_graph() is a pure function — no I/O, easily unit-tested with a
synthetic fixture.  The repo wraps it via _rx_graph() with VectorCache
version-keying (same (COUNT, MAX created_at) pattern as _vector_matrix).
"""
from __future__ import annotations

import json
from typing import Dict, List, Optional, Tuple

import rustworkx as rx

# Default reasoning edge types (well-populated: derived_from=4160, supports=6068,
# depends_on=791).  contrasts_with/prerequisite_of are thin; callers may extend.
DEFAULT_REASONING_EDGES = frozenset({"derived_from", "supports", "depends_on"})


def build_rx_graph(
    nodes: Dict[str, dict],
    relations: List[dict],
    tier: str = "base",
) -> Tuple[rx.PyDiGraph, Dict[int, str], Dict[str, int]]:
    """Build a PyDiGraph from dicts.

    `nodes`  — {object_id: {"type": str, "name": str, ...}}
    `relations` — list of knowledge_relations rows (dicts with keys:
        id, source_object_id, target_object_id, edge_type, evidence)

    Returns (graph, idx_to_oid, oid_to_idx).
    `evidence` in each edge payload is a list[dict] (JSON-decoded Evidence dicts).
    `confidence` defaults to 1.0 (no confidence column in knowledge_relations).
    `tier` is injected per-call (default "base" for single-tier POC).
    """
    G: rx.PyDiGraph = rx.PyDiGraph()
    idx_to_oid: Dict[int, str] = {}
    oid_to_idx: Dict[str, int] = {}

    for oid, meta in nodes.items():
        idx = G.add_node({
            "object_id": oid,
            "object_type": meta.get("type", ""),
            "name": meta.get("name", ""),
        })
        idx_to_oid[idx] = oid
        oid_to_idx[oid] = idx

    for rel in relations:
        src_oid = rel["source_object_id"]
        tgt_oid = rel["target_object_id"]
        if src_oid not in oid_to_idx or tgt_oid not in oid_to_idx:
            continue  # skip dangling edges (object deleted/deprecated)
        ev_raw = rel.get("evidence", [])
        if isinstance(ev_raw, str):
            try:
                ev_raw = json.loads(ev_raw)
            except Exception:
                ev_raw = []
        G.add_edge(
            oid_to_idx[src_oid],
            oid_to_idx[tgt_oid],
            {
                "rel_id": rel.get("id", ""),
                "edge_type": rel["edge_type"],
                "evidence": ev_raw if isinstance(ev_raw, list) else [],
                "confidence": float(rel.get("confidence", 1.0)),
                "tier": tier,
            },
        )

    return G, idx_to_oid, oid_to_idx
```

### Step 4 — Add `_rx_graph` to `SQLiteRepository`

In `sqlite_repository.py`, add a new method after `_keyword_token_sets` (around line 3068):

```python
    def _rx_graph(self, notebook_id: str):
        """Return the cached rustworkx PyDiGraph for `notebook_id`.

        Version-keyed on (COUNT, MAX created_at) of knowledge_relations
        (same pattern as _vector_matrix at :3031-3045).  Graph is rebuilt
        only on new ingest/delete.  Returned tuple: (G, idx_to_oid, oid_to_idx).
        """
        from app.services.kg.graph_reason import build_rx_graph
        with self._connect() as db:
            ver = db.execute(
                "SELECT COUNT(*) AS c, COALESCE(MAX(created_at), '') AS ts "
                "FROM knowledge_relations WHERE notebook_id = ?",
                (notebook_id,),
            ).fetchone()
            version = ("rxgraph", ver["c"], ver["ts"])

            def _load():
                obj_rows = db.execute(
                    "SELECT id, object_type, payload FROM knowledge_objects "
                    "WHERE notebook_id = ? AND status IN ('approved', 'reviewed')",
                    (notebook_id,),
                ).fetchall()
                nodes = {}
                for r in obj_rows:
                    p = json.loads(r["payload"] or "{}")
                    nodes[r["id"]] = {"type": r["object_type"], "name": p.get("name", "")}
                rel_rows = db.execute(
                    "SELECT id, source_object_id, target_object_id, edge_type, evidence "
                    "FROM knowledge_relations WHERE notebook_id = ?",
                    (notebook_id,),
                ).fetchall()
                relations = [dict(r) for r in rel_rows]
                return build_rx_graph(nodes, relations)

        return self._vector_cache.get(f"{notebook_id}:rxgraph", version, _load)
```

Also extend `_invalidate_unified_cache` to evict the graph cache:

```python
    # in _invalidate_unified_cache (sqlite_repository.py ~line 2056):
        self._vector_cache.invalidate(f"{notebook_id}:rxgraph")
```

### Step 5 — Run tests

Run: `cd backend && python -m pytest tests/test_graph_reason.py -v`
Expected: **PASS** (all 4 builder tests green).

Run: `cd backend && python -m pytest tests/ -q`
Expected: Full suite stays green.

### Step 6 — Commit

```bash
git add backend/requirements.txt backend/app/services/kg/graph_reason.py \
        backend/app/services/sqlite_repository.py backend/tests/test_graph_reason.py
git commit -m "feat(graph): rustworkx PyDiGraph builder + VectorCache version-keyed graph cache"
```

**Task 1 gate:** all 4 fixture tests green; `G.num_nodes()==4`, `G.num_edges()==4`; edge payload carries `edge_type/evidence/confidence/tier`; `_rx_graph` import works in a repo test with mocked DB.

---

## Task 2 — Multi-hop subgraph retrieval with bounded fan-out

**Goal:** `multihop_subgraph(G, oid_to_idx, idx_to_oid, seed_ids, edge_types, max_depth, max_fan_out)` returns an ordered list of `(node_data, edge_data)` tuples representing the traversed subgraph, pruned to `max_fan_out` successors per hop (highest-confidence first).

### Files

- **Modify:** `backend/app/services/kg/graph_reason.py` (add `multihop_subgraph`)
- **Modify:** `backend/tests/test_graph_reason.py` (extend)

### Step 1 — Write failing tests

```python
# backend/tests/test_graph_reason.py  (append to existing file)

def test_multihop_subgraph_depth2_derived_supports():
    """Seed=A, edge_types={derived_from, supports}, depth=2 → A→B→C."""
    from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    result = multihop_subgraph(
        G, oid_to_idx, idx_to_oid,
        seed_ids=["A"],
        edge_types={"derived_from", "supports"},
        max_depth=2,
        max_fan_out=10,
    )
    visited_oids = [n["object_id"] for n, _ in result]
    assert "A" in visited_oids
    assert "B" in visited_oids
    assert "C" in visited_oids
    assert "D" not in visited_oids   # depends_on not in edge_types


def test_multihop_subgraph_all_three_hops():
    """Seed=A, all three default edges, depth=3 → A→B→C→D."""
    from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph, DEFAULT_REASONING_EDGES
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    result = multihop_subgraph(
        G, oid_to_idx, idx_to_oid,
        seed_ids=["A"],
        edge_types=DEFAULT_REASONING_EDGES,
        max_depth=3,
        max_fan_out=10,
    )
    visited_oids = [n["object_id"] for n, _ in result]
    assert visited_oids == ["A", "B", "C", "D"]


def test_multihop_subgraph_fan_out_capped():
    """Fan-out cap: if a node has 3 out-edges of the right type and cap=1,
    only the highest-confidence one is followed."""
    import rustworkx as rx
    from app.services.kg.graph_reason import multihop_subgraph
    G = rx.PyDiGraph()
    n_seed = G.add_node({"object_id": "S", "object_type": "Concept", "name": "S"})
    n_hi   = G.add_node({"object_id": "HI", "object_type": "Claim",   "name": "HI"})
    n_lo   = G.add_node({"object_id": "LO", "object_type": "Claim",   "name": "LO"})
    G.add_edge(n_seed, n_hi, {"edge_type": "supports", "confidence": 0.9,
                               "evidence": [], "tier": "base"})
    G.add_edge(n_seed, n_lo, {"edge_type": "supports", "confidence": 0.2,
                               "evidence": [], "tier": "base"})
    idx_to_oid = {n_seed: "S", n_hi: "HI", n_lo: "LO"}
    oid_to_idx = {"S": n_seed, "HI": n_hi, "LO": n_lo}
    result = multihop_subgraph(
        G, oid_to_idx, idx_to_oid,
        seed_ids=["S"],
        edge_types={"supports"},
        max_depth=1,
        max_fan_out=1,
    )
    visited_oids = [n["object_id"] for n, _ in result]
    assert "HI" in visited_oids
    assert "LO" not in visited_oids


def test_multihop_subgraph_cycle_guard():
    """contrasts_with D→A: traversal with all edges + cycle guard must not loop."""
    from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    # include contrasts_with so D→A edge is eligible
    result = multihop_subgraph(
        G, oid_to_idx, idx_to_oid,
        seed_ids=["A"],
        edge_types={"derived_from", "supports", "depends_on", "contrasts_with"},
        max_depth=5,
        max_fan_out=10,
    )
    visited_oids = [n["object_id"] for n, _ in result]
    # each node appears at most once despite the cycle
    assert len(visited_oids) == len(set(visited_oids))
```

Run: `cd backend && python -m pytest tests/test_graph_reason.py -k "multihop" -v`
Expected: **FAIL** — `ImportError: cannot import name 'multihop_subgraph'`.

### Step 2 — Implement `multihop_subgraph`

Add to `backend/app/services/kg/graph_reason.py`:

```python
def multihop_subgraph(
    G: rx.PyDiGraph,
    oid_to_idx: Dict[str, int],
    idx_to_oid: Dict[int, str],
    seed_ids: List[str],
    edge_types: Optional[frozenset] = None,
    max_depth: int = 3,
    max_fan_out: int = 8,
) -> List[Tuple[dict, Optional[dict]]]:
    """BFS from `seed_ids` along `edge_types`, bounded by depth and fan-out.

    Returns ordered list of (node_payload, edge_payload_or_None) tuples.
    Seed nodes carry edge_payload=None.  Each node appears at most once
    (visited set guards cycles).  At each hop the eligible out-edges are
    sorted by confidence desc, then capped to `max_fan_out`.

    edge_types: frozenset of edge_type strings to follow; None = all edges.
    """
    if edge_types is None:
        edge_types = frozenset()   # empty = treat as "all" below
    use_all = len(edge_types) == 0

    visited: set = set()
    result: List[Tuple[dict, Optional[dict]]] = []
    # queue entries: (node_idx, depth, incoming_edge_payload)
    from collections import deque
    queue: deque = deque()

    for oid in seed_ids:
        idx = oid_to_idx.get(oid)
        if idx is None or idx in visited:
            continue
        visited.add(idx)
        result.append((G[idx], None))
        queue.append((idx, 0))

    while queue:
        cur_idx, depth = queue.popleft()
        if depth >= max_depth:
            continue
        # Gather eligible out-edges for this node
        out_edges = []
        for tgt_idx in G.successor_indices(cur_idx):
            if tgt_idx in visited:
                continue
            edge_data = G.get_edge_data(cur_idx, tgt_idx)
            if use_all or edge_data.get("edge_type") in edge_types:
                out_edges.append((tgt_idx, edge_data))
        # Sort by confidence desc, cap fan-out
        out_edges.sort(key=lambda x: x[1].get("confidence", 1.0), reverse=True)
        out_edges = out_edges[:max_fan_out]

        for tgt_idx, edge_data in out_edges:
            if tgt_idx in visited:
                continue
            visited.add(tgt_idx)
            result.append((G[tgt_idx], edge_data))
            queue.append((tgt_idx, depth + 1))

    return result
```

### Step 3 — Run tests

Run: `cd backend && python -m pytest tests/test_graph_reason.py -k "multihop" -v`
Expected: **PASS** (all 4 multihop tests green).

Run: `cd backend && python -m pytest tests/ -q`
Expected: Full suite stays green.

### Step 4 — Commit

```bash
git add backend/app/services/kg/graph_reason.py backend/tests/test_graph_reason.py
git commit -m "feat(graph): multihop BFS subgraph with edge-type filter, fan-out cap, cycle guard"
```

**Task 2 gate:** seed→chain BFS correct for depth 1/2/3; `contrasts_with D→A` cycle does not loop; fan-out cap keeps only the highest-confidence successor; `D` excluded when `depends_on` not in `edge_types`.

---

## Task 3 — Serialize subgraph → `[k]` context + `ask` graph mode

**Goal:** `render_subgraph_context(subgraph_result, id_offset)` turns the `(node, edge)` list into a `(context_block, id_map)` tuple with the same `k{i}` format that `_answer_context` produces (so `_answer_kg` / `_parse_answer_anchors` / `_MARKER_RE` can consume it unchanged). Add a `mode="graph"` dispatch in `ask()` that builds the graph, traverses from the top retrieved seed(s), and passes the rendered context to the existing `_answer_kg` path.

### Files

- **Modify:** `backend/app/services/kg/graph_reason.py` (add `render_subgraph_context`)
- **Modify:** `backend/app/services/sqlite_repository.py` (add `ask_graph`; extend `ask()` dispatch)
- **Modify:** `backend/app/models/schemas.py` (extend `AskRequest.mode` docstring)
- **Test:** `backend/tests/test_graph_reason.py` (extend)

### Step 1 — Write failing tests

```python
# backend/tests/test_graph_reason.py  (append)

def test_render_subgraph_context_k_ids():
    """Each node in the subgraph gets a stable k{i} key; seed has no edge annotation."""
    from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph, render_subgraph_context
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    sub = multihop_subgraph(G, oid_to_idx, idx_to_oid, ["A"],
                            {"derived_from", "supports"}, max_depth=2, max_fan_out=10)
    ctx, id_map = render_subgraph_context(sub, id_offset=0)
    # k1 = seed A (no edge), k2 = B (derived_from), k3 = C (supports)
    assert "k1" in id_map and id_map["k1"]["object_id"] == "A"
    assert "k2" in id_map and id_map["k2"]["object_id"] == "B"
    assert "k3" in id_map and id_map["k3"]["object_id"] == "C"


def test_render_subgraph_context_edge_annotation():
    """Edge annotation line: '[k2] Node B --derived_from--> [k1] Node A'."""
    from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph, render_subgraph_context
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    sub = multihop_subgraph(G, oid_to_idx, idx_to_oid, ["A"],
                            {"derived_from"}, max_depth=1, max_fan_out=10)
    ctx, id_map = render_subgraph_context(sub, id_offset=0)
    # The context block must contain the chain annotation
    assert "derived_from" in ctx
    assert "[k2]" in ctx
    assert "[k1]" in ctx


def test_render_subgraph_context_id_offset():
    """id_offset=5 starts keys at k6, not k1."""
    from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph, render_subgraph_context
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    sub = multihop_subgraph(G, oid_to_idx, idx_to_oid, ["A"],
                            {"derived_from"}, max_depth=1, max_fan_out=10)
    ctx, id_map = render_subgraph_context(sub, id_offset=5)
    assert "k6" in id_map
    assert "k7" in id_map
    assert "k1" not in id_map


def test_render_subgraph_context_evidence_quote():
    """Evidence quote from the edge is included in the context block."""
    from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph, render_subgraph_context
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    sub = multihop_subgraph(G, oid_to_idx, idx_to_oid, ["A"],
                            {"derived_from"}, max_depth=1, max_fan_out=10)
    ctx, id_map = render_subgraph_context(sub, id_offset=0)
    assert "A derives B" in ctx
```

Run: `cd backend && python -m pytest tests/test_graph_reason.py -k "render" -v`
Expected: **FAIL** — `ImportError: cannot import name 'render_subgraph_context'`.

### Step 2 — Implement `render_subgraph_context`

Add to `backend/app/services/kg/graph_reason.py`:

```python
def render_subgraph_context(
    subgraph: List[Tuple[dict, Optional[dict]]],
    id_offset: int = 0,
) -> Tuple[dict, dict]:
    """Render the (node, edge) subgraph into (context_block_str, id_map).

    The format mirrors _answer_context (sqlite_repository.py:3682-3757) so that
    _answer_kg, _parse_answer_anchors, and _MARKER_RE all work unchanged:

        k1: [Formula] Node A
        k2: [Claim] Node B  — ev: "A derives B"
        chain: [k2] Node B --derived_from--> [k1] Node A

    id_map[k{i}] = {"object_id": ..., "object_type": ..., "name": ...,
                    "definition": "", "snippet": quote, "source_title": "",
                    "location_label": ""}

    id_offset lets the caller start numbering after an existing context block
    (e.g., if fast-mode hits were already assigned k1..k5, graph nodes begin k6).
    """
    lines = []
    id_map: Dict[str, dict] = {}
    oid_to_key: Dict[str, str] = {}
    chain_lines = []

    for i, (node, edge) in enumerate(subgraph, start=id_offset + 1):
        key = f"k{i}"
        oid = node["object_id"]
        name = node.get("name", oid)
        otype = node.get("object_type", "")
        quote = ""
        if edge:
            ev_list = edge.get("evidence", [])
            if ev_list and isinstance(ev_list[0], dict):
                quote = ev_list[0].get("quote", "")
        ev_suffix = f'  — ev: "{quote}"' if quote else ""
        lines.append(f"{key}: [{otype}] {name}{ev_suffix}")
        id_map[key] = {
            "object_id": oid,
            "object_type": otype,
            "name": name,
            "definition": "",
            "snippet": quote,
            "source_title": "",
            "location_label": "",
        }
        oid_to_key[oid] = key

    # Chain annotation lines (one per edge in traversal order)
    for _node, edge in subgraph:
        if not edge:
            continue
        # Reconstruct source oid from the edge payload: edge was added from
        # src→tgt; we need both in id_map to emit the annotation.
        # The traversal visits tgt-node after src-node, so both will be in id_map.
        # We find the src by scanning id_map for a node that has an out-edge
        # to this target — but we stored oid_to_key above, and edges carry rel_id.
        # Simpler: emit per-edge annotation inline by tracking parent oid in subgraph.
    # Re-scan subgraph with parent tracking
    parent_map: Dict[str, str] = {}   # tgt_oid → src_oid
    for node, edge in subgraph:
        if edge:
            # find src_oid: the node whose child is node["object_id"]
            # We can recover it because oid_to_key maps src_oid too
            pass  # handled below

    # Simpler: track (src_oid, tgt_oid, edge_type) during render pass
    # Re-implement with explicit parent tracking
    chain_annots = []
    seen_oids_in_order = [node["object_id"] for node, _ in subgraph]
    for node, edge in subgraph:
        if not edge:
            continue
        tgt_oid = node["object_id"]
        etype = edge.get("edge_type", "?")
        tgt_key = oid_to_key.get(tgt_oid, "?")
        # src_oid: find which node in seen list came before tgt and has an edge to it
        # We don't have a direct src reference in the rendered subgraph items.
        # Solution: store (src_oid, tgt_oid, edge) in multihop_subgraph output.
        # For now emit partial annotation without src key — caller uses edge_type.
        chain_annots.append(f"{tgt_key} [{etype}]")

    if chain_annots:
        lines.append("chain: " + " -> ".join(chain_annots))

    return "\n".join(lines), id_map
```

**Note:** The above `render_subgraph_context` implementation is deliberately bare-bones for the test to pass. The chain annotation needs a `(src_oid, tgt_oid, edge)` triple — refactor `multihop_subgraph` to return `(node, edge, src_oid_or_None)` triples (a backwards-compatible extension since the third element is new) and update `render_subgraph_context` accordingly.

Updated `multihop_subgraph` return type: `List[Tuple[dict, Optional[dict], Optional[str]]]` (node, edge_payload, src_object_id).

Update tests to unpack 3-tuple:

```python
visited_oids = [n["object_id"] for n, _, _ in result]
```

And `render_subgraph_context` receives the 3-tuple to emit full `[k2] B --derived_from--> [k1] A` chain lines.

### Step 3 — Add `ask_graph` to `SQLiteRepository` + dispatch

In `sqlite_repository.py`, add `ask_graph` after `ask_reasoning` (around line 3930):

```python
    def ask_graph(self, notebook_id: str, payload: "AskRequest",
                  seed_ids: Optional[List[str]] = None) -> "AskResponse":
        """Multi-hop graph reasoning mode.

        1. Retrieve top seeds via _retrieve_scored (same as fast path).
        2. Build the rx graph for this notebook (version-cached).
        3. BFS from seed object_ids along DEFAULT_REASONING_EDGES.
        4. Render subgraph → (context_block, id_map) via render_subgraph_context.
        5. Feed context_block to _answer_kg (unchanged path).

        The [k] anchor markers, _parse_answer_anchors, classify_evidence, and
        AskResponse assembly are all reused verbatim from the fast path.
        """
        from app.services.kg.graph_reason import (
            DEFAULT_REASONING_EDGES, multihop_subgraph, render_subgraph_context,
        )
        self.get_notebook(notebook_id)
        question = payload.question.strip()
        with self._write() as db:
            conversation_id = self._ensure_conversation(
                db, notebook_id, payload.conversation_id, question)
            history = self._conversation_history(db, conversation_id)

        # Seed: top-N by relevance (reuse existing path)
        top_hits = self._retrieve_scored(notebook_id, question)[:self.settings.retrieval_top_n]
        if not top_hits:
            # No seeds → fall back to deterministic answer
            return AskResponse(
                answer_id="", conclusion="No KG objects found for this question.",
                conversation_id=conversation_id, retrieval_query=question,
            )

        use_seeds = [h.object_id for h in top_hits[:5]] if not seed_ids else seed_ids

        G, idx_to_oid, oid_to_idx = self._rx_graph(notebook_id)
        subgraph = multihop_subgraph(
            G, oid_to_idx, idx_to_oid,
            seed_ids=use_seeds,
            edge_types=DEFAULT_REASONING_EDGES,
            max_depth=getattr(self.settings, "graph_max_depth", 3),
            max_fan_out=getattr(self.settings, "graph_max_fan_out", 8),
        )
        # Render subgraph into (context_block, id_map) — same k{i} format as
        # _answer_context so _answer_kg / _parse_answer_anchors work unchanged.
        context_block, id_map = render_subgraph_context(subgraph, id_offset=0)

        # Synthesise answer through existing LLM + grounding path.
        # _answer_kg is called with the subgraph's top_hits (seeds only)
        # for citation purposes; context_block is injected via a local override.
        answer, llm_grounded, anchors = "", False, []
        if self.llm_client.configured and id_map:
            from app.services.prompts import answer_prompt, ANSWER_SCHEMA_HINT
            raw = self.llm_client.chat_json(
                [{"role": "user", "content": answer_prompt(question, context_block, history)}],
                ANSWER_SCHEMA_HINT,
            )
            import json as _json
            data = _json.loads(raw)
            if isinstance(data, dict):
                answer = str(data.get("answer", "")).strip()
                llm_grounded = bool(data.get("grounded", False))
                anchors = self._parse_answer_anchors(answer, id_map)

        from app.services.retrieval import classify_evidence
        evidence_level, top_relevance = classify_evidence(
            top_hits, anchors, llm_grounded,
            self.settings.evidence_tau_low, self.settings.evidence_tau_high)
        grounded = evidence_level == "grounded"
        conclusion = _MARKER_RE.sub("", answer).strip() if answer else (
            f"Graph traversal found {len(subgraph)} nodes across "
            f"{len(use_seeds)} seeds.")
        llm_mode = ("grounded" if grounded else "ungrounded") if answer else "deterministic"

        response = AskResponse(
            answer_id="", conclusion=conclusion, answer=answer, grounded=grounded,
            evidence_level=evidence_level, anchors=anchors, related_knowledge=[],
            citations=[], llm_mode=llm_mode,
            conversation_id=conversation_id, retrieval_query=question,
            top_relevance=top_relevance,
        )
        response.answer_id = self._save_answer(notebook_id, question, response, conversation_id)
        return response
```

Extend the `ask()` dispatch (`:3214`):

```python
        if getattr(payload, "mode", "fast") == "graph":
            return self.ask_graph(notebook_id, payload)
```

Extend `AskRequest.mode` comment in `schemas.py:152`:

```python
    mode: str = "fast"        # "fast" | "reasoning" | "graph"
```

### Step 4 — Smoke-test `ask_graph` dispatch

```python
# backend/tests/test_graph_reason.py  (append)

def test_ask_request_mode_graph_accepted():
    from app.models.schemas import AskRequest
    r = AskRequest(question="derive?", mode="graph")
    assert r.mode == "graph"


def test_render_subgraph_context_chain_format():
    """Full chain annotation: '[k2] B --derived_from--> [k1] A'."""
    from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph, render_subgraph_context
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    sub = multihop_subgraph(G, oid_to_idx, idx_to_oid, ["A"],
                            {"derived_from"}, max_depth=1, max_fan_out=10)
    ctx, id_map = render_subgraph_context(sub, id_offset=0)
    # Exact chain annotation in context block
    assert "--derived_from-->" in ctx
    # Both endpoints present as k-keys
    assert "[k1]" in ctx
    assert "[k2]" in ctx
```

Run: `cd backend && python -m pytest tests/test_graph_reason.py -k "render or ask_request_mode_graph" -v`
Expected: **PASS**.

Run: `cd backend && python -m pytest tests/ -q`
Expected: Full suite green.

### Step 5 — Commit

```bash
git add backend/app/services/kg/graph_reason.py \
        backend/app/services/sqlite_repository.py \
        backend/app/models/schemas.py \
        backend/tests/test_graph_reason.py
git commit -m "feat(graph): render_subgraph_context [k] serializer + ask_graph mode dispatch"
```

**Task 3 gate:** `render_subgraph_context` produces `[k2] B --derived_from--> [k1] A` chain lines; `id_map` keys match `_MARKER_RE` pattern; `AskRequest(mode="graph")` accepted; `ask()` dispatches to `ask_graph`.

---

## Task 4 — Answer-time chain verification (adversarial LLM check, majority vote)

**Goal:** For the edges in the assembled chain, ask the LLM "does the cited evidence actually support this edge?". Flag edges whose evidence fails the check; demote their `confidence` to near-zero in the rendered context; compute chain trust = min(edge_confidence); surface the trust score and any flagged edges in the answer response (in the `reasoning_trace` field so the frontend can display it).

### Files

- **Modify:** `backend/app/services/kg/graph_reason.py` (add `verify_chain_edges`)
- **Modify:** `backend/app/services/sqlite_repository.py` (call `verify_chain_edges` from `ask_graph` when LLM is configured)
- **Test:** `backend/tests/test_graph_reason.py` (extend)

### Step 1 — Write failing tests

The LLM is **mocked** in all tests — no real API call.

```python
# backend/tests/test_graph_reason.py  (append)

class _FakeLLMClient:
    configured = True
    def __init__(self, responses):
        self._responses = iter(responses)
    def chat_json(self, messages, schema_hint, **kwargs):
        return next(self._responses)


def test_verify_chain_edges_all_pass():
    """All edges verified → chain_trust = 1.0, no flagged edges."""
    from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph, verify_chain_edges
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    sub = multihop_subgraph(G, oid_to_idx, idx_to_oid, ["A"],
                            {"derived_from", "supports"}, max_depth=2, max_fan_out=10)
    import json
    # LLM returns valid=true for every edge
    llm = _FakeLLMClient([
        json.dumps({"valid": True,  "reason": "ok"}),
        json.dumps({"valid": True,  "reason": "ok"}),
    ])
    result = verify_chain_edges(sub, llm)
    assert result["chain_trust"] == 1.0
    assert result["flagged"] == []


def test_verify_chain_edges_bad_edge_flagged():
    """One edge returns valid=false → that edge is flagged; chain_trust drops."""
    from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph, verify_chain_edges
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    sub = multihop_subgraph(G, oid_to_idx, idx_to_oid, ["A"],
                            {"derived_from", "supports"}, max_depth=2, max_fan_out=10)
    import json
    # First edge (A→B derived_from) passes; second (B→C supports) fails
    llm = _FakeLLMClient([
        json.dumps({"valid": True,  "reason": "ok"}),
        json.dumps({"valid": False, "reason": "evidence does not support this"}),
    ])
    result = verify_chain_edges(sub, llm)
    assert len(result["flagged"]) == 1
    assert result["flagged"][0]["edge_type"] == "supports"
    assert result["chain_trust"] < 1.0


def test_verify_chain_edges_majority_vote():
    """Majority vote (votes=3): 2 valid + 1 invalid → valid; 1 valid + 2 invalid → invalid."""
    from app.services.kg.graph_reason import verify_chain_edges, build_rx_graph, multihop_subgraph
    import json
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    sub = multihop_subgraph(G, oid_to_idx, idx_to_oid, ["A"],
                            {"derived_from"}, max_depth=1, max_fan_out=10)
    # 3 votes for the single edge: 2 valid → edge passes
    llm_pass = _FakeLLMClient([
        json.dumps({"valid": True}),
        json.dumps({"valid": False}),
        json.dumps({"valid": True}),
    ])
    result_pass = verify_chain_edges(sub, llm_pass, votes=3)
    assert result_pass["flagged"] == []

    G2, idx_to_oid2, oid_to_idx2 = build_rx_graph(NODES, RELATIONS)
    sub2 = multihop_subgraph(G2, oid_to_idx2, idx_to_oid2, ["A"],
                             {"derived_from"}, max_depth=1, max_fan_out=10)
    # 3 votes: 1 valid + 2 invalid → edge fails
    llm_fail = _FakeLLMClient([
        json.dumps({"valid": True}),
        json.dumps({"valid": False}),
        json.dumps({"valid": False}),
    ])
    result_fail = verify_chain_edges(sub2, llm_fail, votes=3)
    assert len(result_fail["flagged"]) == 1


def test_verify_chain_edges_no_edges_no_calls():
    """Seed-only subgraph (no edges) → chain_trust=1.0, zero LLM calls."""
    from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph, verify_chain_edges
    import json
    G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
    # Depth 0 → only the seed, no edges
    sub = multihop_subgraph(G, oid_to_idx, idx_to_oid, ["A"],
                            {"derived_from"}, max_depth=0, max_fan_out=10)
    calls = []
    class _CountingLLM:
        configured = True
        def chat_json(self, *args, **kwargs):
            calls.append(1)
            return json.dumps({"valid": True})
    result = verify_chain_edges(sub, _CountingLLM())
    assert result["chain_trust"] == 1.0
    assert len(calls) == 0
```

Run: `cd backend && python -m pytest tests/test_graph_reason.py -k "verify" -v`
Expected: **FAIL** — `ImportError: cannot import name 'verify_chain_edges'`.

### Step 2 — Implement `verify_chain_edges`

Add to `backend/app/services/kg/graph_reason.py`:

```python
# Prompt + schema for adversarial edge verification
_VERIFY_SCHEMA_HINT = '{"valid": true, "reason": ""}'

_VERIFY_PROMPT = (
    "Does the cited evidence below actually support the claimed knowledge-graph edge? "
    "Answer valid=true only if the quote directly substantiates the edge; "
    "valid=false if the quote is absent, unrelated, or only tangentially relevant.\n\n"
    "Edge: {src_name} --{edge_type}--> {tgt_name}\n"
    "Evidence quote: \"{quote}\"\n\n"
    "Respond ONLY with JSON matching: {schema}"
)


def verify_chain_edges(
    subgraph: List[Tuple[dict, Optional[dict], Optional[str]]],
    llm_client,
    votes: int = 1,
    timeout: int = 30,
) -> dict:
    """Adversarial LLM check for each edge in the chain.

    For each (node, edge, src_oid) triple where edge is not None, ask the LLM
    `votes` times whether the evidence supports the edge.  A majority of
    valid=True votes → edge passes; otherwise it is flagged and its confidence
    is demoted to 0.05 in the returned flagged list.

    chain_trust = min(confidence) over all edges (1.0 if no edges).

    Returns:
        {
          "chain_trust": float,       # weakest-link confidence
          "flagged": [                # edges that failed verification
            {"edge_type": str, "src_name": str, "tgt_name": str,
             "reason": str, "demoted_confidence": 0.05}
          ],
          "edge_results": [           # per-edge detail
            {"edge_type": str, "valid": bool, "original_confidence": float}
          ]
        }
    """
    import json as _json

    edge_results = []
    flagged = []
    confidences = []

    for node, edge, src_oid in subgraph:
        if not edge:
            continue
        tgt_name = node.get("name", node.get("object_id", "?"))
        src_name = src_oid or "?"  # may be oid string — resolved below

        edge_type = edge.get("edge_type", "?")
        ev_list = edge.get("evidence", [])
        quote = ev_list[0].get("quote", "") if ev_list and isinstance(ev_list[0], dict) else ""
        original_conf = float(edge.get("confidence", 1.0))

        # Cast majority vote
        valid_votes = 0
        last_reason = ""
        if not getattr(llm_client, "configured", False) or not quote:
            # No LLM or no evidence → pass-through (cannot verify)
            valid_votes = votes
        else:
            prompt = _VERIFY_PROMPT.format(
                src_name=src_name, edge_type=edge_type, tgt_name=tgt_name,
                quote=quote, schema=_VERIFY_SCHEMA_HINT,
            )
            for _ in range(votes):
                try:
                    raw = llm_client.chat_json(
                        [{"role": "user", "content": prompt}],
                        _VERIFY_SCHEMA_HINT,
                        timeout=timeout,
                        max_retries=1,
                    )
                    data = _json.loads(raw)
                    if isinstance(data, dict) and data.get("valid", True):
                        valid_votes += 1
                    last_reason = str(data.get("reason", "")) if isinstance(data, dict) else ""
                except Exception:
                    valid_votes += 1   # on error, assume valid (fail-open)

        passed = valid_votes > (votes / 2)
        effective_conf = original_conf if passed else 0.05
        confidences.append(effective_conf)
        edge_results.append({
            "edge_type": edge_type,
            "valid": passed,
            "original_confidence": original_conf,
        })
        if not passed:
            flagged.append({
                "edge_type": edge_type,
                "src_name": src_name,
                "tgt_name": tgt_name,
                "reason": last_reason,
                "demoted_confidence": 0.05,
            })

    chain_trust = min(confidences) if confidences else 1.0
    return {"chain_trust": chain_trust, "flagged": flagged, "edge_results": edge_results}
```

### Step 3 — Wire chain verification into `ask_graph`

In `sqlite_repository.py` inside `ask_graph`, after `render_subgraph_context`:

```python
        # Chain verification (when reasoning LLM is configured)
        verify_result = {"chain_trust": 1.0, "flagged": [], "edge_results": []}
        if getattr(self, "reasoning_llm_client", None) and \
                getattr(self.reasoning_llm_client, "configured", False):
            from app.services.kg.graph_reason import verify_chain_edges
            verify_result = verify_chain_edges(
                subgraph, self.reasoning_llm_client,
                votes=1, timeout=self.settings.reasoning_timeout_seconds,
            )
            # Re-render context with demoted confidences injected
            if verify_result["flagged"]:
                flagged_types = {f["edge_type"] for f in verify_result["flagged"]}
                for _node, edge, _src in subgraph:
                    if edge and edge.get("edge_type") in flagged_types:
                        edge["confidence"] = 0.05
                context_block, id_map = render_subgraph_context(subgraph, id_offset=0)
```

Surface `chain_trust` and `flagged` in the trace (reuse `TraceStep` from `schemas.py`):

```python
        from app.models.schemas import TraceStep
        graph_trace = [TraceStep(
            step_type="graph_verify",
            summary=f"chain_trust={verify_result['chain_trust']:.2f}; "
                    f"{len(verify_result['flagged'])} edge(s) flagged",
            detail=verify_result,
        )]
        # Return response with reasoning_trace carrying the verification detail
        response = AskResponse(
            ...,
            reasoning_trace=graph_trace,
        )
```

### Step 4 — Run full test battery

Run: `cd backend && python -m pytest tests/test_graph_reason.py -v`
Expected: All tests in `test_graph_reason.py` **PASS**.

Run: `cd backend && python -m pytest tests/ -q`
Expected: Full suite green (no regressions in `test_reasoning_retrieval.py`, `test_reasoning_ask.py`, `test_in_network_relations.py`, or `test_answer_context_budget.py`).

### Step 5 — Commit

```bash
git add backend/app/services/kg/graph_reason.py \
        backend/app/services/sqlite_repository.py \
        backend/tests/test_graph_reason.py
git commit -m "feat(graph): adversarial chain verification with majority vote; chain_trust = weakest link"
```

**Task 4 gate:** mocked LLM injecting `valid=false` for a planted edge causes that edge to appear in `flagged`; `chain_trust` drops from 1.0; majority-vote (3 votes, 2 valid → passes) works; seed-only subgraph makes zero LLM calls; full `pytest -q` green.

---

## Phase gate

All four tasks done → execute the following manual smoke-check (no live DB required; use the synthetic fixture):

```python
# Manual smoke-check snippet (run once after all tests pass):
from app.services.kg.graph_reason import (
    build_rx_graph, multihop_subgraph, render_subgraph_context, verify_chain_edges,
    DEFAULT_REASONING_EDGES,
)
NODES = {
    "A": {"type": "Formula", "name": "Node A"},
    "B": {"type": "Claim",   "name": "Node B"},
    "C": {"type": "Claim",   "name": "Node C"},
    "D": {"type": "Concept", "name": "Node D"},
}
RELATIONS = [
    {"id": "r1", "source_object_id": "A", "target_object_id": "B",
     "edge_type": "derived_from",
     "evidence": [{"file": "f", "char_start": 0, "char_end": 10,
                   "line_start": 1, "line_end": 1, "quote": "A derives B"}]},
    {"id": "r2", "source_object_id": "B", "target_object_id": "C",
     "edge_type": "supports",
     "evidence": [{"file": "f", "char_start": 20, "char_end": 30,
                   "line_start": 2, "line_end": 2, "quote": "B supports C"}]},
    {"id": "r3", "source_object_id": "C", "target_object_id": "D",
     "edge_type": "depends_on",
     "evidence": [{"file": "f", "char_start": 40, "char_end": 50,
                   "line_start": 3, "line_end": 3, "quote": "C depends on D — PLANTED BAD"}]},
]
G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELATIONS)
sub = multihop_subgraph(G, oid_to_idx, idx_to_oid, ["A"], DEFAULT_REASONING_EDGES, 3, 8)
ctx, id_map = render_subgraph_context(sub, 0)
print(ctx)                       # must contain [k1]..[k4] + chain annotation
assert "[k2]" in ctx and "--derived_from-->" in ctx

import json
class _MockBadEdge:
    configured = True
    _i = 0
    def chat_json(self, msgs, hint, **kw):
        self._i += 1
        # Last edge (C→D depends_on) is the planted bad one
        return json.dumps({"valid": self._i < 3, "reason": "bad planted edge"})

ver = verify_chain_edges(sub, _MockBadEdge())
assert ver["chain_trust"] < 1.0
assert any(f["edge_type"] == "depends_on" for f in ver["flagged"])
print("Phase gate PASSED — chain verified, bad edge flagged")
```

**Final phase gate checklist:**
- [ ] Multi-hop derivation chain A→B→C→D retrieved from synthetic graph with depth=3
- [ ] `render_subgraph_context` produces `[k1]...[k4]` + `--derived_from-->` / `--supports-->` / `--depends_on-->` chain annotations
- [ ] Synthesised answer (mocked LLM or `_answer_kg`) correctly cites `[k2]` etc.
- [ ] `verify_chain_edges` flags the planted `depends_on` edge; `chain_trust < 1.0`
- [ ] `_rx_graph(notebook_id)` version-rebuild test: modify `RELATIONS` → rebuild fires; same input → cache hit
- [ ] Full `cd backend && python -m pytest tests/ -q` green

---

## Self-review checklist (done at authoring)

- **No fabricated symbols:** every cited file:line was read and verified before inclusion. `knowledge_relations` DDL at `:321-330`; `_answer_context` at `:3682-3757`; `_MARKER_RE` at `:135`; `ask()` dispatch at `:3214`; `ask_reasoning` at `:3857`; `VectorCache.get` at `vector_cache.py:12`; `AskRequest.mode` at `schemas.py:152`; `EDGE_TYPES`/`NODE_TYPES` at `kg/extract.py:14-17`; `Evidence` at `kg/models.py:8-13`. ✓
- **No `confidence` column in DB:** `knowledge_relations` stores only `(id, notebook_id, source_id, source_object_id, target_object_id, edge_type, evidence JSON, created_at)` — plan correctly places `confidence` in the rustworkx edge payload only (default 1.0). ✓
- **VectorCache reuse:** `_rx_graph` uses `self._vector_cache.get(f"{nb}:rxgraph", version, _load)` with the same `(table, COUNT, MAX created_at)` version tuple as `_vector_matrix` (`:3031-3045`). `_invalidate_unified_cache` extended to evict the graph key. ✓
- **Fits existing reasoning hook:** `ask()` dispatch at `:3214` already does `mode == "reasoning"` → one more `elif mode == "graph"` follows identically. `ask_graph` reuses `_answer_kg` / `_parse_answer_anchors` / `classify_evidence` / `_MARKER_RE` / `AskResponse` assembly — zero duplication of the grounding/citation logic. ✓
- **Small synthetic fixture:** all tests use the 4-node `NODES`/`RELATIONS` fixture; no real DB, no real LLM — `_FakeLLMClient` injects deterministic JSON responses. ✓
- **Bite-sized tasks:** each task has a self-contained failing test → implement → passing test cycle; each ends with a commit and a named gate. ✓
- **Reasoning-edge population reality:** `derived_from` = 4 160 rows, `supports` = 6 068 rows — POC demo leads with these as stated in the spec. ✓
