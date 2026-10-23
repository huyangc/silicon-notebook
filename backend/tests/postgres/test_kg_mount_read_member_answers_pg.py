"""PostgreSQL twin of tests/test_kg_mount_read_member_answers.py (2026-10-23
ruling, R2), at the service level: a reader who is not a member of a mounted
library never gets a merged knowledge object's cluster id or stored name from
neighbour hydration or concept detail; a cluster id given as input and a
hidden focus answer exactly what an unknown id does; stale ids and names left
by a pruned Knowhow member stay out; the library's member is unaffected.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from app.core.request_context import reset_request_user, set_request_user
from app.models.notebooks import NotebookCreate
from tests.postgres.test_kg_viewer_scope_pg import (
    T0,
    _ev,
    _memory,
    _object_id,
    _source,
    as_user,
)
from tests.pre_isolation_graph import build_pre_isolation
from tests.test_kg_mount_read_member_answers import (
    HIDDEN_NAME,
    NEVER_ID,
    SEEDED_ID,
    assert_names_no_hidden_member,
    drop_knowhow_member,
    serve_from,
)

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_kg_viewer_scope"),
]


@pytest.fixture
def repo(postgres_settings):
    from app.services.postgres_repository import PostgresRepository

    repository = PostgresRepository(postgres_settings)
    try:
        yield repository
    finally:
        repository.close()


def build_world(repo):
    """``owner`` owns the shared notebook (where ``member``'s Memory concept
    ``Shared`` is clustered with the visible ``Shared``, the pre-isolation
    shape) and the public library ``lib`` mounted into it: visible ``QZX`` and
    ``Widget`` (``Widget`` -> ``QZX``), and the Knowhow concept
    ``Quiet Zebra Xylophone (QZX)`` that mints the QZX cluster's id; the
    stored cluster name is set to that hidden spelling."""
    identity = repo._runtime.identity
    owner = identity.create_user("a00923001", "pw123456")
    member = identity.create_user("a00923002", "pw123456")
    token = set_request_user(owner)
    try:
        nb = repo.create_notebook(NotebookCreate(name="shared")).id
        lib = repo.create_notebook(NotebookCreate(name="lib")).id
        repo._runtime.sharing.add_member(nb, member.id)
    finally:
        reset_request_user(token)
    repo.mark_notebook_base(lib)
    database = repo._runtime.database
    with database.write() as db:
        _source(db, nb, "src-s", elements=[("el-s", "VISIBLE text")])
        _memory(db, nb, "mem-m", member.id)
        _source(db, nb, "src-mm", memory_id="mem-m", elements=[("el-mm", "M-PRIVATE")])
        _source(db, lib, "src-lib", elements=[("el-lib", "LIB visible")])
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "file_name,created_at,updated_at) VALUES ('src-kh',%s,'kh','knowhow',"
            "'extracted','parsed','',%s,%s)", (lib, T0, T0))
        db.execute(
            "INSERT INTO source_elements (id,source_id,element_type,location_label,text,"
            "created_at) VALUES ('el-kh','src-kh','paragraph','p','KNOWHOW row text',%s)",
            (T0,))
    repo.store_kg(nb, "src-s", [
        {"local_id": "v", "object_type": "concept",
         "payload": {"name": "Shared", "section_path": "1"},
         "evidence": [_ev("src-s", "el-s")]}], [])
    repo.store_kg(nb, "src-mm", [
        {"local_id": "s", "object_type": "concept",
         "payload": {"name": "Shared", "section_path": "1"},
         "evidence": [_ev("src-mm", "el-mm")]},
        {"local_id": "x", "object_type": "concept",
         "payload": {"name": "Membernote", "section_path": "1"},
         "evidence": [_ev("src-mm", "el-mm")]},
    ], [{"source_local_id": "x", "target_local_id": "s", "edge_type": "related_to",
         "evidence": []}])
    build_pre_isolation(repo, nb)
    repo.store_kg(lib, "src-lib", [
        {"local_id": "q", "object_type": "concept",
         "payload": {"name": "QZX", "section_path": "1"},
         "evidence": [_ev("src-lib", "el-lib")]},
        {"local_id": "w", "object_type": "concept",
         "payload": {"name": "Widget", "section_path": "1"},
         "evidence": [_ev("src-lib", "el-lib")]},
    ], [{"source_local_id": "w", "target_local_id": "q", "edge_type": "related_to",
         "evidence": []}])
    repo.store_kg(lib, "src-kh", [
        {"local_id": "k", "object_type": "concept",
         "payload": {"name": HIDDEN_NAME, "section_path": "1"},
         "evidence": [_ev("src-kh", "el-kh")]}], [])
    repo.rebuild_unified_kg(lib)
    with database.write() as db:
        ids = SimpleNamespace(
            nb=nb, lib=lib, owner=owner, member=member,
            qzx=_object_id(db, lib, "QZX", "src-lib"),
            widget=_object_id(db, lib, "Widget", "src-lib"),
            kh=_object_id(db, lib, HIDDEN_NAME, "src-kh"),
            member_shared=_object_id(db, nb, "Shared", "src-mm"),
        )
        db.execute("UPDATE concept_clusters SET canonical_name=%s "
                   "WHERE notebook_id=%s AND canonical_id=%s", (HIDDEN_NAME, lib, SEEDED_ID))
    cmap = repo.cluster_map(lib)
    # Precondition: the cluster id was minted from the HIDDEN member's name.
    assert cmap[ids.kh] == SEEDED_ID and cmap[ids.qzx] == SEEDED_ID, cmap
    ids.widget_c = cmap[ids.widget]
    repo.replace_notebook_bases(nb, [lib], owner.id)
    return ids


def neighbours(repo, w, user, object_id):
    return as_user(user, repo.kg_neighbors, w.nb, object_id, source_notebook_id=w.lib)


def detail(repo, w, user, concept_id, **kwargs):
    return as_user(user, repo.concept_detail, w.nb, concept_id,
                   source_notebook_id=w.lib, **kwargs)


def text_of(answer) -> str:
    return json.dumps(answer, ensure_ascii=False, default=str)


def assert_answered_by_members(repo, w):
    members = {w.qzx, w.widget}
    returned: set = set()
    for focus in (w.widget, w.qzx):
        view = neighbours(repo, w, w.member, focus)
        assert_names_no_hidden_member(text_of(view), f"neighbours of {focus}")
        assert view["focus_id"] == focus and view["focus_object_id"] == focus, view
        node_ids = {n["id"] for n in view["nodes"]}
        assert node_ids == members, view
        assert {n["payload"]["name"] for n in view["nodes"]} == {"QZX", "Widget"}, view
        assert {(e["source_object_id"], e["target_object_id"]) for e in view["edges"]} == {
            (w.widget, w.qzx)}, view
        returned |= node_ids
    page = detail(repo, w, w.member, w.qzx)
    assert_names_no_hidden_member(text_of(page), "detail of QZX")
    assert page["canonical_id"] == w.qzx and page["canonical_name"] == "QZX", page
    assert [m["id"] for m in page["members"]] == [w.qzx] and page["member_total"] == 1
    for object_id in returned:
        again = detail(repo, w, w.member, object_id)
        assert again["canonical_id"] == object_id
        assert_names_no_hidden_member(text_of(again), f"detail of {object_id}")


@pytest.mark.parametrize("path", ["preview", "live"])
def test_pg_a_non_member_reads_a_mounted_library_by_member_ids_only(repo, monkeypatch, path):
    w = build_world(repo)
    serve_from(repo, monkeypatch, path, w.lib)
    assert_answered_by_members(repo, w)


def test_pg_the_librarys_member_reads_cluster_ids_through_the_same_mount(repo):
    w = build_world(repo)
    view = neighbours(repo, w, w.owner, w.widget)
    assert {n["id"] for n in view["nodes"]} == {SEEDED_ID, w.widget_c}
    page = detail(repo, w, w.owner, SEEDED_ID)
    assert page["canonical_id"] == SEEDED_ID and page["canonical_name"] == HIDDEN_NAME
    assert {m["id"] for m in page["members"]} == {w.qzx, w.kh}


@pytest.mark.parametrize("path", ["preview", "live"])
def test_pg_a_cluster_id_given_as_input_answers_like_an_unknown_id(repo, monkeypatch, path):
    w = build_world(repo)
    serve_from(repo, monkeypatch, path, w.lib)
    with pytest.raises(KeyError):
        detail(repo, w, w.member, NEVER_ID)
    unknown = text_of(neighbours(repo, w, w.member, NEVER_ID))
    assert unknown == text_of(neighbours(repo, w, w.owner, NEVER_ID))
    for cluster_id in (SEEDED_ID, w.widget_c):
        with pytest.raises(KeyError):
            detail(repo, w, w.member, cluster_id)
        view = text_of(neighbours(repo, w, w.member, cluster_id))
        assert view.replace(cluster_id, "<id>") == unknown.replace(NEVER_ID, "<id>"), view


@pytest.mark.parametrize("large", [False, True])
def test_pg_a_hidden_focus_answers_like_an_unknown_id(repo, monkeypatch, large):
    w = build_world(repo)
    serve_from(repo, monkeypatch, "preview", w.lib)
    if large:
        monkeypatch.setattr(repo._runtime.knowledge_lifecycle.settings,
                            "viz_sync_build_max_objects", 1)
    unknown_id = "ko-neverexisted"
    unknown = text_of(neighbours(repo, w, w.member, unknown_id))
    assert unknown == text_of(neighbours(repo, w, w.owner, unknown_id))
    assert ("locating_unavailable" in unknown) is large, unknown
    hidden = text_of(neighbours(repo, w, w.member, w.kh))
    assert hidden.replace(w.kh, "<id>") == unknown.replace(unknown_id, "<id>"), hidden
    # The shared notebook's owner may not read the member's Memory concept.
    own = text_of(as_user(w.owner, repo.kg_neighbors, w.nb, w.member_shared))
    own_unknown = text_of(as_user(w.owner, repo.kg_neighbors, w.nb, unknown_id))
    assert own.replace(w.member_shared, "<id>") == own_unknown.replace(
        unknown_id, "<id>"), own
    assert "Membernote" in text_of(as_user(w.member, repo.kg_neighbors, w.nb, w.member_shared))


@pytest.mark.parametrize("hint", ["library", "omitted"])
def test_pg_a_refused_focus_answers_like_an_unknown_id_before_the_neighbour_read(
    repo, monkeypatch, hint,
):
    """The route passes the active notebook when the client gives no hint:
    the hidden Knowhow object (found in the library) and, with the hint, the
    cluster-id input answer and echo exactly what an unknown id does, and no
    neighbour read is made for them."""
    w = build_world(repo)
    source = w.lib if hint == "library" else w.nb
    lifecycle = repo._runtime.knowledge_lifecycle

    def read(object_id):
        return text_of(as_user(w.member, repo.kg_neighbors, w.nb, object_id,
                               source_notebook_id=source))

    unknown = read(NEVER_ID)
    assert json.loads(unknown)["source_notebook_id"] == source
    reads: list = []
    unchecked = lifecycle._kg_neighbors_unchecked
    monkeypatch.setattr(lifecycle, "_kg_neighbors_unchecked",
                        lambda *a, **k: reads.append(a[1]) or unchecked(*a, **k))
    for object_id in (w.kh, SEEDED_ID) if hint == "library" else (w.kh,):
        view = read(object_id)
        assert view.replace(object_id, "<id>") == unknown.replace(NEVER_ID, "<id>"), view
    assert reads == [], reads


@pytest.mark.parametrize("path", ["preview", "live"])
@pytest.mark.parametrize("drop_source", [False, True])
def test_pg_stale_names_left_by_a_pruned_knowhow_member_stay_out(
    repo, monkeypatch, drop_source, path,
):
    w = build_world(repo)
    if path == "preview":
        serve_from(repo, monkeypatch, path, w.lib)
    drop_knowhow_member(repo, w.lib, drop_source=drop_source)
    if path == "live":
        serve_from(repo, monkeypatch, path, w.lib)
    cmap = repo.cluster_map(w.lib)
    assert cmap.get(w.qzx) == SEEDED_ID and w.kh not in cmap, cmap
    with repo._runtime.database.connect() as db:
        stored = db.execute("SELECT canonical_name FROM concept_clusters WHERE "
                            "notebook_id=%s AND canonical_id=%s", (w.lib, SEEDED_ID)).fetchall()
    assert {row["canonical_name"] for row in stored} == {HIDDEN_NAME}
    assert bool(repo._runtime.source_store.hidden_source_ids(w.lib, "")) is not drop_source
    assert_answered_by_members(repo, w)
