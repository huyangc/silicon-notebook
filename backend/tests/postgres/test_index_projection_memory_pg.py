"""PostgreSQL twin of the M1 artifact-isolation tests (E4-6).

A member's Memory never enters a shared viz index or scale index: the standalone
viz derive, the scale build's viz arrays, the graph rows, the ANN feeds and the
fold delta all read the notebook without Memory-derived rows. The scenario is
the one ``tests/memory_artifact_fixture`` seeds for the SQLite tests.
"""
from __future__ import annotations

import json
import os

import pytest

from app.core.config import Settings
from app.repositories.postgres.repository import PostgresRepository
from app.services.embedding import FakeEmbedder
from tests.memory_artifact_fixture import (
    SECRET_NAMES,
    assert_a_memory_confirmed_mid_build_never_publishes,
    assert_a_memory_confirmed_at_the_object_read_never_reaches_a_published_index,
    assert_a_pre_isolation_index_is_rebuilt_once_its_memory_is_deleted,
    assert_a_pre_isolation_index_is_rebuilt_by_the_next_fold,
    assert_no_artifact_built_before_the_isolation_is_served,
    assert_a_re_stamp_never_hides_a_shared_re_extraction,
    assert_a_re_stamp_never_hides_an_online_re_extraction,
    assert_a_re_stamp_never_hides_a_shared_change_of,
    assert_a_re_stamp_republishes_the_source_partition_companion,
    assert_a_re_stamp_publishes_the_identity_it_verified,
    assert_a_re_stamp_never_copies_a_graph_built_under_other_settings,
    assert_an_index_built_before_the_first_memory_recovers_by_fold,
    assert_a_memory_vector_takes_no_synonym_slot,
    assert_a_memory_confirmed_mid_viz_build_never_publishes,
    assert_no_partition_names_a_memory_cluster,
    make_it_look_pre_isolation,
    mix_a_shared_object_into_a_memory_cluster,
    seed_notebook_without_memory,
    seed_shared_notebook_with_memory,
    sql,
)
from tests.model_testkit import bind_all_embedding_clients

pytestmark = [
    pytest.mark.postgres_integration,
    pytest.mark.xdist_group(name="postgres_offline_maintenance"),
]


@pytest.fixture
def repo(postgres_settings: Settings):
    repository = PostgresRepository(postgres_settings)
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    try:
        yield repository
    finally:
        repository.close()


def _edges(idx):
    return sorted(tuple(row) for row in idx.viz_edges.rows(idx.viz_ids))


def _artifact_bytes(directory: str) -> bytes:
    blob = b""
    for root, _dirs, files in os.walk(directory):
        for name in sorted(files):
            with open(os.path.join(root, name), "rb") as handle:
                blob += handle.read()
    return blob


def test_viz_artifact_has_no_memory_object_name_or_memory_only_edge(repo):
    seeded = seed_shared_notebook_with_memory(repo)
    nb_id, canon = seeded["notebook_id"], seeded["canonical"]

    manifest = repo.build_viz_index(nb_id)
    idx = repo._viz_index(nb_id)

    assert not set(SECRET_NAMES) & set(idx.viz_names)
    blob = _artifact_bytes(repo._viz_index_dir(nb_id)).lower()
    for secret in SECRET_NAMES:
        assert secret.lower().encode() not in blob
    for object_id in seeded["memory_object_ids"]:
        assert object_id.encode() not in blob

    edges = _edges(idx)
    a, b, c, x, y = (canon[n] for n in ("MOSFET", "gain", "bias", "left", "right"))
    assert (x, y, "depends_on") not in edges
    assert (a, b, "contrasts_with") not in edges
    assert edges.count((a, b, "depends_on")) == 1
    assert (a, c, "depends_on") in edges
    assert len(edges) == 2
    assert manifest["n_viz_nodes"] == len(idx.viz_ids) == 5
    assert manifest["n_viz_edges"] == len(idx.viz_edges) == 2


def test_the_relation_read_excludes_memory_relations_between_visible_nodes(repo):
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
    assert len(rows) == 2


def test_the_neighbour_view_drops_a_memory_only_edge(repo):
    seeded = seed_shared_notebook_with_memory(repo)
    nb_id, canon = seeded["notebook_id"], seeded["canonical"]
    left = repo.kg_neighbors(nb_id, canon["left"])
    assert left["edges"] == []
    core = repo.kg_neighbors(nb_id, canon["MOSFET"])
    assert sorted(
        (e["source_object_id"], e["target_object_id"], e["edge_type"])
        for e in core["edges"]
    ) == sorted([
        (canon["MOSFET"], canon["gain"], "depends_on"),
        (canon["MOSFET"], canon["bias"], "depends_on"),
    ])
    rendered = repr(core) + repr(repo.unified_graph(nb_id, "object", 10))
    for secret in SECRET_NAMES:
        assert secret not in rendered


def test_every_notebook_carries_the_isolation_marker_in_its_version(repo):
    """With Memory, after its Memory is gone, and never having had any."""
    seeded = seed_shared_notebook_with_memory(repo)
    nb_id = seeded["notebook_id"]

    def pair(version):
        at = version.index("memory_isolation")
        return version[at:at + 2]

    assert pair(repo._scale_index_version(nb_id)) == ["memory_isolation", 1]
    with repo._write() as db:
        db.execute(
            "DELETE FROM sources WHERE notebook_id=%s AND source_type='memory'",
            (nb_id,),
        )
    assert pair(repo._scale_index_version(nb_id)) == ["memory_isolation", 1]
    from app.models.schemas import NotebookCreate

    other = repo.create_notebook(NotebookCreate(name="never held memory")).id
    assert pair(repo._scale_index_version(other)) == ["memory_isolation", 1]


def test_a_viz_built_before_the_isolation_is_stale_after_it(repo):
    seeded = seed_shared_notebook_with_memory(repo)
    nb_id = seeded["notebook_id"]
    repo.build_viz_index(nb_id)
    assert repo._viz_index_probe(nb_id)["viz_stale"] is False
    make_it_look_pre_isolation(repo._viz_index_dir(nb_id))
    repo._viz_idx_cache.pop(nb_id, None)
    assert repo._viz_index_probe(nb_id)["viz_stale"] is True


def test_scale_index_holds_no_memory_object_relation_edge_or_source(repo):
    seeded = seed_shared_notebook_with_memory(repo)
    nb_id, ids = seeded["notebook_id"], seeded["objects"]
    memory_objects = set(seeded["memory_object_ids"])
    memory_relations = set(seeded["memory_relation_ids"])
    projections = repo._runtime.index_projections
    assert projections.source_ids(nb_id) == ["src-shared"]
    assert projections.memory_source_ids(nb_id) == ["src-memory"]

    repo.build_scale_index(nb_id)
    idx = repo._scale_index(nb_id, allow_stale=True)

    assert idx.manifest["watermark_sources"] == ["src-shared"]
    assert not memory_objects & set(idx.ann_labels)
    assert not memory_objects & set(idx.node_ids)
    assert not memory_relations & set(idx.relation_ann_labels)
    assert set(seeded["shared_relation_ids"]) <= set(idx.relation_ann_labels)
    assert not set(SECRET_NAMES) & set(idx.viz_names)
    assert idx.manifest["n_ann"] == len(idx.ann_labels)
    assert idx.manifest["n_kg_nodes"] == 5
    assert idx.manifest["n_relation_ann"] == len(idx.relation_ann_labels) == 2
    position = {node: i for i, node in enumerate(idx.node_ids)}
    left, right = position[ids["left"]], position[ids["right"]]
    assert float(idx.transition[left, right] + idx.transition[right, left]) == 0.0
    gain, core = position[ids["gain"]], position[ids["MOSFET"]]
    assert float(idx.transition[gain, core] + idx.transition[core, gain]) > 0.0


def test_scale_graph_leaves_out_a_cluster_that_holds_a_memory_member(repo):
    seeded = seed_shared_notebook_with_memory(repo)
    nb_id, ids = seeded["notebook_id"], seeded["objects"]
    mix_a_shared_object_into_a_memory_cluster(repo, seeded)
    repo.build_scale_index(nb_id)
    idx = repo._scale_index(nb_id, allow_stale=True)
    assert "cluster:K-secret-alpha-cluster" not in idx.node_ids
    assert not any("secret" in str(node).lower() for node in idx.node_ids)
    assert any(str(node).startswith("cluster:K-") for node in idx.node_ids)


def test_gathered_graph_holds_no_membership_or_synonym_edge_of_a_memory_object(repo):
    seeded = seed_shared_notebook_with_memory(repo)
    nb_id, ids = seeded["notebook_id"], seeded["objects"]
    memory_id, shared_id = ids[SECRET_NAMES[0]], ids["MOSFET"]
    with repo._write() as db:
        db.execute(
            "INSERT INTO chunks (id,notebook_id,source_id,text,section_path,"
            "element_ids,created_at) VALUES ('chunk-1',%s,%s,%s,%s,%s,%s)",
            (nb_id, "src-shared", "text", "", json.dumps(["el-1"]),
             "2026-09-29T00:00:00"),
        )
        for object_id in (memory_id, shared_id):
            db.execute(
                "UPDATE knowledge_objects SET evidence=%s WHERE id=%s",
                (json.dumps([{"element_id": "el-1"}]), object_id),
            )
    repo._runtime.scale_artifacts.version_memo.clear()
    _nodes, edges, _chunks, _kg, counts = repo._runtime.scale_builder.gather_graph(
        nb_id
    )
    assert shared_id in counts and memory_id not in counts
    assert all(memory_id not in (a, b) for a, b, _w in edges)


def test_fold_refuses_an_index_that_was_built_over_memory_sources(repo, monkeypatch):
    # the fake embedder is 16-wide; without this the fold would refuse first, on
    # the deployment-wide dimension check, before it ever looked at the sources
    monkeypatch.setattr(repo.settings, "embed_dim", 16)
    seeded = seed_shared_notebook_with_memory(repo)
    nb_id = seeded["notebook_id"]
    repo.build_scale_index(nb_id)
    path = os.path.join(
        str(repo.settings.storage_dir), "kg_index", nb_id, "manifest.json"
    )
    make_it_look_pre_isolation(os.path.dirname(path))
    with open(path) as handle:
        manifest = json.load(handle)
    manifest["watermark_sources"] = sorted(
        manifest["watermark_sources"] + ["src-memory"]
    )
    with open(path, "w") as handle:
        json.dump(manifest, handle)
    repo._scale_idx_cache.pop(nb_id, None)
    before = manifest["build_id"]

    events = []
    log = repo._runtime.scale_builder.event_log
    original = log.emit
    log.emit = lambda event: (events.append(event), original(event))[1]
    result = repo.fold_scale_index_delta(nb_id)

    assert [e["reason"] for e in events if e.get("kind") == "scale_fold_refused"] == [
        "memory_isolation"
    ]
    assert result["build_id"] != before
    assert result["watermark_sources"] == ["src-shared"]


def _build_the_way_the_previous_release_did(repo, seeded):
    """A standalone viz built while the Memory source was an ordinary source:
    contaminated, without the isolation field and without the version pair."""
    nb_id = seeded["notebook_id"]
    with repo._write() as db:
        db.execute("UPDATE sources SET source_type='md' WHERE id='src-memory'")
    repo.build_viz_index(nb_id)
    make_it_look_pre_isolation(repo._viz_index_dir(nb_id))
    with repo._write() as db:
        db.execute("UPDATE sources SET source_type='memory' WHERE id='src-memory'")
    repo._viz_idx_cache.pop(nb_id, None)
    old = repo._runtime.scale_artifacts.artifacts.load_viz(nb_id)
    assert set(SECRET_NAMES) <= set(old.viz_names)
    assert "memory_isolation" not in old.manifest


def test_a_large_notebook_does_not_serve_a_viz_built_before_the_isolation(repo, monkeypatch):
    seeded = seed_shared_notebook_with_memory(repo)
    nb_id = seeded["notebook_id"]
    _build_the_way_the_previous_release_did(repo, seeded)
    monkeypatch.setattr(repo.settings, "viz_sync_build_max_objects", 0)

    assert repo._viz_index(nb_id) is None
    probe = repo._viz_index_probe(nb_id)
    assert (probe["viz_indexed"], probe["viz_nodes"], probe["viz_edges"]) == (False, 0, 0)
    graph = repo.unified_graph(nb_id, "object", 10)
    assert graph["nodes"] == [] and graph["viz_unavailable"] is True


def test_a_small_notebook_rebuilds_a_viz_built_before_the_isolation_on_the_spot(repo):
    seeded = seed_shared_notebook_with_memory(repo)
    nb_id = seeded["notebook_id"]
    _build_the_way_the_previous_release_did(repo, seeded)

    idx = repo._viz_index(nb_id)
    assert idx is not None
    assert not set(SECRET_NAMES) & set(idx.viz_names)
    assert not set(seeded["memory_object_ids"]) & set(idx.viz_ids)
    assert "memory_isolation" in idx.manifest["version"]


def test_viz_does_not_fold_a_shared_object_into_a_cluster_that_holds_memory(repo):
    seeded = seed_shared_notebook_with_memory(repo)
    nb_id, ids = seeded["notebook_id"], seeded["objects"]
    mix_a_shared_object_into_a_memory_cluster(repo, seeded)
    repo.build_viz_index(nb_id)
    idx = repo._viz_index(nb_id)
    assert not any("secret" in str(v).lower()
                   for v in list(idx.viz_ids) + list(idx.viz_names))
    assert ids["left"] in idx.viz_ids
    assert b"secret" not in _artifact_bytes(repo._viz_index_dir(nb_id)).lower()


def test_a_memory_vector_takes_no_synonym_neighbour_slot(repo, monkeypatch):
    assert_a_memory_vector_takes_no_synonym_slot(repo, monkeypatch)


def test_no_source_partition_names_a_cluster_that_holds_memory(repo):
    assert_no_partition_names_a_memory_cluster(repo, seed_shared_notebook_with_memory(repo))


@pytest.fixture
def repo16(postgres_settings: Settings):
    """The same repository with the embedding width the fake embedder writes,
    so a fold is judged on the isolation and never refused for a dim drift."""
    repository = PostgresRepository(postgres_settings.model_copy(update={"embed_dim": 16}))
    bind_all_embedding_clients(repository, FakeEmbedder(dim=16))
    try:
        yield repository
    finally:
        repository.close()


def test_the_first_memory_confirmed_during_a_build_never_reaches_a_published_index(
    repo16, monkeypatch
):
    """Interleaving B on PostgreSQL, where the window is real: keyset pages are
    separate statements; the notebook's first Memory is confirmed right before
    the object read."""
    notebook_id = seed_notebook_without_memory(repo16)
    repo16.build_scale_index(notebook_id)
    assert_a_memory_confirmed_at_the_object_read_never_reaches_a_published_index(
        repo16, monkeypatch, notebook_id, vanish=False
    )


def test_a_memory_confirmed_and_deleted_during_a_build_never_reaches_it(
    repo16, monkeypatch
):
    """The re-review's ``race2 add_delete`` on PostgreSQL."""
    notebook_id = seed_notebook_without_memory(repo16)
    repo16.build_scale_index(notebook_id)
    assert_a_memory_confirmed_at_the_object_read_never_reaches_a_published_index(
        repo16, monkeypatch, notebook_id, vanish=True
    )


@pytest.mark.parametrize("window", ["ann_feed", "relation_feed"])
def test_an_existing_memory_re_extracted_during_a_build_never_reaches_the_ann_labels(
    repo16, monkeypatch, window
):
    notebook_id = seed_shared_notebook_with_memory(repo16)["notebook_id"]
    repo16.build_scale_index(notebook_id)
    assert_a_memory_confirmed_mid_build_never_publishes(
        repo16, monkeypatch, notebook_id, window, existing_source="src-memory"
    )


def test_fold_never_turns_a_pre_isolation_index_with_a_deleted_memory_into_an_isolated_one(
    repo16,
):
    assert_a_pre_isolation_index_is_rebuilt_once_its_memory_is_deleted(repo16)


def test_a_further_memory_confirmed_during_a_build_never_reaches_the_ann_labels(
    repo16, monkeypatch
):
    """Interleaving A: a further Memory confirmed after the KG ANN feed learnt
    its Memory object-id set."""
    notebook_id = seed_shared_notebook_with_memory(repo16)["notebook_id"]
    repo16.build_scale_index(notebook_id)
    assert_a_memory_confirmed_mid_build_never_publishes(
        repo16, monkeypatch, notebook_id, "ann_feed"
    )


def test_a_memory_confirmed_during_a_viz_build_never_reaches_the_viz(repo16, monkeypatch):
    assert_a_memory_confirmed_mid_viz_build_never_publishes(repo16, monkeypatch)


def test_an_index_built_before_the_first_memory_is_re_stamped_by_the_next_fold(repo16):
    assert_an_index_built_before_the_first_memory_recovers_by_fold(repo16)


@pytest.mark.parametrize("window", ["ann_feed", "relation_feed"])
def test_a_memory_observed_after_a_feed_and_deleted_again_never_reaches_the_ann_labels(
    repo16, monkeypatch, window
):
    notebook_id = seed_shared_notebook_with_memory(repo16)["notebook_id"]
    repo16.build_scale_index(notebook_id)
    assert_a_memory_confirmed_mid_build_never_publishes(
        repo16, monkeypatch, notebook_id, window, vanish=True
    )


def test_a_re_stamp_never_hides_a_shared_re_extraction(repo16):
    assert_a_re_stamp_never_hides_a_shared_re_extraction(repo16)


def test_a_re_stamp_never_hides_an_online_re_extraction(repo16):
    assert_a_re_stamp_never_hides_an_online_re_extraction(repo16)


@pytest.mark.parametrize("change", ["vectors", "clusters"])
def test_a_re_stamp_never_hides_a_shared_change_of(repo16, change):
    assert_a_re_stamp_never_hides_a_shared_change_of(repo16, change)


def test_a_re_stamp_republishes_the_source_partition_companion(repo16):
    assert_a_re_stamp_republishes_the_source_partition_companion(repo16)


def test_a_re_stamp_publishes_the_identity_it_verified(repo16):
    assert_a_re_stamp_publishes_the_identity_it_verified(repo16)


def test_a_re_stamp_never_copies_a_graph_built_under_other_settings(repo16):
    assert_a_re_stamp_never_copies_a_graph_built_under_other_settings(repo16)


@pytest.mark.parametrize("tier", ["personal", "base"])
def test_a_pre_isolation_index_is_rebuilt_by_the_next_fold(repo16, tier):
    assert_a_pre_isolation_index_is_rebuilt_by_the_next_fold(repo16, tier)


@pytest.mark.parametrize("tier", ["personal", "base"])
def test_no_notebook_serves_an_artifact_built_before_the_isolation(
    repo16, monkeypatch, tier
):
    assert_no_artifact_built_before_the_isolation_is_served(repo16, monkeypatch, tier)


def test_a_relation_without_a_source_is_kept_in_a_notebook_holding_memory(repo):
    """``knowledge_relations.source_id`` is nullable on PostgreSQL. In a notebook
    holding Memory, ``NULL NOT IN (its Memory source ids)`` is NULL, so without
    the ``source_id IS NULL`` arm every such relation would silently drop out
    of the viz derive and of the scale graph."""
    seeded = seed_shared_notebook_with_memory(repo)
    nb_id, ids, canon = seeded["notebook_id"], seeded["objects"], seeded["canonical"]
    with repo._write() as db:
        db.execute(
            "INSERT INTO knowledge_relations (id,notebook_id,source_id,"
            "source_object_id,target_object_id,edge_type,evidence,created_at,"
            "review_status) VALUES (%s,%s,NULL,%s,%s,%s,%s,%s,%s)",
            ("kr-no-source", nb_id, ids["bias"], ids["right"], "depends_on",
             "[]", "2026-09-29T00:00:00", "pending"),
        )
    lite = repo._derive_object_graph_lite(nb_id)
    assert (canon["bias"], canon["right"], "depends_on") in {
        (e["source_object_id"], e["target_object_id"], e["edge_type"])
        for e in lite["edges"]
    }
    repo._runtime.scale_artifacts.version_memo.clear()
    repo.build_scale_index(nb_id)
    idx = repo._scale_index(nb_id, allow_stale=True)
    assert (canon["bias"], canon["right"], "depends_on") in _edges(idx)
    position = {node: i for i, node in enumerate(idx.node_ids)}
    bias, right = position[ids["bias"]], position[ids["right"]]
    assert float(idx.transition[bias, right] + idx.transition[right, bias]) > 0.0
