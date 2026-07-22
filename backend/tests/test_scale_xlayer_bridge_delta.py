"""C1 (+ fix wave): `_scale_xlayer_bridge_edges` vector-load iteration domain
is dispatched PER PARTICIPANT:

- participant == self (the active notebook's own scale index): domain = the
  ACTIVE DELTA node set (core nodes' synonym edges are already baked into
  self's CSR by build_scale_index — re-bridging into their own index is
  redundant). This is the C1 saving: the current production shape (one big
  self-indexed library, no external base) never loads the full 490k×1024
  embedding matrix.
- participant != self (EXTERNAL base): domain = ALL active nodes (full
  `_vector_matrix`, version-cached). Case-B regression pinned below: when
  self is indexed AND an external base exists, self's core (pre-watermark)
  nodes are NOT in the delta but their cross-layer bridges to the external
  base can only be computed at query time — the original delta-only scoping
  silently severed them (build_scale_index never bakes cross-library
  bridges; exact-id unification can't help since the two libraries mint
  different object ids for the same concept).
"""
import json
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from tests.model_testkit import bind_embedding_client


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    for k, v in {"EMBED_DIM": "16"}.items():
        monkeypatch.setenv(k, v)
    r = SQLiteRepository(Settings())
    bind_embedding_client(r, FakeEmbedder(dim=16))
    return r


def _insert_concept(repo, nb_id, oid, name, sid, day=1):
    """Insert a source + concept object + embedding directly (deterministic ids)."""
    now = f"2026-06-{day:02d}T00:00:00"
    with repo._write() as db:
        db.execute(
            "INSERT OR IGNORE INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?)", (sid, nb_id, "t", "md", "ready", now, now))
        db.execute(
            "INSERT INTO knowledge_objects (id,notebook_id,object_type,status,owner,payload,"
            "evidence,source_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
            (oid, nb_id, "concept", "approved", "", json.dumps({"name": name}), "[]",
             sid, now, now))
        vec = repo.embedder.embed_query(name)
        db.execute(
            "INSERT INTO knowledge_embeddings (object_id,notebook_id,vector,created_at) "
            "VALUES (?,?,?,?)", (oid, nb_id, json.dumps(vec), now))


def _build_indexed_base(repo, name="base", concept="MOSFET", oid="eB", sid="sB", mark_base=True):
    nb = repo.create_notebook(NotebookCreate(name=name))
    if mark_base:
        repo.mark_notebook_base(nb.id)
    _insert_concept(repo, nb.id, oid, concept, sid)
    repo.rebuild_unified_kg(nb.id)
    repo.build_scale_index(nb.id)
    return nb


def _seed_active_unindexed(repo, n_extra=3):
    """Active notebook (no scale index) with several concepts. Returns
    (notebook, {name: object_id})."""
    active = repo.create_notebook(NotebookCreate(name="active"))
    names = ["MOSFET"] + [f"concept-{i}" for i in range(n_extra)]
    for i, n in enumerate(names):
        _insert_concept(repo, active.id, f"oa{i}", n, "sA")
    repo.rebuild_unified_kg(active.id)
    name_to_id = {n: f"oa{i}" for i, n in enumerate(names)}
    return active, name_to_id


# ── Case A: active NOT indexed, external base participant ────────────────────

def test_case_a_external_base_uses_full_domain_equals_oracle(repo):
    """External base participant: domain = ALL active nodes regardless of the
    active_node_ids value passed — result must equal the full-load oracle
    (active_node_ids=None). Scoping the caller's delta must NOT reduce
    external-base bridges (that was the Case-B bug class)."""
    base = _build_indexed_base(repo)
    active, name_to_id = _seed_active_unindexed(repo, n_extra=3)
    base_idx = repo._scale_index(base.id, allow_stale=True)
    base_indexes = [(base.id, base_idx)]

    all_node_ids, all_edges, _ = repo._active_kg_delta(active.id)
    assert len(all_node_ids) >= 2

    oracle_edges = repo.retrieval.graph._scale_xlayer_bridge_edges(
        active.id, base_indexes, list(all_edges), active_node_ids=None)
    oracle_new = set(oracle_edges) - set(all_edges)
    assert oracle_new, "oracle should find the MOSFET<->base bridge"

    # Even when the caller passes a strict subset as delta, the EXTERNAL base
    # participant still bridges from the FULL active node set.
    mosfet_id = name_to_id["MOSFET"]
    subset = [nid for nid in all_node_ids if nid != mosfet_id][:1]
    assert subset, "need a non-MOSFET subset node"
    scoped_edges = repo.retrieval.graph._scale_xlayer_bridge_edges(
        active.id, base_indexes, list(all_edges), active_node_ids=subset)
    scoped_new = set(scoped_edges) - set(all_edges)
    assert scoped_new == oracle_new, (
        f"external-base domain must be the full active set; scoped={scoped_new} "
        f"oracle={oracle_new}")


# ── Case B regression: self indexed + external base — core nodes keep bridges ─

def test_case_b_self_indexed_plus_external_base_keeps_core_bridges(repo):
    """Reviewer's reproduction, pinned: self-indexed library contains MOSFET
    (core, pre-watermark → NOT in delta) + external base contains MOSFET.
    build_scale_index builds each library in isolation (never bakes
    cross-library bridges) and exact-id unification can't merge them (the
    two libraries minted different object ids), so the ONLY cross-layer
    synonym path is the query-time bridge. The delta-only scoping regression
    returned set() here; the per-participant dispatch must restore the
    bidirectional bridge, identical to the pre-delta-scoping oracle."""
    base = _build_indexed_base(repo, name="ext-base", concept="MOSFET",
                               oid="eB", sid="sB")
    # Self: separate notebook, ALSO indexed, with its own MOSFET (different id).
    selfnb = _build_indexed_base(repo, name="self-lib", concept="MOSFET",
                                 oid="eS", sid="sS", mark_base=False)
    base_idx = repo._scale_index(base.id, allow_stale=True)
    self_idx = repo._scale_index(selfnb.id, allow_stale=True)
    assert base_idx is not None and self_idx is not None
    # scale_ppr's participant construction: external bases + self (P0-00).
    base_indexes = [(base.id, base_idx), (selfnb.id, self_idx)]

    # Self is indexed with no post-watermark sources → delta is empty.
    delta_ids, delta_edges, _ = repo._active_kg_delta(selfnb.id)
    assert delta_ids == [], "fixture: self fully indexed, delta must be empty"

    # Pre-delta-scoping oracle: full domain for every participant.
    oracle = repo.retrieval.graph._scale_xlayer_bridge_edges(
        selfnb.id, base_indexes, [], active_node_ids=None)
    oracle_set = set(oracle)
    assert any(a == "eS" and b == "eB" for a, b, _ in oracle_set), (
        "oracle must contain the self-core -> external-base bridge")
    assert any(a == "eB" and b == "eS" for a, b, _ in oracle_set), (
        "oracle must contain the reverse bridge")

    # Fixed code path (delta passed, as _scale_combined_graph does).
    got = repo.retrieval.graph._scale_xlayer_bridge_edges(
        selfnb.id, base_indexes, [], active_node_ids=delta_ids)
    assert set(got) == oracle_set, (
        f"Case B: core-node bridges to the external base must match the "
        f"pre-delta-scoping oracle; got={set(got)} oracle={oracle_set}")


# ── self-only participant: delta domain preserved (the C1 saving) ────────────

def test_self_only_participant_uses_delta_no_full_load(repo, monkeypatch):
    """Production shape (single self-indexed library, NO external base):
    iteration domain stays the delta — spy proves the full `_vector_matrix`
    loader is never touched, and the delta node still bridges into the self
    core (its synonym edge to the same-name core concept).

    opt-in self-delta: `_active_kg_delta` only returns a non-empty self-delta
    when scale_search_include_delta=True (default OFF — see
    test_indexed_only_principle.py); this test explicitly opts in so the
    delta-domain scoping being verified here (vs. the full-table load) has a
    non-empty delta to scope."""
    monkeypatch.setattr(repo.settings, "scale_search_include_delta", True)
    selfnb = _build_indexed_base(repo, name="self-lib", concept="MOSFET",
                                 oid="eS", sid="sS", mark_base=False)
    # Post-watermark delta: a new source with a same-name concept.
    _insert_concept(repo, selfnb.id, "eNew", "MOSFET", "sNew", day=15)
    self_idx = repo._scale_index(selfnb.id, allow_stale=True)
    assert self_idx is not None
    base_indexes = [(selfnb.id, self_idx)]

    delta_ids, delta_edges, _ = repo._active_kg_delta(selfnb.id)
    assert "eNew" in delta_ids and "eS" not in delta_ids

    calls = {"full": 0, "delta": 0}
    orig = repo.retrieval.candidates._vector_matrix
    orig_delta = repo.retrieval.graph._delta_vector_matrix

    def spy(*a, **kw):
        calls["full"] += 1
        return orig(*a, **kw)

    def spy_delta(*a, **kw):
        calls["delta"] += 1
        return orig_delta(*a, **kw)

    monkeypatch.setattr(repo.retrieval.candidates, "_vector_matrix", spy)
    monkeypatch.setattr(repo.retrieval.graph, "_delta_vector_matrix", spy_delta)
    got = repo.retrieval.graph._scale_xlayer_bridge_edges(
        selfnb.id, base_indexes, list(delta_edges), active_node_ids=delta_ids)
    assert calls["full"] == 0, (
        "self-only participant must keep the delta domain (no full matrix load)")
    assert calls["delta"] == 1
    new_edges = set(got) - set(delta_edges)
    assert any(a == "eNew" and b == "eS" for a, b, _ in new_edges), (
        "delta node must still bridge into the self core")


def test_self_only_empty_delta_no_load_at_all(repo, monkeypatch):
    """Self-only participant with an EMPTY delta: nothing to bridge — neither
    the delta loader nor the full loader runs, edges unchanged. (Core-node
    synonym edges are already in self's CSR; with no external base there is
    no query-time bridging work left.)"""
    selfnb = _build_indexed_base(repo, name="self-lib", concept="MOSFET",
                                 oid="eS", sid="sS", mark_base=False)
    self_idx = repo._scale_index(selfnb.id, allow_stale=True)
    base_indexes = [(selfnb.id, self_idx)]
    delta_ids, _, _ = repo._active_kg_delta(selfnb.id)
    assert delta_ids == []

    calls = {"full": 0, "delta": 0}
    orig_full = repo.retrieval.candidates._vector_matrix
    orig_delta = repo.retrieval.graph._delta_vector_matrix

    def spy_full(*a, **kw):
        calls["full"] += 1
        return orig_full(*a, **kw)

    def spy_delta(*a, **kw):
        calls["delta"] += 1
        return orig_delta(*a, **kw)

    monkeypatch.setattr(repo.retrieval.candidates, "_vector_matrix", spy_full)
    monkeypatch.setattr(repo.retrieval.graph, "_delta_vector_matrix", spy_delta)
    got = repo.retrieval.graph._scale_xlayer_bridge_edges(
        selfnb.id, base_indexes, [], active_node_ids=[])
    assert got == []
    assert calls == {"full": 0, "delta": 0}, (
        f"empty delta + self-only must load nothing, got {calls}")


# ── wiring: the real caller passes the delta id set ──────────────────────────

def test_scale_combined_graph_passes_delta_node_ids(repo, monkeypatch):
    """`_scale_combined_graph` (the real caller inside scale_ppr) must invoke
    the bridge helper with active_node_ids set (not None) so the
    per-participant dispatch is what actually runs in production."""
    base = _build_indexed_base(repo)
    active, _ = _seed_active_unindexed(repo, n_extra=2)
    base_idx = repo._scale_index(base.id, allow_stale=True)
    base_indexes = [(base.id, base_idx)]

    captured = {}
    orig = repo.retrieval.graph._scale_xlayer_bridge_edges

    def spy(notebook_id, base_idxs, active_edges, active_node_ids=None):
        captured["active_node_ids"] = active_node_ids
        return orig(notebook_id, base_idxs, active_edges, active_node_ids=active_node_ids)

    monkeypatch.setattr(repo.retrieval.graph, "_scale_xlayer_bridge_edges", spy)
    repo.retrieval.graph._scale_combined_graph(active.id, base_indexes)
    assert captured.get("active_node_ids") is not None, (
        "_scale_combined_graph must pass the delta node id set, not rely on the "
        "None-fallback full-table load")


# Fast inner-loop opt-out: these tests build real HNSW/ANN scale indexes.
# Skip them with `pytest -m "not slow"`; full runs (default) still include them.
import pytest as _pytest_slow  # noqa: E402
pytestmark = _pytest_slow.mark.slow
