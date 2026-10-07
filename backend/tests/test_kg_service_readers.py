"""E4-7 (M1 on the service layer): KG reads are scoped by the viewer, the
persisted graph artifacts stay viewer-independent, and the graph views
overlay the viewer's OWN Memory from the live tables.

Scenario: tests/test_kg_viewer_scope.py (A owns the notebook and the Memory
``src-ma``; B is a member).  The store keyword ``viewer_id`` lands with E4-4;
the tests that pin the keyword turn the assembly seam on and record what the
service hands the store (a double pops the keyword the real store below does
not take yet).
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.models.schemas import NotebookCreate
from app.services import kg_viewer_scope
from tests.kg_no_memory_baseline import (
    FIXTURE,
    READS,
    build_plain_world,
    comparable,
    reads,
    record,
)
from tests.test_kg_viewer_scope import (  # noqa: F401  (``repo`` is a fixture)
    _ev,
    _now,
    _source,
    as_user,
    build_scenario,
    repo,
)

_MISSING = object()

try:
    # E4-2: a rebuild no longer clusters Memory objects with shared ones;
    # the legacy (pre-isolation) graph the viewer rule must stay right on is
    # built through this seam.  Before E4-2 every rebuild builds it.
    from tests.pre_isolation_graph import build_pre_isolation
except ImportError:
    def build_pre_isolation(repo, notebook_id, **kwargs):
        return repo.rebuild_unified_kg(notebook_id, **kwargs)


# ------------------------------------------------ the store keyword (D2/D5)
def _spy_viewer_keyword(monkeypatch, store, name, seen, answer=None):
    original = getattr(store, name)

    def spy(*args, **kwargs):
        viewer = kwargs.pop("viewer_id", _MISSING)
        seen.append((name, viewer))
        if answer is not None:
            return answer(viewer)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, name, spy)


@pytest.fixture
def keyword_log(repo, monkeypatch):
    """Turn the E4-4 assembly seam on and record the ``viewer_id`` each KG
    read receives (the real store below does not take it yet)."""
    monkeypatch.setattr(kg_viewer_scope, "STORE_READERS_TAKE_VIEWER_ID", True)
    seen: list = []
    knowledge = repo._runtime.knowledge
    for name in ("list_knowledge_page", "type_counts", "fts_search"):
        _spy_viewer_keyword(monkeypatch, knowledge, name, seen)
    # E4-4's ``sources_only`` on the list page's element read (P3-B).
    enrich = knowledge._enrich_evidence
    monkeypatch.setattr(knowledge, "_enrich_evidence",
                        lambda db, items, sources_only=False: enrich(db, items))
    # The notebook summary the reads validate against takes the keyword too
    # (its own test below); record it apart so these tests see the KG reads.
    summary: list = []
    for name in ("knowledge_type_count_rows", "notebook_has_kg"):
        _spy_viewer_keyword(monkeypatch, repo._runtime.queries, name, summary)
    return seen


def _read_all(repo, user, nb):
    as_user(user, repo.list_knowledge, nb, "concept")
    as_user(user, repo.knowledge_types, nb)
    as_user(user, repo.kg_search, nb, "Engram")


def test_viewer_id_reaches_the_store_only_when_the_scope_filters(repo, keyword_log):
    s = build_scenario(repo, b_memory=False)
    # B: A's Memory is foreign -> every read carries B.
    _read_all(repo, s.b, s.nb)
    assert keyword_log == [("list_knowledge_page", s.b.id), ("type_counts", s.b.id),
                           ("fts_search", s.b.id)]
    keyword_log.clear()
    # A owns the only Memory: an overlay-only scope, no keyword, today's SQL.
    _read_all(repo, s.a, s.nb)
    assert {value for _name, value in keyword_log} == {_MISSING}
    keyword_log.clear()
    # A notebook without Memory: no scope, no keyword.
    token_nb = as_user(s.a, repo.create_notebook, NotebookCreate(name="plain")).id
    _read_all(repo, s.a, token_nb)
    assert {value for _name, value in keyword_log} == {_MISSING}


def test_a_closed_memory_channel_reads_as_the_empty_identity(repo, keyword_log, monkeypatch):
    """Plan correction 1: a token without ``memory:read`` sees no
    Memory-derived row, its own included -- the scope exists whenever the
    notebook holds any Memory, and the store is handed ''."""
    s = build_scenario(repo, b_memory=False)
    monkeypatch.setattr(kg_viewer_scope, "memory_channel_allowed", lambda: False)
    _read_all(repo, s.a, s.nb)
    assert keyword_log == [("list_knowledge_page", ""), ("type_counts", ""),
                           ("fts_search", "")]
    scope = as_user(s.a, repo._runtime.knowledge_query.viewer_scope, s.nb)
    assert scope.foreign == {"src-ma"} and scope.own_memory == frozenset()


def test_the_seam_off_passes_nothing(repo, monkeypatch):
    s = build_scenario(repo, b_memory=False)
    monkeypatch.setattr(kg_viewer_scope, "STORE_READERS_TAKE_VIEWER_ID", False)
    seen: list = []
    for name in ("list_knowledge_page", "type_counts", "fts_search"):
        _spy_viewer_keyword(monkeypatch, repo._runtime.knowledge, name, seen)
    _read_all(repo, s.b, s.nb)
    assert {value for _name, value in seen} == {_MISSING}


# ------------------------------------------------ what the reads return
def test_list_drops_foreign_evidence_items_and_search_drops_hidden_hits(repo):
    s = build_scenario(repo, b_memory=False)
    repo.store_kg(s.nb, "src-s", [{
        "local_id": "m", "object_type": "concept",
        "payload": {"name": "Mixture", "section_path": "1"},
        "evidence": [_ev("src-s", "el-s-def"), _ev("src-ma", "el-ma-def")]}], [])
    blob = repr(as_user(s.b, repo.list_knowledge, s.nb, "concept"))
    assert "Mixture" in blob
    assert "el-ma-def" not in blob
    assert "el-ma-def" in repr(as_user(s.a, repo.list_knowledge, s.nb, "concept"))
    # A folded concept hit (its cluster has no visible member) and a raw hit
    # of A's Memory that no cluster holds yet (written after the rebuild).
    repo.store_kg(s.nb, "src-ma", [{
        "local_id": "u", "object_type": "claim",
        "payload": {"name": "Unclustered note", "section_path": "1"},
        "evidence": [_ev("src-ma", "el-ma-def")]}], [])
    for query, name in (("SecretProject", "SecretProject"),
                        ("Unclustered note", "Unclustered note")):
        b_hits = as_user(s.b, repo.kg_search, s.nb, query)
        assert name not in repr(b_hits), (query, b_hits)
        a_hits = as_user(s.a, repo.kg_search, s.nb, query)
        assert name in repr(a_hits), (query, a_hits)


def test_list_judges_an_item_by_the_source_its_element_lives_in(repo, monkeypatch):
    """P2-4: a legacy-shape item names the readable ``src-s`` but quotes an
    element of A's Memory: B's list page leaves it out (the rule concept
    detail applies), A's keeps it -- with ONE element read for the page."""
    s = build_scenario(repo, b_memory=False)
    repo.store_kg(s.nb, "src-s", [{
        "local_id": "l", "object_type": "concept",
        "payload": {"name": "Legacyshape", "section_path": "1"},
        "evidence": [_ev("src-s", "el-s-occ"),
                     {**_ev("src-s", "el-ma-secret"),
                      "quoted_span": "A-PRIVATE secret project"}]}], [])
    knowledge = repo._runtime.knowledge
    original = knowledge._enrich_evidence
    reads: list = []
    monkeypatch.setattr(
        knowledge, "_enrich_evidence",
        lambda db, items, **kw: reads.append(len(items)) or original(db, items, **kw))
    page = as_user(s.b, repo.list_knowledge, s.nb, "concept")
    legacy = [item for item in page.items if item.headline == "Legacyshape"]
    assert len(legacy) == 1
    assert [e.element_id for e in legacy[0].evidence] == ["el-s-occ"]
    assert "A-PRIVATE" not in repr(page)
    assert len(page.items) >= 2 and len(reads) == 1, reads
    owner = as_user(s.a, repo.list_knowledge, s.nb, "concept")
    assert "A-PRIVATE secret project" in repr(owner)


def test_list_judges_an_item_without_an_element_by_the_source_it_names(repo):
    """P3-C: an item that names A's Memory and carries no element id never
    reaches the element read; the source it names hides it from B."""
    s = build_scenario(repo, b_memory=False)
    repo.store_kg(s.nb, "src-s", [{
        "local_id": "n", "object_type": "concept",
        "payload": {"name": "Namedonly", "section_path": "1"},
        "evidence": [_ev("src-s", "el-s-occ"),
                     {**_ev("src-ma", ""), "quoted_span": "A-PRIVATE named only"}]}], [])
    page = as_user(s.b, repo.list_knowledge, s.nb, "concept")
    item = [i for i in page.items if i.headline == "Namedonly"][0]
    assert [e.source_id for e in item.evidence] == ["src-s"]
    assert "A-PRIVATE named only" not in repr(page)
    assert "A-PRIVATE named only" in repr(
        as_user(s.a, repo.list_knowledge, s.nb, "concept"))


def test_list_resolves_each_distinct_element_once_and_sources_only(repo, monkeypatch):
    """P3-B: the list page's element read takes every DISTINCT element id
    once, in batches of 900 distinct ids, and -- with the E4-4 stores
    assembled -- asks for sources only (``sources_only=True``; the double
    below stands in for E4-4's keyword)."""
    s = build_scenario(repo, b_memory=False)
    shared = [{**_ev("src-s", "el-ma-secret"), "quoted_span": f"copy {i}"}
              for i in range(3)]
    repo.store_kg(s.nb, "src-s", [
        {"local_id": f"d{i}", "object_type": "concept",
         "payload": {"name": f"Dupe {i}", "section_path": "1"},
         "evidence": [_ev("src-s", "el-s-occ"), *shared]} for i in range(3)], [])
    knowledge = repo._runtime.knowledge
    original = knowledge._enrich_evidence
    calls: list = []

    def double(db, items, **kwargs):
        calls.append(([item["element_id"] for item in items], kwargs))
        return original(db, items)

    monkeypatch.setattr(knowledge, "_enrich_evidence", double)
    monkeypatch.setattr(kg_viewer_scope, "STORE_READERS_TAKE_VIEWER_ID", True)
    keywords: list = []
    for name in ("list_knowledge_page", "knowledge_type_count_rows", "notebook_has_kg"):
        store = knowledge if name == "list_knowledge_page" else repo._runtime.queries
        _spy_viewer_keyword(monkeypatch, store, name, keywords)
    page = as_user(s.b, repo.list_knowledge, s.nb, "concept")
    assert "copy 0" not in repr(page) and "A-PRIVATE" not in repr(page)
    assert len(calls) == 1, calls
    elements, kwargs = calls[0]
    assert kwargs == {"sources_only": True}
    assert len(elements) == len(set(elements)), elements
    assert elements.count("el-ma-secret") == 1


def _foldterm_world(repo, count):
    """``count`` concepts named alike in the visible source and in A's
    Memory: one cluster each, every one holding a hidden member for B."""
    s = build_scenario(repo, b_memory=False)
    words = ["alpha", "beta", "gamma", "delta", "epsilon", "zeta"][:count]
    for source, element in (("src-s", "el-s-occ"), ("src-ma", "el-ma-occ")):
        repo.store_kg(s.nb, source, [
            {"local_id": word, "object_type": "concept",
             "payload": {"name": f"Foldterm {word}", "section_path": "1"},
             "evidence": [_ev(source, element)]} for word in words], [])
    build_pre_isolation(repo, s.nb)
    return s, words


def test_folded_search_hits_are_labelled_in_one_member_read(repo, monkeypatch):
    """P2-1: every folded hit of a page is judged in ONE batched member read,
    never one read (and one connection) per hit."""
    s, words = _foldterm_world(repo, 6)
    knowledge = repo._runtime.knowledge
    detail = knowledge.concept_cluster_detail_rows
    calls: list = []
    monkeypatch.setattr(knowledge, "concept_cluster_detail_rows",
                        lambda *a, **k: calls.append(k) or detail(*a, **k))
    reader = repo._runtime.knowledge_query.viewer_scope.__self__
    connect = reader.connect
    connects: list = []
    monkeypatch.setattr(reader, "connect", lambda: connects.append(1) or connect())
    hits = as_user(s.b, repo.kg_search, s.nb, "Foldterm")
    assert sorted(h["name"] for h in hits) == sorted(f"Foldterm {w}" for w in words)
    assert all(h["object_id"].startswith("K") for h in hits), hits
    assert len(calls) == 1 and calls[0].get("canonical_ids"), calls
    # The scope's own reads: Memory split, owned set, owned fold, members.
    assert len(connects) <= 4, connects


def _seed_named_cluster(repo, s, seed_id, ph="?"):
    """A legacy mixed cluster whose canonical id was minted from A's private
    Memory concept (``K-<seed>``) and that holds the shared Engram too."""
    with repo._runtime.database.write() as db:
        db.execute(f"UPDATE concept_clusters SET canonical_id={ph} "
                   f"WHERE notebook_id={ph} AND canonical_id={ph}",
                   (seed_id, s.nb, s.ids.engram_canonical))


@pytest.mark.parametrize("pending", [True, False])
def test_a_pending_notebook_never_answers_a_search_hit_by_a_seeded_cluster_id(
    repo, monkeypatch, pending,
):
    """E4-4 §7.1: before the isolated rebuild a mixed cluster's canonical id
    can carry a hidden member's text.  While the marker says pending, B's
    folded hit is answered by its first visible member's object id -- same
    name, no seeded id, openable; once isolated, the canonical id again."""
    s = build_scenario(repo, b_memory=False)
    seed = "K-a private seedname"
    _seed_named_cluster(repo, s, seed)
    monkeypatch.setattr(repo._runtime.knowledge_lifecycle, "_isolation_pending",
                        lambda _db, _nb: pending)
    hits = as_user(s.b, repo.kg_search, s.nb, "Engram")
    engram = [h for h in hits if h["name"] == "Engram"]
    assert len(engram) == 1, hits
    if pending:
        assert engram[0]["object_id"] == s.ids.engram_s, hits
        assert "private seedname" not in repr(hits)
        ctx = as_user(s.b, repo.node_context, s.nb, engram[0]["object_id"])
        assert ctx["id"] == s.ids.engram_s
    else:
        assert engram[0]["object_id"] == seed, hits


def test_a_cluster_hit_with_no_visible_member_is_dropped(repo, monkeypatch):
    """P2-2: a hit that reaches the folded stage as a CANONICAL id (an ANN
    leg answering with a cluster) whose every member is hidden is dropped,
    and never answers under the stored (hidden) name."""
    s = build_scenario(repo, b_memory=False)
    query = repo._runtime.knowledge_query
    hit = {"object_id": s.ids.secret_canonical, "name": "SecretProject",
           "object_type": "concept", "score": 0.9, "match": "semantic"}
    monkeypatch.setattr(query, "semantic_search", lambda *_a, **_k: [dict(hit)])
    monkeypatch.setattr(query, "hydrate_search_hits", lambda _nb, hits: hits)
    b_hits = as_user(s.b, repo.kg_search, s.nb, "nothing-lexical")
    assert s.ids.secret_canonical not in repr(b_hits)
    assert "SecretProject" not in repr(b_hits)
    a_hits = as_user(s.a, repo.kg_search, s.nb, "nothing-lexical")
    assert [h["object_id"] for h in a_hits] == [s.ids.secret_canonical]


def test_legacy_graph_leaves_out_foreign_objects_and_relations(repo, monkeypatch):
    s = build_scenario(repo, b_memory=False)
    folds: list = []
    unified_kg = repo._runtime.unified_kg
    fold = unified_kg.cluster_fold_rows
    monkeypatch.setattr(unified_kg, "cluster_fold_rows",
                        lambda *a, **k: folds.append(a) or fold(*a, **k))
    view = as_user(s.b, repo.knowledge_graph, s.nb)
    # P3-3: the raw graph needs the owned set only -- no cluster fold read.
    assert folds == []
    ids = {node.id for node in view.nodes}
    assert s.ids.secret not in ids and s.ids.engram_ma not in ids
    assert s.ids.definer_ma not in ids
    assert all(s.ids.definer_ma not in (e.from_id, e.to_id) for e in view.edges)
    owner = as_user(s.a, repo.knowledge_graph, s.nb)
    assert s.ids.secret in {node.id for node in owner.nodes}


# ---------------------------------------- graph views: shared + own overlay
def _node_ids(graph):
    return {str(node["id"]) for node in graph["nodes"]}


def _private(s):
    return {s.ids.secret, s.ids.engram_ma, s.ids.definer_ma, s.ids.secret_canonical}


@pytest.mark.parametrize("path", ["full", "artifact"])
def test_graph_view_is_the_shared_graph_plus_the_viewers_own_memory(repo, path):
    s = build_scenario(repo, b_memory=False)
    kwargs = {"level": "object"}
    if path == "artifact":
        assert repo._runtime.scale_artifacts.build_viz(s.nb) is not None
        kwargs["limit"] = 80
    member = as_user(s.b, repo.unified_graph, s.nb, **kwargs)
    assert not (_node_ids(member) & _private(s)), member
    for text in ("SecretProject", "private definer"):
        assert text not in repr(member)
    owner = as_user(s.a, repo.unified_graph, s.nb, **kwargs)
    # A's own Memory objects arrive as raw ids from the live tables, with
    # the relation between two of them.
    assert {s.ids.secret, s.ids.engram_ma, s.ids.definer_ma} <= _node_ids(owner)
    assert any(e["source_object_id"] == s.ids.secret
               and e["target_object_id"] == s.ids.engram_ma for e in owner["edges"])
    assert owner["total_nodes"] >= member["total_nodes"] + 3


@pytest.mark.parametrize("level", ["object", "concept"])
def test_the_shared_graph_cache_is_the_same_for_every_viewer(repo, level):
    """P1-1 / F3: the shared graph is cached once per notebook and level and
    handed to every member, so it holds NOBODY's Memory -- the requester's
    own included.  The owner reads first (and fills the cache); the member
    then reads the same entry."""
    s = build_scenario(repo, b_memory=False)
    owner = as_user(s.a, repo.unified_graph, s.nb, level=level)
    member = as_user(s.b, repo.unified_graph, s.nb, level=level)
    for view in (owner, member):
        ids = [str(node["id"]) for node in view["nodes"]]
        assert len(ids) == len(set(ids)), ids
    assert not (_node_ids(member) & _private(s)), member
    assert "SecretProject" not in repr(member)
    assert s.ids.secret in _node_ids(owner)


def test_an_unfiltered_cache_entry_never_serves_a_member(repo):
    """P1-1: an unfiltered read of the whole graph (the scale build's
    ``_unified_graph_full``) caches under its own key; a member's read does
    not pick it up."""
    s = build_scenario(repo, b_memory=False)
    everything = repo._runtime.knowledge_lifecycle._unified_graph_full(s.nb, "object")
    assert "SecretProject" in repr(everything)
    member = as_user(s.b, repo.unified_graph, s.nb, level="object")
    assert not (_node_ids(member) & _private(s)), member
    assert "SecretProject" not in repr(member)


def _memory_relation(repo, s, rel_id, source_object, target_object, edge_type):
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_relations (id,notebook_id,source_id,"
            "source_object_id,target_object_id,edge_type,evidence,created_at) "
            "VALUES (?,?,'src-ma',?,?,?,'[]',?)",
            (rel_id, s.nb, source_object, target_object, edge_type, _now()))


def assert_a_foreign_memory_relation_between_own_objects_stays_out(repo, s, q) -> None:
    """codex #824 r5: a relation is judged by its OWN source, not by its
    endpoints'. A relation of B's Memory (historical or imported) joining two
    of A's own Memory objects is B's: A's graph (edges AND totals), A's
    neighbour view of her own object and her concept detail never carry it.
    ``q`` adapts the placeholders to the backend."""
    before = as_user(s.a, repo.unified_graph, s.nb, level="object")
    with repo._write() as db:
        db.execute(
            q("INSERT INTO knowledge_relations (id,notebook_id,source_id,"
              "source_object_id,target_object_id,edge_type,evidence,created_at) "
              "VALUES (?,?,'src-mb',?,?,'bforeignedge','[]',?)"),
            ("rel-b-on-a", s.nb, s.ids.secret, s.ids.definer_ma, _now()),
        )
    for limit in (None, 1, 80):
        kwargs = {"level": "object"} if limit is None else {"level": "object", "limit": limit}
        view = as_user(s.a, repo.unified_graph, s.nb, **kwargs)
        assert "bforeignedge" not in repr(view["edges"]), (limit, view["edges"])
        if limit is None:
            assert view["total_edges"] == before["total_edges"], (view, before)
    neighbours = as_user(s.a, repo.kg_neighbors, s.nb, s.ids.secret)
    assert "bforeignedge" not in repr(neighbours)
    assert s.ids.definer_ma not in _node_ids(neighbours)
    detail = as_user(s.a, repo.concept_detail, s.nb, s.ids.secret_canonical)
    assert [m["id"] for m in detail["members"]] == [s.ids.secret]
    assert "bforeignedge" not in repr(detail["attached"]), detail["attached"]
    # B cannot see A's objects at all, so the relation is in no view of B's
    for_b = as_user(s.b, repo.unified_graph, s.nb, level="object")
    assert "bforeignedge" not in repr(for_b)


def test_a_foreign_memory_relation_between_own_objects_stays_out(repo):
    s = build_scenario(repo, b_memory=True)
    assert_a_foreign_memory_relation_between_own_objects_stays_out(repo, s, lambda x: x)


def test_a_memory_relation_between_shared_objects_is_in_no_shared_graph(repo):
    """P3-5 (M12): a relation extracted from A's Memory that joins two
    SHARED objects is judged on its own source: it is in neither the shared
    graph (whoever reads it) nor B's legacy graph."""
    s = build_scenario(repo, b_memory=False)
    _memory_relation(repo, s, "rel-mem-shared", s.ids.flow, s.ids.engram_s,
                     "privateedge")
    for user in (s.b, s.a):
        view = as_user(user, repo.unified_graph, s.nb, level="object")
        assert "privateedge" not in repr(view), user.id
    legacy = as_user(s.b, repo.knowledge_graph, s.nb)
    assert "privateedge" not in repr(legacy)


@pytest.mark.parametrize("path", ["full", "artifact"])
def test_overlay_edges_join_two_overlay_nodes(repo, path):
    """P3-5 (M4): with the overlay cut by ``limit``, a relation of A's Memory
    whose other end fell outside the cut is not returned."""
    s = build_scenario(repo, b_memory=False)
    _memory_relation(repo, s, "rel-mem-own", s.ids.engram_ma, s.ids.definer_ma,
                     "ownedge")
    kwargs = {"level": "object", "limit": 1}
    if path == "artifact":
        assert repo._runtime.scale_artifacts.build_viz(s.nb) is not None
    view = as_user(s.a, repo.unified_graph, s.nb, **kwargs)
    ids = _node_ids(view)
    assert s.ids.engram_ma in ids and s.ids.definer_ma not in ids, ids
    for edge in view["edges"]:
        assert {edge["source_object_id"], edge["target_object_id"]} <= ids, edge
    # P3-1: ``limit`` bounds each layer -- one shared node, one own node.
    assert len(view["nodes"]) <= 2 * kwargs["limit"]


@pytest.mark.parametrize("path", ["full", "artifact"])
def test_graph_totals_do_not_depend_on_the_limit(repo, path):
    """codex #824 r2: the totals describe the whole graph the viewer may see,
    so ``limit`` (which cuts the own-Memory overlay too) never changes them
    -- the own-Memory edges whose other end fell outside the cut included."""
    s = build_scenario(repo, b_memory=False)
    _memory_relation(repo, s, "rel-mem-own", s.ids.engram_ma, s.ids.definer_ma,
                     "ownedge")
    if path == "artifact":
        assert repo._runtime.scale_artifacts.build_viz(s.nb) is not None
    narrow = as_user(s.a, repo.unified_graph, s.nb, level="object", limit=1)
    wide = as_user(s.a, repo.unified_graph, s.nb, level="object", limit=80)
    assert len(wide["edges"]) > len(narrow["edges"]), "fixture: the cut drops an edge"
    assert (narrow["total_nodes"], narrow["total_edges"]) == (
        wide["total_nodes"], wide["total_edges"]), (narrow, wide)
    assert narrow["truncated"] is True


def test_a_closed_channel_hides_the_owners_own_memory_from_the_graph(repo, monkeypatch):
    s = build_scenario(repo, b_memory=False)
    monkeypatch.setattr(kg_viewer_scope, "memory_channel_allowed", lambda: False)
    view = as_user(s.a, repo.unified_graph, s.nb, level="object")
    assert not (_node_ids(view) & {s.ids.secret, s.ids.engram_ma, s.ids.definer_ma})
    assert "SecretProject" not in repr(view)


def test_neighbours_of_an_own_memory_object_come_from_the_live_tables(repo):
    s = build_scenario(repo, b_memory=False)
    assert repo._runtime.scale_artifacts.build_viz(s.nb) is not None
    view = as_user(s.a, repo.kg_neighbors, s.nb, s.ids.secret)
    assert view["focus_id"] == s.ids.secret
    assert _node_ids(view) == {s.ids.secret, s.ids.engram_ma}
    assert [(e["source_object_id"], e["target_object_id"]) for e in view["edges"]] == [
        (s.ids.secret, s.ids.engram_ma)]
    names = {n["id"]: n["payload"]["name"] for n in view["nodes"]}
    assert names == {s.ids.secret: "SecretProject", s.ids.engram_ma: "Engram"}
    hidden = as_user(s.b, repo.kg_neighbors, s.nb, s.ids.secret)
    assert hidden["nodes"] == [] and hidden["edges"] == []


def test_own_memory_neighbours_are_own_memory_objects_only(repo):
    """P3-5 (M5): A's own ``private definer`` is linked by a relation of the
    visible source to the shared Engram; its own-Memory neighbourhood holds
    A's Memory objects only."""
    s = build_scenario(repo, b_memory=False)
    view = as_user(s.a, repo.kg_neighbors, s.nb, s.ids.definer_ma)
    assert view["focus_id"] == s.ids.definer_ma
    assert _node_ids(view) == {s.ids.definer_ma}, view
    assert view["edges"] == []


@pytest.mark.parametrize("assembled", [False, True])
def test_the_neighbour_filter_reads_owners_without_evidence(repo, monkeypatch, assembled):
    """E4-4 review P2-2: the neighbour filter's owner read uses only ``id``
    and ``source_id``; with the E4-4 stores assembled it asks for no
    evidence (``with_evidence=False``; the doubles stand in for E4-4's
    keywords)."""
    s = build_scenario(repo, b_memory=False)
    monkeypatch.setattr(kg_viewer_scope, "STORE_READERS_TAKE_VIEWER_ID", assembled)
    knowledge = repo._runtime.knowledge
    original = knowledge.object_evidence_rows
    calls: list = []

    def double(db, ids, **kwargs):
        calls.append(kwargs)
        return original(db, ids)

    monkeypatch.setattr(knowledge, "object_evidence_rows", double)
    _spy_viewer_keyword(monkeypatch, knowledge, "neighbor_relation_rows", [])
    for name in ("knowledge_type_count_rows", "notebook_has_kg"):
        _spy_viewer_keyword(monkeypatch, repo._runtime.queries, name, [])
    view = as_user(s.b, repo.kg_neighbors, s.nb, s.ids.engram_s)
    assert "visible definer" in repr(view) and "private definer" not in repr(view)
    assert calls, "the owner read ran"
    expected = {"with_evidence": False} if assembled else {}
    assert all(kwargs == expected for kwargs in calls), calls


def test_the_neighbour_route_reads_the_focus_row_not_every_own_object(repo, monkeypatch):
    """P3-2: routing a click to the live own-Memory path costs one
    primary-key read of the focus row; a shared focus never reads the
    viewer's own Memory object ids."""
    s = build_scenario(repo, b_memory=False)
    knowledge = repo._runtime.knowledge
    relink = knowledge.relink_object_rows_for_source
    get_row = knowledge.get_object_row
    relinks: list = []
    rows: list = []
    monkeypatch.setattr(knowledge, "relink_object_rows_for_source",
                        lambda *a, **k: relinks.append(k) or relink(*a, **k))
    monkeypatch.setattr(knowledge, "get_object_row",
                        lambda *a, **k: rows.append(a) or get_row(*a, **k))
    shared = as_user(s.a, repo.kg_neighbors, s.nb, s.ids.engram_s)
    assert shared["focus_id"] == s.ids.engram_canonical
    assert relinks == [] and len(rows) == 1, (relinks, rows)
    own = as_user(s.a, repo.kg_neighbors, s.nb, s.ids.secret)
    assert own["focus_id"] == s.ids.secret


# ------------------- neighbours: a focus the viz artifact does not hold
@pytest.mark.parametrize("large", [False, True])
def test_a_focus_missing_from_the_artifact_falls_back_to_the_live_path(
    repo, monkeypatch, large,
):
    """Assembly decision (1): the viz artifact answers only for a focus it
    holds.  Here the served artifact is another notebook's (built by the
    same code): a small notebook answers the focus from the live DB path, a
    large one reports ``locating_unavailable`` instead of an empty view."""
    s = build_scenario(repo, b_memory=False)
    scale = repo._runtime.scale_artifacts
    other = as_user(s.a, repo.create_notebook, NotebookCreate(name="other")).id
    with repo._write() as db:
        _source(db, other, "src-o", elements=[("el-o", "OTHER text")])
    repo.store_kg(other, "src-o", [
        {"local_id": "o", "object_type": "concept",
         "payload": {"name": "Otherword", "section_path": "1"},
         "evidence": [_ev("src-o", "el-o")]},
        {"local_id": "p", "object_type": "claim",
         "payload": {"name": "other claim", "section_path": "1"},
         "evidence": [_ev("src-o", "el-o")]}],
        [{"source_local_id": "p", "target_local_id": "o", "edge_type": "about",
          "evidence": []}])
    repo.rebuild_unified_kg(other)
    assert scale.build_viz(other) is not None
    foreign_idx = scale.viz_index(other)
    assert getattr(foreign_idx, "viz_ids", None) is not None
    assert s.ids.engram_canonical not in foreign_idx.viz_node_index()
    monkeypatch.setattr(scale, "viz_index", lambda *_a, **_k: foreign_idx)
    if large:
        monkeypatch.setattr(repo._runtime.knowledge_lifecycle.settings,
                            "viz_sync_build_max_objects", 1)
    for user in (s.a, s.b):
        view = as_user(user, repo.kg_neighbors, s.nb, s.ids.engram_s)
        assert view["focus_id"] == s.ids.engram_canonical
        if large:
            assert view.get("locating_unavailable") is True, view
            # P3-D: a preview exists, it just lacks this node.
            assert view.get("preview_lacks_focus") is True, view
            assert view["nodes"] == []
        else:
            assert s.ids.engram_canonical in _node_ids(view), view
            assert "visible definer" in repr(view), view
    if not large:
        member = as_user(s.b, repo.kg_neighbors, s.nb, s.ids.engram_s)
        assert not (_node_ids(member) & _private(s)), member


# ------------------------------------------- the M1 transition marker (E4-5)
@pytest.mark.parametrize("large", [False, True])
def test_a_notebook_pending_its_isolated_rebuild_serves_no_artifact(
    repo, monkeypatch, large,
):
    s = build_scenario(repo, b_memory=False)
    lifecycle = repo._runtime.knowledge_lifecycle
    scale = repo._runtime.scale_artifacts
    assert scale.build_viz(s.nb) is not None
    probes: list = []
    original = scale.viz_index
    monkeypatch.setattr(scale, "viz_index",
                        lambda *a, **k: probes.append(a) or original(*a, **k))
    monkeypatch.setattr(lifecycle, "_isolation_pending", lambda _db, _nb: True)
    if large:
        monkeypatch.setattr(lifecycle.settings, "viz_sync_build_max_objects", 1)
    view = as_user(s.b, repo.unified_graph, s.nb, level="object", limit=80)
    neighbours = as_user(s.b, repo.kg_neighbors, s.nb, s.ids.engram_s)
    assert probes == []
    if large:
        assert view["nodes"] == [] and view.get("viz_unavailable") is True
        assert neighbours.get("locating_unavailable") is True
        # No preview consulted: the client says "no preview yet".
        assert "preview_lacks_focus" not in neighbours
    else:
        # The live shared graph (S3): the Engram cluster folded -- and, while
        # the notebook awaits its isolated rebuild, answered by its first
        # visible member's id, since it holds A's Memory (E4-8 ruling B) --
        # the visible procedure and claim as raw objects, nothing of A's.
        assert _node_ids(view) == {
            s.ids.engram_s, s.ids.flow, s.ids.definer_s}, view
        assert "SecretProject" not in repr(view) + repr(neighbours)
    monkeypatch.setattr(lifecycle, "_isolation_pending", lambda _db, _nb: False)
    as_user(s.b, repo.unified_graph, s.nb, level="object", limit=80)
    assert probes, "the marker cleared: the artifact is read again"


# ------------------------------- a notebook without Memory: today's bytes
def _traced(repo, monkeypatch, statements):
    database = repo._runtime.database
    original = database.connect

    class Tracing:
        def __init__(self, cm):
            self.cm = cm

        def __enter__(self):
            self.db = self.cm.__enter__()
            self.db.set_trace_callback(statements.append)
            return self.db

        def __exit__(self, *exc):
            self.db.set_trace_callback(None)
            return self.cm.__exit__(*exc)

    monkeypatch.setattr(database, "connect", lambda *a, **k: Tracing(original(*a, **k)))


def test_a_notebook_without_memory_answers_the_pre_e4_7_bytes(repo, monkeypatch):
    """Every E4-7 read of a notebook without Memory answers exactly what it
    answered at fac0298ec, before E4-7 -- the record in
    ``tests/fixtures/kg_no_memory_reads_baseline.json``, captured there by
    ``tests/kg_no_memory_baseline.py`` (P3-7: not the current code with the
    scope forced off, which shares every E4-7 edit with the scoped path).
    Its statements are those of the same code with the viewer scope
    switched off, plus at most the one Memory probe (statements are not
    compared with the record: the store work beside E4-7 changes them for
    its own reasons, e.g. E4-4's count memo)."""
    world = build_plain_world(repo, as_user=as_user)
    baseline = json.loads(
        (Path(__file__).parent / "fixtures" / FIXTURE).read_text(encoding="utf-8"))
    assert sorted(baseline) == sorted(READS)
    now = record(world, as_user=as_user)
    for name in READS:
        assert comparable(name, now[name]) == comparable(name, baseline[name]), name

    knowledge_query = repo._runtime.knowledge_query
    reader = knowledge_query.viewer_scope
    probe: list = []
    with repo._runtime.database.connect() as db:
        db.set_trace_callback(probe.append)
        assert reader.__self__.sources.memory_source_ids(db, world["nb"]) == []
        db.set_trace_callback(None)
    read_all = reads(world)

    def statements_of(name, scoped):
        monkeypatch.setattr(knowledge_query, "viewer_scope",
                            reader if scoped else (lambda *_a, **_k: None))
        as_user(world["user"], read_all[name])       # warm the memos
        statements: list = []
        with monkeypatch.context() as patch:
            _traced(repo, patch, statements)
            as_user(world["user"], read_all[name])
        return statements

    for name in READS:
        before = statements_of(name, scoped=False)
        after = statements_of(name, scoped=True)
        # (With the E4-4 seam on, the summary's own no-Memory probe runs on
        # both sides; it is the same statement.)
        assert [s for s in after if s not in probe] == [
            s for s in before if s not in probe], name
        assert 0 <= len(after) - len(before) <= 1, (name, after)


# ------------------------------------------------ summary counts (item 3)
def test_a_closed_channel_shows_no_memory_count(repo, monkeypatch):
    """Coordinator decision (e4-7-close): with the Memory channel closed the
    viewer's own Memory count is 0 in the notebook detail and in the list."""
    s = build_scenario(repo, b_memory=False)
    assert as_user(s.a, repo.get_notebook, s.nb).counts["memories"] == 1
    monkeypatch.setattr(kg_viewer_scope, "memory_channel_allowed", lambda: False)
    assert as_user(s.a, repo.get_notebook, s.nb).counts["memories"] == 0
    listed = {nb.id: nb for nb in as_user(s.a, repo.list_notebooks)}
    assert listed[s.nb].counts["memories"] == 0


def test_summary_counts_and_kg_ready_carry_the_viewer(repo, monkeypatch):
    """F1 (D2's three values, E4-4): on a notebook holding Memory the counts
    and ``kg_ready`` are read for the viewer -- the member's id, the owner's
    id, '' with the channel closed or without a user; a notebook without
    Memory keeps today's statement.  ``kg_ready`` follows the keyword (the
    store double answers True only for A's own-Memory view)."""
    s = build_scenario(repo, b_memory=False)
    monkeypatch.setattr(kg_viewer_scope, "STORE_READERS_TAKE_VIEWER_ID", True)
    queries = repo._runtime.queries
    seen: list = []
    _spy_viewer_keyword(monkeypatch, queries, "knowledge_type_count_rows", seen)
    _spy_viewer_keyword(monkeypatch, queries, "notebook_has_kg", seen,
                        answer=lambda viewer: viewer == s.a.id)
    member = as_user(s.b, repo.get_notebook, s.nb)
    assert sorted(seen) == [("knowledge_type_count_rows", s.b.id),
                            ("notebook_has_kg", s.b.id)]
    assert member.kg_ready is False
    seen.clear()
    owner = as_user(s.a, repo.get_notebook, s.nb)
    assert sorted(seen) == [("knowledge_type_count_rows", s.a.id),
                            ("notebook_has_kg", s.a.id)]
    assert owner.kg_ready is True
    seen.clear()
    monkeypatch.setattr(kg_viewer_scope, "memory_channel_allowed", lambda: False)
    listed = {nb.id: nb for nb in as_user(s.a, repo.list_notebooks)}
    assert {value for _name, value in seen} == {""}
    assert listed[s.nb].kg_ready is False
    monkeypatch.setattr(kg_viewer_scope, "memory_channel_allowed", lambda: True)
    summaries = repo._runtime.notebook_summaries
    with repo._runtime.database.connect() as db:
        assert summaries.viewer_count_kwargs(db, s.nb, None) == {"viewer_id": ""}
        assert summaries.viewer_count_kwargs(db, s.nb, s.b.id) == {"viewer_id": s.b.id}
    plain = as_user(s.a, repo.create_notebook, NotebookCreate(name="plain")).id
    seen.clear()
    as_user(s.a, repo.get_notebook, plain)
    assert {value for _name, value in seen} == {_MISSING}
    monkeypatch.setattr(kg_viewer_scope, "STORE_READERS_TAKE_VIEWER_ID", False)
    with repo._runtime.database.connect() as db:
        assert summaries.viewer_count_kwargs(db, s.nb, s.b.id) == {}


def test_analytics_and_the_search_box_carry_the_viewer(repo, monkeypatch):
    """E4-4 handover: ``notebook_analytics``' knowledge counts and the search
    box's knowledge leg (HTTP ``/search``, MCP ``search_notebook_context``)
    take the viewer on the summary counts' rule."""
    s = build_scenario(repo, b_memory=False)
    queries = repo._runtime.queries
    seen: list = []
    for name in ("notebook_analytics", "search_notebook"):
        _spy_viewer_keyword(monkeypatch, queries, name, seen)

    def read(user, nb):
        as_user(user, repo.notebook_analytics, nb)
        as_user(user, repo.search_notebook, nb, "Engram")

    monkeypatch.setattr(kg_viewer_scope, "STORE_READERS_TAKE_VIEWER_ID", False)
    read(s.b, s.nb)
    assert seen == [("notebook_analytics", _MISSING), ("search_notebook", _MISSING)]
    seen.clear()
    monkeypatch.setattr(kg_viewer_scope, "STORE_READERS_TAKE_VIEWER_ID", True)
    read(s.b, s.nb)
    assert seen == [("notebook_analytics", s.b.id), ("search_notebook", s.b.id)]
    seen.clear()
    read(s.a, s.nb)
    assert seen == [("notebook_analytics", s.a.id), ("search_notebook", s.a.id)]
    seen.clear()
    monkeypatch.setattr(kg_viewer_scope, "memory_channel_allowed", lambda: False)
    read(s.a, s.nb)
    assert seen == [("notebook_analytics", ""), ("search_notebook", "")]
    seen.clear()
    monkeypatch.setattr(kg_viewer_scope, "memory_channel_allowed", lambda: True)
    plain = as_user(s.a, repo.create_notebook, NotebookCreate(name="plain")).id
    read(s.a, plain)
    assert seen == [("notebook_analytics", _MISSING), ("search_notebook", _MISSING)]


def test_the_notebook_list_probes_memory_once_for_all_its_notebooks(repo, monkeypatch):
    """P2-A: with the E4-4 seam on, the notebook list learns which of its
    notebooks hold Memory in ONE statement -- the page costs exactly one
    statement more than with the seam off, however many notebooks it lists
    -- and only the notebook holding Memory is counted for the viewer."""
    s = build_scenario(repo, b_memory=False)
    plains = [as_user(s.a, repo.create_notebook, NotebookCreate(name=f"p{i}")).id
              for i in range(5)]
    queries = repo._runtime.queries
    seen: list = []
    for name in ("knowledge_type_count_rows", "notebook_has_kg"):
        _spy_viewer_keyword(monkeypatch, queries, name, seen)

    def listed(switch):
        monkeypatch.setattr(kg_viewer_scope, "STORE_READERS_TAKE_VIEWER_ID", switch)
        as_user(s.a, repo.list_notebooks)                # warm the memos
        seen.clear()
        statements: list = []
        with monkeypatch.context() as patch:
            _traced(repo, patch, statements)
            answer = as_user(s.a, repo.list_notebooks)
        return answer, statements

    off, off_sql = listed(False)
    on, on_sql = listed(True)
    assert [nb.model_dump() for nb in on] == [nb.model_dump() for nb in off]
    extra = [sql for sql in on_sql if sql not in off_sql]
    assert len(on_sql) == len(off_sql) + 1, extra
    assert len(extra) == 1 and "DISTINCT notebook_id FROM sources" in extra[0], extra
    carried = [value for _name, value in seen]
    assert carried.count(s.a.id) == 2, seen
    assert carried.count(_MISSING) == 2 * len(plains), seen
    assert len(carried) == 2 * (len(plains) + 1), seen


def test_memory_holders_is_one_indexed_statement(repo):
    """The batched presence read (``memory_source_ids(holders_among=...)``):
    exact answer, one statement, a seek of ``idx_sources_nb_hidden_type`` per
    notebook (EXPLAIN pin) and no scan of ``sources``."""
    s = build_scenario(repo, b_memory=False)
    plain = as_user(s.a, repo.create_notebook, NotebookCreate(name="plain")).id
    with repo._write() as db:              # a source, but no Memory
        _source(db, plain, "src-plain-only", elements=[("el-po", "PLAIN")])
    sources = repo._runtime.source_store
    statements: list = []
    with repo._runtime.database.connect() as db:
        db.set_trace_callback(statements.append)
        holders = sources.memory_source_ids(
            db, "", holders_among=[plain, s.nb, "", s.nb, "nb-missing"])
        db.set_trace_callback(None)
        assert holders == [s.nb]
        assert len(statements) == 1
        assert sources.memory_source_ids(db, s.nb, holders_among=[]) == []
        from app.repositories.sqlite.id_binding import bind_ids, drive_by
        from app.repositories.sqlite.source_store import _MEMORY_HOLDERS_SQL
        bound = bind_ids([plain, s.nb])
        plan = " | ".join(str(row["detail"]) for row in db.execute(
            "EXPLAIN QUERY PLAN "
            + _MEMORY_HOLDERS_SQL.format(drive=drive_by("notebook_id", bound)),
            (bound.param,)).fetchall())
    assert "idx_sources_nb_hidden_type" in plan, plan
    assert "SCAN sources" not in plan, plan
