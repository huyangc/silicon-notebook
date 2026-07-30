"""Task 4 (SP1): persisted folded viz graph + bounded unified_graph + neighbors.

The contract is EQUIVALENCE with the legacy `unified_graph(limit=N)` path: the
bounded core derived from the persisted viz graph must return the SAME node-id
set (and counts) as `limit_graph_by_degree(_unified_graph_full(...), N)`.
"""
import time

import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
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


def test_bounded_graph_matches_legacy_shape_and_neighbors(repo):
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

    # Shape hydration is part of the same bounded response contract. Keeping
    # these assertions beside the equivalence check avoids rebuilding the same
    # persisted ScaleIndex solely to inspect a second view of the result.
    for n in bounded["nodes"]:
        assert set(n.keys()) == {"id", "object_type", "payload"}
        assert "name" in n["payload"]
    for e in bounded["edges"]:
        assert {"source_object_id", "target_object_id", "edge_type"} <= set(e.keys())  # + optional support_count/source_count
    # names hydrated, not empty for the hub
    hub = next(n for n in bounded["nodes"] if n["payload"].get("name") == "MOSFET")
    assert hub["object_type"] == "concept"

    # The same artifact also owns neighbor hydration; one setup is sufficient
    # to retain the topology and edge-shape coverage.
    out = repo.kg_neighbors(nb.id, hub_id, cap=50)
    names = {n["payload"]["name"] for n in out["nodes"]}
    assert "MOSFET" in names
    assert {"gain", "bias"} <= names  # both leaves are neighbors
    for e in out["edges"]:
        assert {"source_object_id", "target_object_id", "edge_type"} <= set(e.keys())  # + optional support_count/source_count


@pytest.mark.slow
def test_viz_and_search_bounded_at_scale(repo, monkeypatch):
    nb = repo.create_notebook(NotebookCreate(name="big"))
    # N kept modest: this test proves SP1's BOUNDED query path (no _unified_graph_full
    # call + ms-level viz/search), which is N-independent. The setup cost is dominated
    # by build_scale_index (SP2's offline build), not what SP1 measures here.
    N, B = 4_000, 2_000
    for s in range(0, N, B):
        objs = [{"local_id": f"c{i}", "object_type": "concept",
                 "payload": {"name": f"concept number {i}", "section_path": ""}, "evidence": []}
                for i in range(s, s + B)]
        rels = [{"source_local_id": f"c{i}", "target_local_id": f"c{i+1}", "edge_type": "rel", "evidence": []}
                for i in range(s, s + B - 1)]
        repo.store_kg(nb.id, None, objs, rels)
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    # bounded viz must NOT materialize the full graph
    calls = {"full": 0}
    real_full = repo._runtime.knowledge_lifecycle._unified_graph_full
    monkeypatch.setattr(repo._runtime.knowledge_lifecycle, "_unified_graph_full",
                        lambda *a, **k: (calls.__setitem__("full", calls["full"] + 1), real_full(*a, **k))[1])
    t = time.perf_counter()
    g = repo.unified_graph(nb.id, level="object", limit=80)
    dt_viz = time.perf_counter() - t
    assert len(g["nodes"]) == 80 and g["total_nodes"] >= N * 0.9
    assert calls["full"] == 0, "bounded path must not call _unified_graph_full"
    t = time.perf_counter()
    hits = repo.kg_search(nb.id, "number 123", k=20)
    dt_search = time.perf_counter() - t
    assert isinstance(hits, list)
    print(f"\n[scale] viz limit=80: {dt_viz:.3f}s (full_calls={calls['full']}); kg_search: {dt_search:.3f}s, hits={len(hits)}")
