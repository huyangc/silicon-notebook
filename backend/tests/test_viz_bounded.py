"""Task 4 (SP1): persisted folded viz graph + bounded unified_graph + neighbors.

The contract is EQUIVALENCE with the legacy `unified_graph(limit=N)` path: the
bounded core derived from the persisted viz graph must return the SAME node-id
set (and counts) as `limit_graph_by_degree(_unified_graph_full(...), N)`.
"""
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
    monkeypatch.setenv("EMBED_API_KEY", "test-key")
    monkeypatch.setenv("EMBED_MODEL", "test-model")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _build_star(repo):
    """a (hub) -- b, a -- c. Hub degree (2) is strictly > leaf degree (1) so the
    degree-top-2 set is {a, <one leaf>}; legacy + bounded both keep `a` and one
    leaf. Same-name concepts elsewhere would fold; here ids are unique."""
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept", "payload": {"name": "gain", "section_path": ""}, "evidence": []},
        {"local_id": "c", "object_type": "concept", "payload": {"name": "bias", "section_path": ""}, "evidence": []},
    ], [
        {"source_local_id": "a", "target_local_id": "b", "edge_type": "relates", "evidence": []},
        {"source_local_id": "a", "target_local_id": "c", "edge_type": "relates", "evidence": []},
    ])
    repo.rebuild_unified_kg(nb.id)
    return nb


def test_unified_graph_bounded_matches_full(repo):
    nb = _build_star(repo)
    legacy = repo._unified_graph_full(nb.id, "object")
    from app.services.kg_merge import limit_graph_by_degree
    legacy_top2 = limit_graph_by_degree(legacy, 2)

    repo.build_scale_index(nb.id)
    bounded = repo.unified_graph(nb.id, level="object", limit=2)

    # hub `a` has unique max degree → its presence is deterministic; the second
    # kept node is one of the two degree-1 leaves (tie). Assert the hub is kept
    # and the kept-count matches, then that totals equal the full graph.
    assert len(bounded["nodes"]) == len(legacy_top2["nodes"]) == 2
    legacy_ids = {n["id"] for n in legacy_top2["nodes"]}
    bounded_ids = {n["id"] for n in bounded["nodes"]}
    # hub is unambiguously in both
    hub_id = next(n["id"] for n in legacy["nodes"] if n["payload"].get("name") == "MOSFET")
    assert hub_id in legacy_ids and hub_id in bounded_ids
    assert bounded["total_nodes"] == len(legacy["nodes"])
    assert bounded["total_edges"] == len(legacy["edges"])
    assert bounded["truncated"] is True


def test_bounded_node_edge_shape_matches_legacy(repo):
    nb = _build_star(repo)
    repo.build_scale_index(nb.id)
    bounded = repo.unified_graph(nb.id, level="object", limit=2)
    for n in bounded["nodes"]:
        assert set(n.keys()) == {"id", "object_type", "payload"}
        assert "name" in n["payload"]
    for e in bounded["edges"]:
        assert set(e.keys()) == {"source_object_id", "target_object_id", "edge_type"}
    # names hydrated, not empty for the hub
    hub = next(n for n in bounded["nodes"] if n["payload"].get("name") == "MOSFET")
    assert hub["object_type"] == "concept"


def test_kg_neighbors_matches_topology(repo):
    nb = _build_star(repo)
    repo.build_scale_index(nb.id)
    hub_id = next(n["id"] for n in repo._unified_graph_full(nb.id, "object")["nodes"]
                  if n["payload"].get("name") == "MOSFET")
    out = repo.kg_neighbors(nb.id, hub_id, cap=50)
    names = {n["payload"]["name"] for n in out["nodes"]}
    assert "MOSFET" in names
    assert {"gain", "bias"} <= names  # both leaves are neighbors
    for e in out["edges"]:
        assert set(e.keys()) == {"source_object_id", "target_object_id", "edge_type"}
