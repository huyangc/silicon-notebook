"""Task 3: rebuild_communities on the CANONICAL entity graph (cross-document
bridging via cluster_map), large-lib guard (scale-tier + no CSR refuses
networkx), and reverse-index population.

Fixtures are self-contained (no conftest factory exists): a tmp-path repo with a
FakeEmbedder, seeded by direct inserts mirroring test_ppr_retrieve._seed_two_doc_moe.
"""
import json

import pytest

from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 't.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    r = SQLiteRepository(Settings(_env_file=None))
    r.embedder = FakeEmbedder(dim=16)
    return r


NOW = "2026-07-07T00:00:00"


def _obj(db, nb_id, oid, name, sid):
    db.execute(
        "INSERT INTO knowledge_objects "
        "(id,notebook_id,object_type,status,owner,payload,evidence,source_id,created_at,updated_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?)",
        (oid, nb_id, "concept", "approved", "", json.dumps({"name": name}), "[]", sid, NOW, NOW),
    )


def _cluster(db, nb_id, oid, canonical_id, canonical_name):
    db.execute(
        "INSERT INTO concept_clusters "
        "(id,notebook_id,canonical_id,member_object_id,canonical_name,object_type,created_at) "
        "VALUES (?,?,?,?,?,?,?)",
        (f"cl-{oid}", nb_id, canonical_id, oid, canonical_name, "concept", NOW),
    )


def _rel(db, nb_id, sid, a, b, n):
    db.execute(
        "INSERT INTO knowledge_relations "
        "(id,notebook_id,source_id,source_object_id,target_object_id,edge_type,evidence,created_at) "
        "VALUES (?,?,?,?,?,?,?,?)",
        (f"r-{n}", nb_id, sid, a, b, "related_to", "[]", NOW),
    )


def _seed_two_docs_shared_canonical(repo):
    """Two papers (src-A / src-B) that DISCUSS THE SAME concepts: each paper has
    its own KG objects, but every object is clustered onto one of 4 SHARED
    canonicals K1..K4 (each canonical therefore has members from BOTH docs — the
    real PoC shape: two frontier-model papers sharing MoE/routing/expert concepts).

    Relations wire each doc's own 4 objects into a COMPLETE graph (all 6 pairs);
    under cluster_map both docs' cliques collapse onto the SAME canonical clique
    K1-K2-K3-K4, one dense community Louvain keeps whole (a bare 4-cycle would
    split 2+2 and fall under min_size — a clique will not). Object ids: doc A =
    A1..A4, doc B = B1..B4; A_i and B_i both map to canonical K_i.
    """
    import itertools

    nb = repo.create_notebook(NotebookCreate(name="kb"))
    kname = {"K1": "MoE", "K2": "Routing", "K3": "Experts", "K4": "Attention"}
    with repo._write() as db:
        for sid in ("src-A", "src-B"):
            db.execute(
                "INSERT INTO sources (id,notebook_id,title,source_type,status,created_at,updated_at) "
                "VALUES (?,?,?,?,?,?,?)",
                (sid, nb.id, sid, "md", "ready", NOW, NOW),
            )
        for doc, sid in (("A", "src-A"), ("B", "src-B")):
            for i in (1, 2, 3, 4):
                oid, can = f"{doc}{i}", f"K{i}"
                _obj(db, nb.id, oid, kname[can], sid)
                _cluster(db, nb.id, oid, can, kname[can])
            # Complete graph (K4) among THIS doc's own object ids → canonical clique.
            for a, b in itertools.combinations((1, 2, 3, 4), 2):
                _rel(db, nb.id, sid, f"{doc}{a}", f"{doc}{b}", f"{doc}-{a}{b}")
    return nb


def test_canonical_bridges_documents(repo):
    nb = _seed_two_docs_shared_canonical(repo)
    n = repo.rebuild_communities(nb.id, level=0)
    assert n >= 1
    with repo._connect() as db:
        rows = db.execute(
            "SELECT canonical_id, community_id FROM community_members "
            "WHERE notebook_id=? AND canonical_id IN ('K1','K2','K3','K4')",
            (nb.id,),
        ).fetchall()
    # All 4 shared canonicals present and in ONE community => cross-document bridge
    # holds (each K_i has members from both docs; the community spans src-A+src-B).
    got = {r["canonical_id"] for r in rows}
    assert {"K1", "K2", "K3", "K4"} <= got
    assert len({r["community_id"] for r in rows}) == 1


def test_naive_relation_graph_splits_documents(repo, monkeypatch):
    """Contrast: on the RAW-object graph (cluster_map neutralized so every object
    is its own singleton canonical) the two docs are DISJOINT (8 distinct object
    ids, edges only within each doc) → their members never share a community. This
    is the exact regression the canonical mapping fixes."""
    nb = _seed_two_docs_shared_canonical(repo)
    # Neutralize cluster_map → identity (member id maps to itself): reproduces the
    # old逐篇封闭 behavior on the very same seed.
    monkeypatch.setattr(repo, "cluster_map", lambda _nb: {})
    repo.rebuild_communities(nb.id, level=0)
    with repo._connect() as db:
        rows = db.execute(
            "SELECT canonical_id, community_id FROM community_members WHERE notebook_id=?",
            (nb.id,)).fetchall()
    comm_of = {r["canonical_id"]: r["community_id"] for r in rows}
    a_comms = {comm_of[o] for o in ("A1", "A2", "A3", "A4") if o in comm_of}
    b_comms = {comm_of[o] for o in ("B1", "B2", "B3", "B4") if o in comm_of}
    # Doc-A object ids and doc-B object ids live in disjoint communities (no bridge).
    assert a_comms and b_comms and not (a_comms & b_comms)


def test_member_ids_are_canonical(repo):
    """member_ids / community_members store CANONICAL ids, not raw object ids."""
    nb = _seed_two_docs_shared_canonical(repo)
    repo.rebuild_communities(nb.id, level=0)
    with repo._connect() as db:
        member_ids = set()
        for r in db.execute("SELECT member_ids FROM communities WHERE notebook_id=?", (nb.id,)):
            member_ids |= set(json.loads(r["member_ids"]))
        mem_rows = {r["canonical_id"] for r in db.execute(
            "SELECT canonical_id FROM community_members WHERE notebook_id=?", (nb.id,))}
    # Canonical ids (K1..K4) appear; raw object ids (A1../B1..) must NOT.
    assert {"K1", "K2", "K3", "K4"} <= member_ids
    assert not any(m[0] in ("A", "B") and m[1:].isdigit() for m in member_ids)
    assert member_ids == mem_rows  # communities.member_ids and reverse index agree


def test_reverse_index_has_names_and_centrality(repo):
    nb = _seed_two_docs_shared_canonical(repo)
    repo.rebuild_communities(nb.id, level=0)
    with repo._connect() as db:
        row = db.execute(
            "SELECT canonical_name, centrality FROM community_members "
            "WHERE notebook_id=? AND canonical_id='K1'", (nb.id,)).fetchone()
    assert row is not None
    assert row["canonical_name"] == "MoE"
    # K1 sits on the canonical 4-cycle (degree ≥ 2) → centrality (degree) > 0.
    assert row["centrality"] >= 2.0


def test_min_size_filter(repo):
    nb = _seed_two_docs_shared_canonical(repo)
    repo.settings.community_min_size = 999  # nothing qualifies
    assert repo.rebuild_communities(nb.id, level=0) == 0
    with repo._connect() as db:
        assert db.execute(
            "SELECT COUNT(*) FROM community_members WHERE notebook_id=?", (nb.id,)).fetchone()[0] == 0


def test_disabled_returns_zero(repo):
    nb = _seed_two_docs_shared_canonical(repo)
    repo.settings.community_layer_enabled = False
    assert repo.rebuild_communities(nb.id, level=0) == 0


def test_rerun_replaces_rows(repo):
    """Re-running deletes prior rows for the level (idempotent, no dup accumulation)."""
    nb = _seed_two_docs_shared_canonical(repo)
    repo.rebuild_communities(nb.id, level=0)
    with repo._connect() as db:
        first = db.execute(
            "SELECT COUNT(*) FROM community_members WHERE notebook_id=?", (nb.id,)).fetchone()[0]
    repo.rebuild_communities(nb.id, level=0)
    with repo._connect() as db:
        second = db.execute(
            "SELECT COUNT(*) FROM community_members WHERE notebook_id=?", (nb.id,)).fetchone()[0]
    assert first == second and first > 0


def test_large_no_index_refuses(repo, monkeypatch):
    """scale-tier (copyable=False) with no persistent CSR must REFUSE networkx,
    emit community_build_refused, and return 0 — never OOM/hang."""
    nb = _seed_two_docs_shared_canonical(repo)
    monkeypatch.setattr(repo, "notebook_copy_stats", lambda _nb: {"copyable": False})
    monkeypatch.setattr(repo, "_scale_index", lambda _nb, allow_stale=False: None)
    events = []
    monkeypatch.setattr(repo.event_log, "emit", lambda e: events.append(e))
    assert repo.rebuild_communities(nb.id, level=0) == 0
    assert any(e.get("kind") == "community_build_refused" for e in events)


def test_large_with_index_uses_networkx(repo, monkeypatch):
    """scale-tier but CSR present → guard passes, networkx path runs (Task 3
    covers base/small; Task 11 will swap in igraph for true scale)."""
    nb = _seed_two_docs_shared_canonical(repo)
    monkeypatch.setattr(repo, "notebook_copy_stats", lambda _nb: {"copyable": False})
    monkeypatch.setattr(repo, "_scale_index", lambda _nb, allow_stale=False: object())
    assert repo.rebuild_communities(nb.id, level=0) >= 1
