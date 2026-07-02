"""C1: `_scale_xlayer_bridge_edges` must load vectors scoped to the ACTIVE
DELTA node id set (the nodes being spliced this call), not the notebook's
FULL `knowledge_embeddings` table.

Equivalence oracle: on a fixture where delta ⊂ all (active notebook has more
KG nodes than the delta passed to the bridge helper), the delta-scoped load
must produce bridge edges IDENTICAL to the old full-table-load implementation
(oracle = call with active_node_ids=None, which still uses the full
`_vector_matrix` path).
"""
import json
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
    for k, v in {"EMBED_PROVIDER": "dashscope", "EMBED_BASE_URL": "https://e.test",
                 "EMBED_API_KEY": "k", "EMBED_MODEL": "m", "EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings())
    r.embedder = FakeEmbedder(dim=16)
    return r


def _seed_base_with_chunk(repo, name="MOSFET"):
    """Base notebook: one KG concept with an embedding + ANN index built."""
    base = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base.id)
    with repo._write() as db:
        now = "2026-06-29T00:00:00"
        db.execute("INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?)", ("sB", base.id, "paper", "md", "ready", now, now))
        db.execute("INSERT INTO chunks (id,notebook_id,source_id,text,section_path,element_ids,created_at) "
                   "VALUES (?,?,?,?,?,?,?)",
                   ("cB", base.id, "sB", f"A {name} provides voltage gain.", "S",
                    json.dumps(["elB"]), now))
        ev = json.dumps([{"source_id": "sB", "source_title": "", "element_id": "elB",
                          "element_type": "paragraph", "location_label": "p1",
                          "quoted_span": name, "confidence": 1.0}])
        db.execute("INSERT INTO knowledge_objects "
                   "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                   "VALUES (?,?,?,?,?,?,?,?,?,?)",
                   ("eB", base.id, "concept", "approved", "",
                    json.dumps({"name": name}), ev, "sB", now, now))
        vec = repo.embedder.embed_query(name)
        db.execute("INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) "
                   "VALUES (?,?,?,?)", ("eB", base.id, json.dumps(vec), now))
    repo.rebuild_unified_kg(base.id)
    repo.build_scale_index(base.id)
    return base


def _seed_active_with_extra_nodes(repo, n_extra=5):
    """Active notebook with several KG concepts (all with embeddings) — the
    'all' set for the delta ⊂ all equivalence check. Returns
    (notebook, {name: object_id}) since store_kg re-mints ids (local_id is not
    the persisted id)."""
    active = repo.create_notebook(NotebookCreate(name="active"))
    names = ["MOSFET"] + [f"concept-{i}" for i in range(n_extra)]
    objects, relations = [], []
    for i, n in enumerate(names):
        objects.append({"local_id": f"o{i}", "object_type": "concept",
                         "payload": {"name": n, "section_path": ""}, "evidence": []})
    repo.store_kg(active.id, None, objects, relations)
    repo.rebuild_unified_kg(active.id)
    # Give every knowledge_object an embedding directly (store_kg alone does not embed).
    name_to_id = {}
    with repo._write() as db:
        rows = db.execute(
            "SELECT id, payload FROM knowledge_objects WHERE notebook_id=?",
            (active.id,)).fetchall()
        now = "2026-06-30T00:00:00"
        for r in rows:
            name = json.loads(r["payload"]).get("name", "")
            name_to_id[name] = r["id"]
            vec = repo.embedder.embed_query(name)
            db.execute(
                "INSERT OR REPLACE INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) "
                "VALUES (?,?,?,?)", (r["id"], active.id, json.dumps(vec), now))
    return active, name_to_id


def test_bridge_edges_equal_oracle_when_delta_subset_of_all(repo):
    """delta ⊂ all: scoping to a strict subset of active node ids must produce
    a subset of the edges the old full-load path (active_node_ids=None) finds
    — and for the nodes actually included, the SAME edges (byte-identical
    ids/weights)."""
    base = _seed_base_with_chunk(repo)
    active, name_to_id = _seed_active_with_extra_nodes(repo, n_extra=5)
    base_idx = repo._scale_index(base.id, allow_stale=True)
    base_indexes = [(base.id, base_idx)]

    all_node_ids, all_edges, _ = repo._active_kg_delta(active.id)
    assert len(all_node_ids) >= 2, "fixture should have multiple active KG nodes"

    # Oracle: full-table load (old behaviour).
    oracle_edges = repo._scale_xlayer_bridge_edges(
        active.id, base_indexes, list(all_edges), active_node_ids=None)

    # Delta scoped to a strict subset (only the MOSFET-named node).
    mosfet_id = name_to_id["MOSFET"]
    delta_ids = [nid for nid in all_node_ids if nid == mosfet_id]
    assert delta_ids and len(delta_ids) < len(all_node_ids), "delta must be a proper subset"
    scoped_edges = repo._scale_xlayer_bridge_edges(
        active.id, base_indexes, list(all_edges), active_node_ids=delta_ids)

    oracle_new = set(oracle_edges) - set(all_edges)
    scoped_new = set(scoped_edges) - set(all_edges)

    # Every scoped bridge edge must appear in the oracle's edge set (no
    # spurious edges introduced by scoping) — and since delta_ids ⊂ all with
    # only the MOSFET node included, all oracle edges touching it must be
    # reproduced.
    assert scoped_new, "scoped bridge should find at least the mosfet<->base edge"
    assert scoped_new <= oracle_new, f"scoped edges must be subset of oracle: {scoped_new - oracle_new}"
    oracle_mosfet_edges = {e for e in oracle_new if e[0] == mosfet_id or e[1] == mosfet_id}
    assert scoped_new == oracle_mosfet_edges, (
        f"scoped(delta={{mosfet}}) must equal oracle's mosfet-touching edges; "
        f"scoped={scoped_new} oracle_mosfet={oracle_mosfet_edges}")


def test_bridge_edges_empty_delta_no_load(repo, monkeypatch):
    """Empty active_node_ids → no vector load at all (short-circuit before any
    DB query), and no bridge edges added."""
    base = _seed_base_with_chunk(repo)
    active, _ = _seed_active_with_extra_nodes(repo, n_extra=2)
    base_idx = repo._scale_index(base.id, allow_stale=True)
    base_indexes = [(base.id, base_idx)]

    calls = {"n": 0}
    orig = repo._delta_vector_matrix

    def spy(*a, **kw):
        calls["n"] += 1
        return orig(*a, **kw)

    monkeypatch.setattr(repo, "_delta_vector_matrix", spy)
    result = repo._scale_xlayer_bridge_edges(
        active.id, base_indexes, [], active_node_ids=[])
    assert result == []
    assert calls["n"] == 0, "empty delta must short-circuit before any vector load"


def test_bridge_edges_spy_no_full_vector_matrix_call(repo, monkeypatch):
    """Spy on `_vector_matrix` (the full-table loader): when active_node_ids is
    given (the normal `_scale_combined_graph` call path), the bridge helper
    must NOT call it — it must use `_delta_vector_matrix` instead."""
    base = _seed_base_with_chunk(repo)
    active, _ = _seed_active_with_extra_nodes(repo, n_extra=3)
    base_idx = repo._scale_index(base.id, allow_stale=True)
    base_indexes = [(base.id, base_idx)]

    all_node_ids, all_edges, _ = repo._active_kg_delta(active.id)

    calls = {"n": 0}
    orig = repo._vector_matrix

    def spy(*a, **kw):
        calls["n"] += 1
        return orig(*a, **kw)

    monkeypatch.setattr(repo, "_vector_matrix", spy)
    repo._scale_xlayer_bridge_edges(
        active.id, base_indexes, list(all_edges), active_node_ids=all_node_ids)
    assert calls["n"] == 0, "delta-scoped call must not touch the full _vector_matrix loader"


def test_scale_combined_graph_passes_delta_node_ids(repo, monkeypatch):
    """End-to-end: `_scale_combined_graph` (the real caller inside scale_ppr)
    must invoke the bridge helper with active_node_ids set (not None) so the
    delta-scoped path is what actually runs in production, not just in
    direct-call tests."""
    base = _seed_base_with_chunk(repo)
    active, _ = _seed_active_with_extra_nodes(repo, n_extra=2)
    base_idx = repo._scale_index(base.id, allow_stale=True)
    base_indexes = [(base.id, base_idx)]

    captured = {}
    orig = repo._scale_xlayer_bridge_edges

    def spy(notebook_id, base_idxs, active_edges, active_node_ids=None):
        captured["active_node_ids"] = active_node_ids
        return orig(notebook_id, base_idxs, active_edges, active_node_ids=active_node_ids)

    monkeypatch.setattr(repo, "_scale_xlayer_bridge_edges", spy)
    repo._scale_combined_graph(active.id, base_indexes)
    assert captured.get("active_node_ids") is not None, (
        "_scale_combined_graph must pass the delta node id set, not rely on the "
        "None-fallback full-table load")
