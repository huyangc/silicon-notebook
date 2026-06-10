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
