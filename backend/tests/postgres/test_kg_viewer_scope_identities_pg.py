"""PostgreSQL twin of tests/test_kg_viewer_scope_identities.py (codex #806
round 1): the viewer rule on canonical nodes, legacy sibling procedures and
the actual source of a legacy step's element."""
from __future__ import annotations

import json

import pytest

from tests.postgres.test_kg_viewer_scope_pg import (  # noqa: F401  (``repo`` is a fixture)
    _ev,
    _object_id,
    as_user,
    build_scenario,
    repo,
)


pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_kg_viewer_scope"),
]


from tests.pre_isolation_graph import build_pre_isolation  # noqa: E402


def _reader(repo):
    return repo._runtime.knowledge_query.viewer_scope.__self__


@pytest.fixture(params=["viz", "db"])
def neighbour_path(request, repo, monkeypatch):
    if request.param == "db":
        monkeypatch.setattr(
            repo._runtime.scale_artifacts, "viz_index", lambda *_a, **_k: None)
    return request.param


def _certify(repo, nb, certified):
    with repo._runtime.database.write() as db:
        db.execute(
            "UPDATE unified_kg_state SET source_index_backfilled=%s WHERE notebook_id=%s",
            (1 if certified else 0, nb))


@pytest.mark.parametrize("certified", [True, False])
def test_pg_neighbours_keep_a_cluster_whose_member_only_cites_foreign_memory(
    repo, neighbour_path, certified,
):
    """E4-7 (D2/D4): owned by the visible source, so B sees it; its evidence
    item from A's Memory never reaches B."""
    s = build_scenario(repo, b_memory=False)
    repo.store_kg(s.nb, "src-s", [
        {"local_id": "hub", "object_type": "concept",
         "payload": {"name": "Engram", "section_path": "1"},
         "evidence": [_ev("src-s", "el-s-occ")]},
        {"local_id": "ph", "object_type": "concept",
         "payload": {"name": "Phantom", "section_path": "1"},
         "evidence": [_ev("src-ma", "el-ma-secret")]},
    ], [{"source_local_id": "ph", "target_local_id": "hub", "edge_type": "related_to",
         "evidence": []}])
    build_pre_isolation(repo, s.nb)
    with repo._runtime.database.connect() as db:
        phantom = _object_id(db, s.nb, "Phantom", "src-s")
    phantom_c = repo.cluster_map(s.nb)[phantom]
    _certify(repo, s.nb, certified)
    owner = as_user(s.a, repo.kg_neighbors, s.nb, s.ids.engram_s)
    assert phantom_c in {n["id"] for n in owner["nodes"]}
    view = as_user(s.b, repo.kg_neighbors, s.nb, s.ids.engram_s)
    assert phantom_c in {n["id"] for n in view["nodes"]}
    assert "A-PRIVATE" not in repr(view)
    detail = as_user(s.b, repo.concept_detail, s.nb, phantom_c)
    assert [m["id"] for m in detail["members"]] == [phantom]
    assert detail["members"][0]["evidence"] == [] and detail["evidence"] == []
    assert "el-ma" not in repr(detail) and "src-ma" not in repr(detail)


@pytest.mark.parametrize("certified", [True, False])
def test_pg_cluster_labels_never_come_from_a_hidden_member(repo, certified):
    s = build_scenario(repo, b_memory=False)
    repo.store_kg(s.nb, "src-s", [
        {"local_id": "g1", "object_type": "concept",
         "payload": {"name": "Gadget", "section_path": "G"},
         "evidence": [_ev("src-s", "el-s-def")]},
    ], [])
    repo.store_kg(s.nb, "src-ma", [
        {"local_id": "g2", "object_type": "concept",
         "payload": {"name": "Gadget", "section_path": "A-PRIVATE G"},
         "evidence": [_ev("src-ma", "el-ma-def")]},
    ], [])
    build_pre_isolation(repo, s.nb)
    database = repo._runtime.database
    with database.connect() as db:
        visible = db.execute(
            "SELECT id FROM knowledge_objects WHERE notebook_id=%s AND source_id='src-s' "
            "AND payload->>'section_path'='G'", (s.nb,)).fetchone()["id"]
    canonical = repo.cluster_map(s.nb)[visible]
    _certify(repo, s.nb, certified)
    with database.write() as db:
        db.execute(
            "UPDATE concept_clusters SET canonical_name='A-PRIVATE Gadget' "
            "WHERE notebook_id=%s AND canonical_id=%s", (s.nb, canonical))
    detail = as_user(s.b, repo.concept_detail, s.nb, canonical)
    assert [m["id"] for m in detail["members"]] == [visible]
    assert detail["canonical_name"] == "Gadget"
    assert "A-PRIVATE" not in repr(detail)
    scope = as_user(s.b, _reader(repo).for_notebook, s.nb)
    assert scope.cluster_display_name(canonical, "A-PRIVATE Gadget") == "Gadget"
    kept, _edges = as_user(
        s.b, scope.filter_neighbourhood,
        [{"id": canonical, "object_type": "concept",
          "payload": {"name": "A-PRIVATE baked Gadget"}}], [], (s.ids.engram_canonical,))
    assert [n["payload"]["name"] for n in kept] == ["Gadget"]
    assert as_user(s.a, repo.concept_detail, s.nb, canonical)["canonical_name"] == (
        "A-PRIVATE Gadget")


def test_pg_a_raw_object_is_judged_on_its_own_source_whatever_its_status(repo, monkeypatch):
    """Twin of the SQLite test: a deprecated object of A's Memory that
    absorbed a visible evidence item keeps its Memory relation to Engram, so
    the DB neighbour path returns it as a raw node; B never gets it."""
    s = build_scenario(repo, b_memory=False)
    raw = json.dumps([_ev("src-ma", "el-ma-secret"), _ev("src-s", "el-s-occ")])
    with repo._runtime.database.write() as db:
        db.execute(
            "UPDATE knowledge_objects SET evidence=%s::jsonb, status='deprecated' WHERE id=%s",
            (raw, s.ids.secret))
        repo._runtime.knowledge.replace_object_sources(db, s.ids.secret, s.nb, raw)
    build_pre_isolation(repo, s.nb, force=True)
    assert s.ids.secret not in repo.cluster_map(s.nb)
    monkeypatch.setattr(repo._runtime.scale_artifacts, "viz_index", lambda *_a, **_k: None)
    owner = as_user(s.a, repo.kg_neighbors, s.nb, s.ids.engram_s)
    assert s.ids.secret in {n["id"] for n in owner["nodes"]}, owner
    view = as_user(s.b, repo.kg_neighbors, s.nb, s.ids.engram_s)
    assert s.ids.secret not in {n["id"] for n in view["nodes"]}
    assert "SecretProject" not in repr(view)
    hidden = as_user(s.b, repo.kg_neighbors, s.nb, s.ids.secret)
    assert hidden["nodes"] == [] and hidden["edges"] == []


def _legacy(repo, nb, name, section, source_id, evidence):
    oid = repo._test_insert_object(
        nb, "procedure", {"name": name, "section_path": section}, source_id=source_id)
    raw = json.dumps(evidence)
    with repo._runtime.database.write() as db:
        db.execute("UPDATE knowledge_objects SET evidence=%s::jsonb WHERE id=%s", (raw, oid))
        repo._runtime.knowledge.replace_object_sources(db, oid, nb, raw)
    return oid


@pytest.mark.parametrize("section", ["L", ""])
def test_pg_legacy_sibling_owned_by_foreign_memory_is_not_a_step(repo, section):
    s = build_scenario(repo, b_memory=False)
    target = _legacy(repo, s.nb, "visible step", section, "src-s",
                     [_ev("src-s", "el-s-step")])
    _legacy(repo, s.nb, "A-PRIVATE step name", section, "src-ma",
            [_ev("src-s", "el-s-step")])
    member = as_user(s.b, repo.node_context, s.nb, target)
    assert [st["name"] for st in member["steps"]] == ["visible step"]
    assert "A-PRIVATE" not in repr(member)
    owner = as_user(s.a, repo.node_context, s.nb, target)
    assert sorted(st["name"] for st in owner["steps"]) == [
        "A-PRIVATE step name", "visible step"]


@pytest.mark.parametrize("section", ["L", ""])
def test_pg_legacy_step_text_comes_from_the_elements_actual_source(repo, section):
    s = build_scenario(repo, b_memory=False)
    target = _legacy(repo, s.nb, "visible step", section, "src-s",
                     [_ev("src-s", "el-s-step")])
    _legacy(repo, s.nb, "mislabelled sibling", section, "src-s",
            [_ev("src-s", "el-ma-step")])
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


def test_pg_fused_description_counts_a_member_owned_by_foreign_memory(repo):
    s = build_scenario(repo, b_memory=False)
    repo.store_kg(s.nb, "src-s", [
        {"local_id": "z", "object_type": "concept",
         "payload": {"name": "Gizmo", "section_path": "Z"},
         "evidence": [_ev("src-s", "el-s-def")]}], [])
    repo.store_kg(s.nb, "src-ma", [
        {"local_id": "z", "object_type": "concept",
         "payload": {"name": "Gizmo", "section_path": "Z"},
         "evidence": [_ev("src-s", "el-s-occ")]}], [])
    build_pre_isolation(repo, s.nb)
    database = repo._runtime.database
    with database.connect() as db:
        visible = _object_id(db, s.nb, "Gizmo", "src-s")
        owned = _object_id(db, s.nb, "Gizmo", "src-ma")
    canonical = repo.cluster_map(s.nb)[visible]
    assert repo.cluster_map(s.nb)[owned] == canonical
    with database.write() as db:
        db.execute(
            "UPDATE concept_clusters SET canonical_description='A-PRIVATE fused Gizmo' "
            "WHERE notebook_id=%s AND canonical_id=%s", (s.nb, canonical))
    ctx = as_user(s.b, repo.node_context, s.nb, visible)
    assert ctx["definition_basis"] != "cluster_description"
    assert "A-PRIVATE" not in repr(ctx)
    assert as_user(s.a, repo.node_context, s.nb, visible)["definition"] == (
        "A-PRIVATE fused Gizmo")


def test_pg_evidence_items_are_judged_on_named_and_actual_source(repo):
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
    build_pre_isolation(repo, s.nb)
    with repo._runtime.database.connect() as db:
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


def test_pg_owned_and_citing_sets_come_from_one_statement(repo):
    """``relink_object_rows_for_source(..., with_citing=True)`` on
    PostgreSQL: owned rows, citing rows and the certificate marker; an
    uncertified reverse index yields owned rows only; an empty list issues
    nothing (same contract as the SQLite twin)."""
    s = build_scenario(repo, b_memory=False)
    knowledge = repo._runtime.knowledge

    def kinds():
        with repo._runtime.database.connect() as db:
            out: dict = {}
            for row in knowledge.relink_object_rows_for_source(
                db, s.nb, source_ids=["src-ma", "src-none"], with_citing=True
            ):
                out.setdefault(row["kind"], set()).add(row["id"])
            return out

    got = kinds()
    assert got["owned"] == {s.ids.engram_ma, s.ids.secret, s.ids.definer_ma}
    assert {s.ids.engram_ma, s.ids.secret, s.ids.definer_ma} <= got["citing"]
    assert s.ids.engram_s not in got["citing"]
    assert got["certified"] == {None}
    _certify(repo, s.nb, False)
    assert set(kinds()) == {"owned"}
    with repo._runtime.database.connect() as db:
        assert knowledge.relink_object_rows_for_source(
            db, s.nb, source_ids=[], with_citing=True) == []


# ---------------------------------- neighbour member windows (codex #806 r3)
# Twin of the SQLite block: one suspect hub no longer sizes every cluster's
# member window; only a cluster with nothing visible in its window is paged
# further, on its own; a cluster with no visible member at all stays hidden.
from tests.test_kg_viewer_scope_identities import (  # noqa: E402
    DARK,
    DEEP,
    HUB,
    MEMBER_SPECS,
    ORDINARY,
    _member_ids,
    count_hydrated_member_rows,
    neighbour_nodes,
    seed_member_clusters,
)


def _seed(repo, nb):
    return seed_member_clusters(repo, nb, MEMBER_SPECS, ph="%s", cast="::jsonb")


def test_pg_a_suspect_hub_no_longer_sizes_every_clusters_member_window(repo, monkeypatch):
    from app.services.kg_viewer_scope import _FIRST_MEMBER_WINDOW as window

    s = build_scenario(repo, b_memory=False)
    _seed(repo, s.nb)
    _certify(repo, s.nb, True)
    reader = _reader(repo)
    scope = as_user(s.b, reader.for_notebook, s.nb)
    assert scope.hidden_member_bound(HUB) == 30
    hydrated = count_hydrated_member_rows(reader.knowledge, monkeypatch)
    clusters = [HUB, *ORDINARY, DEEP]
    nodes, edges = neighbour_nodes(s.ids.engram_canonical, clusters)
    kept, _edges = as_user(s.b, scope.filter_neighbourhood, nodes, edges,
                           (s.ids.engram_canonical,))
    assert [n["id"] for n in kept] == clusters
    assert hydrated == [len(clusters) * window + 2, len(MEMBER_SPECS[DEEP]) - window]
    assert sum(hydrated) <= (len(clusters) + 1) * window + len(MEMBER_SPECS[DEEP])


@pytest.mark.parametrize("certified", [True, False])
def test_pg_a_cluster_is_labelled_by_a_visible_member_beyond_the_first_window(
    repo, certified,
):
    s = build_scenario(repo, b_memory=False)
    _seed(repo, s.nb)
    _certify(repo, s.nb, certified)
    scope = as_user(s.b, _reader(repo).for_notebook, s.nb)
    nodes, edges = neighbour_nodes(s.ids.engram_canonical, [HUB, DEEP])
    kept, kept_edges = as_user(s.b, scope.filter_neighbourhood, nodes, edges,
                               (s.ids.engram_canonical,))
    labels = {n["id"]: n["payload"]["name"] for n in kept}
    assert labels == {HUB: f"{HUB} v0", DEEP: f"{DEEP} v12"}
    assert len(kept_edges) == 2 and "A-PRIVATE" not in repr(kept)


@pytest.mark.parametrize("certified", [True, False])
def test_pg_a_cluster_with_no_visible_member_anywhere_is_hidden(repo, certified):
    s = build_scenario(repo, b_memory=False)
    _seed(repo, s.nb)
    _certify(repo, s.nb, certified)
    scope = as_user(s.b, _reader(repo).for_notebook, s.nb)
    nodes, edges = neighbour_nodes(s.ids.engram_canonical, [DARK, ORDINARY[0]])
    kept, kept_edges = as_user(s.b, scope.filter_neighbourhood, nodes, edges,
                               (s.ids.engram_canonical,))
    assert [n["id"] for n in kept] == [ORDINARY[0]]
    assert [e["target_object_id"] for e in kept_edges] == [ORDINARY[0]]
    assert "A-PRIVATE" not in repr(kept)
    assert as_user(s.b, scope.filter_neighbourhood, *neighbour_nodes(DARK, []), (DARK,)) is None


# ------------------------------------- concept detail pages (codex #806 r4)
def test_pg_a_concept_page_reads_the_page_size_not_the_hubs_suspect_count(repo, monkeypatch):
    from app.services.kg_viewer_scope import _FIRST_MEMBER_WINDOW as window

    s = build_scenario(repo, b_memory=False)
    _seed(repo, s.nb)
    _certify(repo, s.nb, True)
    knowledge = _reader(repo).knowledge
    assert knowledge is repo._runtime.knowledge_query.knowledge
    hydrated = count_hydrated_member_rows(knowledge, monkeypatch)
    first = as_user(s.b, repo.concept_detail, s.nb, HUB, limit=5)
    assert [m["payload"]["name"] for m in first["members"]] == [f"{HUB} v{i}" for i in range(5)]
    assert first["canonical_name"] == f"{HUB} v0"
    assert hydrated == [6]
    second = as_user(s.b, repo.concept_detail, s.nb, HUB, limit=5,
                     after=first["next_cursor"])
    assert [m["payload"]["name"] for m in second["members"]] == [
        f"{HUB} v{i}" for i in range(5, 10)]
    assert second["canonical_name"] == f"{HUB} v0"
    assert hydrated == [6, 6, window]


@pytest.mark.parametrize("certified", [True, False])
def test_pg_a_concept_page_widens_past_hidden_members(repo, certified):
    s = build_scenario(repo, b_memory=False)
    _seed(repo, s.nb)
    _certify(repo, s.nb, certified)
    page = as_user(s.b, repo.concept_detail, s.nb, DEEP, limit=3)
    assert [m["payload"]["name"] for m in page["members"]] == [
        f"{DEEP} v12", f"{DEEP} v13", f"{DEEP} v14"]
    assert page["canonical_name"] == f"{DEEP} v12"
    assert "A-PRIVATE" not in repr(page)
    rest = as_user(s.b, repo.concept_detail, s.nb, DEEP, limit=3, after=page["next_cursor"])
    assert [m["payload"]["name"] for m in rest["members"]] == [f"{DEEP} v15", f"{DEEP} v16"]
    assert rest["canonical_name"] == f"{DEEP} v12" and rest["next_cursor"] is None


@pytest.mark.parametrize("certified", [True, False])
def test_pg_a_cluster_with_no_visible_member_is_absent_on_every_page(repo, certified):
    s = build_scenario(repo, b_memory=False)
    names = _seed(repo, s.nb)
    _certify(repo, s.nb, certified)
    members = _member_ids(names, DARK)
    for after in ("", members[0], members[7], members[-1]):
        with pytest.raises(KeyError):
            as_user(s.b, repo.concept_detail, s.nb, DARK, limit=3, after=after)
    owner = as_user(s.a, repo.concept_detail, s.nb, DARK, limit=3, after=members[0])
    assert owner["canonical_name"] == f"A-PRIVATE {DARK}"
