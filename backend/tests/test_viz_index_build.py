"""build_viz_index:lite 折叠等价 _unified_graph_full('object') + 落盘 + 空图 None。"""
import pytest
from app.core.config import Settings
from app.services.sqlite_repository import SQLiteRepository
from app.services.embedding import FakeEmbedder
from app.models.schemas import NotebookCreate
from tests.model_testkit import bind_all_embedding_clients


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path/'t.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "s"))
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("EMBED_DIM", "16")
    r = SQLiteRepository(Settings())
    bind_all_embedding_clients(r, FakeEmbedder(dim=16))
    return r


def _star(repo):
    nb = repo.create_notebook(NotebookCreate(name="nb"))
    repo.store_kg(nb.id, None, [
        {"local_id": "a", "object_type": "concept", "payload": {"name": "MOSFET", "section_path": ""}, "evidence": []},
        {"local_id": "b", "object_type": "concept", "payload": {"name": "gain", "section_path": ""}, "evidence": []},
        {"local_id": "c", "object_type": "concept", "payload": {"name": "bias", "section_path": ""}, "evidence": []},
    ], [
        {"source_local_id": "a", "target_local_id": "b", "edge_type": "relates", "evidence": []},
        {"source_local_id": "a", "target_local_id": "c", "edge_type": "relates", "evidence": []},
    ])
    repo.rebuild_unified_kg(nb.id)
    return nb


def test_lite_graph_equals_full(repo):
    nb = _star(repo)
    full = repo._unified_graph_full(nb.id, "object")
    lite = repo._derive_object_graph_lite(nb.id)
    # 逐字段相等:节点(id/type/name,同序)与边集
    assert [(n["id"], n["object_type"], (n.get("payload") or {}).get("name", "")) for n in lite["nodes"]] == \
           [(n["id"], n["object_type"], (n.get("payload") or {}).get("name", "")) for n in full["nodes"]]
    assert [(e["source_object_id"], e["target_object_id"], e["edge_type"]) for e in lite["edges"]] == \
           [(e["source_object_id"], e["target_object_id"], e["edge_type"]) for e in full["edges"]]


def test_build_viz_index_persists_and_manifest(repo):
    nb = _star(repo)
    manifest = repo.build_viz_index(nb.id)
    assert manifest is not None
    assert manifest["n_viz_nodes"] == 3
    assert manifest["n_viz_edges"] == 2
    assert manifest["version"] == repo._scale_index_version(nb.id)
    # 落在 kg_viz/,不在 kg_index/
    import os
    assert os.path.exists(os.path.join(repo._viz_index_dir(nb.id), "manifest.json"))
    assert not os.path.exists(os.path.join(str(repo.settings.storage_dir), "kg_index", nb.id, "manifest.json"))


def test_build_viz_index_empty_notebook_returns_none(repo):
    nb = repo.create_notebook(NotebookCreate(name="empty"))
    assert repo.build_viz_index(nb.id) is None


# ───────────────────────── M1: a member's Memory never enters a shared viz ────
import json  # noqa: E402
import os  # noqa: E402

from tests.memory_artifact_fixture import (  # noqa: E402
    NOW,
    SECRET_NAMES,
    _file_identities,
    assert_a_memory_confirmed_mid_build_never_publishes,
    assert_a_pre_isolation_index_is_rebuilt_once_its_memory_is_deleted,
    assert_a_re_stamp_never_hides_a_shared_re_extraction,
    assert_a_re_stamp_never_hides_an_online_re_extraction,
    assert_a_re_stamp_never_hides_a_shared_change_of,
    assert_a_re_stamp_republishes_the_source_partition_companion,
    assert_a_pre_isolation_index_is_rebuilt_by_the_next_fold,
    assert_no_artifact_built_before_the_isolation_is_served,
    confirm_a_memory,
    assert_a_memory_confirmed_at_the_object_read_never_reaches_a_published_index,
    assert_an_index_built_before_the_first_memory_recovers_by_fold,
    assert_a_memory_vector_takes_no_synonym_slot,
    assert_a_memory_confirmed_mid_viz_build_never_publishes,
    assert_no_partition_names_a_memory_cluster,
    decoded_artifact_bytes as _decoded_artifact_bytes,
    make_it_look_pre_isolation,
    mix_a_shared_object_into_a_memory_cluster,
    make_the_shared_source_partitionable,
    seed_notebook_without_memory,
    seed_shared_notebook_with_memory,
)


def _edges(idx):
    return sorted(
        (src, dst, kind) for src, dst, kind in idx.viz_edges.rows(idx.viz_ids)
    )


def _artifact_bytes(directory: str) -> bytes:
    blob = b""
    for root, _dirs, files in os.walk(directory):
        for name in sorted(files):
            with open(os.path.join(root, name), "rb") as handle:
                blob += handle.read()
    return blob


def test_viz_artifact_has_no_memory_object_name_or_memory_only_edge(repo):
    seeded = seed_shared_notebook_with_memory(repo)
    nb_id = seeded["notebook_id"]
    ids = seeded["objects"]

    manifest = repo.build_viz_index(nb_id)
    idx = repo._viz_index(nb_id)

    # no Memory object, no Memory-derived name -- in the arrays AND in the files
    assert not set(seeded["memory_object_ids"]) & set(idx.viz_ids)
    assert not set(SECRET_NAMES) & set(idx.viz_names)
    blob = _artifact_bytes(repo._viz_index_dir(nb_id)).lower()
    for secret in SECRET_NAMES:
        assert secret.lower().encode() not in blob  # also covers "K-<seed name>"
    for object_id in seeded["memory_object_ids"]:
        assert object_id.encode() not in blob

    edges = _edges(idx)
    canon = seeded["canonical"]
    a, b, c, x, y = (canon[n] for n in ("MOSFET", "gain", "bias", "left", "right"))
    # supported only by a Memory relation -> gone
    assert (x, y, "depends_on") not in edges
    assert (a, b, "contrasts_with") not in edges
    # supported by a shared relation (alone or together with a Memory one) -> kept once
    assert edges.count((a, b, "depends_on")) == 1
    assert (a, c, "depends_on") in edges
    assert len(edges) == 2

    # headers agree with the filtered content
    assert manifest["n_viz_nodes"] == len(idx.viz_ids) == 5
    assert manifest["n_viz_edges"] == len(idx.viz_edges) == 2
    assert idx.manifest["n_viz_nodes"] == 5 and idx.manifest["n_viz_edges"] == 2


def test_the_derive_reads_no_memory_relation_even_between_visible_nodes(repo):
    """The relation read itself excludes Memory relations: the edge (x,y) joins
    two SHARED, visible objects, so dropping it cannot come from a missing node."""
    seeded = seed_shared_notebook_with_memory(repo)
    ids = seeded["canonical"]
    lite = repo._derive_object_graph_lite(seeded["notebook_id"])
    pairs = {
        (e["source_object_id"], e["target_object_id"], e["edge_type"])
        for e in lite["edges"]
    }
    assert (ids["left"], ids["right"], "depends_on") not in pairs
    with repo._connect() as db:
        rows = repo._runtime.index_projections.active_relation_graph_rows(
            db, seeded["notebook_id"]
        )
    assert len(rows) == 2  # the two shared relations; four Memory ones excluded


def test_a_notebook_without_memory_is_unchanged_by_the_exclusion(repo):
    nb = _star(repo)
    with repo._connect() as db:
        rows = repo._runtime.index_projections.active_relation_graph_rows(db, nb.id)
    assert len(rows) == len(repo.relations_for_notebook(nb.id)) == 2
    lite = repo._derive_object_graph_lite(nb.id)
    full = repo._unified_graph_full(nb.id, "object")
    assert [n["id"] for n in lite["nodes"]] == [n["id"] for n in full["nodes"]]


def _isolation_pair(version) -> list:
    at = version.index("memory_isolation")
    return version[at:at + 2]


def test_every_notebook_carries_the_isolation_marker_in_its_version(repo):
    """Every version list carries a pair no artifact built before the isolation
    carries, so every version-exact check sees such an artifact as stale --
    whatever the notebook holds now: with Memory, after its Memory is gone,
    and never having had any."""
    seeded = seed_shared_notebook_with_memory(repo)
    assert _isolation_pair(repo._scale_index_version(seeded["notebook_id"])) == [
        "memory_isolation", 1
    ]
    with repo._write() as db:
        db.execute("DELETE FROM sources WHERE notebook_id=? AND source_type='memory'",
                   (seeded["notebook_id"],))
    assert _isolation_pair(repo._scale_index_version(seeded["notebook_id"])) == [
        "memory_isolation", 1
    ]
    assert _isolation_pair(repo._scale_index_version(_star(repo).id)) == [
        "memory_isolation", 1
    ]


def test_a_viz_built_before_the_isolation_is_stale_after_it(repo):
    seeded = seed_shared_notebook_with_memory(repo)
    nb_id = seeded["notebook_id"]
    repo.build_viz_index(nb_id)
    assert repo._viz_index_probe(nb_id)["viz_stale"] is False

    # what an artifact built by the previous release looks like
    make_it_look_pre_isolation(repo._viz_index_dir(nb_id))
    repo._viz_idx_cache.pop(nb_id, None)

    assert repo._viz_index_probe(nb_id)["viz_stale"] is True


def test_scale_source_set_and_watermark_leave_out_memory_sources(repo):
    seeded = seed_shared_notebook_with_memory(repo)
    nb_id = seeded["notebook_id"]
    projections = repo._runtime.index_projections
    assert projections.source_ids(nb_id) == ["src-shared"]
    assert projections.memory_source_ids(nb_id) == ["src-memory"]
    manifest = repo.build_scale_index(nb_id)
    idx = repo._scale_index(nb_id, allow_stale=True)
    assert idx.manifest["watermark_sources"] == ["src-shared"]
    assert manifest["n_viz_nodes"] == 5 and manifest["n_viz_edges"] == 2


def test_scale_index_holds_no_memory_object_relation_or_edge(repo):
    seeded = seed_shared_notebook_with_memory(repo)
    nb_id = seeded["notebook_id"]
    memory_objects = set(seeded["memory_object_ids"])
    memory_relations = set(seeded["memory_relation_ids"])
    repo.build_scale_index(nb_id)
    idx = repo._scale_index(nb_id, allow_stale=True)

    assert not memory_objects & set(idx.ann_labels)
    assert not memory_objects & set(idx.node_ids)
    assert not memory_relations & set(idx.relation_ann_labels)
    assert set(seeded["shared_relation_ids"]) <= set(idx.relation_ann_labels)
    assert not memory_objects & set(idx.viz_ids)
    assert not set(SECRET_NAMES) & set(idx.viz_names)
    assert idx.manifest["n_ann"] == len(idx.ann_labels)
    assert idx.manifest["n_nodes"] == len(idx.node_ids)
    assert idx.manifest["n_kg_nodes"] == 5
    assert idx.manifest["n_relation_ann"] == len(idx.relation_ann_labels) == 2
    edges = _edges(idx)
    ids = seeded["canonical"]
    assert (ids["left"], ids["right"], "depends_on") not in edges
    assert (ids["MOSFET"], ids["gain"], "contrasts_with") not in edges


def test_fold_refuses_an_index_that_was_built_over_memory_sources(repo):
    """An index built before the isolation lists the Memory source in its
    watermark and holds its objects. A fold would keep them and stamp the result
    with the current version, so the fold is replaced by a full rebuild."""
    seeded = seed_shared_notebook_with_memory(repo)
    nb_id = seeded["notebook_id"]
    repo.build_scale_index(nb_id)
    manifest_path = os.path.join(
        str(repo.settings.storage_dir), "kg_index", nb_id, "manifest.json"
    )
    make_it_look_pre_isolation(os.path.dirname(manifest_path))
    with open(manifest_path) as handle:
        manifest = json.load(handle)
    manifest["watermark_sources"] = sorted(manifest["watermark_sources"] + ["src-memory"])
    with open(manifest_path, "w") as handle:
        json.dump(manifest, handle)
    repo._scale_idx_cache.pop(nb_id, None)
    before = manifest["build_id"]

    refused = []
    repo._runtime.scale_builder.event_log.emit = (
        lambda event, _emit=repo._runtime.scale_builder.event_log.emit: (
            refused.append(event) if event.get("kind") == "scale_fold_refused" else None,
            _emit(event),
        )[1]
    )
    result = repo.fold_scale_index_delta(nb_id)

    assert [e["reason"] for e in refused] == ["memory_isolation"]
    assert result["build_id"] != before
    assert result["watermark_sources"] == ["src-shared"]


def test_fold_never_turns_a_pre_isolation_index_with_a_deleted_memory_into_an_isolated_one(
    repo,
):
    """The re-review's ``test_fold_launders``: a pre-isolation index holding a
    Memory's names, the Memory deleted since (the notebook now holds none), a
    new shared source folded in. The fold would copy the old viz over and stamp
    it isolated and current; instead the vanished source in its watermark makes
    it rebuild, and no deleted Memory name is ever served."""
    assert_a_pre_isolation_index_is_rebuilt_once_its_memory_is_deleted(repo)


def _transition_weight(idx, ids, left: str, right: str) -> float:
    position = {node: i for i, node in enumerate(idx.node_ids)}
    return float(
        idx.transition[position[ids[left]], position[ids[right]]]
        + idx.transition[position[ids[right]], position[ids[left]]]
    )


def test_scale_graph_has_no_edge_a_memory_relation_supports(repo):
    """The PPR graph the scale index persists is fed by ``graph_rows``: the
    shared objects ``left``/``right`` are joined only by a Memory relation, so
    they must not be adjacent -- while ``gain`` stays adjacent to ``MOSFET``."""
    seeded = seed_shared_notebook_with_memory(repo)
    repo.build_scale_index(seeded["notebook_id"])
    idx = repo._scale_index(seeded["notebook_id"], allow_stale=True)
    ids = seeded["objects"]
    assert _transition_weight(idx, ids, "left", "right") == 0.0
    assert _transition_weight(idx, ids, "MOSFET", "gain") > 0.0


def test_scale_graph_leaves_out_a_cluster_that_holds_a_memory_member(repo):
    """A cluster whose members include a Memory-derived object is dropped as a
    whole: its hub node id is ``cluster:<canonical id>`` and the canonical id is
    minted from a member's name, so filtering the member row alone would still
    leave the private name in the persisted node ids."""
    seeded = seed_shared_notebook_with_memory(repo)
    nb_id, ids = seeded["notebook_id"], seeded["objects"]
    # the stale shape the isolation migration removes: a shared object and a
    # Memory-derived one under one canonical id minted from the Memory name
    mix_a_shared_object_into_a_memory_cluster(repo, seeded)
    repo.build_scale_index(nb_id)
    idx = repo._scale_index(nb_id, allow_stale=True)
    assert "cluster:K-secret-alpha-cluster" not in idx.node_ids
    assert not any("secret" in str(node).lower() for node in idx.node_ids)
    # the ordinary shared clusters keep their hubs
    assert any(str(node).startswith("cluster:K-") for node in idx.node_ids)


def test_gathered_graph_holds_no_membership_of_a_memory_object(repo):
    """``graph_rows`` also feeds online graph builds; the evidence map and the
    embedding matrix cache it is handed key every object of the notebook, so a
    Memory object whose evidence reaches a shared chunk (and whose vector sits
    close to shared ones) must come back as neither a membership edge, a
    synonym edge, nor an IDF count."""
    seeded = seed_shared_notebook_with_memory(repo)
    nb_id, ids = seeded["notebook_id"], seeded["objects"]
    memory_id, shared_id = ids[SECRET_NAMES[0]], ids["MOSFET"]
    with repo._write() as db:
        db.execute(
            "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
            "element_ids,created_at) VALUES ('chunk-1',?,?,?,?,?,?)",
            (nb_id, "src-shared", "text", "", json.dumps(["el-1"]),
             "2026-09-29T00:00:00"),
        )
        for object_id in (memory_id, shared_id):
            db.execute(
                "UPDATE knowledge_objects SET evidence=? WHERE id=?",
                (json.dumps([{"element_id": "el-1"}]), object_id),
            )
    repo._runtime.scale_artifacts.version_memo.clear()

    _nodes, edges, _chunks, _kg, counts = repo._runtime.scale_builder.gather_graph(
        nb_id
    )
    assert shared_id in counts
    assert memory_id not in counts
    assert all(memory_id not in (a, b) for a, b, _w in edges)
    assert any(shared_id in (a, b) and "chunk-1" in (a, b) for a, b, _w in edges)


class _Recorder:
    def __init__(self, inner):
        self.inner, self.statements = inner, []

    def execute(self, statement, params=()):
        self.statements.append(statement)
        return self.inner.execute(statement, params)


def test_every_object_and_relation_read_excludes_memory_in_its_own_statement(repo):
    """The row reads carry the Memory exclusion in the statement itself, for
    every notebook -- no probe or set read decides it beforehand, because a
    Memory confirmed between such a read and the rows would slip through. For
    a notebook without Memory the result is the unfiltered rows; for one with
    Memory nothing Memory-derived comes back; objects without a source stay."""
    plain = _star(repo)
    seeded = seed_shared_notebook_with_memory(repo)
    projections = repo._runtime.index_projections
    for notebook_id, holds_memory in ((plain.id, False), (seeded["notebook_id"], True)):
        with repo._connect() as db:
            recorder = _Recorder(db)
            objects = list(projections.active_object_graph_rows(recorder, notebook_id))
            relations = projections.active_relation_graph_rows(recorder, notebook_id)
        assert len(recorder.statements) == 2  # nothing read beforehand
        assert all("source_type = 'memory'" in s for s in recorder.statements)
        if holds_memory:
            ids = {row["id"] for row in objects}
            assert ids and not ids & set(seeded["memory_object_ids"])
            assert len(relations) == 2  # the two shared relations only
        else:
            assert len(objects) == 3 and len(relations) == 2  # store_kg(None): no source


def test_whole_notebook_vector_loads_skip_memory_rows(repo):
    """Both whole-notebook vector entry points (the paged ANN feed and the
    one-shot matrix) skip a member's Memory objects and relations; the ids come
    back row-aligned with the matrix."""
    seeded = seed_shared_notebook_with_memory(repo)
    nb_id = seeded["notebook_id"]
    projections = repo._runtime.index_projections
    for table, column, memory_ids in (
        ("knowledge_embeddings", "object_id", set(seeded["memory_object_ids"])),
        ("relation_embeddings", "relation_id", set(seeded["memory_relation_ids"])),
    ):
        matrix_ids, matrix = projections.embedding_matrix(nb_id, table, column)
        paged_ids = [
            vid for page_ids, _m in projections.embedding_pages(nb_id, table, column)
            for vid in page_ids
        ]
        assert matrix_ids == paged_ids
        assert len(matrix_ids) == len(matrix) > 0
        assert not memory_ids & set(matrix_ids)


# ───────────────── artifacts built before the isolation are never served ─────
def _build_the_way_the_previous_release_did(repo, seeded):
    """A standalone viz built while the notebook's Memory source was an ordinary
    source: it lists the Memory objects and carries neither the isolation field
    nor the isolation pair in its version -- exactly what is on disk after an
    upgrade. (No manual version-memo reset: the Memory probe is part of the
    version signal.)"""
    nb_id = seeded["notebook_id"]
    with repo._write() as db:
        db.execute("UPDATE sources SET source_type='md' WHERE id=?", ("src-memory",))
    repo.build_viz_index(nb_id)
    make_it_look_pre_isolation(repo._viz_index_dir(nb_id))
    with repo._write() as db:
        db.execute("UPDATE sources SET source_type='memory' WHERE id=?", ("src-memory",))
    repo._viz_idx_cache.pop(nb_id, None)
    old = repo._runtime.scale_artifacts.artifacts.load_viz(nb_id)
    assert set(SECRET_NAMES) <= set(old.viz_names)  # the fixture really is contaminated
    assert "memory_isolation" not in old.manifest


def test_a_large_notebook_does_not_serve_a_viz_built_before_the_isolation(repo, monkeypatch):
    """The refresh of a stale standalone viz is refused above the size budget and
    the stale folding keeps serving -- which after the upgrade would be a member's
    Memory in a shared view. A pre-isolation artifact is served as absent."""
    seeded = seed_shared_notebook_with_memory(repo)
    nb_id = seeded["notebook_id"]
    _build_the_way_the_previous_release_did(repo, seeded)
    monkeypatch.setattr(repo.settings, "viz_sync_build_max_objects", 0)

    assert repo._viz_index(nb_id) is None
    probe = repo._viz_index_probe(nb_id)
    assert (probe["viz_indexed"], probe["viz_nodes"], probe["viz_edges"]) == (False, 0, 0)
    graph = repo.unified_graph(nb_id, "object", 10)
    assert graph["nodes"] == [] and graph["viz_unavailable"] is True
    assert not any(secret in repr(repo.kg_neighbors(nb_id, seeded["canonical"]["MOSFET"]))
                   for secret in SECRET_NAMES)


def test_a_small_notebook_rebuilds_a_viz_built_before_the_isolation_on_the_spot(repo):
    seeded = seed_shared_notebook_with_memory(repo)
    nb_id = seeded["notebook_id"]
    _build_the_way_the_previous_release_did(repo, seeded)

    idx = repo._viz_index(nb_id)
    assert idx is not None
    assert not set(SECRET_NAMES) & set(idx.viz_names)
    assert not set(seeded["memory_object_ids"]) & set(idx.viz_ids)
    assert idx.manifest["memory_isolation"] == 1
    assert "memory_isolation" in idx.manifest["version"]
    assert repo._viz_index_probe(nb_id)["viz_stale"] is False


def test_a_refused_pre_isolation_viz_is_never_materialised(repo, monkeypatch):
    """The refusal reads the small manifest, not the artifact: a large notebook
    whose viz predates the isolation answers every read without loading the
    arrays (which the stale-slot cache exists to avoid)."""
    seeded = seed_shared_notebook_with_memory(repo)
    nb_id = seeded["notebook_id"]
    _build_the_way_the_previous_release_did(repo, seeded)
    monkeypatch.setattr(repo.settings, "viz_sync_build_max_objects", 0)
    store = repo._runtime.scale_artifacts.artifacts
    loads: list = []
    real_load = store.load_viz
    monkeypatch.setattr(store, "load_viz", lambda nb: loads.append(nb) or real_load(nb))

    for _ in range(3):
        assert repo._viz_index(nb_id) is None
    assert loads == []


def test_a_warm_pre_isolation_viz_is_dropped_too(repo, monkeypatch):
    """The process cache may still hold the pre-upgrade artifact: the warm-entry
    branch must refuse it as well."""
    seeded = seed_shared_notebook_with_memory(repo)
    nb_id = seeded["notebook_id"]
    _build_the_way_the_previous_release_did(repo, seeded)
    monkeypatch.setattr(repo.settings, "viz_sync_build_max_objects", 0)
    old = repo._runtime.scale_artifacts.artifacts.load_viz(nb_id)
    repo._viz_idx_cache[nb_id] = (old, repo._runtime.scale_artifacts._viz_signature(nb_id))

    assert repo._viz_index(nb_id) is None


def test_the_standalone_viz_roots_are_listed_for_the_pre_isolation_scan(repo):
    """``viz_notebook_ids`` lists every published ``kg_viz`` root -- also of a
    notebook without a scale index, which ``indexed_notebook_ids`` cannot see --
    and leaves build scratch out, so the post-migration pass can find a large
    notebook whose only artifact is a pre-isolation viz."""
    store = repo._runtime.scale_artifacts.artifacts
    nb = _star(repo)
    repo.build_viz_index(nb.id)
    scratch = store.viz_dir("nb-scratch.tmp")
    os.makedirs(scratch)
    with open(os.path.join(str(scratch), "manifest.json"), "w") as handle:
        handle.write("{}")
    assert store.viz_notebook_ids() == [nb.id]
    assert nb.id not in store.indexed_notebook_ids()
    make_it_look_pre_isolation(store.viz_dir(nb.id))
    manifest = store.read_manifest(store.viz_dir(nb.id))
    assert repo._runtime.index_projections.built_before_memory_isolation(manifest)


@pytest.mark.parametrize("tier", ["personal", "base"])
def test_no_notebook_serves_an_artifact_built_before_the_isolation(
    repo, monkeypatch, tier
):
    """Not only a notebook holding Memory now: one that never held any (or
    whose Memory was deleted before the upgrade, which leaves no trace), and a
    public library alike."""
    assert_no_artifact_built_before_the_isolation_is_served(repo, monkeypatch, tier)


def test_viz_does_not_fold_a_shared_object_into_a_cluster_that_holds_memory(repo):
    """The canonical id of a cluster is minted from a member's name, and a viz node
    of a folded concept carries that id. A shared object folded into a cluster
    that holds a Memory member would show the private name as its node id."""
    seeded = seed_shared_notebook_with_memory(repo)
    nb_id, ids = seeded["notebook_id"], seeded["objects"]
    mix_a_shared_object_into_a_memory_cluster(repo, seeded)  # + the shared, cached map

    repo.build_viz_index(nb_id)
    idx = repo._viz_index(nb_id)
    assert not any("secret" in str(v).lower() for v in list(idx.viz_ids) + list(idx.viz_names))
    assert ids["left"] in idx.viz_ids  # the shared object stays, unfolded, under its own id
    assert "MOSFET" in idx.viz_names and "left" in idx.viz_names
    assert _edges(idx).count((seeded["canonical"]["MOSFET"], seeded["canonical"]["gain"],
                              "depends_on")) == 1
    blob = _artifact_bytes(repo._viz_index_dir(nb_id)).lower()
    assert b"secret" not in blob

    # the scale index embeds the same folded graph
    repo.build_scale_index(nb_id)
    embedded = repo._scale_index(nb_id, allow_stale=True)
    assert not any("secret" in str(v).lower() for v in list(embedded.viz_ids) + list(embedded.node_ids))
    # and the cached map every other reader shares was not edited
    assert repo.cluster_map(nb_id)[ids["left"]] == "K-secret-alpha-cluster"


def test_no_source_partition_names_a_cluster_that_holds_memory(repo):
    """The source-partition companion lists, per shared source, the clusters its
    objects belong to; a cluster that holds a Memory member is not among them."""
    assert_no_partition_names_a_memory_cluster(repo, seed_shared_notebook_with_memory(repo))


def test_a_memory_vector_takes_no_synonym_neighbour_slot(repo, monkeypatch):
    assert_a_memory_vector_takes_no_synonym_slot(repo, monkeypatch)


def test_extra_edges_never_touch_a_memory_object(repo):
    """Name-variant, synonym and mention edges are appended after the relations;
    the string-encoded gather (the fold delta and online graph builds) adds an
    edge without looking whether its endpoints are nodes, so an extra edge whose
    endpoint is a Memory object has to be dropped by the store itself."""
    seeded = seed_shared_notebook_with_memory(repo)
    nb_id, ids = seeded["notebook_id"], seeded["objects"]
    memory_id, shared_id, other = ids[SECRET_NAMES[0]], ids["left"], ids["bias"]
    _nodes, edges, _chunks, _kg, _counts = repo._runtime.scale_builder.gather_graph(
        nb_id,
        synonym_edges=[(memory_id, shared_id, 0.99), (shared_id, memory_id, 0.99),
                       (shared_id, other, 0.98)],
    )
    assert all(memory_id not in (a, b) for a, b, _w in edges)
    assert any(a == shared_id and b == other and w == 0.98 for a, b, w in edges)


def test_a_notebook_without_memory_issues_only_the_probe_for_every_artifact_read(repo):
    """Every whole-notebook read a build makes (graph rows, both vector feeds, the
    cluster canonical read) touches the Memory tables with the one-row probe and
    nothing more when the notebook holds no Memory source; one that does holds
    them all."""
    import re
    from contextlib import contextmanager

    plain = _star(repo)
    seeded = seed_shared_notebook_with_memory(repo)
    projections = repo._runtime.index_projections
    original = projections.connect
    seen: list = []

    @contextmanager
    def traced():
        with original() as db:
            db.set_trace_callback(lambda s: seen.append(re.sub(r"\s+", " ", s)))
            yield db

    projections.connect = traced
    try:
        for notebook_id, holds_memory in ((plain.id, False), (seeded["notebook_id"], True)):
            seen.clear()
            projections.graph_rows(notebook_id, None, synonym_edges=[])
            for table, column in (("knowledge_embeddings", "object_id"),
                                  ("relation_embeddings", "relation_id")):
                projections.embedding_matrix(notebook_id, table, column)
                list(projections.embedding_pages(notebook_id, table, column))
            projections.memory_cluster_canonicals(notebook_id)
            projections.source_ids(notebook_id)
            # beyond the probes, the (empty) Memory source-id reads and the
            # in-statement exclusion of the row reads, a notebook without
            # Memory pays for no Memory id or cluster read
            memory_reads = [s for s in seen if "source_type = 'memory'" in s
                            and (s.startswith("SELECT id FROM knowledge_")
                                 or s.startswith("SELECT DISTINCT c.canonical_id"))]
            assert bool(memory_reads) is holds_memory, (holds_memory, memory_reads)
    finally:
        projections.connect = original


def test_status_of_a_scale_index_built_before_the_isolation_reports_no_leaking_counts(repo):
    """Such an index is stale (its version lacks the marker, so the automatic
    rebuild is offered) and its node / ANN counts, which include a member's
    Memory, are not reported; after the rebuild the counts are the filtered ones."""
    seeded = seed_shared_notebook_with_memory(repo)
    nb_id = seeded["notebook_id"]
    with repo._write() as db:
        db.execute("UPDATE sources SET source_type='md' WHERE id=?", ("src-memory",))
    repo.build_scale_index(nb_id)
    store = repo._runtime.scale_artifacts.artifacts
    make_it_look_pre_isolation(store.scale_dir(nb_id))
    contaminated = store.read_manifest(store.scale_dir(nb_id))
    assert contaminated["n_ann"] == 7 and contaminated["n_kg_nodes"] == 7
    with repo._write() as db:
        db.execute("UPDATE sources SET source_type='memory' WHERE id=?", ("src-memory",))
    repo._scale_idx_cache.pop(nb_id, None)

    status = repo.scale_index_status(nb_id)
    assert status["state"] == "stale" and status["stale"] is True
    assert status["n_nodes"] == 0 and status["n_ann"] == 0

    repo.build_scale_index(nb_id)
    status = repo.scale_index_status(nb_id)
    assert status["state"] == "indexed"
    assert status["n_ann"] == 5 and status["n_nodes"] >= 5


def test_no_file_of_any_artifact_root_mentions_a_memory_object_relation_or_name(repo):
    """Every root a build publishes -- the main index (ANN labels, node ids, graph,
    embedded viz), the standalone viz and the source-partition companion -- checked
    as bytes, not through the loaders."""
    seeded = seed_shared_notebook_with_memory(repo)
    nb_id = seeded["notebook_id"]
    make_the_shared_source_partitionable(repo, seeded)
    repo.build_scale_index(nb_id)
    repo.build_viz_index(nb_id)
    store = repo._runtime.scale_artifacts.artifacts
    roots = [str(store.scale_dir(nb_id)), repo._viz_index_dir(nb_id)]
    partition = str(store.source_partition_dir(nb_id))
    # the companion holds a real partition of the shared source (not only its
    # manifest): it is part of the check
    assert seeded["objects"]["left"].encode() in _decoded_artifact_bytes(partition)
    roots.append(partition)
    forbidden = (
        [name.lower().encode() for name in SECRET_NAMES]
        + [i.encode() for i in seeded["memory_object_ids"]]
        + [i.encode() for i in seeded["memory_relation_ids"]]
        + [b"src-memory"]
    )
    for root in roots:
        blob = _decoded_artifact_bytes(root).lower()
        assert len(blob) > 100, root
        for needle in forbidden:
            assert needle not in blob, (root, needle)


def test_the_first_memory_confirmed_during_a_build_never_reaches_a_published_index(
    repo, monkeypatch
):
    """Interleaving B: the notebook's FIRST Memory is confirmed right before the
    viz derive sends its object read."""
    notebook_id = seed_notebook_without_memory(repo)
    repo.build_scale_index(notebook_id)
    assert_a_memory_confirmed_at_the_object_read_never_reaches_a_published_index(
        repo, monkeypatch, notebook_id, vanish=False
    )


def test_a_memory_confirmed_and_deleted_during_a_build_never_reaches_it(
    repo, monkeypatch
):
    """The re-review's ``race2 add_delete``: the first Memory is confirmed right
    before the object read and deleted again right after it, so neither of the
    build's snapshots sees it. Only a read that is exact in its own statement
    keeps its name out of a published, fresh index."""
    notebook_id = seed_notebook_without_memory(repo)
    repo.build_scale_index(notebook_id)
    assert_a_memory_confirmed_at_the_object_read_never_reaches_a_published_index(
        repo, monkeypatch, notebook_id, vanish=True
    )


def test_a_further_memory_confirmed_during_a_build_never_reaches_the_ann_labels(
    repo, monkeypatch
):
    """Interleaving A: the notebook already holds Memory; one more is confirmed
    after the KG ANN feed read its Memory object-id set and before it streamed
    the vectors, so that attempt's ANN labels hold the new Memory object."""
    notebook_id = seed_shared_notebook_with_memory(repo)["notebook_id"]
    repo.build_scale_index(notebook_id)
    assert_a_memory_confirmed_mid_build_never_publishes(
        repo, monkeypatch, notebook_id, "ann_feed"
    )


@pytest.mark.parametrize("window", ["ann_feed", "relation_feed"])
def test_an_existing_memory_re_extracted_during_a_build_never_reaches_the_ann_labels(
    repo, monkeypatch, window
):
    """No new Memory source: an existing one gains an object (KG feed) or a
    relation (relation feed) while the feed streams -- the object / relation
    parts of the build's Memory snapshot, not the source part, catch it."""
    notebook_id = seed_shared_notebook_with_memory(repo)["notebook_id"]
    repo.build_scale_index(notebook_id)
    assert_a_memory_confirmed_mid_build_never_publishes(
        repo, monkeypatch, notebook_id, window, existing_source="src-memory"
    )


@pytest.mark.parametrize("window", ["ann_feed", "relation_feed"])
def test_a_memory_observed_after_a_feed_and_deleted_again_never_reaches_the_ann_labels(
    repo, monkeypatch, window
):
    """A Memory confirmed while an ANN feed streams, seen by the build's next
    observation and deleted again before the pre-publish snapshot: its id and
    vector are in that attempt's ANN index, and only the running union of the
    observations still knows it is Memory."""
    notebook_id = seed_shared_notebook_with_memory(repo)["notebook_id"]
    repo.build_scale_index(notebook_id)
    assert_a_memory_confirmed_mid_build_never_publishes(
        repo, monkeypatch, notebook_id, window, vanish=True
    )


def test_a_memory_confirmed_during_a_viz_build_never_reaches_the_viz(repo, monkeypatch):
    assert_a_memory_confirmed_mid_viz_build_never_publishes(repo, monkeypatch)


def test_an_index_built_before_the_first_memory_is_re_stamped_by_the_next_fold(repo):
    assert_an_index_built_before_the_first_memory_recovers_by_fold(repo)


def test_a_re_stamp_never_hides_a_shared_re_extraction(repo):
    assert_a_re_stamp_never_hides_a_shared_re_extraction(repo)


@pytest.mark.parametrize(
    ("last_rebuild", "expected"),
    [
        # PostgreSQL answers in UTC; the build time carries the local offset
        ("2026-09-30T10:30:00+00:00", True),   # 18:30 at +08:00: after
        ("2026-09-30T09:30:00+00:00", False),  # 17:30 at +08:00: before
        ("9999-12-31T23:59:59", True),         # no offset: compared as text
        ("", False),
    ],
)
def test_a_kg_rebuild_is_judged_against_the_build_as_an_instant(
    repo, monkeypatch, last_rebuild, expected
):
    builder = repo._runtime.scale_builder
    monkeypatch.setattr(
        builder.projections, "unified_last_rebuild_at", lambda _nb: last_rebuild
    )
    manifest = {"built_at": "2026-09-30T18:00:00+08:00"}
    assert builder.predates_last_kg_rebuild("nb-x", manifest) is expected


def test_a_re_stamp_never_hides_an_online_re_extraction(repo):
    assert_a_re_stamp_never_hides_an_online_re_extraction(repo)


@pytest.mark.parametrize("change", ["vectors", "clusters"])
def test_a_re_stamp_never_hides_a_shared_change_of(repo, change):
    assert_a_re_stamp_never_hides_a_shared_change_of(repo, change)


@pytest.mark.parametrize(
    ("last_rebuild", "expected"),
    [("2026-09-30T10:30:00+00:00", "full"), ("2026-09-30T09:30:00+00:00", "fold")],
)
def test_the_worker_judges_a_kg_rebuild_against_the_build_as_an_instant(
    repo, monkeypatch, last_rebuild, expected
):
    """The worker's mode choice uses the shared instant test, whatever the
    machine's time zone: PostgreSQL answers ``last_rebuild_at`` in UTC while
    ``built_at`` carries the local offset (+08:00 here)."""
    from types import SimpleNamespace

    from app.services.vector_index import resolve_runtime_dim

    runtime = repo._runtime.scale_artifacts
    dim = resolve_runtime_dim(repo.settings) or repo.settings.embed_dim
    index = SimpleNamespace(manifest={"built_at": "2026-09-30T18:00:00+08:00", "dim": dim})
    monkeypatch.setattr(runtime, "load", lambda _nb, allow_stale=False: index)
    monkeypatch.setattr(
        runtime.projections, "unified_last_rebuild_at", lambda _nb: last_rebuild
    )
    monkeypatch.setattr(
        runtime.builder, "_index_delta", lambda _nb, **_kw: {"delta_sources": []}
    )
    assert runtime._resolve_mode("nb-x", "fold") == expected


def test_a_re_stamp_republishes_the_source_partition_companion(repo):
    assert_a_re_stamp_republishes_the_source_partition_companion(repo)


@pytest.mark.parametrize("tier", ["personal", "base"])
def test_a_pre_isolation_index_is_rebuilt_by_the_next_fold(repo, tier):
    assert_a_pre_isolation_index_is_rebuilt_by_the_next_fold(repo, tier)


def test_a_re_stamp_copies_the_files_where_the_filesystem_refuses_a_link(
    repo, monkeypatch
):
    """``os.link`` failing (EXDEV: staging on another filesystem) falls back to
    a copy: new inodes, and the re-stamped index loads exactly with the same
    content."""
    notebook_id = seed_notebook_without_memory(repo)
    first = repo.build_scale_index(notebook_id)
    runtime = repo._runtime.scale_artifacts
    root = runtime.artifacts.scale_dir(notebook_id)
    before = _file_identities(root)
    names_before = sorted(runtime.load(notebook_id).viz_names)

    def refuse_link(_source, _target):
        raise OSError(18, "Invalid cross-device link")

    monkeypatch.setattr(os, "link", refuse_link)
    confirm_a_memory(repo, notebook_id, 901)
    folded = repo.fold_scale_index_delta(notebook_id)
    after = _file_identities(root)
    assert folded["build_id"] != first["build_id"]
    assert set(after) == set(before) and before
    assert all(after[name] != before[name] for name in after)
    idx = runtime.load(notebook_id)
    assert idx is not None and idx.manifest["build_id"] == folded["build_id"]
    assert sorted(idx.viz_names) == names_before


def test_a_re_stamp_publishes_nothing_over_a_root_swapped_since_it_was_loaded(
    repo, monkeypatch
):
    """A direct builder fold (no runtime claim) loads the index before it
    claims; if another publisher swaps the live root in between, re-stamping
    would put the loaded manifest over the other build's files. It publishes
    nothing and returns the live manifest."""
    notebook_id = seed_notebook_without_memory(repo)
    repo.build_scale_index(notebook_id)
    confirm_a_memory(repo, notebook_id, 902)
    builder = repo._runtime.scale_builder
    real_load = builder.load_scale
    other: list = []

    def load_then_superseded(nb):
        loaded = real_load(nb)
        if not other:
            other.append(repo.build_scale_index(nb))
        return loaded

    monkeypatch.setattr(builder, "load_scale", load_then_superseded)
    root = repo._runtime.scale_artifacts.artifacts.scale_dir(notebook_id)
    out = builder.fold(notebook_id)
    live = repo._runtime.scale_artifacts.artifacts.read_manifest(root)
    assert out["build_id"] == live["build_id"] == other[0]["build_id"]


def test_a_memory_source_row_without_an_id_hides_no_row(repo):
    """SQLite's ``TEXT PRIMARY KEY`` admits a NULL id; one such Memory source
    row would make every ``NOT IN`` of the in-statement exclusion NULL and
    hide every object and relation of the notebook."""
    notebook_id = seed_notebook_without_memory(repo)
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources (id,notebook_id,title,source_type,status,"
            "created_at,updated_at) VALUES (NULL,?,?,?,?,?,?)",
            (notebook_id, "dirty", "memory", "ready", NOW, NOW),
        )
    lite = repo._derive_object_graph_lite(notebook_id)
    assert sorted(
        (n.get("payload") or {}).get("name", "") for n in lite["nodes"]
    ) == ["MOSFET", "gain"]
    assert len(lite["edges"]) == 1


# ─────────────── a discarded build is never reported as a finished one ───────
_DISCARDED = {"status": "discarded", "reason": "memory_appeared_during_build"}


@pytest.mark.parametrize("operation", ["fold", "build"])
def test_the_worker_rings_no_index_done_bell_for_a_discarded_build(
    repo, monkeypatch, operation
):
    """Directly, or as the full build a refused fold fell back to."""
    import time

    runtime = repo._runtime.scale_artifacts
    calls: list = []
    monkeypatch.setattr(runtime, "notify_index_done", lambda nb: calls.append(nb))
    monkeypatch.setattr(runtime, "_resolve_mode", lambda nb, mode: operation)
    monkeypatch.setattr(runtime, "eligible", lambda nb: True)
    discarded = lambda nb, *a, **kw: {**_DISCARDED, "notebook_id": nb}  # noqa: E731
    monkeypatch.setattr(runtime.builder, "fold", discarded)
    monkeypatch.setattr(runtime.builder, "build", discarded)
    nb = repo.create_notebook(NotebookCreate(name="worker")).id
    runtime.trigger(nb, when="now", mode="auto")
    for _ in range(200):
        if nb not in runtime.building:
            break
        time.sleep(0.02)
    time.sleep(0.2)
    assert nb not in runtime.building
    assert calls == []


def test_the_offline_cli_fails_a_discarded_build_with_its_reason(repo, monkeypatch):
    from app.services.scale_build_cli import ScaleBuildCliFailure, run_build

    nb = repo.create_notebook(NotebookCreate(name="cli")).id
    monkeypatch.setattr(repo, "build_scale_index",
                        lambda notebook_id, on_stage=None: {**_DISCARDED, "notebook_id": notebook_id})
    with pytest.raises(ScaleBuildCliFailure, match="memory_appeared_during_build"):
        run_build(repo, nb, mode="build", report=lambda _line: None)


def test_batch_indexing_reports_a_discarded_build_as_discarded(repo, monkeypatch):
    from app.services import batch_ingest

    monkeypatch.setattr(repo, "build_scale_index",
                        lambda notebook_id, on_stage=None: {**_DISCARDED, "notebook_id": notebook_id})
    assert batch_ingest.run_index(repo, "nb-x") == {
        "indexed_nodes": 0, "scale_index_discarded": 1,
    }
    assert batch_ingest._scale_index_outcome({"n_nodes": 3}) == "scale_index_built"


@pytest.mark.parametrize("outcome", ["discarded", "built"])
def test_the_batch_kg_phase_reports_what_its_scale_build_did(
    repo, monkeypatch, outcome
):
    """``run_kg``'s own scale build (after its KG rebuild) logs a discarded
    build as discarded and a published one as built."""
    from app.services import batch_ingest

    nb = repo.create_notebook(NotebookCreate(name="batch kg")).id
    manifest = (
        {**_DISCARDED, "notebook_id": nb} if outcome == "discarded"
        else {"n_nodes": 3, "notebook_id": nb}
    )
    monkeypatch.setattr(repo, "rebuild_unified_kg", lambda *a, **kw: 0)
    monkeypatch.setattr(repo.maintenance, "has_scale_index", lambda _nb: True)
    monkeypatch.setattr(repo, "build_scale_index",
                        lambda notebook_id, on_stage=None: manifest)
    logged: list = []
    batch_ingest.run_kg(repo, nb, rebuild_only=True, log=logged.append)
    assert [e for e in logged if e.get("status", "").startswith("scale_index")] == [
        {"phase": "kg", "status": f"scale_index_{outcome}",
         "nodes": 3 if outcome == "built" else 0}
    ]
