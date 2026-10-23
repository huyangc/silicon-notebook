"""A mounted library read by a reader who is not its member never names a
merged knowledge object by its cluster id or stored name (2026-10-23 ruling,
plan ``2026-10-23-kg-mount-read-merged-object-names_zh.md`` R2), and a hidden
focus answers exactly what an unknown id does -- through the REAL routes and
auth dependency.

A canonical id is ``K-`` + the smallest seed of the merged group and the
stored ``canonical_name`` is one member's spelling: both can come from a
member the reader may not see.  Here the public library ``lib`` holds the
visible concepts ``QZX`` and ``Widget`` (``Widget`` -> ``QZX``) and, in a
Knowhow projection, ``Quiet Zebra Xylophone (QZX)``, which merges with
``QZX`` through its acronym and mints the cluster id.  The owner mounts
``lib`` into the shared notebook; the member of the shared notebook reads it
through the mount without being a member of ``lib``.

PostgreSQL twin (service level):
tests/postgres/test_kg_mount_read_member_answers_pg.py.
"""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from tests.pre_isolation_graph import build_pre_isolation
from tests.test_kg_viewer_scope import _ev, _memory, _object_id, _source

#: Every spelling of the hidden member a response could carry: its name, the
#: seed its name mints, and a word of it.
HIDDEN_NEEDLES = ("quiet zebra xylophone", "quietzebraxylophone", "zebra")
HIDDEN_NAME = "Quiet Zebra Xylophone (QZX)"
SEEDED_ID = "K-quietzebraxylophone"
NEVER_ID = "K-neverexisted"


def assert_names_no_hidden_member(text: str, where: str) -> None:
    """The WHOLE response text, case-insensitively: no spelling of the hidden
    member (its name, its seed, or the id minted from it)."""
    lowered = text.lower()
    for needle in HIDDEN_NEEDLES:
        assert needle not in lowered, (where, needle, text)


@pytest.fixture
def env(monkeypatch, tmp_path):
    """Two registered users over HTTP (``auth_optional=false``): ``owner``
    owns the shared notebook (and, later, the library); ``member`` is a member
    of the shared notebook only, and owns a Memory there whose ``Shared``
    concept sits in one cluster with the visible ``Shared`` (the graph as it
    was built before the Memory isolation)."""
    from fastapi.testclient import TestClient

    from app.api.deps import repository
    from app.main import create_app

    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'mount_names.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    client = TestClient(create_app())

    def register(username):
        body = client.post(
            "/api/auth/register", json={"username": username, "password": "pw"}
        ).json()
        return body["user"]["id"], {"Authorization": f"Bearer {body['token']}"}

    owner_id, owner = register("a00923001")
    member_id, member = register("a00923002")
    nb = client.post("/api/notebooks", headers=owner, json={"name": "shared"}).json()["id"]
    repo = repository()
    repo._runtime.sharing.add_member(nb, member_id)
    with repo._write() as db:
        _source(db, nb, "src-s", elements=[("el-s", "VISIBLE text")])
        _memory(db, nb, "mem-m", member_id)
        _source(db, nb, "src-mm", memory_id="mem-m", elements=[("el-mm", "M-PRIVATE")])
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
    with repo._write() as db:
        member_shared = _object_id(db, nb, "Shared", "src-mm")
        visible_shared = _object_id(db, nb, "Shared", "src-s")
        membernote = _object_id(db, nb, "Membernote", "src-mm")
    cmap = repo.cluster_map(nb)
    assert cmap[member_shared] == cmap[visible_shared], cmap
    return SimpleNamespace(client=client, repo=repo, nb=nb, owner=owner,
                           owner_id=owner_id, member=member, member_id=member_id,
                           member_shared=member_shared,
                           # a cluster whose only member is the member's Memory
                           membernote_c=cmap[membernote])


def build_library(env):
    """The owner's public library, mounted into the shared notebook (module
    docstring).  The stored cluster name is set to the hidden spelling, the
    one a merge may pick (the name of the seed most members share).  Returns
    the library and its object and cluster ids."""
    c, repo = env.client, env.repo
    lib = c.post("/api/notebooks", headers=env.owner, json={"name": "lib"}).json()["id"]
    repo.mark_notebook_base(lib)
    with repo._write() as db:
        _source(db, lib, "src-lib", elements=[("el-lib", "LIB visible")])
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,parse_status,"
            "file_name,file_path,file_size,file_hash,summary,doc_type,created_at,"
            "updated_at) VALUES ('src-kh',?,'kh','knowhow','extracted','parsed','',"
            "'',0,'','','','2026-09-01T00:00:00','2026-09-01T00:00:00')", (lib,),
        )
        db.execute(
            "INSERT INTO source_elements (id,source_id,element_type,location_label,"
            "text,metadata,created_at) VALUES ('el-kh','src-kh','paragraph','p',"
            "'KNOWHOW row text','{}','2026-09-01T00:00:00')",
        )
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
    with repo._write() as db:
        ids = SimpleNamespace(
            lib=lib,
            qzx=_object_id(db, lib, "QZX", "src-lib"),
            widget=_object_id(db, lib, "Widget", "src-lib"),
            kh=_object_id(db, lib, HIDDEN_NAME, "src-kh"),
        )
    cmap = repo.cluster_map(lib)
    # Precondition: the cluster id was minted from the HIDDEN member's name,
    # and the visible QZX is in that cluster.
    assert cmap[ids.kh] == SEEDED_ID and cmap[ids.qzx] == SEEDED_ID, cmap
    ids.widget_c = cmap[ids.widget]
    with repo._write() as db:
        db.execute("UPDATE concept_clusters SET canonical_name=? "
                   "WHERE notebook_id=? AND canonical_id=?", (HIDDEN_NAME, lib, SEEDED_ID))
    mounted = c.put(f"/api/notebooks/{env.nb}/bases", headers=env.owner,
                    json={"base_notebook_ids": [lib]})
    assert mounted.status_code == 200, mounted.text
    return ids


def neighbours(env, headers, object_id, lib):
    return env.client.get(
        f"/api/notebooks/{env.nb}/objects/{object_id}/neighbors",
        headers=headers, params={"source_notebook_id": lib})


def detail(env, headers, concept_id, lib, **params):
    return env.client.get(
        f"/api/notebooks/{env.nb}/concepts/{concept_id}/detail",
        headers=headers, params={"source_notebook_id": lib, **params})


def assert_answered_by_members(env, ids):
    """The non-member's neighbour and concept-detail reads of ``lib``: no
    spelling of the hidden member anywhere in the response, every id a
    visible member's object id, and every returned id reopens its detail."""
    members = {ids.qzx, ids.widget}
    returned: set = set()
    for focus in (ids.widget, ids.qzx):
        r = neighbours(env, env.member, focus, ids.lib)
        assert r.status_code == 200, r.text
        assert_names_no_hidden_member(r.text, f"neighbours of {focus}")
        body = r.json()
        assert body["focus_id"] == focus and body["focus_object_id"] == focus, body
        node_ids = {n["id"] for n in body["nodes"]}
        assert node_ids == members, body
        assert {n["payload"]["name"] for n in body["nodes"]} == {"QZX", "Widget"}, body
        assert {(e["source_object_id"], e["target_object_id"]) for e in body["edges"]} == {
            (ids.widget, ids.qzx)}, body
        returned |= node_ids | {body["focus_id"]}
    r = detail(env, env.member, ids.qzx, ids.lib)
    assert r.status_code == 200, r.text
    assert_names_no_hidden_member(r.text, "detail of QZX")
    body = r.json()
    assert body["canonical_id"] == ids.qzx and body["canonical_name"] == "QZX", body
    assert [m["id"] for m in body["members"]] == [ids.qzx] and body["member_total"] == 1
    returned.add(body["canonical_id"])
    for object_id in returned:
        again = detail(env, env.member, object_id, ids.lib)
        assert again.status_code == 200, (object_id, again.text)
        assert again.json()["canonical_id"] == object_id
        assert_names_no_hidden_member(again.text, f"detail of {object_id}")


def serve_from(repo, monkeypatch, path: str, lib: str) -> None:
    """``preview``: the neighbour view answers from the library's persisted
    preview (a small library builds one on first read; built here so a later
    change leaves it stale, and a stale one is not refreshed behind the
    test's back).  ``live``: from the live tables -- the path a library
    awaiting its isolated rebuild takes (no persisted artifact is read)."""
    scale = repo._runtime.scale_artifacts
    if path == "preview":
        assert scale.build_viz(lib) is not None
        monkeypatch.setattr(scale, "_spawn_viz_build", lambda _nb: None)
        return
    monkeypatch.setattr(repo._runtime.knowledge_lifecycle, "_isolation_pending",
                        lambda _db, notebook_id: notebook_id == lib)
    viz_index = scale.viz_index

    def no_preview(notebook_id, *args, **kwargs):
        assert notebook_id != lib, "the live path reads no persisted preview"
        return viz_index(notebook_id, *args, **kwargs)

    monkeypatch.setattr(scale, "viz_index", no_preview)


@pytest.mark.parametrize("path", ["preview", "live"])
def test_a_non_member_reads_a_mounted_library_by_member_ids_only(env, monkeypatch, path):
    """R2: neighbours (focused on the visible member and on the object next
    to it) and concept detail (opened by the visible member's id), from the
    persisted preview and from the live tables."""
    ids = build_library(env)
    serve_from(env.repo, monkeypatch, path, ids.lib)
    assert_answered_by_members(env, ids)


def test_the_librarys_member_reads_cluster_ids_through_the_same_mount(env):
    """The owner is a member of ``lib``: the Knowhow member is theirs to read,
    so the cluster id and the stored name are answered as they are."""
    ids = build_library(env)
    r = neighbours(env, env.owner, ids.widget, ids.lib)
    assert r.status_code == 200
    assert {n["id"] for n in r.json()["nodes"]} == {SEEDED_ID, ids.widget_c}
    assert r.json()["focus_id"] == ids.widget_c
    r = detail(env, env.owner, SEEDED_ID, ids.lib)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["canonical_id"] == SEEDED_ID and body["canonical_name"] == HIDDEN_NAME
    assert {m["id"] for m in body["members"]} == {ids.qzx, ids.kh}


@pytest.mark.parametrize("path", ["preview", "live"])
def test_a_cluster_id_given_as_input_answers_like_an_unknown_id(env, monkeypatch, path):
    """A cluster id is an existence oracle for a non-member: guessing the
    hidden spelling's ``K-<seed>`` must answer exactly what an id that never
    existed does -- concept detail 404, the neighbour view the unknown id's
    empty neighbourhood (compared with the id strings swapped).  The same for
    the cluster of visible members only."""
    ids = build_library(env)
    serve_from(env.repo, monkeypatch, path, ids.lib)
    unknown = detail(env, env.member, NEVER_ID, ids.lib)
    assert unknown.status_code == 404, unknown.text
    unknown_view = neighbours(env, env.member, NEVER_ID, ids.lib)
    assert unknown_view.status_code == 200
    # The non-member's unknown-id answer is the one the notebook's own
    # unchecked path gives (the library's member, through the same mount).
    assert unknown_view.text == neighbours(env, env.owner, NEVER_ID, ids.lib).text
    for cluster_id in (SEEDED_ID, ids.widget_c):
        r = detail(env, env.member, cluster_id, ids.lib)
        assert (r.status_code, r.text) == (404, unknown.text), (cluster_id, r.text)
        r = neighbours(env, env.member, cluster_id, ids.lib)
        assert r.status_code == 200
        assert r.text.replace(cluster_id, "<id>") == unknown_view.text.replace(
            NEVER_ID, "<id>"), (cluster_id, r.text)


@pytest.mark.parametrize("path", ["preview", "live"])
@pytest.mark.parametrize("large", [False, True])
def test_a_hidden_focus_answers_like_an_unknown_id(env, monkeypatch, large, path):
    """The Knowhow object the non-member may not read, as the focus: the
    response is the unknown id's (no ``focus_id`` naming its cluster, no
    ``focus_object_id`` differing from it) -- on a small library, and on one
    too large for the live path (``locating_unavailable``, with
    ``preview_lacks_focus`` when the preview exists but holds neither id).
    The same rule for a reader of the shared notebook whose focus is another
    member's Memory object."""
    ids = build_library(env)
    serve_from(env.repo, monkeypatch, path, ids.lib)
    if large:
        monkeypatch.setattr(env.repo._runtime.knowledge_lifecycle.settings,
                            "viz_sync_build_max_objects", 1)
    unknown_id = "ko-neverexisted"
    unknown = neighbours(env, env.member, unknown_id, ids.lib)
    assert unknown.status_code == 200
    assert unknown.text == neighbours(env, env.owner, unknown_id, ids.lib).text
    assert ("locating_unavailable" in unknown.json()) is large, unknown.text
    assert ("preview_lacks_focus" in unknown.json()) is (large and path == "preview")
    hidden = neighbours(env, env.member, ids.kh, ids.lib)
    assert hidden.status_code == 200
    assert hidden.text.replace(ids.kh, "<id>") == unknown.text.replace(
        unknown_id, "<id>"), hidden.text
    assert_names_no_hidden_member(hidden.text, "hidden focus")
    # A notebook's own reader: the owner may not read the member's Memory
    # concept, whose cluster they do see -- its focus is not that cluster.
    own = env.client.get(
        f"/api/notebooks/{env.nb}/objects/{env.member_shared}/neighbors", headers=env.owner)
    own_unknown = env.client.get(
        f"/api/notebooks/{env.nb}/objects/{unknown_id}/neighbors", headers=env.owner)
    assert own.status_code == own_unknown.status_code == 200
    assert own.text.replace(env.member_shared, "<id>") == own_unknown.text.replace(
        unknown_id, "<id>"), own.text
    assert "Membernote" in env.client.get(
        f"/api/notebooks/{env.nb}/objects/{env.member_shared}/neighbors",
        headers=env.member).text


@pytest.mark.parametrize("preview", ["absent", "stale"])
def test_a_refused_neighbour_read_reports_a_refused_preview_build_once(
    env, monkeypatch, preview,
):
    """On a library too large for an in-process preview build, every
    neighbour read reports the refused build (``viz_lazy_build_refused``)
    exactly once, as an unknown id's read does: the non-member's unknown id,
    cluster-id input and hidden focus, and -- answered as an unknown id only
    after the neighbour read -- a notebook reader's cluster id whose every
    member is hidden from them.  ``preview``: none at all (trigger
    ``absent``), or a stale one (``stale_refresh``)."""
    ids = build_library(env)
    repo = env.repo
    scale = repo._runtime.scale_artifacts
    if preview == "absent":
        # Whatever an earlier read or rebuild tail published goes.
        for notebook_id in (ids.lib, env.nb):
            scale.retire_viz(notebook_id)
            scale.viz_cache.pop(notebook_id, None)
            assert scale.artifacts.load_viz(notebook_id) is None
    if preview == "stale":
        monkeypatch.setattr(scale, "_spawn_viz_build", lambda _nb: None)
        for notebook_id, source_id in ((ids.lib, "src-late-l"), (env.nb, "src-late-s")):
            assert scale.build_viz(notebook_id) is not None
            with repo._write() as db:
                _source(db, notebook_id, source_id, elements=[(f"el-{source_id}", "late")])
            repo.store_kg(notebook_id, source_id, [
                {"local_id": "z", "object_type": "concept",
                 "payload": {"name": "Latecomer", "section_path": "1"},
                 "evidence": [_ev(source_id, f"el-{source_id}")]}], [])
    for settings in {id(s): s for s in (repo._runtime.knowledge_lifecycle.settings,
                                        scale.settings)}.values():
        monkeypatch.setattr(settings, "viz_sync_build_max_objects", 1)
    trigger = {"absent": "absent", "stale": "stale_refresh"}[preview]
    events: list = []
    emit = scale._emit_viz_lazy_build_refused
    monkeypatch.setattr(
        scale, "_emit_viz_lazy_build_refused",
        lambda nb, objects, why: events.append((nb, why)) or emit(nb, objects, why))
    # The neighbour read's object count (``locating_unavailable``'s gate).
    knowledge = repo._runtime.knowledge_lifecycle.knowledge
    counts: list = []
    count = knowledge.count_active_objects
    monkeypatch.setattr(knowledge, "count_active_objects",
                        lambda db, nb, **k: counts.append(nb) or count(db, nb, **k))

    def refusals(url_notebook, headers, object_id, lib=None):
        events.clear()
        counts.clear()
        params = {"source_notebook_id": lib} if lib else {}
        r = env.client.get(
            f"/api/notebooks/{url_notebook}/objects/{object_id}/neighbors",
            headers=headers, params=params)
        assert r.status_code == 200 and r.json().get("locating_unavailable") is True, r.text
        return list(events), list(counts)

    # The references: an unknown id read by the library's member, and by
    # the shared notebook's owner -- one refusal, and the count read once.
    once_lib = refusals(env.nb, env.owner, NEVER_ID, ids.lib)
    once_nb = refusals(env.nb, env.owner, "K-neverexisted-here")
    assert once_lib[0] == [(ids.lib, trigger)] and once_lib[1].count(ids.lib) == 1
    assert once_nb[0] == [(env.nb, trigger)] and once_nb[1].count(env.nb) == 1
    for object_id in (NEVER_ID, SEEDED_ID, ids.kh):
        assert refusals(env.nb, env.member, object_id, ids.lib) == once_lib, object_id
    assert refusals(env.nb, env.owner, env.membernote_c) == once_nb


@pytest.mark.parametrize("path", ["preview", "live"])
def test_a_refused_focus_is_decided_before_the_neighbour_read(env, monkeypatch, path):
    """A focus the scope refuses outright is answered before any neighbour,
    preview or cluster read is made for it -- the non-member's cluster-id
    input, hidden Knowhow object and unknown id, and the shared notebook's
    owner asking about the member's Memory concept -- with the same bytes
    as before (the unknown id's answer, ids swapped); a visible focus still
    reads its neighbourhood."""
    ids = build_library(env)
    serve_from(env.repo, monkeypatch, path, ids.lib)
    lifecycle = env.repo._runtime.knowledge_lifecycle
    unknown_lib = neighbours(env, env.owner, NEVER_ID, ids.lib).text
    unknown_nb = env.client.get(
        f"/api/notebooks/{env.nb}/objects/{NEVER_ID}/neighbors", headers=env.owner).text
    reads: list = []
    unchecked = lifecycle._kg_neighbors_unchecked
    monkeypatch.setattr(lifecycle, "_kg_neighbors_unchecked",
                        lambda *a, **k: reads.append(a[1]) or unchecked(*a, **k))
    for object_id in (SEEDED_ID, ids.widget_c, ids.kh, NEVER_ID):
        r = neighbours(env, env.member, object_id, ids.lib)
        assert r.text.replace(object_id, "<id>") == unknown_lib.replace(NEVER_ID, "<id>")
    own = env.client.get(
        f"/api/notebooks/{env.nb}/objects/{env.member_shared}/neighbors", headers=env.owner)
    assert own.text.replace(env.member_shared, "<id>") == unknown_nb.replace(NEVER_ID, "<id>")
    assert reads == [], reads
    assert neighbours(env, env.member, ids.widget, ids.lib).json()["focus_id"] == ids.widget
    assert reads == [ids.widget], reads


def drop_knowhow_member(repo, lib, *, drop_source: bool):
    """What a Knowhow re-projection that drops the object (or a table delete,
    ``drop_source``) does: the dropped object's cluster member rows go, the
    surviving rows keep the cluster's old id and name until the next merge.
    Both backends (store calls of ``KnowhowProjection``'s teardown)."""
    knowledge, sources = repo._runtime.knowledge, repo._runtime.source_store
    with repo._runtime.database.write() as db:
        knowledge.prune_cluster_rows_for_source(db, lib, "src-kh")
        knowledge.delete_relations_by_source(db, "src-kh")
        knowledge.delete_objects_by_source(db, "src-kh")
        if drop_source:
            sources.replace_elements(db, "src-kh", [], created_at="2026-09-02T00:00:00")
            sources.delete_source_row(db, "src-kh")
        repo._mark_unified_kg_dirty_in_tx(db, lib)
    repo._invalidate_unified_cache(lib)


@pytest.mark.parametrize("path", ["preview", "live"])
@pytest.mark.parametrize("drop_source", [False, True])
def test_stale_names_left_by_a_pruned_knowhow_member_stay_out(
    env, monkeypatch, drop_source, path,
):
    """The Knowhow member is gone, its old canonical id and stored name are
    not: no live member of the cluster is hidden any more -- with
    ``drop_source`` the library holds no hidden source at all -- and the
    non-member still reads member ids and names only, from the preview built
    before the prune (now stale) and from the live tables."""
    ids = build_library(env)
    if path == "preview":
        serve_from(env.repo, monkeypatch, path, ids.lib)
    drop_knowhow_member(env.repo, ids.lib, drop_source=drop_source)
    if path == "live":
        serve_from(env.repo, monkeypatch, path, ids.lib)
    repo = env.repo
    cmap = repo.cluster_map(ids.lib)
    assert cmap.get(ids.qzx) == SEEDED_ID and ids.kh not in cmap, cmap
    with repo._write() as db:
        stored = db.execute("SELECT canonical_name FROM concept_clusters WHERE "
                            "notebook_id=? AND canonical_id=?", (ids.lib, SEEDED_ID)).fetchall()
    assert {row["canonical_name"] for row in stored} == {HIDDEN_NAME}
    assert bool(repo._runtime.source_store.hidden_source_ids(ids.lib, "")) is not drop_source
    assert_answered_by_members(env, ids)


def test_a_library_without_a_hidden_source_reads_one_member_row_per_cluster(
    env, monkeypatch,
):
    """Cost: with no hidden source left the scope hides nothing -- no store
    reader takes ``viewer_id``, no owned-hidden read, no fold of hidden
    objects to clusters -- and the neighbour view adds the identity read of
    its ids (which are objects, which are clusters: the focus before the
    neighbour read, the others after it) and ONE batched first-member read,
    one row per cluster; concept detail adds no member read at all on its
    first page."""
    ids = build_library(env)
    drop_knowhow_member(env.repo, ids.lib, drop_source=True)
    knowledge = env.repo._runtime.knowledge
    unified_kg = env.repo._runtime.unified_kg
    batched, relinks, relation_kwargs, identities, folds = [], [], [], [], []
    detail_rows = knowledge.concept_cluster_detail_rows
    relink = knowledge.relink_object_rows_for_source
    relations = knowledge.neighbor_relation_rows
    evidence_rows = knowledge.object_evidence_rows
    fold = unified_kg.cluster_fold_rows

    def spy_detail(*args, **kwargs):
        if kwargs.get("canonical_ids") is not None:
            batched.append((sorted(kwargs["canonical_ids"]), kwargs.get("limit")))
        return detail_rows(*args, **kwargs)

    def spy_relations(*args, **kwargs):
        relation_kwargs.append(sorted(kwargs))
        return relations(*args, **kwargs)

    monkeypatch.setattr(knowledge, "concept_cluster_detail_rows", spy_detail)
    monkeypatch.setattr(knowledge, "relink_object_rows_for_source",
                        lambda *a, **k: relinks.append(k) or relink(*a, **k))
    monkeypatch.setattr(knowledge, "neighbor_relation_rows", spy_relations)
    monkeypatch.setattr(knowledge, "object_evidence_rows",
                        lambda *a, **k: identities.append(a[1]) or evidence_rows(*a, **k))
    monkeypatch.setattr(unified_kg, "cluster_fold_rows",
                        lambda *a, **k: folds.append(a[2]) or fold(*a, **k))
    r = neighbours(env, env.member, ids.widget, ids.lib)
    assert r.status_code == 200 and {n["id"] for n in r.json()["nodes"]} == {
        ids.qzx, ids.widget}
    assert batched == [(sorted([SEEDED_ID, ids.widget_c]), 1)], batched
    assert relinks == [], relinks
    assert all("viewer_id" not in kwargs for kwargs in relation_kwargs), relation_kwargs
    # The focus is judged before the neighbour read, the rest after it; no id
    # is read twice.
    assert [sorted(i) for i in identities] == [
        [ids.widget], sorted([SEEDED_ID, ids.widget_c])], identities
    # The focus's fold and the edge-support fold of the response's own ids:
    # nothing hidden is folded to clusters.
    assert [ids.widget] in folds, folds
    assert all(set(f) <= {ids.widget, SEEDED_ID, ids.widget_c} for f in folds), folds
    batched.clear()
    r = detail(env, env.member, ids.qzx, ids.lib)
    assert r.status_code == 200 and r.json()["canonical_id"] == ids.qzx
    assert batched == [] and relinks == [], (batched, relinks)


def test_a_non_members_neighbour_read_pays_no_hidden_member_fold(env, monkeypatch):
    """Cost, library WITH its Knowhow source: the non-member's scope answers
    every cluster by member without asking which clusters may hold a hidden
    member, so no statement folds the hidden-owned objects to clusters (the
    only folds are the focus's and the edge-support fold of the response's
    own ids), and the batched first-member read takes the full window."""
    from app.services.kg_viewer_scope import _FIRST_MEMBER_WINDOW

    ids = build_library(env)
    knowledge = env.repo._runtime.knowledge
    unified_kg = env.repo._runtime.unified_kg
    batched, folds = [], []
    detail_rows = knowledge.concept_cluster_detail_rows
    fold = unified_kg.cluster_fold_rows

    def spy_detail(*args, **kwargs):
        if kwargs.get("canonical_ids") is not None:
            batched.append((sorted(kwargs["canonical_ids"]), kwargs.get("limit")))
        return detail_rows(*args, **kwargs)

    monkeypatch.setattr(knowledge, "concept_cluster_detail_rows", spy_detail)
    monkeypatch.setattr(unified_kg, "cluster_fold_rows",
                        lambda *a, **k: folds.append(list(a[2])) or fold(*a, **k))
    r = neighbours(env, env.member, ids.widget, ids.lib)
    assert r.status_code == 200 and {n["id"] for n in r.json()["nodes"]} == {
        ids.qzx, ids.widget}
    assert batched == [(sorted([SEEDED_ID, ids.widget_c]), _FIRST_MEMBER_WINDOW)], batched
    assert not any(ids.kh in f for f in folds), folds
    assert all(set(f) <= {ids.widget, SEEDED_ID, ids.widget_c} for f in folds), folds


@pytest.mark.parametrize("drop_source", [False, True])
def test_detail_pages_answer_by_the_same_member(env, drop_source):
    """Paging a cluster: every page answers by the first visible member, and
    a later page opened by the member id the first page answered with reads
    the next members of the same cluster -- under the filtering scope, and
    (``drop_source``: no hidden source left, stale id and name) unfiltered."""
    ids = build_library(env)
    repo = env.repo
    with repo._write() as db:
        _source(db, ids.lib, "src-lib2", elements=[("el-lib2", "LIB second")])
    repo.store_kg(ids.lib, "src-lib2", [
        {"local_id": "q2", "object_type": "concept",
         "payload": {"name": "QZX", "section_path": "2"},
         "evidence": [_ev("src-lib2", "el-lib2")]}], [])
    repo.rebuild_unified_kg(ids.lib)
    cmap = repo.cluster_map(ids.lib)
    cluster = cmap[ids.qzx]
    members = sorted(m for m, c in cmap.items() if c == cluster and m != ids.kh)
    assert len(members) == 2, cmap
    with repo._write() as db:
        db.execute("UPDATE concept_clusters SET canonical_name=? "
                   "WHERE notebook_id=? AND canonical_id=?", (HIDDEN_NAME, ids.lib, cluster))
    if drop_source:
        drop_knowhow_member(repo, ids.lib, drop_source=True)
        assert repo.cluster_map(ids.lib)[members[0]] == cluster
    first = detail(env, env.member, members[1], ids.lib, limit=1)
    assert first.status_code == 200, first.text
    page = first.json()
    assert page["canonical_id"] == members[0] and page["canonical_name"] == "QZX"
    assert [m["id"] for m in page["members"]] == [members[0]] and page["next_cursor"]
    second = detail(env, env.member, page["canonical_id"], ids.lib, limit=1,
                    after=page["next_cursor"])
    assert second.status_code == 200, second.text
    later = second.json()
    assert later["canonical_id"] == members[0] and later["canonical_name"] == "QZX"
    assert [m["id"] for m in later["members"]] == [members[1]]
    for response in (first, second):
        assert_names_no_hidden_member(response.text, "paged detail")
        assert cluster not in response.text
    assert json.loads(second.text)["next_cursor"] is None
