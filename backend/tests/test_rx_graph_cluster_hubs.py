"""TD2 — activate cross-document cluster hubs in the reasoning graph path.

These tests cover the *integration* seam (TD1 already unit-tests build_rx_graph's
hub construction in test_graph_reason.py):

  1. _federated_rx_graph aggregates concept_clusters across ALL participants
     (active + base tier) so a base-side member bridges an active-side member.

C3 (hotpath cleanup): this file used to also cover the *repo-level* single-
notebook path via `SqliteRepository._rx_graph` (loads concept_clusters into
cluster_groups, version-cache invalidation on cluster-row changes, cross-doc
bridge reachability) — six tests in total. `_rx_graph` had zero production
callers (grep-confirmed across two review passes; both the reasoning follow_chain
path and the (since retired) full-graph ask engine only ever went through
`_federated_rx_graph`, which merges base+active and subsumes the single-notebook
case), so it was deleted as dead code and those six tests were deleted with it.
No unique coverage is lost: the underlying pure function (`build_rx_graph`,
including cluster-hub construction, transit-only guarantees, and version-key
soundness reasoning) remains exhaustively unit-tested in test_graph_reason.py,
and the repo-level integration seam for the live code path is covered here via
`_federated_rx_graph` (test 1 below).

The retired full-graph ask engine used to demonstrate a second integration seam
here — its own local `multihop_subgraph` call widened edge_types with
`| {"synonym"}` so the cluster hub above wasn't dormant on that path — but
that mode and its call site are gone; only the scope-guard on the shared
`DEFAULT_REASONING_EDGES` constant remains below.
"""
import json

import pytest
from tests.model_testkit import bind_chat_client, bind_all_embedding_clients


class _AnswerLLM:
    configured = True

    def chat_json(self, messages, schema, **kwargs):
        return json.dumps({"answer": "Alpha evidence [k1].", "grounded": True})


# ── fixtures ─────────────────────────────────────────────────────────────────

@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    from app.core.config import Settings
    from app.services.sqlite_repository import SQLiteRepository
    from app.services.embedding import FakeEmbedder
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    bind_chat_client(r, "ask_answer", _AnswerLLM())
    return r


_NOW = "2026-06-29T00:00:00"


def _seed_two_docs(repo, notebook_id):
    """Two objects a1 (src-A) + b1 (src-B), no direct relation between them.

    Each gets a tiny intra-doc partner so the graph is non-trivial, but there is
    NO a*↔b* relation — only co-membership in a cluster can bridge them.
    """
    with repo._write() as db:
        for oid, src in (("a1", "src-A"), ("a2", "src-A"),
                         ("b1", "src-B"), ("b2", "src-B")):
            db.execute(
                "INSERT INTO knowledge_objects "
                "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (oid, notebook_id, "concept", "approved", "",
                 json.dumps({"name": oid.upper()}), "[]", src, _NOW, _NOW))
        # intra-doc edges only (a1->a2, b1->b2); no cross-doc relation
        for rid, s, t in (("ra", "a1", "a2"), ("rb", "b1", "b2")):
            db.execute(
                "INSERT INTO knowledge_relations "
                "(id,notebook_id,source_object_id,target_object_id,edge_type,evidence,created_at,review_status) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (rid, notebook_id, s, t, "supports", "[]", _NOW, "pending"))


def _add_cluster(repo, notebook_id, canonical_id, members, cc_prefix="cc"):
    with repo._write() as db:
        for i, m in enumerate(members):
            db.execute(
                "INSERT INTO concept_clusters "
                "(id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,canonical_description,created_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                (f"{cc_prefix}-{i}", notebook_id, canonical_id, m,
                 canonical_id, "concept", "", _NOW))
        # Production cluster writes bump cluster_mutation_seq (the graph/PPR
        # version key gates on it); mirror that so this raw-insert helper is
        # faithful and the seq-keyed federated cache invalidates.
        repo._bump_cluster_mutation_seq(db, notebook_id)


# ── 1: _federated_rx_graph aggregates clusters across participants ───────────

def test_federated_rx_graph_bridges_base_and_active_via_cluster(repo):
    """_federated_rx_graph spans clusters from ALL participants: a base-tier
    member and an active-tier member sharing one canonical bridge cross-tier."""
    from app.models.schemas import NotebookCreate
    from app.services.kg.graph_reason import multihop_subgraph
    base_nb = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base_nb.id)
    pers_nb = repo.create_notebook(NotebookCreate(name="personal"))
    repo.replace_notebook_bases(pers_nb.id, [base_nb.id], "user-local")

    now = _NOW
    with repo._write() as db:
        # base object B1, active object P1; no relation between them at all
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("B1", base_nb.id, "concept", "approved", "",
             json.dumps({"name": "Shared Concept"}), "[]", "sb", now, now))
        db.execute(
            "INSERT INTO knowledge_objects "
            "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            ("P1", pers_nb.id, "concept", "approved", "",
             json.dumps({"name": "Shared Concept"}), "[]", "sp", now, now))
    # Same canonical_id stored under BOTH notebooks (name-derived, shared).
    _add_cluster(repo, base_nb.id, "K-shared", ["B1"], cc_prefix="ccb")
    _add_cluster(repo, pers_nb.id, "K-shared", ["P1"], cc_prefix="ccp")

    G, idx_to_oid, oid_to_idx = repo._federated_rx_graph(pers_nb.id)
    # Hub present: 2 members of K-shared present across participants.
    hub_nodes = [G[i] for i in G.node_indices() if G[i].get("kind") == "cluster"]
    assert any(h["object_id"] == "cluster:K-shared" for h in hub_nodes), (
        "expected a federated cluster hub spanning base + active members")

    sub = multihop_subgraph(
        G, oid_to_idx, idx_to_oid,
        seed_ids=["P1"],
        edge_types={"supports", "synonym"},
        max_depth=3, max_fan_out=10,
    )
    oids = [n["object_id"] for n, _, _ in sub]
    assert "B1" in oids, "active P1 should reach base B1 via the shared cluster hub"
    assert all(not str(o).startswith("cluster:") for o in oids)


def test_federated_rx_graph_version_invalidates_on_cluster_row(repo):
    """Adding a concept_clusters row to a participant must rebuild the federated
    graph (per-participant concept_clusters in the version key)."""
    from app.models.schemas import NotebookCreate
    base_nb = repo.create_notebook(NotebookCreate(name="base"))
    repo.mark_notebook_base(base_nb.id)
    pers_nb = repo.create_notebook(NotebookCreate(name="personal"))
    repo.replace_notebook_bases(pers_nb.id, [base_nb.id], "user-local")
    with repo._write() as db:
        for oid, nbid in (("B1", base_nb.id), ("P1", pers_nb.id)):
            db.execute(
                "INSERT INTO knowledge_objects "
                "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (oid, nbid, "concept", "approved", "",
                 json.dumps({"name": "Shared Concept"}), "[]", "s", _NOW, _NOW))

    G1, _, _ = repo._federated_rx_graph(pers_nb.id)
    G1b, _, _ = repo._federated_rx_graph(pers_nb.id)
    assert G1 is G1b, "expected a federated cache hit on identical data"

    # Add a cluster row on the BASE participant → version must change.
    _add_cluster(repo, base_nb.id, "K-shared", ["B1"], cc_prefix="ccb")
    G2, _, _ = repo._federated_rx_graph(pers_nb.id)
    assert G2 is not G1, "cluster row on a participant did not invalidate fed graph"


def test_default_reasoning_edges_unchanged_globally():
    """Scope guard: DEFAULT_REASONING_EDGES is a shared constant consumed by
    reasoning's follow_chain (retrieval_candidates.py) and must never grow
    "synonym" — a local edge-type widening a caller needs for its own
    multihop call must stay scoped to that call's own kwargs, never leak into
    the shared frozenset. (The full-graph ask engine that used to demonstrate
    this scoping with its own local `| {"synonym"}` widening has been retired;
    the invariant on the shared constant itself still stands.)"""
    from app.services.kg.graph_reason import DEFAULT_REASONING_EDGES
    assert "synonym" not in DEFAULT_REASONING_EDGES
    assert DEFAULT_REASONING_EDGES == frozenset({"derived_from", "supports", "depends_on"})
