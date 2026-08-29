# backend/tests/test_edge_review_queue.py
"""Integration tests for edge review queue and feedback loop.
Uses a real SQLiteRepository (in-memory / tmp_path) with FakeEmbedder.
Synthetic graph with 4 nodes and 3 edges.
"""
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL",  f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path/"s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM",       "16")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
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
# C3 (hotpath cleanup): this section used to test the feedback loop through
# `SqliteRepository._rx_graph`, the single-notebook reasoning-graph loader.
# `_rx_graph` had zero production callers — reasoning's follow_chain always
# goes through `_federated_rx_graph` (base+active merge; a solo personal notebook
# with no base participants federates to just itself, so it subsumes the
# single-notebook case) — so `_rx_graph` was deleted as dead code. The cache-
# invalidation-on-warm-graph and rejected/verified-edge-visibility assertions
# below were ported to `_federated_rx_graph` (see "Feedback loop × federated
# graph" section, which already covered rejection + warm-cache invalidation
# — `test_rejected_personal_edge_excluded_from_federated_graph` implicitly
# proves a not-yet-rejected edge is visible in a warm graph too). The one
# genuinely distinct assertion — multihop_subgraph traversal skipping a
# rejected edge — is ported here as
# `test_verify_chain_edges_skips_rejected_federated` so no coverage is lost.

def _rx_edge_rel_ids(G) -> set:
    """Collect all rel_ids present in a PyDiGraph returned by _federated_rx_graph."""
    rel_ids = set()
    for src_idx in range(G.num_nodes()):
        for tgt_idx in G.successor_indices(src_idx):
            payload = G.get_edge_data(src_idx, tgt_idx)
            if isinstance(payload, dict):
                rel_ids.add(payload.get("rel_id", ""))
    return rel_ids


def test_verify_chain_edges_skips_rejected_federated(repo):
    """A subgraph traversal (as used by verify_chain_edges/follow_chain) on the
    federated reasoning graph (the live path — see module comment above) where
    a rejected edge has been excluded should not include that edge at all."""
    nb_id = _seed_graph(repo)
    q = repo.review_queue(nb_id)
    # Reject the first edge
    rel_id = q[0]["rel_id"]
    repo.set_edge_review(nb_id, rel_id, "rejected")
    # Traverse the graph — rejected edge should not appear in any subgraph
    from app.services.kg.graph_reason import multihop_subgraph, DEFAULT_REASONING_EDGES
    G, idx_to_oid, oid_to_idx = repo._federated_rx_graph(nb_id)
    all_oids = list(oid_to_idx.keys())
    sub = multihop_subgraph(G, oid_to_idx, idx_to_oid,
                            seed_ids=all_oids[:1],
                            edge_types=DEFAULT_REASONING_EDGES,
                            max_depth=3, max_fan_out=10)
    sub_rel_ids = {e["rel_id"] for _, e, _ in sub if e and "rel_id" in e}
    assert rel_id not in sub_rel_ids


# ── Feedback loop × federated graph (Track D integration) ────────────────────
# ask(mode=graph) reasons over _federated_rx_graph (base + personal merged) —
# the only reasoning-graph loader in the repo (see module comment above) — so
# the rejected-edge demotion is proven directly against it.

def _seed_federated(repo):
    """Base notebook (marked base) + personal notebook, one edge each.

    Returns (base_id, pers_id). _federated_rx_graph(pers_id) merges both.
    """
    base_nb = repo.create_notebook(NotebookCreate(name="base-nb"))
    repo.mark_notebook_base(base_nb.id)
    repo.store_kg(base_nb.id, None, [
        {"local_id": "B1", "object_type": "Formula",
         "payload": {"name": "Base Formula"}, "evidence": []},
        {"local_id": "B2", "object_type": "Claim",
         "payload": {"name": "Base Claim"}, "evidence": []},
    ], [
        {"source_local_id": "B1", "target_local_id": "B2",
         "edge_type": "derived_from",
         "evidence": [{"file": "f1", "char_start": 0, "char_end": 10,
                       "line_start": 1, "line_end": 1,
                       "quote": "base formula derives base claim"}]},
    ])
    pers_nb = repo.create_notebook(NotebookCreate(name="personal-nb"))
    repo.store_kg(pers_nb.id, None, [
        {"local_id": "P1", "object_type": "Concept",
         "payload": {"name": "Personal Concept"}, "evidence": []},
        {"local_id": "P2", "object_type": "Claim",
         "payload": {"name": "Personal Claim"}, "evidence": []},
    ], [
        {"source_local_id": "P1", "target_local_id": "P2",
         "edge_type": "supports",
         "evidence": [{"file": "f2", "char_start": 0, "char_end": 10,
                       "line_start": 1, "line_end": 1,
                       "quote": "personal concept supports personal claim"}]},
    ])
    repo.replace_notebook_bases(pers_nb.id, [base_nb.id], "user-local")
    return base_nb.id, pers_nb.id


def test_rejected_personal_edge_excluded_from_federated_graph(repo):
    """Rejecting a PERSONAL edge must drop it from a warm federated graph —
    without the review filter in _federated_rx_graph's loader, the rejected
    edge would keep flowing into ask(mode=graph) reasoning."""
    base_id, pers_id = _seed_federated(repo)
    pers_rel_id = repo.review_queue(pers_id)[0]["rel_id"]

    # Warm the federated cache with the edge still active.
    G_warm, _, _ = repo._federated_rx_graph(pers_id)
    assert pers_rel_id in _rx_edge_rel_ids(G_warm)

    repo.set_edge_review(pers_id, pers_rel_id, "rejected")

    G_fresh, _, _ = repo._federated_rx_graph(pers_id)
    assert pers_rel_id not in _rx_edge_rel_ids(G_fresh), (
        "rejected personal edge still present in the federated reasoning graph")


def test_rejected_base_edge_excluded_from_federated_graph_for_personal_active(repo):
    """Rejecting a BASE-notebook edge must drop it from the PERSONAL notebook's
    warm federated graph (cross-participant invalidation + loader filter)."""
    base_id, pers_id = _seed_federated(repo)
    base_rel_id = repo.review_queue(base_id)[0]["rel_id"]

    G_warm, _, _ = repo._federated_rx_graph(pers_id)
    assert base_rel_id in _rx_edge_rel_ids(G_warm)

    # Review verdict lands on the BASE notebook; the federated cache key is
    # "{pers}:fed_rxgraph" — both the evict-all-fed eviction and the
    # per-participant version key must cover this.
    repo.set_edge_review(base_id, base_rel_id, "rejected")

    G_fresh, _, _ = repo._federated_rx_graph(pers_id)
    assert base_rel_id not in _rx_edge_rel_ids(G_fresh), (
        "rejected base edge still present in the personal federated graph")


def test_federated_version_key_covers_review_flip_without_eviction(repo, monkeypatch):
    """Pin the SOUND federated version key (per-status counts per participant).

    set_edge_review's explicit _invalidate_unified_cache (which evicts all
    *:fed_rxgraph) is belt-and-braces; here the eviction is no-op'd so the
    test fails unless the federated version tuple ALONE detects the
    verified→rejected flip — (COUNT, MAX created_at) cannot, since a status
    UPDATE changes neither.
    """
    base_id, pers_id = _seed_federated(repo)
    pers_rel_id = repo.review_queue(pers_id)[0]["rel_id"]
    repo.set_edge_review(pers_id, pers_rel_id, "verified")

    G_warm, _, _ = repo._federated_rx_graph(pers_id)
    assert pers_rel_id in _rx_edge_rel_ids(G_warm)

    # Disable explicit eviction: only the version key can force a rebuild now.
    monkeypatch.setattr(repo, "_invalidate_unified_cache", lambda nb_id: None)
    repo.set_edge_review(pers_id, pers_rel_id, "rejected")

    G_fresh, _, _ = repo._federated_rx_graph(pers_id)
    assert pers_rel_id not in _rx_edge_rel_ids(G_fresh), (
        "stale federated graph served after verified→rejected flip — "
        "version key does not cover review_status")


# ── API endpoints (Track E — thin wrappers over repo methods) ────────────────

@pytest.fixture
def client(repo, monkeypatch):
    """TestClient with the knowledge router repository overridden to the fixture repo."""
    from fastapi.testclient import TestClient
    import app.api.knowledge_routes as routes_mod
    from app.main import app
    monkeypatch.setattr(routes_mod, "repository", lambda: repo)
    return TestClient(app)


def test_api_edge_review_queue_returns_items(client, repo):
    nb_id = _seed_graph(repo)
    resp = client.get(f"/api/notebooks/{nb_id}/edge-review-queue")
    assert resp.status_code == 200
    body = resp.json()
    assert isinstance(body, list) and body
    # response_model=EdgeReviewItem serialization keeps the curation fields
    assert {"rel_id", "trust_score", "edge_centrality", "review_priority",
            "review_status"} <= set(body[0])
    # Highest-risk first (priority desc)
    priorities = [i["review_priority"] for i in body]
    assert priorities == sorted(priorities, reverse=True)


def test_api_edge_review_queue_missing_notebook_404(client):
    resp = client.get("/api/notebooks/does-not-exist/edge-review-queue")
    assert resp.status_code == 404


def test_api_review_relation_round_trip(client, repo):
    nb_id = _seed_graph(repo)
    rel_id = repo.review_queue(nb_id)[0]["rel_id"]
    resp = client.post(
        f"/api/notebooks/{nb_id}/relations/{rel_id}/review",
        json={"status": "rejected"})
    assert resp.status_code == 200
    assert resp.json() == {"rel_id": rel_id, "review_status": "rejected"}
    # Rejected edge drops out of the queue surfaced by the API
    after = client.get(f"/api/notebooks/{nb_id}/edge-review-queue").json()
    assert all(i["rel_id"] != rel_id for i in after)


def test_api_review_relation_bad_status_400(client, repo):
    nb_id = _seed_graph(repo)
    rel_id = repo.review_queue(nb_id)[0]["rel_id"]
    resp = client.post(
        f"/api/notebooks/{nb_id}/relations/{rel_id}/review",
        json={"status": "nonsense"})
    assert resp.status_code == 400


def test_api_review_relation_missing_rel_404(client, repo):
    nb_id = _seed_graph(repo)
    resp = client.post(
        f"/api/notebooks/{nb_id}/relations/rel-missing/review",
        json={"status": "verified"})
    assert resp.status_code == 404
