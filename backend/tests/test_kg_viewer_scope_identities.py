"""PR-A·A5, codex #806 round 1: the viewer rule is evaluated on EVERY identity
a detail read names, not only on raw objects and owner columns.

Three identities used to escape it:

1. a folded CLUSTER (canonical id) none of whose members is owned by an
   unreadable hidden source, but every member of which cites only such a
   source — neighbour hydration returned the node, its private label and its
   edge; a partly hidden cluster of that kind kept a label taken from the
   hidden member (neighbours and concept detail);
2. a legacy SIBLING procedure owned by another user's Memory that carries a
   visible evidence item (merged) — its private name became a step of a
   visible procedure;
3. a legacy sibling's evidence item that NAMES a readable source but whose
   ``element_id`` belongs to an unreadable one — the step text was that
   element's text.

Scenario helpers: tests/test_kg_viewer_scope.py (user A owns the notebook
and Memory ``src-ma``; user B is a member, so ``src-ma`` is foreign to B).
PostgreSQL twin: tests/postgres/test_kg_viewer_scope_identities_pg.py.
"""
from __future__ import annotations

import json

import pytest

from tests.test_kg_viewer_scope import (  # noqa: F401  (``repo`` is a fixture)
    _ev,
    _object_id,
    as_user,
    build_scenario,
    reader_of,
    repo,
)


@pytest.fixture(params=["viz", "db"])
def neighbour_path(request, repo, monkeypatch):
    if request.param == "db":
        monkeypatch.setattr(
            repo._runtime.scale_artifacts, "viz_index", lambda *_a, **_k: None)
    return request.param


# ------------------------------------------------ 1. canonical identities
def add_phantom(repo, s):
    """A concept OWNED by the visible source whose only evidence is A's
    Memory (hidden from B by the evidence half), linked to the visible
    Engram. Its cluster has no member owned by an unreadable source."""
    repo.store_kg(s.nb, "src-s", [
        {"local_id": "hub", "object_type": "concept",
         "payload": {"name": "Engram", "section_path": "1"},
         "evidence": [_ev("src-s", "el-s-occ")]},
        {"local_id": "ph", "object_type": "concept",
         "payload": {"name": "A-PRIVATE Phantom", "section_path": "1"},
         "evidence": [_ev("src-ma", "el-ma-secret")]},
    ], [{"source_local_id": "ph", "target_local_id": "hub", "edge_type": "related_to",
         "evidence": []}])
    repo.rebuild_unified_kg(s.nb)
    with repo._write() as db:
        phantom = _object_id(db, s.nb, "A-PRIVATE Phantom", "src-s")
    return phantom, repo.cluster_map(s.nb)[phantom]


def add_gadget(repo, s):
    """A cluster of two members, both OWNED by the visible source: one
    evidenced by it, one only by A's Memory. The stored canonical name is the
    hidden member's (the merge review may pick any member's name)."""
    repo.store_kg(s.nb, "src-s", [
        {"local_id": "g1", "object_type": "concept",
         "payload": {"name": "Gadget", "section_path": "G"},
         "evidence": [_ev("src-s", "el-s-def")]},
        {"local_id": "g2", "object_type": "concept",
         "payload": {"name": "Gadget", "section_path": "A-PRIVATE G"},
         "evidence": [_ev("src-ma", "el-ma-def")]},
    ], [])
    repo.rebuild_unified_kg(s.nb)
    with repo._write() as db:
        visible, hidden = (
            db.execute(
                "SELECT id FROM knowledge_objects WHERE notebook_id=? AND source_id='src-s' "
                "AND json_extract(payload,'$.section_path')=?", (s.nb, section),
            ).fetchone()["id"]
            for section in ("G", "A-PRIVATE G"))
    canonical = repo.cluster_map(s.nb)[visible]
    assert repo.cluster_map(s.nb)[hidden] == canonical
    with repo._write() as db:
        db.execute(
            "UPDATE concept_clusters SET canonical_name='A-PRIVATE Gadget' "
            "WHERE notebook_id=? AND canonical_id=?", (s.nb, canonical))
    return visible, hidden, canonical


def certify(repo, nb, certified):
    with repo._write() as db:
        db.execute(
            "UPDATE unified_kg_state SET source_index_backfilled=? WHERE notebook_id=?",
            (1 if certified else 0, nb))


@pytest.mark.parametrize("certified", [True, False])
def test_neighbours_omit_a_cluster_hidden_by_the_evidence_half(
    repo, neighbour_path, certified,
):
    s = build_scenario(repo, b_memory=False)
    phantom, phantom_c = add_phantom(repo, s)
    certify(repo, s.nb, certified)
    owner = as_user(s.a, repo.kg_neighbors, s.nb, s.ids.engram_s)
    assert phantom_c in {n["id"] for n in owner["nodes"]}
    view = as_user(s.b, repo.kg_neighbors, s.nb, s.ids.engram_s)
    assert phantom_c not in {n["id"] for n in view["nodes"]}
    assert all(phantom_c not in (e["source_object_id"], e["target_object_id"])
               for e in view["edges"])
    assert "A-PRIVATE" not in repr(view)
    for focus in (phantom, phantom_c):
        hidden = as_user(s.b, repo.kg_neighbors, s.nb, focus)
        assert hidden["nodes"] == [] and hidden["edges"] == []
    with pytest.raises(KeyError):
        as_user(s.b, repo.concept_detail, s.nb, phantom_c)


@pytest.mark.parametrize("certified", [True, False])
def test_cluster_labels_never_come_from_an_evidence_hidden_member(repo, certified):
    s = build_scenario(repo, b_memory=False)
    visible, hidden, canonical = add_gadget(repo, s)
    certify(repo, s.nb, certified)
    detail = as_user(s.b, repo.concept_detail, s.nb, canonical)
    assert [m["id"] for m in detail["members"]] == [visible]
    assert detail["canonical_name"] == "Gadget"
    assert "A-PRIVATE" not in repr(detail)
    # Later pages label through ``cluster_display_name``.
    scope = as_user(s.b, reader_of(repo).for_notebook, s.nb)
    assert scope.cluster_display_name(canonical, "A-PRIVATE Gadget") == "Gadget"
    # Both hydration paths bake a cluster label from one member's payload.
    nodes = [{"id": canonical, "object_type": "concept",
              "payload": {"name": "A-PRIVATE baked Gadget"}},
             {"id": s.ids.engram_canonical, "object_type": "concept",
              "payload": {"name": "Engram"}}]
    edges = [{"source_object_id": canonical, "target_object_id": s.ids.engram_canonical,
              "edge_type": "x"}]
    kept, kept_edges = as_user(
        s.b, scope.filter_neighbourhood, nodes, edges, (s.ids.engram_canonical,))
    assert [(n["id"], n["payload"]["name"]) for n in kept] == [
        (canonical, "Gadget"), (s.ids.engram_canonical, "Engram")]
    assert len(kept_edges) == 1
    owner = as_user(s.a, repo.concept_detail, s.nb, canonical)
    assert owner["canonical_name"] == "A-PRIVATE Gadget"


def test_unrelated_clusters_keep_their_stored_label(repo):
    """Clusters no member of which touches an unreadable source are not
    relabelled: the fix reaches only the clusters it has to. (Without a
    certified reverse index every cluster is examined and labelled by its
    first visible member — fail closed.)"""
    s = build_scenario(repo, b_memory=False)
    visible, _hidden, canonical = add_gadget(repo, s)
    repo.store_kg(s.nb, "src-s", [
        {"local_id": "w", "object_type": "concept",
         "payload": {"name": "Widget", "section_path": "W"},
         "evidence": [_ev("src-s", "el-s-def")]}], [])
    repo.rebuild_unified_kg(s.nb)
    with repo._write() as db:
        widget = _object_id(db, s.nb, "Widget", "src-s")
    widget_c = repo.cluster_map(s.nb)[widget]
    with repo._write() as db:
        db.execute(
            "UPDATE concept_clusters SET canonical_name='Widget (fused)' "
            "WHERE notebook_id=? AND canonical_id=?", (s.nb, widget_c))
    assert as_user(s.b, repo.concept_detail, s.nb, widget_c)["canonical_name"] == (
        "Widget (fused)")


def _kinds(rows):
    out: dict = {}
    for row in rows:
        out.setdefault(row["kind"], set()).add(row["id"])
    return out


def test_owned_and_citing_sets_come_from_one_indexed_statement(repo):
    """``relink_object_rows_for_source(source_ids=..., with_citing=True)``:
    the owned objects, the objects CITING the listed sources (reverse index)
    and the index's certificate in ONE statement; the owned leg seeks
    ``idx_knowledge_objects_source`` and the citing leg
    ``idx_kos_source_object`` per listed id, never a notebook index; an empty
    list issues nothing; an uncertified index yields no citing rows and no
    certificate marker (unknown, never "none")."""
    s = build_scenario(repo, b_memory=False)
    phantom, _phantom_c = add_phantom(repo, s)
    knowledge = repo._runtime.knowledge
    statements = []
    with repo._runtime.database.connect() as db:
        db.set_trace_callback(statements.append)
        assert knowledge.relink_object_rows_for_source(
            db, s.nb, source_ids=[], with_citing=True) == []
        assert statements == []
        kinds = _kinds(knowledge.relink_object_rows_for_source(
            db, s.nb, source_ids=["src-ma", "src-none"], with_citing=True))
        db.set_trace_callback(None)
        assert len(statements) == 1, statements
        plan = " | ".join(
            str(tuple(row)) for row in
            db.execute("EXPLAIN QUERY PLAN " + statements[0]).fetchall())
    assert kinds["owned"] == {s.ids.engram_ma, s.ids.secret, s.ids.definer_ma}
    assert phantom in kinds["citing"] and s.ids.secret in kinds["citing"]
    assert s.ids.engram_s not in kinds["citing"]
    assert kinds["certified"] == {None}
    assert "idx_knowledge_objects_source (source_id=?)" in plan, plan
    assert "idx_kos_source_object (source_id=?)" in plan, plan
    assert "idx_kos_notebook" not in plan and "idx_knowledge_objects_nb_updated" not in plan, plan
    assert "SCAN knowledge_objects" not in plan and "SCAN knowledge_object_sources" not in plan, plan
    certify(repo, s.nb, False)
    with repo._runtime.database.connect() as db:
        kinds = _kinds(knowledge.relink_object_rows_for_source(
            db, s.nb, source_ids=["src-ma"], with_citing=True))
    assert set(kinds) == {"owned"}, kinds


# ------------------------------------------ same gap, found by the sweep
def test_fused_description_counts_a_member_owned_by_foreign_memory(repo):
    """Q1 on the member's OWN source: a member owned by A's Memory whose
    evidence is all visible (merged) still makes the fused description
    unattributable to B."""
    s = build_scenario(repo, b_memory=False)
    repo.store_kg(s.nb, "src-s", [
        {"local_id": "z", "object_type": "concept",
         "payload": {"name": "Gizmo", "section_path": "Z"},
         "evidence": [_ev("src-s", "el-s-def")]}], [])
    repo.store_kg(s.nb, "src-ma", [
        {"local_id": "z", "object_type": "concept",
         "payload": {"name": "Gizmo", "section_path": "Z"},
         "evidence": [_ev("src-s", "el-s-occ")]}], [])
    repo.rebuild_unified_kg(s.nb)
    with repo._write() as db:
        visible = _object_id(db, s.nb, "Gizmo", "src-s")
        owned = _object_id(db, s.nb, "Gizmo", "src-ma")
    canonical = repo.cluster_map(s.nb)[visible]
    assert repo.cluster_map(s.nb)[owned] == canonical
    with repo._write() as db:
        db.execute(
            "UPDATE concept_clusters SET canonical_description='A-PRIVATE fused Gizmo' "
            "WHERE notebook_id=? AND canonical_id=?", (s.nb, canonical))
    ctx = as_user(s.b, repo.node_context, s.nb, visible)
    assert ctx["definition_basis"] != "cluster_description"
    assert "A-PRIVATE" not in repr(ctx)
    assert as_user(s.a, repo.node_context, s.nb, visible)["definition"] == (
        "A-PRIVATE fused Gizmo")


def test_evidence_items_are_judged_on_named_and_actual_source(repo):
    """Occurrences (object context) and every evidence list of concept detail
    — the page's, each member's and each attached object's — drop an item
    whose element lives in an unreadable source although it names a readable
    one, and an item naming an unreadable source although its element is
    readable (its ``source_title`` is that source's)."""
    s = build_scenario(repo, b_memory=False)
    repo.store_kg(s.nb, "src-s", [
        {"local_id": "g", "object_type": "concept",
         "payload": {"name": "Gadget", "section_path": "G"},
         "evidence": [_ev("src-s", "el-s-def"), _ev("src-s", "el-ma-occ"),
                      _ev("src-ma", "el-s-step")]},
        {"local_id": "t", "object_type": "claim",
         "payload": {"name": "Attachment", "section_path": "G"},
         "evidence": [_ev("src-s", "el-s-occ"), _ev("src-s", "el-ma-def")]},
    ], [{"source_local_id": "t", "target_local_id": "g", "edge_type": "about",
         "evidence": []}])
    repo.rebuild_unified_kg(s.nb)
    with repo._write() as db:
        gadget = _object_id(db, s.nb, "Gadget", "src-s")
    canonical = repo.cluster_map(s.nb)[gadget]
    ctx = as_user(s.b, repo.node_context, s.nb, gadget)
    assert [(o["source_id"], o["element_id"]) for o in ctx["occurrences"]] == [
        ("src-s", "el-s-def")]
    detail = as_user(s.b, repo.concept_detail, s.nb, canonical)
    assert [e["element_id"] for e in detail["evidence"]] == ["el-s-def"]
    assert [e["element_id"] for e in detail["members"][0]["evidence"]] == ["el-s-def"]
    assert [e["element_id"] for a in detail["attached"] for e in a["evidence"]] == [
        "el-s-occ"]
    for secret in ("el-ma", "src-ma", "A-PRIVATE"):
        assert secret not in repr(ctx) + repr(detail), secret
    owner = as_user(s.a, repo.concept_detail, s.nb, canonical)
    assert len(owner["members"][0]["evidence"]) == 3


def deprecate_with_visible_evidence(repo, s, object_id):
    """A's Memory object that absorbed a visible evidence item (a merge into
    it) and was later merged away itself: deprecated, owned by A's Memory,
    citing a readable source. Its Memory relation to Engram stays (merges do
    not re-point relations), so after the rebuild it is a RAW neighbour of
    the Engram cluster on the DB path."""
    raw = json.dumps([_ev("src-ma", "el-ma-secret"), _ev("src-s", "el-s-occ")])
    with repo._write() as db:
        db.execute(
            "UPDATE knowledge_objects SET evidence=?, status='deprecated' WHERE id=?",
            (raw, object_id))
        repo._runtime.knowledge.replace_object_sources(db, object_id, s.nb, raw)
    repo.rebuild_unified_kg(s.nb, force=True)
    assert object_id not in repo.cluster_map(s.nb)


def test_a_raw_object_is_judged_on_its_own_source_whatever_its_status(repo, monkeypatch):
    """The owner half is judged on the row's own ``source_id``, not only
    through the live owned set: a deprecated object of A's Memory that
    cites a readable source never reaches B as a neighbour."""
    s = build_scenario(repo, b_memory=False)
    deprecate_with_visible_evidence(repo, s, s.ids.secret)
    monkeypatch.setattr(repo._runtime.scale_artifacts, "viz_index", lambda *_a, **_k: None)
    owner = as_user(s.a, repo.kg_neighbors, s.nb, s.ids.engram_s)
    assert s.ids.secret in {n["id"] for n in owner["nodes"]}, owner
    view = as_user(s.b, repo.kg_neighbors, s.nb, s.ids.engram_s)
    assert s.ids.secret not in {n["id"] for n in view["nodes"]}
    assert "SecretProject" not in repr(view)
    hidden = as_user(s.b, repo.kg_neighbors, s.nb, s.ids.secret)
    assert hidden["nodes"] == [] and hidden["edges"] == []
    scope = as_user(s.b, reader_of(repo).for_notebook, s.nb)
    kept, edges = scope.filter_neighbourhood(
        [{"id": s.ids.secret, "object_type": "concept",
          "payload": {"name": "SecretProject"}},
         {"id": s.ids.engram_canonical, "object_type": "concept",
          "payload": {"name": "Engram"}}],
        [{"source_object_id": s.ids.secret, "target_object_id": s.ids.engram_canonical,
          "edge_type": "related_to"}],
        (s.ids.engram_canonical,))
    assert [n["id"] for n in kept] == [s.ids.engram_canonical] and edges == []


# ------------------------------------------------- 2./3. legacy siblings
def _legacy(repo, nb, name, section, source_id, evidence):
    """A legacy procedure (no payload ``steps``) with exactly ``evidence``,
    the reverse index kept in step like the production write path."""
    oid = repo._test_insert_object(
        nb, "procedure", {"name": name, "section_path": section}, source_id=source_id)
    raw = json.dumps(evidence)
    with repo._connect() as db:
        db.execute("UPDATE knowledge_objects SET evidence=? WHERE id=?", (raw, oid))
        repo._runtime.knowledge.replace_object_sources(db, oid, nb, raw)
    return oid


@pytest.mark.parametrize("section", ["L", ""])
def test_legacy_sibling_owned_by_foreign_memory_is_not_a_step(repo, section):
    s = build_scenario(repo, b_memory=False)
    target = _legacy(repo, s.nb, "visible step", section, "src-s",
                     [_ev("src-s", "el-s-step")])
    # Owned by A's Memory; the visible evidence item came in by a merge.
    _legacy(repo, s.nb, "A-PRIVATE step name", section, "src-ma",
            [_ev("src-s", "el-s-step")])
    member = as_user(s.b, repo.node_context, s.nb, target)
    assert [st["name"] for st in member["steps"]] == ["visible step"]
    assert "A-PRIVATE" not in repr(member)
    owner = as_user(s.a, repo.node_context, s.nb, target)
    assert sorted(st["name"] for st in owner["steps"]) == [
        "A-PRIVATE step name", "visible step"]


@pytest.mark.parametrize("section", ["L", ""])
def test_legacy_step_text_comes_from_the_elements_actual_source(repo, section):
    s = build_scenario(repo, b_memory=False)
    target = _legacy(repo, s.nb, "visible step", section, "src-s",
                     [_ev("src-s", "el-s-step")])
    # Names the visible source, points at an element of A's Memory.
    _legacy(repo, s.nb, "mislabelled sibling", section, "src-s",
            [_ev("src-s", "el-ma-step")])
    # Same lie first, a genuine readable element second: the step keeps the
    # readable element's text.
    _legacy(repo, s.nb, "second item wins", section, "src-s",
            [_ev("src-s", "el-ma-def"), _ev("src-s", "el-s-def")])
    member = as_user(s.b, repo.node_context, s.nb, target)
    got = sorted((st["name"], st["element_text"]) for st in member["steps"])
    assert got == [("second item wins", "VISIBLE definition of Engram"),
                   ("visible step", "VISIBLE step text")]
    assert "A-PRIVATE" not in repr(member)
    owner = as_user(s.a, repo.node_context, s.nb, target)
    assert ("mislabelled sibling", "A-PRIVATE step text") in [
        (st["name"], st["element_text"]) for st in owner["steps"]]
