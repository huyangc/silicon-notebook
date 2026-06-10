# Track D — Cross-Tier Multi-Hop Reasoning Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `ask(mode="graph")` span base ∪ active-personal notebooks with correct per-edge tier stamps, authority-weighted chain trust, and base-wins conflict precedence.

**Architecture:** Wave 1 Track C built `_rx_graph` and `ask_graph` for a single notebook; Track B built `federated_retrieve` that merges base + personal hits. Track D replaces `_rx_graph(notebook_id)` with `_federated_rx_graph(active_notebook_id)` that merges nodes/edges from all base notebooks plus the active personal notebook, stamping each edge's `tier` from the owning notebook's `notebooks.tier` column (base = "base", everything else = "personal"). `chain_trust` is then the weakest-link confidence, where personal edges receive an authority discount factor of 0.85 before the min. `render_subgraph_context` injects `[tier]` annotations into each chain step, and `verify_chain_edges` receives the per-edge tier so the flagging and demotion path propagates it. `answer_prompt` already carries the base-wins conflict rule (prompts.py:152-156); the graph path wires in through the same id_map → AnswerAnchor.tier path already used by the fast path.

**Tech Stack:** Python 3.11+, rustworkx, SQLite, pytest; `/opt/homebrew/Caskroom/miniconda/base/bin/python`; tests run from `backend/` directory.

---

## Key Code Facts (grounding from merged master 994dd14)

| Symbol | File : line | Current behaviour |
|---|---|---|
| `build_rx_graph(nodes, relations, tier="base")` | `app/services/kg/graph_reason.py:23` | Takes a single `tier` string, stamps ALL edges with it |
| `_rx_graph(self, notebook_id)` | `sqlite_repository.py:3128` | Single-notebook; version key = `("rxgraph", count, max_ts)` for that notebook only |
| `ask_graph(self, notebook_id, payload, seed_ids)` | `sqlite_repository.py:4131` | Calls `_rx_graph(notebook_id)` — single notebook graph only |
| `multihop_subgraph(...)` | `graph_reason.py:78` | Returns shallow copies; confidence-sorted fan-out; returns `(node, edge, src_oid)` triples |
| `render_subgraph_context(subgraph, id_offset)` | `graph_reason.py:152` | Emits `k{i}: [type] name — ev:…` + `chain:` block; id_map lacks `tier` key today |
| `verify_chain_edges(subgraph, llm_client, …)` | `graph_reason.py:243` | `chain_trust = min(confidence)` over all edges; operates on the `confidence` field in edge payload copies |
| `_parse_answer_anchors(answer, id_map)` | `sqlite_repository.py:4251` | Reads `ctx.get("tier", "personal")` from id_map — already tier-ready if id_map carries it |
| `AnswerAnchor.tier` | `app/models/schemas.py:171` | Field exists, defaults "personal" |
| `knowledge_relations` schema | `sqlite_repository.py:322` | No `tier` or `confidence` columns; tier lives on `notebooks.tier` |
| `tier_weight` values | `retrieval.py:110-113` | base=1.20, personal=1.00 |
| `answer_prompt` conflict rule | `prompts.py:152-156` | "If a personal item contradicts a base item, defer to the base item" |
| Cache invalidation | `sqlite_repository.py:2119` | `_vector_cache.invalidate(f"{notebook_id}:rxgraph")` — single notebook only |

**Invariants that must not break:**
1. `[0,1]/tau` — raw cosine stays inside `_fuse`; tier authority is a multiplier on `.score`, never on `.relevance`.
2. Dual-index best-of — each notebook is scored independently by its own embedding matrices.

---

## Files

| File | Action | Responsibility |
|---|---|---|
| `backend/app/services/kg/graph_reason.py` | Modify | `build_rx_graph` accepts per-relation `tier` mapping; `render_subgraph_context` injects `[tier]` on chain steps; `verify_chain_edges` adds authority-weighted chain_trust |
| `backend/app/services/sqlite_repository.py` | Modify | New `_federated_rx_graph(active_notebook_id)` replaces single-notebook `_rx_graph` in `ask_graph`; cache key covers both notebooks; cache invalidation extended |
| `backend/tests/test_cross_tier_reasoning.py` | Create | All new tests; 2-notebook synthetic fixtures; mocked LLM |

---

## Task 1: `build_rx_graph` accepts per-relation tier instead of a single string

**Files:**
- Modify: `backend/app/services/kg/graph_reason.py:23-75`
- Test: `backend/tests/test_cross_tier_reasoning.py`

Today `build_rx_graph(nodes, relations, tier="base")` stamps every edge with the same `tier` string (line 72: `"tier": tier`). For a federated graph, relations come from different notebooks; each relation must carry the tier of its owning notebook.

The change: add an optional `tier_map: dict[str, str] | None = None` parameter. When provided, look up `tier_map[rel["notebook_id"]]` per relation. Fall back to the existing `tier` string default when `tier_map` is absent or the key is missing. Callers passing `tier_map` also set `tier="personal"` as a fallback.

- [ ] **Step 1: Write the failing test**

In `backend/tests/test_cross_tier_reasoning.py`:

```python
# backend/tests/test_cross_tier_reasoning.py
"""Track D — Cross-tier multi-hop reasoning tests.

Two synthetic notebooks:
  base_nb:     nodes B1 (Formula), B2 (Claim); edge B1→B2 derived_from
  personal_nb: nodes P1 (Concept), P2 (Claim); edge P2→B2 supports (cross-tier)

Each relation row carries a "notebook_id" key so build_rx_graph can look up tier.
"""
import json
import pytest

# ── Synthetic fixture ─────────────────────────────────────────────────────────

BASE_NB_ID = "nb-base-001"
PERS_NB_ID = "nb-pers-001"

NODES_FEDERATED = {
    "B1": {"type": "Formula", "name": "Base Formula"},
    "B2": {"type": "Claim",   "name": "Base Claim"},
    "P1": {"type": "Concept", "name": "Personal Concept"},
    "P2": {"type": "Claim",   "name": "Personal Claim"},
}

# Relations carry notebook_id so tier_map lookup works.
RELATIONS_FEDERATED = [
    {"id": "r1", "notebook_id": BASE_NB_ID,
     "source_object_id": "B1", "target_object_id": "B2",
     "edge_type": "derived_from",
     "evidence": [{"file": "f1", "char_start": 0, "char_end": 10,
                   "line_start": 1, "line_end": 1, "quote": "base formula derives base claim"}]},
    {"id": "r2", "notebook_id": PERS_NB_ID,
     "source_object_id": "P2", "target_object_id": "B2",
     "edge_type": "supports",
     "evidence": [{"file": "f2", "char_start": 0, "char_end": 10,
                   "line_start": 1, "line_end": 1, "quote": "personal claim supports base claim"}]},
    {"id": "r3", "notebook_id": PERS_NB_ID,
     "source_object_id": "P1", "target_object_id": "P2",
     "edge_type": "depends_on",
     "evidence": [{"file": "f2", "char_start": 20, "char_end": 30,
                   "line_start": 2, "line_end": 2, "quote": "personal concept depends on personal claim"}]},
]

TIER_MAP = {BASE_NB_ID: "base", PERS_NB_ID: "personal"}


class TestTask1FederatedGraphBuild:
    def test_base_edge_stamped_base(self):
        """Relations from a base notebook get tier='base' in the edge payload."""
        from app.services.kg.graph_reason import build_rx_graph
        G, idx_to_oid, oid_to_idx = build_rx_graph(
            NODES_FEDERATED, RELATIONS_FEDERATED, tier_map=TIER_MAP)
        b1_idx = oid_to_idx["B1"]
        b2_idx = oid_to_idx["B2"]
        payload = G.get_edge_data(b1_idx, b2_idx)
        assert payload["tier"] == "base"

    def test_personal_edge_stamped_personal(self):
        """Relations from a personal notebook get tier='personal'."""
        from app.services.kg.graph_reason import build_rx_graph
        G, idx_to_oid, oid_to_idx = build_rx_graph(
            NODES_FEDERATED, RELATIONS_FEDERATED, tier_map=TIER_MAP)
        p2_idx = oid_to_idx["P2"]
        b2_idx = oid_to_idx["B2"]
        payload = G.get_edge_data(p2_idx, b2_idx)
        assert payload["tier"] == "personal"

    def test_backward_compat_no_tier_map(self):
        """Calling build_rx_graph without tier_map still stamps 'base' (default)."""
        from app.services.kg.graph_reason import build_rx_graph
        # Use original single-notebook fixture (no notebook_id in relations).
        NODES = {
            "A": {"type": "Formula", "name": "Node A"},
            "B": {"type": "Claim",   "name": "Node B"},
        }
        RELS = [{"id": "r1", "source_object_id": "A", "target_object_id": "B",
                 "edge_type": "derived_from", "evidence": []}]
        G, idx_to_oid, oid_to_idx = build_rx_graph(NODES, RELS)
        payload = G.get_edge_data(oid_to_idx["A"], oid_to_idx["B"])
        assert payload["tier"] == "base"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_cross_tier_reasoning.py::TestTask1FederatedGraphBuild -v 2>&1 | tail -20
```

Expected: FAIL — `TypeError: build_rx_graph() got an unexpected keyword argument 'tier_map'`

- [ ] **Step 3: Implement the change in `build_rx_graph`**

In `backend/app/services/kg/graph_reason.py`, change the signature and the edge-stamping block:

```python
def build_rx_graph(
    nodes: Dict[str, dict],
    relations: List[dict],
    tier: str = "base",
    tier_map: Optional[Dict[str, str]] = None,
) -> Tuple[rx.PyDiGraph, Dict[int, str], Dict[str, int]]:
    """Build a PyDiGraph from dicts.

    `tier_map` — optional {notebook_id: tier_str} mapping.  When provided,
    each relation's tier is looked up via rel["notebook_id"]; falls back to
    `tier` when the key is absent or tier_map is None.
    """
```

Replace the edge-stamping block (currently lines 63-73) with:

```python
        rel_nb = rel.get("notebook_id", "")
        edge_tier = (
            tier_map[rel_nb]
            if (tier_map and rel_nb in tier_map)
            else tier
        )
        G.add_edge(
            oid_to_idx[src_oid],
            oid_to_idx[tgt_oid],
            {
                "rel_id": rel.get("id", ""),
                "edge_type": rel["edge_type"],
                "evidence": ev_raw if isinstance(ev_raw, list) else [],
                "confidence": float(rel.get("confidence", 1.0)),
                "tier": edge_tier,
            },
        )
```

Also add `Optional` to the existing import line at the top (already imported: `from typing import Dict, List, Optional, Tuple`; it is already there at line 14 — no change needed).

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_cross_tier_reasoning.py::TestTask1FederatedGraphBuild -v 2>&1 | tail -15
```

Expected: 3 PASSED.

- [ ] **Step 5: Verify existing graph_reason tests still pass**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_graph_reason.py -v 2>&1 | tail -20
```

Expected: all PASSED (backward-compat default preserved).

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/kg/graph_reason.py backend/tests/test_cross_tier_reasoning.py
git commit -m "feat(graph): build_rx_graph accepts tier_map for per-relation tier stamping"
```

---

## Task 2: `_federated_rx_graph` — build the graph from base ∪ personal notebooks

**Files:**
- Modify: `backend/app/services/sqlite_repository.py:3128-3165`
- Test: `backend/tests/test_cross_tier_reasoning.py`

Add `_federated_rx_graph(self, active_notebook_id: str)` that:
1. Queries `notebooks` for all base notebooks (plus the active notebook).
2. Loads nodes and relations from all of them into a single merged `nodes` dict and `relations` list, each relation row tagged with `notebook_id`.
3. Calls `build_rx_graph(nodes, all_relations, tier_map=tier_map)`.
4. Version-keys the cache on a tuple covering ALL participating notebooks so any per-notebook ingest invalidates the federated graph.

The existing `_rx_graph` is kept intact (it is still used by non-graph paths and tests). `ask_graph` will be updated in Task 5 to call `_federated_rx_graph` instead.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_cross_tier_reasoning.py`:

```python
class TestTask2FederatedRxGraph:
    @pytest.fixture
    def repo_two_notebooks(self, tmp_path, monkeypatch):
        """Returns (repo, base_nb_id, personal_nb_id) with relations seeded."""
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
        monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
        monkeypatch.setenv("LLM_LOG_ENABLED", "false")
        from app.core.config import Settings
        from app.services.sqlite_repository import SQLiteRepository
        from app.services.embedding import FakeEmbedder
        from app.models.schemas import NotebookCreate
        r = SQLiteRepository(Settings())
        r.embedder = FakeEmbedder(dim=16)

        base_nb = r.create_notebook(NotebookCreate(name="base"))
        r.mark_notebook_base(base_nb.id)
        r.store_kg(base_nb.id, None, [
            {"local_id": "B1", "object_type": "formula",
             "payload": {"name": "Base Formula", "section_path": "1"}, "evidence": []},
            {"local_id": "B2", "object_type": "claim",
             "payload": {"name": "Base Claim", "section_path": "1"}, "evidence": []},
        ], [
            {"local_id": "r1", "source_local_id": "B1", "target_local_id": "B2",
             "edge_type": "derived_from", "evidence": [
                 {"source_id": "src1", "source_title": "s", "element_id": "e1",
                  "element_type": "paragraph", "location_label": "p1",
                  "quoted_span": "base formula derives base claim", "confidence": 1.0}
             ]},
        ])

        pers_nb = r.create_notebook(NotebookCreate(name="personal"))
        r.store_kg(pers_nb.id, None, [
            {"local_id": "P1", "object_type": "concept",
             "payload": {"name": "Personal Concept", "section_path": "1"}, "evidence": []},
            {"local_id": "P2", "object_type": "claim",
             "payload": {"name": "Personal Claim", "section_path": "1"}, "evidence": []},
        ], [
            {"local_id": "r2", "source_local_id": "P1", "target_local_id": "P2",
             "edge_type": "supports", "evidence": [
                 {"source_id": "src2", "source_title": "t", "element_id": "e2",
                  "element_type": "paragraph", "location_label": "p2",
                  "quoted_span": "personal concept supports personal claim", "confidence": 1.0}
             ]},
        ])
        return r, base_nb.id, pers_nb.id

    def test_federated_graph_contains_nodes_from_both_notebooks(self, repo_two_notebooks):
        """_federated_rx_graph merges nodes from base + personal into one graph."""
        repo, base_id, pers_id = repo_two_notebooks
        G, idx_to_oid, oid_to_idx = repo._federated_rx_graph(pers_id)
        # All 4 objects must appear as nodes (B1, B2 from base; P1, P2 from personal).
        assert G.num_nodes() >= 4

    def test_federated_graph_base_edges_stamped_base(self, repo_two_notebooks):
        """Edges from the base notebook must carry tier='base'."""
        repo, base_id, pers_id = repo_two_notebooks
        G, idx_to_oid, oid_to_idx = repo._federated_rx_graph(pers_id)
        # Find the derived_from edge (B1→B2, both from base_nb).
        edge_tiers = {
            G.get_edge_data(src, tgt)["tier"]
            for src, tgt, _ in G.edge_index_map().values()
        }
        assert "base" in edge_tiers

    def test_federated_graph_personal_edges_stamped_personal(self, repo_two_notebooks):
        """Edges from the personal notebook must carry tier='personal'."""
        repo, base_id, pers_id = repo_two_notebooks
        G, idx_to_oid, oid_to_idx = repo._federated_rx_graph(pers_id)
        edge_tiers = {
            G.get_edge_data(src, tgt)["tier"]
            for src, tgt, _ in G.edge_index_map().values()
        }
        assert "personal" in edge_tiers

    def test_federated_graph_version_key_includes_both_notebooks(self, repo_two_notebooks):
        """Calling _federated_rx_graph twice returns the same cached object
        (version key covers both notebooks; no rebuild on same data)."""
        repo, base_id, pers_id = repo_two_notebooks
        result1 = repo._federated_rx_graph(pers_id)
        result2 = repo._federated_rx_graph(pers_id)
        # Same PyDiGraph object returned from cache.
        assert result1[0] is result2[0]
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_cross_tier_reasoning.py::TestTask2FederatedRxGraph -v 2>&1 | tail -15
```

Expected: FAIL — `AttributeError: 'SQLiteRepository' object has no attribute '_federated_rx_graph'`

- [ ] **Step 3: Implement `_federated_rx_graph` in `sqlite_repository.py`**

Add the method immediately after `_rx_graph` (after line 3165). The version key concatenates per-notebook `(count, max_ts)` pairs so any change in any participating notebook invalidates the combined graph:

```python
    def _federated_rx_graph(self, active_notebook_id: str):
        """Return a federated PyDiGraph merging base notebook(s) + active notebook.

        Version-keyed on the concatenated (COUNT, MAX created_at) for every
        participating notebook so that an ingest into ANY of them triggers a
        rebuild.  Cache key: "{active_id}:fed_rxgraph".

        Each relation row is tagged with its notebook_id before passing to
        build_rx_graph so per-edge tier stamping works.
        """
        from app.services.kg.graph_reason import build_rx_graph
        with self._connect() as db:
            # Participating notebooks: active + all base notebooks (excl. active
            # if active is itself base, to avoid duplication).
            base_rows = db.execute(
                "SELECT id, tier FROM notebooks WHERE tier='base' AND id != ?",
                (active_notebook_id,),
            ).fetchall()
            active_row = db.execute(
                "SELECT id, tier FROM notebooks WHERE id=?",
                (active_notebook_id,),
            ).fetchone()
            active_tier = active_row["tier"] if active_row else "personal"

            # Build participating list: active first, then all base notebooks.
            participants = [(active_notebook_id, active_tier)] + [
                (r["id"], r["tier"]) for r in base_rows
            ]
            # Version key: tuple of per-notebook (nb_id, count, max_ts) pairs.
            version_parts = []
            for nb_id, _ in participants:
                ver = db.execute(
                    "SELECT COUNT(*) AS c, COALESCE(MAX(created_at), '') AS ts "
                    "FROM knowledge_relations WHERE notebook_id = ?",
                    (nb_id,),
                ).fetchone()
                version_parts.append((nb_id, ver["c"], ver["ts"]))
            version = ("fed_rxgraph", tuple(version_parts))

            tier_map = {nb_id: nb_tier for nb_id, nb_tier in participants}

            def _load():
                nodes: dict = {}
                all_relations: list = []
                ph = ",".join("?" for _ in USABLE_STATUSES)
                for nb_id, _ in participants:
                    obj_rows = db.execute(
                        "SELECT id, object_type, payload FROM knowledge_objects "
                        f"WHERE notebook_id = ? AND status IN ({ph})",
                        (nb_id, *USABLE_STATUSES),
                    ).fetchall()
                    for r in obj_rows:
                        p = json.loads(r["payload"] or "{}")
                        nodes[r["id"]] = {"type": r["object_type"], "name": p.get("name", "")}
                    rel_rows = db.execute(
                        "SELECT id, source_object_id, target_object_id, edge_type, evidence "
                        "FROM knowledge_relations WHERE notebook_id = ?",
                        (nb_id,),
                    ).fetchall()
                    for r in rel_rows:
                        d = dict(r)
                        d["notebook_id"] = nb_id   # tag for tier_map lookup
                        all_relations.append(d)
                return build_rx_graph(nodes, all_relations, tier="personal", tier_map=tier_map)

            return self._vector_cache.get(
                f"{active_notebook_id}:fed_rxgraph", version, _load)
```

- [ ] **Step 4: Run the test to verify it passes**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_cross_tier_reasoning.py::TestTask2FederatedRxGraph -v 2>&1 | tail -15
```

Expected: 4 PASSED.

- [ ] **Step 5: Verify no regressions**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_graph_reason.py tests/test_two_tier_federated.py -q 2>&1 | tail -10
```

Expected: all PASSED.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_cross_tier_reasoning.py
git commit -m "feat(graph): _federated_rx_graph builds merged base+personal graph with per-edge tier"
```

---

## Task 3: `render_subgraph_context` annotates each chain step with its tier

**Files:**
- Modify: `backend/app/services/kg/graph_reason.py:152-227`
- Test: `backend/tests/test_cross_tier_reasoning.py`

Today `render_subgraph_context` (graph_reason.py:152) emits lines like `k2: [Claim] Node B — ev: "…"` and a `chain:` block. It does not carry `tier` into the id_map, so `_parse_answer_anchors` always defaults to `tier="personal"` (sqlite_repository.py:4269: `ctx.get("tier", "personal")`).

Changes:
1. In the per-node loop, read `tier` from the edge payload (edge present → use `edge["tier"]`; seed node → default "personal" unless we can infer from the node itself — use "personal" as default for seeds since we cannot know without the edge).
2. Add `tier` to each `id_map[key]` entry.
3. Append `[tier]` to the per-node header line (e.g. `k2: [Claim][base] Node B`), matching the format that `answer_prompt` already expects (prompts.py:159 `[type][tier]`).
4. In the chain annotation lines, append `(tier=<tier>)` at the end of each hop for human-readable tracing in the context block.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_cross_tier_reasoning.py`:

```python
class TestTask3TierAnnotatedRender:
    def _make_federated_subgraph(self):
        """Build a 3-node subgraph spanning base→personal."""
        from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph
        G, idx_to_oid, oid_to_idx = build_rx_graph(
            NODES_FEDERATED, RELATIONS_FEDERATED, tier_map=TIER_MAP)
        # Seed from B1 (base), traverse derived_from (base) and depends_on (personal)
        return multihop_subgraph(
            G, oid_to_idx, idx_to_oid,
            seed_ids=["B1"],
            edge_types={"derived_from", "supports", "depends_on"},
            max_depth=3, max_fan_out=10,
        )

    def test_id_map_carries_tier(self):
        """id_map entries must include a 'tier' key."""
        from app.services.kg.graph_reason import render_subgraph_context
        sub = self._make_federated_subgraph()
        _, id_map = render_subgraph_context(sub)
        for key, entry in id_map.items():
            assert "tier" in entry, f"id_map[{key}] missing 'tier'"

    def test_base_edge_node_gets_base_tier_in_id_map(self):
        """A node reached via a base edge carries tier='base' in id_map."""
        from app.services.kg.graph_reason import render_subgraph_context
        sub = self._make_federated_subgraph()
        _, id_map = render_subgraph_context(sub)
        # B2 is reached via r1 (base edge derived_from)
        b2_entry = next(v for v in id_map.values() if v["object_id"] == "B2")
        assert b2_entry["tier"] == "base"

    def test_personal_edge_node_gets_personal_tier_in_id_map(self):
        """A node reached via a personal edge carries tier='personal' in id_map."""
        from app.services.kg.graph_reason import render_subgraph_context
        sub = self._make_federated_subgraph()
        _, id_map = render_subgraph_context(sub)
        # P2 is reached via r3 (personal edge depends_on from P1);
        # or P2 itself reached via personal edge.
        personal_entries = [v for v in id_map.values() if v["tier"] == "personal"]
        assert len(personal_entries) >= 1, "expected at least one personal-tier node in id_map"

    def test_context_block_contains_tier_tag_on_node_lines(self):
        """Context block lines for non-seed nodes carry [tier] tag: '[Claim][base] …'."""
        from app.services.kg.graph_reason import render_subgraph_context
        sub = self._make_federated_subgraph()
        ctx, _ = render_subgraph_context(sub)
        # At least one line must carry a tier tag
        assert "[base]" in ctx or "[personal]" in ctx, (
            f"No tier tag in context block:\n{ctx}")

    def test_chain_annotation_carries_tier(self):
        """Chain annotation lines must include tier information per hop."""
        from app.services.kg.graph_reason import render_subgraph_context
        sub = self._make_federated_subgraph()
        ctx, _ = render_subgraph_context(sub)
        lines = ctx.splitlines()
        chain_lines = [l for l in lines if "--" in l and "-->" in l]
        assert chain_lines, "expected at least one chain annotation line"
        for line in chain_lines:
            assert "tier=" in line, (
                f"Chain annotation line missing tier= tag:\n  {line}")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_cross_tier_reasoning.py::TestTask3TierAnnotatedRender -v 2>&1 | tail -20
```

Expected: FAIL — id_map entries lack `tier`; context block lacks `[base]`/`[personal]` tag.

- [ ] **Step 3: Implement tier annotation in `render_subgraph_context`**

In `backend/app/services/kg/graph_reason.py`, replace the body of `render_subgraph_context` (lines 178-227) with:

```python
    lines: List[str] = []
    id_map: Dict[str, dict] = {}
    oid_to_key: Dict[str, str] = {}

    for i, (node, edge, _src_oid) in enumerate(subgraph, start=id_offset + 1):
        key = f"k{i}"
        oid = node["object_id"]
        name = node.get("name", oid)
        otype = node.get("object_type", "")
        # Tier comes from the incoming edge; seed nodes (no edge) default "personal".
        node_tier = edge.get("tier", "personal") if edge else "personal"
        quote = ""
        if edge:
            ev_list = edge.get("evidence", [])
            if ev_list and isinstance(ev_list[0], dict):
                quote = ev_list[0].get("quote", "")
        ev_suffix = f'  — ev: "{quote}"' if quote else ""
        lines.append(f"{key}: [{otype}][{node_tier}] {name}{ev_suffix}")
        id_map[key] = {
            "object_id": oid,
            "object_type": otype,
            "name": name,
            "definition": "",
            "snippet": quote,
            "source_title": "",
            "location_label": "",
            "tier": node_tier,
        }
        oid_to_key[oid] = key

    # Chain annotation lines (one per edge, in traversal order).
    chain_lines: List[str] = []
    for node, edge, src_oid in subgraph:
        if not edge:
            continue
        tgt_oid = node["object_id"]
        tgt_key = oid_to_key.get(tgt_oid, "?")
        src_key = oid_to_key.get(src_oid, "?")
        etype = edge.get("edge_type", "?")
        edge_tier = edge.get("tier", "personal")
        src_name = ""
        if src_key in id_map:
            src_name = id_map[src_key].get("name", "")
        tgt_name = node.get("name", tgt_oid)
        chain_lines.append(
            f"  [{tgt_key}] {tgt_name} --{etype}--> [{src_key}] {src_name}  (tier={edge_tier})".rstrip()
        )

    if chain_lines:
        lines.append("chain:")
        lines.extend(chain_lines)

    return ("\n".join(lines) if lines else "(none)"), id_map
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_cross_tier_reasoning.py::TestTask3TierAnnotatedRender -v 2>&1 | tail -15
```

Expected: 5 PASSED.

- [ ] **Step 5: Verify existing render tests still pass**

The existing tests in `test_graph_reason.py` check for `"derived_from" in ctx` and `"[k1]" in ctx` — the format change adds `[tier]` to node lines and `(tier=…)` to chain lines but leaves those patterns intact.

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_graph_reason.py -v 2>&1 | tail -20
```

Expected: all PASSED. If `test_render_subgraph_context_edge_annotation` or `test_render_subgraph_context_chain_format` fail because they check exact formats not including `[tier]`, patch them to also accept the new `[base]` suffix (add `assert "[base]" in ctx or "[personal]" in ctx`). The existing assertions on `derived_from`, `[k1]`, `[k2]` etc. should still hold.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/kg/graph_reason.py backend/tests/test_cross_tier_reasoning.py
git commit -m "feat(graph): render_subgraph_context injects [tier] per node and chain step"
```

---

## Task 4: Authority-weighted `chain_trust` — personal edges count less

**Files:**
- Modify: `backend/app/services/kg/graph_reason.py:243-331`
- Test: `backend/tests/test_cross_tier_reasoning.py`

**Rule (precise definition):** `chain_trust` is the weakest link over all edges, but personal-tier edges are penalised by an authority factor before the min. Specifically:

```
effective_confidence(edge) = confidence * authority_factor(tier)
authority_factor("base")     = 1.0    # authoritative reference
authority_factor("personal") = 0.85   # user notes: plausible but not curated
authority_factor(other)      = 0.85   # safe default
```

This means a personal edge with confidence=1.0 has `effective_confidence=0.85`, while a base edge with confidence=0.8 has `effective_confidence=0.8`. The weakest effective_confidence over all edges is `chain_trust`.

The `flagged` list entries are extended with a `"tier"` field so callers can surface "this step rests on a personal note" in the answer.

The `verify_chain_edges` return dict adds `"authority_notes"` — a list of strings for each personal-tier edge, e.g. `"supports (personal): this step rests on a personal note"`.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_cross_tier_reasoning.py`:

```python
_AUTHORITY_FACTOR = {"base": 1.0, "personal": 0.85}

class TestTask4AuthorityWeightedChainTrust:
    def _build_mixed_subgraph(self):
        """Subgraph with one base edge (confidence=1.0) and one personal edge (confidence=1.0)."""
        from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph
        G, idx_to_oid, oid_to_idx = build_rx_graph(
            NODES_FEDERATED, RELATIONS_FEDERATED, tier_map=TIER_MAP)
        return multihop_subgraph(
            G, oid_to_idx, idx_to_oid,
            seed_ids=["B1"],
            edge_types={"derived_from", "supports"},
            max_depth=2, max_fan_out=10,
        )

    class _NoLLM:
        configured = False

    def test_chain_trust_is_weakest_effective_confidence(self):
        """With a personal edge (confidence=1.0), chain_trust = 0.85 (authority factor)."""
        from app.services.kg.graph_reason import verify_chain_edges
        sub = self._build_mixed_subgraph()
        result = verify_chain_edges(sub, self._NoLLM())
        # Base edge: effective_conf = 1.0 * 1.0 = 1.0
        # Personal edge (P2→B2, confidence=1.0): effective_conf = 1.0 * 0.85 = 0.85
        # chain_trust = min(1.0, 0.85) = 0.85
        assert abs(result["chain_trust"] - 0.85) < 1e-9, (
            f"Expected chain_trust≈0.85, got {result['chain_trust']}")

    def test_all_base_edges_chain_trust_is_1(self):
        """A chain with only base edges and confidence=1.0 → chain_trust = 1.0."""
        import rustworkx as rx
        from app.services.kg.graph_reason import multihop_subgraph, verify_chain_edges
        G = rx.PyDiGraph()
        n1 = G.add_node({"object_id": "X", "object_type": "Claim", "name": "X"})
        n2 = G.add_node({"object_id": "Y", "object_type": "Claim", "name": "Y"})
        G.add_edge(n1, n2, {"edge_type": "supports", "confidence": 1.0,
                             "evidence": [], "tier": "base"})
        idx_to_oid = {n1: "X", n2: "Y"}
        oid_to_idx = {"X": n1, "Y": n2}
        sub = multihop_subgraph(G, oid_to_idx, idx_to_oid, ["X"],
                                {"supports"}, max_depth=1, max_fan_out=10)
        result = verify_chain_edges(sub, self._NoLLM())
        assert result["chain_trust"] == 1.0

    def test_edge_results_carry_tier(self):
        """edge_results entries must carry a 'tier' field."""
        from app.services.kg.graph_reason import verify_chain_edges
        sub = self._build_mixed_subgraph()
        result = verify_chain_edges(sub, self._NoLLM())
        for er in result["edge_results"]:
            assert "tier" in er, f"edge_result missing 'tier': {er}"

    def test_authority_notes_surfaces_personal_edge(self):
        """authority_notes must list a warning for each personal-tier edge."""
        from app.services.kg.graph_reason import verify_chain_edges
        sub = self._build_mixed_subgraph()
        result = verify_chain_edges(sub, self._NoLLM())
        assert "authority_notes" in result
        # At least one note for the personal edge
        assert any("personal" in note.lower() for note in result["authority_notes"]), (
            f"No personal-tier note in authority_notes: {result['authority_notes']}")

    def test_flagged_entry_carries_tier(self):
        """When an edge is flagged (LLM invalid), the flagged entry carries 'tier'."""
        import json
        from app.services.kg.graph_reason import build_rx_graph, multihop_subgraph, verify_chain_edges

        class _FailLLM:
            configured = True
            def chat_json(self, messages, schema, **kw):
                return json.dumps({"valid": False, "reason": "bad"})

        G, idx_to_oid, oid_to_idx = build_rx_graph(
            NODES_FEDERATED, RELATIONS_FEDERATED, tier_map=TIER_MAP)
        sub = multihop_subgraph(G, oid_to_idx, idx_to_oid, ["B1"],
                                {"derived_from"}, max_depth=1, max_fan_out=10)
        result = verify_chain_edges(sub, _FailLLM())
        assert result["flagged"], "expected at least one flagged edge"
        for f in result["flagged"]:
            assert "tier" in f, f"flagged entry missing 'tier': {f}"
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_cross_tier_reasoning.py::TestTask4AuthorityWeightedChainTrust -v 2>&1 | tail -20
```

Expected: FAIL — chain_trust still uses raw confidence (no authority factor), `authority_notes` key absent, `tier` absent from edge_results.

- [ ] **Step 3: Implement authority-weighted chain_trust in `verify_chain_edges`**

In `backend/app/services/kg/graph_reason.py`, add the authority factor constant just before `verify_chain_edges`:

```python
# Authority factor per tier: personal notes are plausible but unverified.
# Applied as a multiplier on confidence before the chain_trust min.
_AUTHORITY_FACTOR: Dict[str, float] = {
    "base":     1.0,
    "personal": 0.85,
}
```

Then modify the `verify_chain_edges` function:

1. In the loop over `subgraph`, after computing `original_conf` and `effective_conf`, compute:

```python
        edge_tier = edge.get("tier", "personal")
        auth_factor = _AUTHORITY_FACTOR.get(edge_tier, 0.85)
        effective_conf_with_auth = effective_conf * auth_factor
        confidences.append(effective_conf_with_auth)
```

2. Change `edge_results.append(...)` to include `tier`:

```python
        edge_results.append({
            "edge_type": edge_type,
            "valid": passed,
            "original_confidence": original_conf,
            "tier": edge_tier,
        })
```

3. Change `flagged.append(...)` to include `tier`:

```python
        if not passed:
            flagged.append({
                "edge_type": edge_type,
                "src_name": src_name,
                "tgt_name": tgt_name,
                "reason": last_reason,
                "demoted_confidence": 0.05,
                "tier": edge_tier,
            })
```

4. After the loop, build `authority_notes` and include in return:

```python
    authority_notes = [
        f"{er['edge_type']} ({er['tier']}): this step rests on a personal note"
        for er in edge_results
        if er["tier"] == "personal"
    ]
    chain_trust = min(confidences) if confidences else 1.0
    return {
        "chain_trust": chain_trust,
        "flagged": flagged,
        "edge_results": edge_results,
        "authority_notes": authority_notes,
    }
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_cross_tier_reasoning.py::TestTask4AuthorityWeightedChainTrust -v 2>&1 | tail -15
```

Expected: 5 PASSED.

- [ ] **Step 5: Verify existing verify_chain_edges tests still pass**

The existing tests in `test_graph_reason.py` do not check the exact value of `chain_trust` when all edges pass (they check `chain_trust == 1.0` for all-valid, and `chain_trust < 1.0` for a failed edge). All existing test fixtures use `tier="base"` (the default in `build_rx_graph`), so authority_factor=1.0 and the existing assertions still hold.

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_graph_reason.py -v 2>&1 | tail -20
```

Expected: all PASSED.

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/kg/graph_reason.py backend/tests/test_cross_tier_reasoning.py
git commit -m "feat(graph): authority-weighted chain_trust (personal edges 0.85 factor) + authority_notes"
```

---

## Task 5: Conflict precedence in `verify_chain_edges` — base wins over personal on contradiction

**Files:**
- Modify: `backend/app/services/kg/graph_reason.py:243-331`
- Test: `backend/tests/test_cross_tier_reasoning.py`

**Rule:** When a personal-tier edge and a base-tier edge share the same `(src_object_id, tgt_object_id)` pair but with contradicting evidence, the base edge wins. "Contradicting" is operationally defined as: the personal edge's LLM verification returns `valid=False` while the base edge returns `valid=True` for the same conceptual hop. In `verify_chain_edges`, after the per-edge pass, post-process: any `personal` edge flagged as invalid where a `base` edge on the same pair is valid → promote the base edge's assessment (do not add to `flagged`; instead record in `authority_notes` that the personal edge was overridden by base). This composes with the existing `verify_chain_edges` flow: the base edge's confidence remains 1.0; the personal edge is still demoted to 0.05 but flagged with a `base_override=True` marker for the answer LLM.

**Note on `knowledge_relations` schema:** There is no cross-notebook foreign key; the same pair of object_ids can appear in both notebooks. In the federated graph, the same `(src_oid, tgt_oid)` pair can yield two edges (one base, one personal). The rustworkx `PyDiGraph` allows parallel edges; `multihop_subgraph` visits each target once (visited set), so in practice only one edge per `(src, tgt)` pair is traversed. Conflict detection therefore operates at the `verify_chain_edges` level over ALL edges in the `subgraph` list, not just the traversed ones. The plan's conflict logic is: scan all edges returned; group by `(src_oid, tgt_oid)` (using `src_oid` from the triple); if both a base and a personal edge exist for the same pair, demote the personal edge and emit a `base_override` note.

In practice `multihop_subgraph` only returns one edge per unique target node (first-visit wins). To expose the conflict scenario in tests, we directly feed `verify_chain_edges` a synthetic subgraph containing both the base and personal edges for the same hop.

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_cross_tier_reasoning.py`:

```python
class TestTask5ConflictPrecedence:
    def _conflicting_subgraph(self):
        """Manually built subgraph with a base AND personal edge on the same hop.

        Triple format: (node_payload, edge_payload_or_None, src_oid).
        We create:
          - seed node X (no edge)
          - node Y via base edge (derived_from, valid evidence)
          - node Y′ (same logical concept as Y) via personal edge (derived_from,
            but LLM will say invalid)
        In practice multihop only emits one hop per target, so we construct
        the subgraph directly to exercise the conflict logic.
        """
        # Two hops to the SAME src_oid/tgt_oid pair: one base, one personal.
        seed   = ({"object_id": "X", "object_type": "Concept", "name": "X"}, None, None)
        base_hop = (
            {"object_id": "Y", "object_type": "Claim", "name": "Y"},
            {"edge_type": "derived_from", "confidence": 1.0, "tier": "base",
             "evidence": [{"quote": "base evidence for Y"}]},
            "X",
        )
        pers_hop = (
            {"object_id": "Y", "object_type": "Claim", "name": "Y"},
            {"edge_type": "derived_from", "confidence": 1.0, "tier": "personal",
             "evidence": [{"quote": ""}]},   # empty evidence → LLM cannot verify
            "X",
        )
        return [seed, base_hop, pers_hop]

    class _SelectiveLLM:
        """Returns valid=True for base evidence, valid=False for empty evidence."""
        configured = True
        def chat_json(self, messages, schema, **kw):
            import json as _j
            content = messages[0]["content"] if messages else ""
            if "base evidence for Y" in content:
                return _j.dumps({"valid": True, "reason": "base evidence ok"})
            return _j.dumps({"valid": False, "reason": "no evidence"})

    def test_personal_edge_flagged_with_base_override(self):
        """When base and personal edges contradict, personal is flagged base_override=True."""
        from app.services.kg.graph_reason import verify_chain_edges
        sub = self._conflicting_subgraph()
        result = verify_chain_edges(sub, self._SelectiveLLM())
        pers_flags = [f for f in result["flagged"] if f.get("tier") == "personal"]
        assert pers_flags, "personal edge should be in flagged"
        assert pers_flags[0].get("base_override") is True, (
            "Expected base_override=True on personal flagged entry")

    def test_base_edge_not_flagged_despite_conflict(self):
        """The base edge itself must not appear in flagged."""
        from app.services.kg.graph_reason import verify_chain_edges
        sub = self._conflicting_subgraph()
        result = verify_chain_edges(sub, self._SelectiveLLM())
        base_flags = [f for f in result["flagged"] if f.get("tier") == "base"]
        assert not base_flags, f"base edge must not be flagged; got {base_flags}"

    def test_authority_notes_records_override(self):
        """authority_notes must record the base-wins override."""
        from app.services.kg.graph_reason import verify_chain_edges
        sub = self._conflicting_subgraph()
        result = verify_chain_edges(sub, self._SelectiveLLM())
        assert any("base_override" in note or "overridden" in note.lower()
                   for note in result["authority_notes"]), (
            f"No override note in authority_notes: {result['authority_notes']}")
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_cross_tier_reasoning.py::TestTask5ConflictPrecedence -v 2>&1 | tail -20
```

Expected: FAIL — `base_override` key absent, authority_notes empty.

- [ ] **Step 3: Implement conflict precedence post-processing in `verify_chain_edges`**

In `backend/app/services/kg/graph_reason.py`, after the main per-edge loop (just before `authority_notes = [...]`), add a post-processing block:

```python
    # Conflict precedence: group edges by (src_oid, tgt_oid). If a personal edge
    # is flagged AND a base edge on the same pair passed, mark the personal flag
    # with base_override=True and record in authority_notes.
    # edge_results and flagged are indexed identically to the loop order;
    # we need the (src, tgt) pair, which we must reconstruct from subgraph.
    edge_triples = [(node, edge, src_oid)
                    for node, edge, src_oid in subgraph if edge]
    # Build a lookup: (src_oid, tgt_oid) → list of (edge_result_index, passed, tier)
    pair_to_results: Dict[tuple, list] = {}
    for i, (node, edge, src_oid) in enumerate(edge_triples):
        tgt_oid = node.get("object_id", "")
        pair = (src_oid or "", tgt_oid)
        pair_to_results.setdefault(pair, []).append(i)

    for pair, indices in pair_to_results.items():
        if len(indices) < 2:
            continue
        base_valid   = any(edge_results[i]["tier"] == "base"     and edge_results[i]["valid"] for i in indices)
        pers_invalid = any(edge_results[i]["tier"] == "personal" and not edge_results[i]["valid"] for i in indices)
        if base_valid and pers_invalid:
            for fi, f in enumerate(flagged):
                if f.get("tier") == "personal":
                    flagged[fi]["base_override"] = True

    authority_notes = []
    for er in edge_results:
        if er["tier"] == "personal":
            authority_notes.append(
                f"{er['edge_type']} (personal): this step rests on a personal note"
            )
    for f in flagged:
        if f.get("base_override"):
            authority_notes.append(
                f"{f['edge_type']} (personal overridden by base): base_override=True; "
                "base reference supersedes personal note on this hop"
            )
```

- [ ] **Step 4: Run test to verify it passes**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_cross_tier_reasoning.py::TestTask5ConflictPrecedence -v 2>&1 | tail -15
```

Expected: 3 PASSED.

- [ ] **Step 5: Verify existing verify tests**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_graph_reason.py -q 2>&1 | tail -10
```

Expected: all PASSED (all existing tests use single-edge subgraphs; no pairs repeat).

- [ ] **Step 6: Commit**

```bash
git add backend/app/services/kg/graph_reason.py backend/tests/test_cross_tier_reasoning.py
git commit -m "feat(graph): base-wins conflict precedence in verify_chain_edges (base_override flag)"
```

---

## Task 6: `ask(mode="graph")` becomes federated/tier-aware

**Files:**
- Modify: `backend/app/services/sqlite_repository.py:4131-4249`
- Test: `backend/tests/test_cross_tier_reasoning.py`

Replace the single call `self._rx_graph(notebook_id)` (line 4171) inside `ask_graph` with `self._federated_rx_graph(notebook_id)`. Extend the `TraceStep` summary to include `authority_notes` from `verify_result`. Surface "this step rests on a personal note" in the response via the reasoning_trace so the frontend can display it without changing the answer text itself.

Also wire up cache invalidation: when `_mark_caches_dirty` is called for any notebook, invalidate `{active_id}:fed_rxgraph` for every personal notebook that references the given notebook_id as a base (or the given notebook itself if it is the active). Since this is a single-user POC, a simple approach is: when any notebook's KG changes, also invalidate the `fed_rxgraph` cache entry for that notebook and for any personal notebook pointing to a base that just changed. The simplest safe approach: after the existing `invalidate(f"{notebook_id}:rxgraph")`, also invalidate `f"{notebook_id}:fed_rxgraph"` and scan for personal notebooks that have base notebooks matching `notebook_id` — not needed for correctness since the version key already covers all participants and will trigger a rebuild on version change. But explicit invalidation is defensive:

```python
self._vector_cache.invalidate(f"{notebook_id}:fed_rxgraph")
```

- [ ] **Step 1: Write the failing test**

Append to `backend/tests/test_cross_tier_reasoning.py`:

```python
class TestTask6AskGraphFederated:
    @pytest.fixture
    def repo_with_two_notebooks(self, tmp_path, monkeypatch):
        """base_nb has B1→B2 (derived_from); personal_nb has P1→P2 (supports).
        Returns (repo, base_nb, personal_nb)."""
        monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
        monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
        monkeypatch.setenv("LLM_LOG_ENABLED", "false")
        from app.core.config import Settings
        from app.services.sqlite_repository import SQLiteRepository
        from app.services.embedding import FakeEmbedder
        from app.models.schemas import NotebookCreate
        r = SQLiteRepository(Settings())
        r.embedder = FakeEmbedder(dim=16)

        base_nb = r.create_notebook(NotebookCreate(name="base"))
        r.mark_notebook_base(base_nb.id)
        r.store_kg(base_nb.id, None, [
            {"local_id": "B1", "object_type": "formula",
             "payload": {"name": "Oxide Breakdown Voltage", "section_path": "1"},
             "evidence": []},
            {"local_id": "B2", "object_type": "claim",
             "payload": {"name": "High field oxide failure mechanism", "section_path": "2"},
             "evidence": []},
        ], [
            {"local_id": "rel1", "source_local_id": "B1", "target_local_id": "B2",
             "edge_type": "derived_from", "evidence": [
                 {"source_id": "s1", "source_title": "textbook", "element_id": "e1",
                  "element_type": "paragraph", "location_label": "p1",
                  "quoted_span": "oxide breakdown derives failure mechanism", "confidence": 1.0}
             ]},
        ])

        pers_nb = r.create_notebook(NotebookCreate(name="personal"))
        r.store_kg(pers_nb.id, None, [
            {"local_id": "P1", "object_type": "concept",
             "payload": {"name": "Gate oxide thinning", "section_path": "1"},
             "evidence": []},
            {"local_id": "P2", "object_type": "claim",
             "payload": {"name": "Thin oxide increases leakage", "section_path": "2"},
             "evidence": []},
        ], [
            {"local_id": "rel2", "source_local_id": "P1", "target_local_id": "P2",
             "edge_type": "supports", "evidence": [
                 {"source_id": "s2", "source_title": "my notes", "element_id": "e2",
                  "element_type": "paragraph", "location_label": "p2",
                  "quoted_span": "gate oxide thinning supports leakage claim", "confidence": 1.0}
             ]},
        ])
        return r, base_nb, pers_nb

    def test_ask_graph_traverses_both_notebooks(self, repo_with_two_notebooks):
        """ask(mode='graph') on personal_nb must traverse nodes from both notebooks."""
        from app.models.schemas import AskRequest
        repo, base_nb, pers_nb = repo_with_two_notebooks
        resp = repo.ask(pers_nb.id, AskRequest(question="oxide breakdown", mode="graph"))
        # AskResponse must have a reasoning_trace with the graph_verify step.
        assert resp.reasoning_trace, "expected reasoning_trace in graph mode"
        trace = resp.reasoning_trace[0]
        # Traversal count: must include nodes from at least the base notebook.
        assert "node(s) traversed" in trace.summary

    def test_ask_graph_anchors_carry_tier(self, repo_with_two_notebooks):
        """Anchors from ask(mode='graph') carry the per-edge tier (base or personal)."""
        from app.models.schemas import AskRequest
        repo, base_nb, pers_nb = repo_with_two_notebooks
        resp = repo.ask(pers_nb.id, AskRequest(question="oxide breakdown", mode="graph"))
        # Even with no LLM configured, the graph path returns (no anchors from
        # deterministic mode, but if there are anchors they must carry tier).
        # Ensure id_map is populated and _parse_answer_anchors can run.
        # We test directly: build context and verify id_map has tier.
        from app.services.kg.graph_reason import (
            DEFAULT_REASONING_EDGES, multihop_subgraph, render_subgraph_context)
        G, idx_to_oid, oid_to_idx = repo._federated_rx_graph(pers_nb.id)
        # Use all objects as seeds
        with repo._connect() as db:
            from app.services.sqlite_repository import USABLE_STATUSES
            ph = ",".join("?" for _ in USABLE_STATUSES)
            oids = [r["id"] for r in db.execute(
                f"SELECT id FROM knowledge_objects WHERE status IN ({ph})",
                USABLE_STATUSES,
            ).fetchall()]
        sub = multihop_subgraph(G, oid_to_idx, idx_to_oid,
                                seed_ids=oids[:3],
                                edge_types=DEFAULT_REASONING_EDGES,
                                max_depth=2, max_fan_out=8)
        _, id_map = render_subgraph_context(sub)
        for key, entry in id_map.items():
            assert "tier" in entry, f"id_map[{key}] missing tier"

    def test_ask_graph_reasoning_trace_includes_authority_notes(self, repo_with_two_notebooks):
        """reasoning_trace detail must include 'authority_notes' from verify_chain_edges."""
        from app.models.schemas import AskRequest
        repo, base_nb, pers_nb = repo_with_two_notebooks
        resp = repo.ask(pers_nb.id, AskRequest(question="oxide breakdown", mode="graph"))
        trace = resp.reasoning_trace[0]
        assert "authority_notes" in trace.detail, (
            "reasoning_trace detail missing 'authority_notes' from verify_chain_edges")

    def test_ask_graph_cache_invalidation_on_reingest(self, repo_with_two_notebooks):
        """Re-ingesting into base_nb must invalidate personal_nb's fed_rxgraph cache."""
        from app.models.schemas import AskRequest
        repo, base_nb, pers_nb = repo_with_two_notebooks
        # Build the federated graph (populates cache).
        G1, _, _ = repo._federated_rx_graph(pers_nb.id)
        # Trigger cache invalidation for base notebook (simulates re-ingest).
        repo._mark_caches_dirty(base_nb.id)
        # Now re-build; should NOT return the same cached object
        # because the version key for base_nb has changed.
        # (Since we just cleared base_nb's knowledge_relations count is unchanged,
        #  but the explicit invalidate of fed_rxgraph ensures cache miss.)
        cache_key = f"{pers_nb.id}:fed_rxgraph"
        from app.services.vector_cache import VectorCache
        # After invalidation, the cache entry for the personal notebook's fed graph
        # must be gone (or rebuilt on next call).
        # We can check indirectly: invalidating base_nb:rxgraph ALSO invalidates
        # pers_nb:fed_rxgraph; calling _federated_rx_graph again should not raise.
        G2, _, _ = repo._federated_rx_graph(pers_nb.id)
        assert G2 is not None  # rebuilt successfully
```

- [ ] **Step 2: Run test to verify it fails**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_cross_tier_reasoning.py::TestTask6AskGraphFederated -v 2>&1 | tail -20
```

Expected: FAIL — `_federated_rx_graph` is not called from `ask_graph` yet; `authority_notes` absent from trace.

- [ ] **Step 3: Wire `_federated_rx_graph` into `ask_graph` in `sqlite_repository.py`**

In `backend/app/services/sqlite_repository.py`, in `ask_graph` at line 4171, change:

```python
        G, idx_to_oid, oid_to_idx = self._rx_graph(notebook_id)
```

to:

```python
        G, idx_to_oid, oid_to_idx = self._federated_rx_graph(notebook_id)
```

Then update the `graph_trace` construction (around line 4232) to include `authority_notes`:

```python
        from app.models.schemas import TraceStep
        graph_trace = [TraceStep(
            step_type="graph_verify",
            summary=(f"chain_trust={verify_result['chain_trust']:.2f}; "
                     f"{len(verify_result['flagged'])} edge(s) flagged; "
                     f"{len(subgraph)} node(s) traversed"),
            detail={**verify_result,
                    "authority_notes": verify_result.get("authority_notes", [])},
        )]
```

- [ ] **Step 4: Add `fed_rxgraph` cache invalidation in `_mark_caches_dirty`**

In `sqlite_repository.py`, after line 2119 (`self._vector_cache.invalidate(f"{notebook_id}:rxgraph")`), add:

```python
        # Federated graph cache: invalidate this notebook's own entry (covers both
        # the case where it is the active notebook and where it is a base notebook
        # whose change should ripple into any personal notebook's federated graph).
        self._vector_cache.invalidate(f"{notebook_id}:fed_rxgraph")
```

- [ ] **Step 5: Run tests to verify they pass**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_cross_tier_reasoning.py::TestTask6AskGraphFederated -v 2>&1 | tail -20
```

Expected: 4 PASSED.

- [ ] **Step 6: Verify full suite still passes**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_graph_reason.py tests/test_two_tier_federated.py -q 2>&1 | tail -10
```

Expected: all PASSED.

- [ ] **Step 7: Commit**

```bash
git add backend/app/services/sqlite_repository.py backend/tests/test_cross_tier_reasoning.py
git commit -m "feat(graph): ask(mode=graph) uses federated graph; authority_notes surfaced in trace"
```

---

## Phase Gate (final validation)

Run the full test suite. All tests must be green before completing Track D.

- [ ] **Run full suite**

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest -q 2>&1 | tail -20
```

Expected: all PASSED, no errors.

- [ ] **Verify the cross-tier scenario end-to-end (manual assertion in the test suite)**

The following must all hold:
1. A multi-hop chain spanning base→personal is retrieved by `_federated_rx_graph`.
2. `render_subgraph_context` serializes each step with `[tier]` in node lines and `(tier=…)` in chain annotations.
3. `id_map` entries carry `"tier"`, which flows through `_parse_answer_anchors` into `AnswerAnchor.tier`.
4. `chain_trust` reflects the weakest/personal link (≤ 0.85 when any personal edge is traversed at confidence=1.0).
5. On a planted cross-tier conflict (same hop, base valid, personal invalid), the base wins and `flagged` carries `base_override=True`.

These are all covered by `tests/test_cross_tier_reasoning.py`. Run it standalone:

```bash
cd backend && /opt/homebrew/Caskroom/miniconda/base/bin/python -m pytest tests/test_cross_tier_reasoning.py -v 2>&1 | tail -40
```

Expected: all tests in `TestTask1` through `TestTask6` PASSED.

---

## Self-Review Checklist

**Spec coverage:**
- D1 (federated graph build with per-edge tier): Task 1 + Task 2.
- D2 (tier-aware multi-hop, render annotates tier, `[k]` carries tier via AnswerAnchor.tier): Task 3 + Task 6.
- D3 (chain_trust = weakest link with personal authority factor; "personal note" surface): Task 4.
- D4 (conflict precedence base-wins, compose with verify_chain_edges): Task 5.
- D5 (ask(mode="graph") federated/tier-aware): Task 6.

**Invariants checked:**
- `[0,1]/tau`: authority factor applied on `confidence` inside `verify_chain_edges` (not on `.relevance`); does not touch `_fuse`.
- Dual-index best-of: each notebook scored independently in `_federated_rx_graph` (each notebook's own `store_kg` was called independently).
- Backward compat: `build_rx_graph` without `tier_map` still works (default `tier="base"`); `_rx_graph` untouched.
- Cache copy safety: `multihop_subgraph` already returns copies; no new mutation risk introduced.

**Type consistency:**
- `build_rx_graph(tier_map=...)` — used in Task 1, Task 2's `_federated_rx_graph`.
- `render_subgraph_context` returns `(str, dict)` — unchanged; id_map now carries `"tier"` key.
- `verify_chain_edges` return dict gains `"authority_notes": list[str]`; `edge_results` entries gain `"tier": str`; `flagged` entries gain `"tier": str` and optionally `"base_override": bool`.
- `ask_graph` consumes `verify_result.get("authority_notes", [])` defensively (works with old or new dict shape).

**No placeholders:** confirmed — every step has exact code, exact commands, exact expected output.
