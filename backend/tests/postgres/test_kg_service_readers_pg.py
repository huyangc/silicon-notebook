"""PostgreSQL twin of tests/test_kg_service_readers.py (E4-7): the shared
graph plus the viewer's own Memory from the live tables and its
viewer-independent cache, the neighbourhood of an own Memory object, the
list's element-source rule, the folded search's one member read, the
viewer keyword of the summary counts / analytics / search box, and the
assembly tripwires (tests/test_kg_viewer_scope_assembly.py)."""
from __future__ import annotations

import pytest

from app.models.notebooks import NotebookCreate
from app.services import kg_viewer_scope
from tests.postgres.test_kg_viewer_scope_pg import (  # noqa: F401  (``repo`` is a fixture)
    T0,
    _ev,
    _source,
    as_user,
    build_scenario,
    repo,
)
from tests.test_kg_service_readers import (
    _MISSING,
    _seed_named_cluster,
    _spy_viewer_keyword,
    build_pre_isolation,
)
from tests.test_kg_viewer_scope_assembly import (
    assert_isolation_marker_is_wired,
    assert_store_seam_matches_the_stores,
)


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_kg_viewer_scope"),
]


def _node_ids(graph):
    return {str(node["id"]) for node in graph["nodes"]}


@pytest.mark.parametrize("path", ["full", "artifact"])
def test_pg_graph_view_is_the_shared_graph_plus_the_viewers_own_memory(repo, path):
    s = build_scenario(repo, b_memory=False)
    kwargs = {"level": "object"}
    if path == "artifact":
        assert repo._runtime.scale_artifacts.build_viz(s.nb) is not None
        kwargs["limit"] = 80
    member = as_user(s.b, repo.unified_graph, s.nb, **kwargs)
    private = {s.ids.secret, s.ids.engram_ma, s.ids.definer_ma, s.ids.secret_canonical}
    assert not (_node_ids(member) & private), member
    assert "SecretProject" not in repr(member) and "private definer" not in repr(member)
    owner = as_user(s.a, repo.unified_graph, s.nb, **kwargs)
    assert {s.ids.secret, s.ids.engram_ma, s.ids.definer_ma} <= _node_ids(owner)
    assert any(e["source_object_id"] == s.ids.secret
               and e["target_object_id"] == s.ids.engram_ma for e in owner["edges"])
    names = {n["id"]: n["payload"]["name"] for n in owner["nodes"]}
    assert names[s.ids.secret] == "SecretProject"


def test_pg_closed_channel_hides_the_owners_own_memory(repo, monkeypatch):
    s = build_scenario(repo, b_memory=False)
    monkeypatch.setattr(kg_viewer_scope, "memory_channel_allowed", lambda: False)
    view = as_user(s.a, repo.unified_graph, s.nb, level="object")
    assert not (_node_ids(view) & {s.ids.secret, s.ids.engram_ma, s.ids.definer_ma})


def test_pg_closed_channel_shows_no_memory_count(repo, monkeypatch):
    s = build_scenario(repo, b_memory=False)
    assert as_user(s.a, repo.get_notebook, s.nb).counts["memories"] == 1
    monkeypatch.setattr(kg_viewer_scope, "memory_channel_allowed", lambda: False)
    assert as_user(s.a, repo.get_notebook, s.nb).counts["memories"] == 0
    listed = {nb.id: nb for nb in as_user(s.a, repo.list_notebooks)}
    assert listed[s.nb].counts["memories"] == 0


def test_pg_neighbours_of_an_own_memory_object(repo):
    s = build_scenario(repo, b_memory=False)
    view = as_user(s.a, repo.kg_neighbors, s.nb, s.ids.secret)
    assert _node_ids(view) == {s.ids.secret, s.ids.engram_ma}
    assert {n["id"]: n["payload"]["name"] for n in view["nodes"]} == {
        s.ids.secret: "SecretProject", s.ids.engram_ma: "Engram"}
    hidden = as_user(s.b, repo.kg_neighbors, s.nb, s.ids.secret)
    assert hidden["nodes"] == [] and hidden["edges"] == []


def _private(s):
    return {s.ids.secret, s.ids.engram_ma, s.ids.definer_ma, s.ids.secret_canonical}


@pytest.mark.parametrize("level", ["object", "concept"])
def test_pg_the_shared_graph_cache_is_the_same_for_every_viewer(repo, level):
    s = build_scenario(repo, b_memory=False)
    owner = as_user(s.a, repo.unified_graph, s.nb, level=level)
    member = as_user(s.b, repo.unified_graph, s.nb, level=level)
    for view in (owner, member):
        ids = [str(node["id"]) for node in view["nodes"]]
        assert len(ids) == len(set(ids)), ids
    assert not (_node_ids(member) & _private(s)), member
    assert s.ids.secret in _node_ids(owner)


def test_pg_an_unfiltered_cache_entry_never_serves_a_member(repo):
    s = build_scenario(repo, b_memory=False)
    everything = repo._runtime.knowledge_lifecycle._unified_graph_full(s.nb, "object")
    assert "SecretProject" in repr(everything)
    member = as_user(s.b, repo.unified_graph, s.nb, level="object")
    assert not (_node_ids(member) & _private(s)), member


def _memory_relation(repo, s, rel_id, source_object, target_object, edge_type):
    with repo._runtime.database.write() as db:
        db.execute(
            "INSERT INTO knowledge_relations (id,notebook_id,source_id,"
            "source_object_id,target_object_id,edge_type,evidence,created_at) "
            "VALUES (%s,%s,'src-ma',%s,%s,%s,'[]'::jsonb,%s)",
            (rel_id, s.nb, source_object, target_object, edge_type, T0))


def test_pg_a_memory_relation_between_shared_objects_is_in_no_shared_graph(repo):
    s = build_scenario(repo, b_memory=False)
    _memory_relation(repo, s, "rel-mem-shared", s.ids.flow, s.ids.engram_s,
                     "privateedge")
    for user in (s.b, s.a):
        assert "privateedge" not in repr(
            as_user(user, repo.unified_graph, s.nb, level="object")), user.id
    assert "privateedge" not in repr(as_user(s.b, repo.knowledge_graph, s.nb))


def test_pg_overlay_edges_join_two_overlay_nodes(repo):
    s = build_scenario(repo, b_memory=False)
    _memory_relation(repo, s, "rel-mem-own", s.ids.engram_ma, s.ids.definer_ma,
                     "ownedge")
    view = as_user(s.a, repo.unified_graph, s.nb, level="object", limit=1)
    ids = _node_ids(view)
    for edge in view["edges"]:
        assert {edge["source_object_id"], edge["target_object_id"]} <= ids, edge
    assert len(view["nodes"]) <= 2


def test_pg_graph_totals_do_not_depend_on_the_limit(repo):
    """Twin of the SQLite case (codex #824 r2)."""
    s = build_scenario(repo, b_memory=False)
    _memory_relation(repo, s, "rel-mem-own", s.ids.engram_ma, s.ids.definer_ma,
                     "ownedge")
    narrow = as_user(s.a, repo.unified_graph, s.nb, level="object", limit=1)
    wide = as_user(s.a, repo.unified_graph, s.nb, level="object", limit=80)
    assert len(wide["edges"]) > len(narrow["edges"]), "fixture: the cut drops an edge"
    assert (narrow["total_nodes"], narrow["total_edges"]) == (
        wide["total_nodes"], wide["total_edges"]), (narrow, wide)


def test_pg_own_memory_neighbours_are_own_memory_objects_only(repo):
    s = build_scenario(repo, b_memory=False)
    view = as_user(s.a, repo.kg_neighbors, s.nb, s.ids.definer_ma)
    assert _node_ids(view) == {s.ids.definer_ma}, view


def test_pg_list_judges_an_item_by_the_source_its_element_lives_in(repo):
    s = build_scenario(repo, b_memory=False)
    repo.store_kg(s.nb, "src-s", [{
        "local_id": "l", "object_type": "concept",
        "payload": {"name": "Legacyshape", "section_path": "1"},
        "evidence": [_ev("src-s", "el-s-occ"),
                     {**_ev("src-s", "el-ma-secret"),
                      "quoted_span": "A-PRIVATE secret project"}]}], [])
    page = as_user(s.b, repo.list_knowledge, s.nb, "concept")
    legacy = [item for item in page.items if item.headline == "Legacyshape"]
    assert [e.element_id for e in legacy[0].evidence] == ["el-s-occ"]
    assert "A-PRIVATE" not in repr(page)
    assert "A-PRIVATE secret project" in repr(
        as_user(s.a, repo.list_knowledge, s.nb, "concept"))


def test_pg_folded_search_hits_are_labelled_in_one_member_read(repo, monkeypatch):
    s = build_scenario(repo, b_memory=False)
    words = ["alpha", "beta", "gamma", "delta"]
    for source, element in (("src-s", "el-s-occ"), ("src-ma", "el-ma-occ")):
        repo.store_kg(s.nb, source, [
            {"local_id": word, "object_type": "concept",
             "payload": {"name": f"Foldterm {word}", "section_path": "1"},
             "evidence": [_ev(source, element)]} for word in words], [])
    build_pre_isolation(repo, s.nb)
    knowledge = repo._runtime.knowledge
    detail = knowledge.concept_cluster_detail_rows
    calls: list = []
    monkeypatch.setattr(knowledge, "concept_cluster_detail_rows",
                        lambda *a, **k: calls.append(k) or detail(*a, **k))
    hits = as_user(s.b, repo.kg_search, s.nb, "Foldterm")
    assert sorted(h["name"] for h in hits) == sorted(f"Foldterm {w}" for w in words)
    assert len(calls) == 1, calls


def test_pg_summary_counts_analytics_and_search_carry_the_viewer(repo, monkeypatch):
    s = build_scenario(repo, b_memory=False)
    monkeypatch.setattr(kg_viewer_scope, "STORE_READERS_TAKE_VIEWER_ID", True)
    queries = repo._runtime.queries
    seen: list = []
    _spy_viewer_keyword(monkeypatch, queries, "knowledge_type_count_rows", seen)
    _spy_viewer_keyword(monkeypatch, queries, "notebook_has_kg", seen,
                        answer=lambda viewer: viewer == s.a.id)
    for name in ("notebook_analytics", "search_notebook"):
        _spy_viewer_keyword(monkeypatch, queries, name, seen)
    assert as_user(s.b, repo.get_notebook, s.nb).kg_ready is False
    as_user(s.b, repo.notebook_analytics, s.nb)
    as_user(s.b, repo.search_notebook, s.nb, "Engram")
    assert {value for _name, value in seen} == {s.b.id}, seen
    seen.clear()
    assert as_user(s.a, repo.get_notebook, s.nb).kg_ready is True
    assert {value for _name, value in seen} == {s.a.id}, seen
    seen.clear()
    monkeypatch.setattr(kg_viewer_scope, "memory_channel_allowed", lambda: False)
    as_user(s.a, repo.get_notebook, s.nb)
    as_user(s.a, repo.search_notebook, s.nb, "Engram")
    assert {value for _name, value in seen} == {""}, seen
    seen.clear()
    monkeypatch.setattr(kg_viewer_scope, "memory_channel_allowed", lambda: True)
    plain = as_user(s.a, repo.create_notebook, NotebookCreate(name="plain")).id
    as_user(s.a, repo.get_notebook, plain)
    as_user(s.a, repo.notebook_analytics, plain)
    assert {value for _name, value in seen} == {_MISSING}, seen


@pytest.mark.parametrize("large", [False, True])
def test_pg_a_focus_missing_from_the_artifact_falls_back_to_the_live_path(
    repo, monkeypatch, large,
):
    s = build_scenario(repo, b_memory=False)
    scale = repo._runtime.scale_artifacts
    other = as_user(s.a, repo.create_notebook, NotebookCreate(name="other")).id
    with repo._runtime.database.write() as db:
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
    assert s.ids.engram_canonical not in foreign_idx.viz_node_index()
    monkeypatch.setattr(scale, "viz_index", lambda *_a, **_k: foreign_idx)
    if large:
        monkeypatch.setattr(repo._runtime.knowledge_lifecycle.settings,
                            "viz_sync_build_max_objects", 1)
    view = as_user(s.b, repo.kg_neighbors, s.nb, s.ids.engram_s)
    if large:
        assert view.get("locating_unavailable") is True, view
    else:
        assert "visible definer" in repr(view), view
        assert not (_node_ids(view) & _private(s)), view


def test_pg_the_store_seam_matches_what_the_stores_take(repo):
    assert_store_seam_matches_the_stores(repo)


def test_pg_the_isolation_marker_reader_is_wired(repo):
    assert_isolation_marker_is_wired(repo, "postgres")


@pytest.mark.parametrize("pending", [True, False])
def test_pg_a_pending_notebook_never_answers_a_search_hit_by_a_seeded_cluster_id(
    repo, monkeypatch, pending,
):
    s = build_scenario(repo, b_memory=False)
    seed = "K-a private seedname"
    _seed_named_cluster(repo, s, seed, ph="%s")
    monkeypatch.setattr(repo._runtime.knowledge_lifecycle, "_isolation_pending",
                        lambda _db, _nb: pending)
    hits = as_user(s.b, repo.kg_search, s.nb, "Engram")
    engram = [h for h in hits if h["name"] == "Engram"]
    assert len(engram) == 1, hits
    if pending:
        assert engram[0]["object_id"] == s.ids.engram_s, hits
        assert "private seedname" not in repr(hits)
        assert as_user(s.b, repo.node_context, s.nb,
                       engram[0]["object_id"])["id"] == s.ids.engram_s
    else:
        assert engram[0]["object_id"] == seed, hits


def test_pg_list_judges_an_item_without_an_element_by_the_source_it_names(repo):
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


def test_pg_memory_holders_is_one_indexed_statement(repo):
    """``memory_source_ids(holders_among=...)`` on PostgreSQL: the exact
    answer, and (EXPLAIN pin, seqscan and bitmapscan off as in
    test_memory_sql_explain_pins.py) an index path through
    ``idx_sources_nb_hidden_type`` with the list bound as one parameter."""
    from app.repositories.postgres.id_binding import bind_ids, member_of
    from app.repositories.postgres.source_store import _MEMORY_HOLDERS_SQL

    s = build_scenario(repo, b_memory=False)
    plain = as_user(s.a, repo.create_notebook, NotebookCreate(name="plain")).id
    with repo._runtime.database.write() as db:     # a source, but no Memory
        _source(db, plain, "src-plain-only", elements=[("el-po", "PLAIN")])
    sources = repo._runtime.source_store
    with repo._runtime.database.connect() as db:
        assert sources.memory_source_ids(
            db, "", holders_among=[plain, s.nb, "", s.nb, "nb-missing"]) == [s.nb]
        assert sources.memory_source_ids(db, s.nb, holders_among=[]) == []
    bound = bind_ids([plain, s.nb])
    sql = _MEMORY_HOLDERS_SQL.format(member=member_of("notebook_id", bound))
    assert sql.count("%s") == 1
    with repo._runtime.database.write() as db:
        db.execute("SET LOCAL enable_seqscan=off")
        db.execute("SET LOCAL enable_bitmapscan=off")
        plan = "\n".join(str(row["QUERY PLAN"]) for row in db.execute(
            f"EXPLAIN (COSTS OFF) {sql}", (bound.param,)).fetchall())
    assert "idx_sources_nb_hidden_type" in plan, plan
    assert "Seq Scan" not in plan, plan
