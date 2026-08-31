from __future__ import annotations

import json
import shutil
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

import app.services.kg.source_partition_index as partition_module
import app.repositories.source_subgraph_projection as source_projection
from app.core.config import Settings
from app.repositories.filesystem.scale_artifact_store import MANIFEST_ABSENT
from app.services.kg.source_partition_index import (
    SourcePartitionUnavailable,
    build_source_partition,
    source_partition_key,
)
from app.services.source_partitioned_ppr import SourcePartitionedPprService
from app.services.source_subgraph_ppr import SourceSubgraphPprService
from app.services.sqlite_repository import SQLiteRepository
from tests.test_source_subgraph import _seed


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'partition.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("SOURCE_PARTITIONED_PPR_ENABLED", "true")
    return SQLiteRepository(Settings(_env_file=None))


def _publish(repo, notebook_id, source_ids, version, build_id=None):
    return repo._runtime.scale_artifact_store.save_source_partitions(
        notebook_id,
        parent_version=version,
        parent_build_id=build_id,
        source_ids=source_ids,
        load_rows=lambda source_id: (
            repo._runtime.index_projections.source_graph_partition_rows(
                notebook_id, source_id
            )
        ),
    )


def _signature_map(repo, notebook_id, source_ids):
    allowed = tuple(sorted(source_ids))
    signature = repo._runtime.index_projections.source_subgraph_signature(
        notebook_id, allowed
    )
    return {
        str(item[0]): (signature[0], signature[1], signature[2], (item,))
        for item in signature[3]
    }


def test_offline_projection_is_source_first_and_preserves_cross_relation(repo):
    notebook_id, source_a, _source_b = _seed(repo)

    rows = repo._runtime.index_projections.source_graph_partition_rows(
        notebook_id, source_a
    )

    assert rows["reasons"] == []
    assert {row["object_id"] for row in rows["objects"]} == {"ko-a1", "ko-a2"}
    assert {row["chunk_id"] for row in rows["chunks"]} == {"chunk-src-a"}
    assert {row["fact_id"] for row in rows["facts"]} == {
        "fact-ko-a1",
        "fact-ko-a2",
    }
    assert {row["relation_id"] for row in rows["relations"]} == {
        "rel-a",
        "rel-cross",
    }
    assert {row["member_object_id"] for row in rows["clusters"]} == {
        "ko-a1",
        "ko-a2",
    }


def test_offline_partition_chunk_plan_is_source_first(repo, monkeypatch):
    notebook_id, source_a, _source_b = _seed(repo)
    statements = []
    original_rows = source_projection._rows

    def capture(connection, sql, params):
        statements.append((sql, tuple(params)))
        return original_rows(connection, sql, params)

    monkeypatch.setattr(source_projection, "_rows", capture)
    repo._runtime.index_projections.source_graph_partition_rows(notebook_id, source_a)
    chunk_sql, params = next(
        (sql, params)
        for sql, params in statements
        if "SELECT id AS chunk_id,element_ids" in sql
    )
    with repo._runtime.database.connect() as db:
        plan = db.execute("EXPLAIN QUERY PLAN " + chunk_sql, params).fetchall()
    detail = "\n".join(str(row["detail"]) for row in plan)
    assert "idx_chunks_source" in detail
    assert "idx_chunks_nb" not in detail


def test_partition_builder_rejects_mixed_source_relation_evidence(repo):
    notebook_id, source_a, source_b = _seed(repo)
    clean_rows = repo._runtime.index_projections.source_graph_partition_rows(
        notebook_id, source_a
    )
    clean = build_source_partition(
        clean_rows, source_id=source_a, parent_version=["v1"]
    )
    polluted_rows = {
        **clean_rows,
        "relations": [dict(row) for row in clean_rows["relations"]],
    }
    for row in polluted_rows["relations"]:
        if row["relation_id"] == "rel-a":
            row["evidence"] = [
                {"source_id": source_a},
                {"source_id": source_b},
            ]
    polluted = build_source_partition(
        polluted_rows, source_id=source_a, parent_version=["v1"]
    )

    assert clean.transition.nnz == polluted.transition.nnz + 2


def test_selected_partition_load_never_opens_unselected_source(repo, monkeypatch):
    notebook_id, source_a, source_b = _seed(repo)
    version = [notebook_id, "v1"]
    _publish(repo, notebook_id, (source_a, source_b), version)
    opened = []
    original = partition_module.load_source_partition

    def recording(root, *, source_id, **kwargs):
        opened.append(source_id)
        return original(root, source_id=source_id, **kwargs)

    monkeypatch.setattr(partition_module, "load_source_partition", recording)
    service = SourcePartitionedPprService(
        settings=repo.settings,
        artifacts=repo._runtime.scale_artifact_store,
        projections=repo._runtime.index_projections,
    )
    result = service.retrieve(
        notebook_id,
        [source_a],
        parent_version=version,
        object_seeds={"ko-a1": 1.0, "ko-b1": 1_000_000.0},
    )

    assert result.capability.enabled
    assert opened == [source_a]
    assert {hit.source_id for hit in result.hits} == {source_a}
    assert {hit.chunk_id for hit in result.hits} == {"chunk-src-a"}


def test_selected_union_adds_only_authorized_cross_partition_relation(repo):
    notebook_id, source_a, source_b = _seed(repo)
    version = [notebook_id, "v1"]
    _publish(repo, notebook_id, (source_a, source_b), version)
    service = SourcePartitionedPprService(
        settings=repo.settings,
        artifacts=repo._runtime.scale_artifact_store,
        projections=repo._runtime.index_projections,
    )

    only_a = service.retrieve(
        notebook_id,
        [source_a],
        parent_version=version,
        object_seeds={"ko-a1": 1.0},
    )
    selected_union = service.retrieve(
        notebook_id,
        [source_a, source_b],
        parent_version=version,
        object_seeds={"ko-a1": 1.0},
    )

    assert only_a.capability.enabled and selected_union.capability.enabled
    assert selected_union.logical_edge_count > only_a.logical_edge_count
    assert {hit.source_id for hit in selected_union.hits} <= {source_a, source_b}


def test_small_snapshot_and_partition_artifact_have_same_authorized_graph(repo):
    notebook_id, source_a, source_b = _seed(repo)
    version = [notebook_id, "v1"]
    _publish(repo, notebook_id, (source_a, source_b), version)
    snapshot = repo._runtime.source_subgraphs.snapshot(notebook_id, [source_a])
    online = SourceSubgraphPprService(settings=repo.settings)
    artifact = SourcePartitionedPprService(
        settings=repo.settings,
        artifacts=repo._runtime.scale_artifact_store,
        projections=repo._runtime.index_projections,
    )

    online_graph, _ = online._graph(snapshot)
    artifact_graph, _ = artifact._graph(notebook_id, (source_a,), version)

    assert online_graph.node_index == artifact_graph.node_index
    assert online_graph.logical_edge_count == artifact_graph.logical_edge_count
    np.testing.assert_allclose(
        online_graph.transition.toarray(), artifact_graph.transition.toarray()
    )


def test_legacy_missing_and_identity_mismatch_fail_closed(repo):
    notebook_id, source_a, _source_b = _seed(repo)
    service = SourcePartitionedPprService(
        settings=repo.settings,
        artifacts=repo._runtime.scale_artifact_store,
        projections=repo._runtime.index_projections,
    )
    missing = service.retrieve(
        notebook_id,
        [source_a],
        parent_version=["missing"],
        object_seeds={"ko-a1": 1.0},
    )
    assert missing.capability.reason == "source_partition_artifact_unavailable"

    _publish(repo, notebook_id, (source_a,), ["v1"])
    mismatch = service.retrieve(
        notebook_id,
        [source_a],
        parent_version=["v2"],
        object_seeds={"ko-a1": 1.0},
    )
    assert mismatch.capability.reason == "source_partition_identity_mismatch"


def test_partition_manifest_row_mismatch_is_corrupt_not_partially_loaded(repo):
    notebook_id, source_a, _source_b = _seed(repo)
    version = ["v1"]
    _publish(repo, notebook_id, (source_a,), version)
    manifest_path = (
        repo._runtime.scale_artifact_store.source_partition_dir(notebook_id)
        / source_partition_key(source_a)
        / "manifest.json"
    )
    manifest = json.loads(manifest_path.read_text())
    manifest["n_nodes"] += 1
    manifest_path.write_text(json.dumps(manifest))

    result = repo._runtime.source_partitioned_ppr.retrieve(
        notebook_id,
        [source_a],
        parent_version=version,
        object_seeds={"ko-a1": 1.0},
    )
    assert result.capability.reason == "source_partition_artifact_corrupt"


def test_parseable_partition_payload_tampering_is_corrupt(repo):
    notebook_id, source_a, _source_b = _seed(repo)
    version = ["v1"]
    _publish(repo, notebook_id, (source_a,), version)
    directory = repo._runtime.scale_artifact_store.source_partition_dir(
        notebook_id
    ) / source_partition_key(source_a)
    chunk_nodes = np.load(directory / "chunk_nodes.npy", allow_pickle=True)
    chunk_nodes[0][1] = "chunk-from-unselected-source"
    np.save(directory / "chunk_nodes.npy", chunk_nodes)

    result = repo._runtime.source_partitioned_ppr.retrieve(
        notebook_id,
        [source_a],
        parent_version=version,
        object_seeds={"ko-a1": 1.0},
    )
    assert result.capability.reason == "source_partition_artifact_corrupt"


def test_single_source_reuses_persisted_csr_without_coo_rebuild(repo, monkeypatch):
    notebook_id, source_a, _source_b = _seed(repo)
    version = ["v1"]
    _publish(repo, notebook_id, (source_a,), version)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("single-source cold load must not rebuild the CSR")

    monkeypatch.setattr("scipy.sparse.csr_matrix.tocoo", forbidden)
    result = repo._runtime.source_partitioned_ppr.retrieve(
        notebook_id,
        [source_a],
        parent_version=version,
        object_seeds={"ko-a1": 1.0},
    )
    assert result.capability.enabled


def test_union_and_iteration_rails_fail_closed_or_bound_work(repo):
    notebook_id, source_a, source_b = _seed(repo)
    version = ["v1"]
    _publish(repo, notebook_id, (source_a, source_b), version)
    repo.settings.source_subgraph_max_objects = 1
    repo.settings.source_subgraph_max_chunks = 1
    repo.settings.source_subgraph_max_cluster_memberships = 1
    limited = repo._runtime.source_partitioned_ppr.retrieve(
        notebook_id,
        [source_a, source_b],
        parent_version=version,
        object_seeds={"ko-a1": 1.0},
    )
    assert limited.capability.reason == "source_partition_union_limit_exceeded"

    repo.settings.source_subgraph_max_objects = 20_000
    repo.settings.source_subgraph_max_chunks = 20_000
    repo.settings.source_subgraph_max_cluster_memberships = 20_000
    repo.settings.source_partitioned_ppr_max_iterations = 2
    bounded = repo._runtime.source_partitioned_ppr.retrieve(
        notebook_id,
        [source_a],
        parent_version=version,
        object_seeds={"ko-a1": 1.0},
    )
    assert bounded.capability.enabled
    assert bounded.iterations <= 2


def test_union_manifest_preflight_rejects_before_any_payload_open(repo, monkeypatch):
    notebook_id, source_a, source_b = _seed(repo)
    version = ["v1"]
    _publish(repo, notebook_id, (source_a, source_b), version)
    opened = []
    original = partition_module.load_source_partition

    def recording(*args, **kwargs):
        opened.append(kwargs["source_id"])
        return original(*args, **kwargs)

    monkeypatch.setattr(partition_module, "load_source_partition", recording)
    with pytest.raises(SourcePartitionUnavailable) as error:
        repo._runtime.scale_artifact_store.load_source_partitions(
            notebook_id,
            [source_a, source_b],
            expected_parent_version=version,
            expected_source_signatures=_signature_map(
                repo, notebook_id, [source_a, source_b]
            ),
            max_nodes=1,
            max_nnz=1,
        )
    assert error.value.reason == "source_partition_union_limit_exceeded"
    assert opened == []


def _write_main_manifest(repo, notebook_id, **manifest):
    """A live main-index manifest, which is where the reader takes the
    generation a companion has to pair with. Only the two keys this gate reads
    are written; nothing in this suite loads the main artifact itself."""
    directory = repo._runtime.scale_artifact_store.scale_dir(notebook_id)
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "manifest.json").write_text(json.dumps(manifest))


def _retrieve(repo, notebook_id, source_id, version):
    return repo._runtime.source_partitioned_ppr.retrieve(
        notebook_id,
        [source_id],
        parent_version=version,
        object_seeds={"ko-a1": 1.0},
    )


def test_a_same_version_companion_from_another_build_is_not_served(repo):
    """P1, codex PR#643 R26 — the whole point of the build id, end to end.

    Both roots say ``version == ["same-main-version"]`` (republishing the same
    version is supported and the reader is asked for exactly that version), and
    they were produced by two different builds: the shape an ``import``
    interrupted after the companion rename but before the main one leaves
    behind. The old version-only gate served this mixed generation. It must
    now degrade to capability-unavailable — fail-SOFT, not an exception
    escaping into the ask path, and never a whole-graph fallback
    (docs/development.md:37).

    Mutation anchor: make ``build_generation_mismatch`` return ``False``
    unconditionally and this goes green with hits. (Dropping just one of the
    two reader gates does not: the root and per-source checks are separately
    load-bearing, pinned by
    ``test_scale_artifact_compatibility::test_a_companion_from_another_build_
    is_refused_at_the_root`` and by the copied-partition test below.)
    """
    notebook_id, source_a, _source_b = _seed(repo)
    version = ["same-main-version"]
    _publish(repo, notebook_id, (source_a,), version, build_id="a" * 32)
    _write_main_manifest(repo, notebook_id, version=version, build_id="b" * 32)

    result = _retrieve(repo, notebook_id, source_a, version)

    assert result.capability.enabled is False
    assert result.capability.reason == "source_partition_identity_mismatch"
    assert not result.hits


def test_a_companion_from_the_live_build_is_served(repo):
    """Negative anchor for the refusal above: the identical pair with ONE
    shared build id is the ordinary healthy publish and must answer. Also pins
    that the id reaches every PER-SOURCE manifest, not just the root — both
    are gates the reader applies."""
    notebook_id, source_a, _source_b = _seed(repo)
    version = ["same-main-version"]
    _publish(repo, notebook_id, (source_a,), version, build_id="a" * 32)
    _write_main_manifest(repo, notebook_id, version=version, build_id="a" * 32)

    result = _retrieve(repo, notebook_id, source_a, version)

    assert result.capability.enabled and result.hits
    partition_manifest = json.loads(
        (
            repo._runtime.scale_artifact_store.source_partition_dir(notebook_id)
            / source_partition_key(source_a)
            / "manifest.json"
        ).read_text()
    )
    assert partition_manifest["parent_build_id"] == "a" * 32


def test_a_partition_copied_in_from_another_generation_is_refused(repo):
    """The per-source gate carries its own weight, not just the root's.

    A companion ROOT that pairs correctly can still contain one partition
    directory from another generation — an operator copying a hashed source
    directory between two companion roots is exactly what
    docs/operations.md's "do not copy a companion root between scale-index
    generations" warns against, and the root manifest says nothing about it.

    Mutation anchor: drop the ``build_generation_mismatch`` check from
    ``inspect_source_partition_manifest`` (leaving
    ``validate_partition_root``'s intact) and this goes green — the root
    check alone never opens a per-source header.
    """
    notebook_id, source_a, _source_b = _seed(repo)
    version = ["same-main-version"]
    _publish(repo, notebook_id, (source_a,), version, build_id="a" * 32)
    _write_main_manifest(repo, notebook_id, version=version, build_id="a" * 32)
    partition_manifest = (
        repo._runtime.scale_artifact_store.source_partition_dir(notebook_id)
        / source_partition_key(source_a)
        / "manifest.json"
    )
    payload = json.loads(partition_manifest.read_text())
    payload["parent_build_id"] = "b" * 32
    partition_manifest.write_text(json.dumps(payload))

    result = _retrieve(repo, notebook_id, source_a, version)

    assert result.capability.enabled is False
    assert result.capability.reason == "source_partition_identity_mismatch"


@pytest.mark.parametrize(
    "main_build_id, companion_build_id",
    [(None, None), ("a" * 32, None), (None, "b" * 32)],
    ids=["neither-side", "main-only", "companion-only"],
)
def test_artifacts_without_a_build_id_keep_serving_on_version_alone(
    repo, main_build_id, companion_build_id
):
    """older-index-stays-valid, end to end. A companion published before this
    key existed — or a live main manifest from before it — keeps pairing on
    ``parent_version``, so a deploy does not silently lose the capability for
    every notebook until its next full rebuild. The residual is recorded on
    ``build_generation_mismatch``: such a pair still has the original
    same-version blind spot until one build stamps both roots."""
    notebook_id, source_a, _source_b = _seed(repo)
    version = ["same-main-version"]
    _publish(repo, notebook_id, (source_a,), version, build_id=companion_build_id)
    _write_main_manifest(repo, notebook_id, version=version, build_id=main_build_id)

    result = _retrieve(repo, notebook_id, source_a, version)

    assert result.capability.enabled and result.hits


def test_a_companion_still_serves_when_no_main_manifest_is_on_disk(repo):
    """The companion root is independent of the main one on disk, and several
    deployments/tests publish it without a main artifact beside it. "No main
    manifest" must therefore mean "no id to compare", not "refuse" — the same
    fail-soft ``scale_build_id`` gives a corrupt main manifest."""
    notebook_id, source_a, _source_b = _seed(repo)
    version = ["same-main-version"]
    _publish(repo, notebook_id, (source_a,), version, build_id="a" * 32)
    assert not (
        repo._runtime.scale_artifact_store.scale_dir(notebook_id) / "manifest.json"
    ).exists()

    result = _retrieve(repo, notebook_id, source_a, version)

    assert result.capability.enabled and result.hits


def test_a_failed_companion_republish_drops_this_process_warm_entries(
    repo, monkeypatch
):
    """P1, codex PR#643 R26. The main root is published FIRST and carries a
    new ``build_id``; if the companion half then fails (a build error, a lost
    claim), the warm entries in this process describe a pair the cold gate
    would now refuse. Nothing else drops them — the cache key is
    database-derived and the disk probe watches the COMPANION root, which a
    failed publish leaves byte-identical — so the rebuild drops them on every
    exit, not only the successful one.

    Mutation anchor: move the ``invalidate_source_partition_cache`` call in
    ``_rebuild_source_partitions`` out of its ``finally`` and back onto the
    success path, and the retired pair stays warm.
    """
    notebook_id, source_a, _source_b = _seed(repo)
    version = ["same-main-version"]
    _publish(repo, notebook_id, (source_a,), version, build_id="a" * 32)
    _write_main_manifest(repo, notebook_id, version=version, build_id="a" * 32)
    service = repo._runtime.source_partitioned_ppr
    assert _retrieve(repo, notebook_id, source_a, version).capability.enabled
    assert service.cache_size == 1

    monkeypatch.setattr(
        repo._runtime.scale_artifact_store,
        "save_source_partitions",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("no disk")),
    )
    repo._runtime.scale_builder._rebuild_source_partitions(
        notebook_id,
        version,
        parent_build_id="b" * 32,
        claim_token="token",
        verify_held=lambda: True,
    )

    assert service.cache_size == 0


def test_source_provenance_drift_invalidates_cache_without_main_version_change(repo):
    notebook_id, source_a, _source_b = _seed(repo)
    version = ["same-main-version"]
    _publish(repo, notebook_id, (source_a,), version)
    service = repo._runtime.source_partitioned_ppr
    first = service.retrieve(
        notebook_id,
        [source_a],
        parent_version=version,
        object_seeds={"ko-a1": 1.0},
    )
    assert first.capability.enabled and service.cache_size == 1

    with repo._write() as db:
        db.execute(
            "UPDATE knowledge_source_fact_backfills "
            "SET updated_at='9999-12-31T23:59:59Z' "
            "WHERE notebook_id=? AND source_id=?",
            (notebook_id, source_a),
        )
    stale = service.retrieve(
        notebook_id,
        [source_a],
        parent_version=version,
        object_seeds={"ko-a1": 1.0},
    )

    assert stale.capability.reason == "source_partition_identity_mismatch"
    assert not stale.hits


def test_cross_process_companion_republish_invalidates_warm_cache(repo, monkeypatch):
    """codex PR#643 R4 P1: a cross-process publish (offline CLI, another
    replica) that republishes the companion under the SAME parent_version and
    with NO database change is invisible to the DB-derived
    ``source_subgraph_signature`` — the in-process ``invalidate()`` call the
    builder makes never fires for it either, since the builder never ran in
    THIS process. Only a disk generation probe on the companion manifest can
    catch it; without one this process would keep serving the retired CSR
    handle until incidental LRU eviction (docs/development.md:37)."""
    notebook_id, source_a, _source_b = _seed(repo)
    version = ["same-main-version"]
    _publish(repo, notebook_id, (source_a,), version)
    store = repo._runtime.scale_artifact_store
    original = store.load_source_partitions
    calls = 0

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "load_source_partitions", counted)
    service = repo._runtime.source_partitioned_ppr

    first = service.retrieve(
        notebook_id,
        [source_a],
        parent_version=version,
        object_seeds={"ko-a1": 1.0},
    )
    assert first.capability.enabled and not first.cache_hit
    assert calls == 1 and service.cache_size == 1

    # Simulate ANOTHER process republishing the companion: same parent_version,
    # same DB rows (so the DB-derived signature this process reads is bit-for-
    # bit identical), reached through the store directly — never through
    # ``service.invalidate()`` or the in-process builder.
    _publish(repo, notebook_id, (source_a,), version)

    reloaded = service.retrieve(
        notebook_id,
        [source_a],
        parent_version=version,
        object_seeds={"ko-a1": 1.0},
    )
    assert reloaded.capability.enabled
    assert not reloaded.cache_hit, (
        "stale generation served: the companion disk signature changed but "
        "the cached CSR handle was still handed back"
    )
    assert calls == 2

    # No further republish: the same identity now hits the freshly-loaded
    # entry — signature unchanged must NOT force another reload.
    again = service.retrieve(
        notebook_id,
        [source_a],
        parent_version=version,
        object_seeds={"ko-a1": 1.0},
    )
    assert again.cache_hit
    assert calls == 2


def test_companion_entry_adopted_with_an_unknown_signature_is_still_supersedable(
    repo, monkeypatch
):
    """codex #643 R5 P2 (companion mirror of the catalog fix): the stat this
    service takes before touching the cache (see ``_graph``) can itself land
    in the companion's live→``.old``→live swap gap and read ``None``, even
    though the ``load_source_partitions`` call a moment later — after the
    rename finished — opens a genuinely new, valid generation. That entry
    gets cached with an unknown recorded signature.

    Unlike a transient CURRENT-side gap (which resolves itself on the very
    next call, since the file is stable again), an unknown RECORDED signature
    never heals on its own — it stays ``None`` on the cached entry forever.
    It must still be treated as supersedable once a real signature is
    available, or a same-identity republish after this point could never be
    picked up again until process restart.

    **Mutation anchor**: reverting ``_companion_signature_superseded`` to
    ``cached_signature is not None and cached_signature != current_signature``
    (the pre-R5 guard) makes this red.
    """
    notebook_id, source_a, _source_b = _seed(repo)
    version = ["same-main-version"]
    _publish(repo, notebook_id, (source_a,), version)
    store = repo._runtime.scale_artifact_store
    real_stat = store.manifest_stat_signature
    calls = {"n": 0}

    def gapped_once(directory):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return real_stat(directory)

    monkeypatch.setattr(store, "manifest_stat_signature", gapped_once)
    service = repo._runtime.source_partitioned_ppr

    first = service.retrieve(
        notebook_id,
        [source_a],
        parent_version=version,
        object_seeds={"ko-a1": 1.0},
    )
    assert first.capability.enabled and not first.cache_hit
    assert service.cache_size == 1

    monkeypatch.setattr(store, "manifest_stat_signature", real_stat)  # the gap has passed

    # Same identity, republished again (offline CLI / another replica).
    _publish(repo, notebook_id, (source_a,), version)

    second = service.retrieve(
        notebook_id,
        [source_a],
        parent_version=version,
        object_seeds={"ko-a1": 1.0},
    )
    assert second.capability.enabled
    assert not second.cache_hit, (
        "stale generation served: an entry recorded with an unknown "
        "companion signature must still be superseded once a real "
        "signature is available, not cached forever"
    )


def _warm(service, notebook_id, source_a, version):
    """One retrieve that leaves exactly one warm cache entry behind."""
    result = service.retrieve(
        notebook_id,
        [source_a],
        parent_version=version,
        object_seeds={"ko-a1": 1.0},
    )
    assert result.capability.enabled
    assert service.cache_size == 1
    return result


def test_a_retired_companion_root_invalidates_the_warm_cache(repo):
    """P1, codex #643 R12: a same-version ``import`` whose package OMITS the
    companion RETIRES the live root (``retire_live_directory`` renames it to
    ``.old``). Nothing else moves: ``parent_version`` is unchanged by
    definition, the cache key is entirely DB-derived, and the manifest probe
    can only answer "no manifest". Reading that as the pre-existing fail-soft
    "can't tell" left the warm ``_CombinedGraph`` serving the RETIRED
    generation for the life of the process — the opposite of the
    capability-unavailable contract a missing companion carries
    (docs/development.md:37).

    **Mutation anchors**, one per half of the fix:

    1. make ``manifest_stat_signature`` answer ``None`` again for a missing
       manifest (drop the ``FileNotFoundError`` branch) → the retirement is
       indistinguishable from a transient probe failure, the fail-soft branch
       hands back the retired ``_CombinedGraph``, and the capability
       assertions below go red;
    2. drop the ``MANIFEST_ABSENT`` branch in ``_graph`` → the queried key is
       still dropped by the ordinary signature comparison, but every OTHER
       cached scope of the same notebook keeps its retired graph until it is
       separately queried; ``cache_size`` below goes red at 1.
    """
    notebook_id, source_a, source_b = _seed(repo)
    version = ["same-main-version"]
    _publish(repo, notebook_id, (source_a, source_b), version)
    store = repo._runtime.scale_artifact_store
    service = repo._runtime.source_partitioned_ppr
    _warm(service, notebook_id, source_a, version)
    # A SECOND scope of the same notebook: its own cache key, its own retired
    # graph once the root goes away.
    service.retrieve(
        notebook_id,
        [source_a, source_b],
        parent_version=version,
        object_seeds={"ko-a1": 1.0},
    )
    assert service.cache_size == 2

    # Exactly what ``import`` does for an omitted optional root — INCLUDING
    # the ``finalize_swap`` that deletes ``.old`` once the post-swap identity
    # check passed (codex PR#643 R22 P2: with ``.old`` still on disk the
    # absence is a transient swap/retire window, deliberately fail-soft; only
    # "live gone AND .old gone" is a durable retirement).
    live = store.source_partition_dir(notebook_id)
    live.rename(str(live) + ".old")
    shutil.rmtree(str(live) + ".old")

    retired = service.retrieve(
        notebook_id,
        [source_a],
        parent_version=version,
        object_seeds={"ko-a1": 1.0},
    )
    assert not retired.capability.enabled, (
        "a retired companion must degrade to capability-unavailable, not keep "
        "serving the generation that was retired"
    )
    assert retired.capability.reason == "source_partition_artifact_unavailable"
    assert service.cache_size == 0, (
        "every cached scope of this notebook describes the retired "
        "generation, so all of them must go — not just the one queried"
    )


def test_the_mid_swap_window_keeps_serving_the_warm_cache(repo):
    """codex PR#643 R22 P2: publication is two renames — ``live → .old``,
    then ``tmp → live`` — so a stat landing between them sees the root
    transiently invisible on a perfectly ordinary republish (and a
    retirement that has not reached ``finalize_swap`` yet looks the same,
    and may still be rolled back). One ENOENT with the previous generation's
    manifest sitting at ``.old`` must stay fail-soft: warm cache preserved,
    capability intact — NOT the durable-retirement eviction.

    Mutation anchor: drop the ``.old`` confirmation in
    ``_companion_signature`` (treat the first ENOENT as durable) and this
    goes red — the warm scope is evicted and the request degrades.
    """
    notebook_id, source_a, _source_b = _seed(repo)
    version = ["same-main-version"]
    _publish(repo, notebook_id, (source_a,), version)
    store = repo._runtime.scale_artifact_store
    service = repo._runtime.source_partitioned_ppr
    _warm(service, notebook_id, source_a, version)
    assert service.cache_size == 1

    # Freeze the mid-swap instant: live renamed away, .old present.
    live = store.source_partition_dir(notebook_id)
    live.rename(str(live) + ".old")

    result = service.retrieve(
        notebook_id,
        [source_a],
        parent_version=version,
        object_seeds={"ko-a1": 1.0},
    )
    assert result.capability.enabled, (
        "a transiently invisible root (.old still on disk) must keep the "
        "fail-soft path, not degrade the capability"
    )
    assert service.cache_size == 1, "the warm scope must survive the window"


def test_a_completed_swap_between_probes_is_not_read_as_retirement(
    repo, monkeypatch
):
    """codex PR#643 R23 P2: the ``.old`` confirmation itself races the
    publisher — between the live probe (ENOENT, mid-swap) and the ``.old``
    probe, the second rename AND the ``.old`` cleanup can both complete, so
    both probes miss a generation that is now live. The probe must recheck
    the live path before declaring durable absence.

    Simulated by scripting the first two stats to answer ``MANIFEST_ABSENT``
    while the root actually sits healthy on disk — exactly what that
    interleaving looks like from this thread. The recheck reads the real
    signature and the warm scope survives.

    Mutation anchor: drop the live recheck and this goes red — the warm
    scope is evicted and the request degrades.
    """
    notebook_id, source_a, _source_b = _seed(repo)
    version = ["same-main-version"]
    _publish(repo, notebook_id, (source_a,), version)
    store = repo._runtime.scale_artifact_store
    service = repo._runtime.source_partitioned_ppr
    _warm(service, notebook_id, source_a, version)
    assert service.cache_size == 1

    real = store.manifest_stat_signature
    answers = iter([MANIFEST_ABSENT, MANIFEST_ABSENT])

    def racing(directory):
        try:
            return next(answers)
        except StopIteration:
            return real(directory)

    monkeypatch.setattr(store, "manifest_stat_signature", racing)
    result = service.retrieve(
        notebook_id,
        [source_a],
        parent_version=version,
        object_seeds={"ko-a1": 1.0},
    )
    assert result.capability.enabled, (
        "a swap that completed between the two probes must not read as "
        "retirement"
    )
    assert service.cache_size == 1


def test_a_second_publication_racing_the_recheck_stays_fail_soft(
    repo, monkeypatch
):
    """codex PR#643 R25 P2: back-to-back publications can thread all three
    probes — live missed during publication A, ``.old`` missed after A's
    finalize, live missed again because publication B already renamed it
    away. The final ``.old`` look must catch B mid-swap (its ``.old`` is on
    disk) and answer "could not tell" instead of durable absence.

    Scripted: three ``MANIFEST_ABSENT`` answers, then the real probe against
    a disk frozen in B's mid-swap shape (live renamed to ``.old``).

    Mutation anchor: drop the final ``.old`` look and this goes red — the
    warm scope is evicted mid-publication.
    """
    notebook_id, source_a, _source_b = _seed(repo)
    version = ["same-main-version"]
    _publish(repo, notebook_id, (source_a,), version)
    store = repo._runtime.scale_artifact_store
    service = repo._runtime.source_partitioned_ppr
    _warm(service, notebook_id, source_a, version)

    live = store.source_partition_dir(notebook_id)
    live.rename(str(live) + ".old")
    real = store.manifest_stat_signature
    answers = iter([MANIFEST_ABSENT, MANIFEST_ABSENT, MANIFEST_ABSENT])

    def racing(directory):
        try:
            return next(answers)
        except StopIteration:
            return real(directory)

    monkeypatch.setattr(store, "manifest_stat_signature", racing)
    result = service.retrieve(
        notebook_id,
        [source_a],
        parent_version=version,
        object_seeds={"ko-a1": 1.0},
    )
    assert result.capability.enabled, (
        "a second publication racing the recheck must stay fail-soft"
    )
    assert service.cache_size == 1


def test_a_probeless_artifacts_adapter_still_serves_from_cache(repo):
    """Negative anchor for the branch above: ``None`` from
    ``_companion_signature`` because the adapter has NO ``manifest_stat_
    signature`` at all (the old test doubles this seam was built duck-typed
    for) is "can't tell", and must keep hitting the cache exactly as before.
    Only a probe that positively reports ABSENCE invalidates."""
    notebook_id, source_a, _source_b = _seed(repo)
    version = ["v1"]
    _publish(repo, notebook_id, (source_a,), version)
    store = repo._runtime.scale_artifact_store

    class _ProbelessArtifacts:
        """Everything the service uses, minus the stat probe."""

        def source_partition_dir(self, nb):
            return store.source_partition_dir(nb)

        def load_source_partitions(self, *args, **kwargs):
            return store.load_source_partitions(*args, **kwargs)

    service = SourcePartitionedPprService(
        settings=repo.settings,
        artifacts=_ProbelessArtifacts(),
        projections=repo._runtime.index_projections,
    )
    _warm(service, notebook_id, source_a, version)
    again = service.retrieve(
        notebook_id,
        [source_a],
        parent_version=version,
        object_seeds={"ko-a1": 1.0},
    )
    assert again.capability.enabled and again.cache_hit


def test_a_transient_probe_failure_still_serves_from_cache(repo, monkeypatch):
    """Negative anchor: ``None`` because THIS stat could not be completed (a
    permission error, transient I/O on a network mount) is also "can't tell"
    — the file may well be there — so the warm entry keeps serving. Only
    ``MANIFEST_ABSENT`` is a statement that the generation is gone."""
    notebook_id, source_a, _source_b = _seed(repo)
    version = ["v1"]
    _publish(repo, notebook_id, (source_a,), version)
    store = repo._runtime.scale_artifact_store
    service = repo._runtime.source_partitioned_ppr
    _warm(service, notebook_id, source_a, version)

    monkeypatch.setattr(store, "manifest_stat_signature", lambda _directory: None)
    again = service.retrieve(
        notebook_id,
        [source_a],
        parent_version=version,
        object_seeds={"ko-a1": 1.0},
    )
    assert again.capability.enabled and again.cache_hit
    assert service.cache_size == 1


def test_the_manifest_probe_separates_absence_from_an_unreadable_stat(
    repo, monkeypatch
):
    """The tri-state itself (P1, codex #643 R12): a confirmed-missing manifest
    answers ``MANIFEST_ABSENT``, while any other ``OSError`` keeps answering
    ``None``. Identity, never truthiness — both are falsy."""
    store = repo._runtime.scale_artifact_store
    missing = store.source_partition_dir("nb-never-published")
    assert store.manifest_stat_signature(missing) is MANIFEST_ABSENT

    def unreadable(_path):
        raise PermissionError("stat refused")

    monkeypatch.setattr("os.stat", unreadable)
    assert store.manifest_stat_signature(missing) is None


def test_companion_signature_is_stat_at_most_once_per_call(repo, monkeypatch):
    """T-W3's 'one load, one stat' discipline, mirrored here: the companion
    manifest stat must not be repeated inside a single ``retrieve()`` call."""
    notebook_id, source_a, _source_b = _seed(repo)
    version = ["v1"]
    _publish(repo, notebook_id, (source_a,), version)
    store = repo._runtime.scale_artifact_store
    original = store.manifest_stat_signature
    stat_calls = 0

    def counted(directory):
        nonlocal stat_calls
        stat_calls += 1
        return original(directory)

    monkeypatch.setattr(store, "manifest_stat_signature", counted)
    service = repo._runtime.source_partitioned_ppr

    service.retrieve(
        notebook_id,
        [source_a],
        parent_version=version,
        object_seeds={"ko-a1": 1.0},
    )
    assert stat_calls == 1

    stat_calls = 0
    service.retrieve(
        notebook_id,
        [source_a],
        parent_version=version,
        object_seeds={"ko-a1": 1.0},
    )
    assert stat_calls == 1


def test_zero_budget_is_zero_io_and_cold_load_is_single_flight(repo, monkeypatch):
    notebook_id, source_a, _source_b = _seed(repo)
    version = ["v1"]
    _publish(repo, notebook_id, (source_a,), version)
    store = repo._runtime.scale_artifact_store
    original = store.load_source_partitions
    calls = 0
    gate = threading.Barrier(2)

    def counted(*args, **kwargs):
        nonlocal calls
        calls += 1
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "load_source_partitions", counted)
    service = SourcePartitionedPprService(
        settings=repo.settings,
        artifacts=store,
        projections=repo._runtime.index_projections,
    )
    zero = service.retrieve(
        notebook_id,
        [source_a],
        parent_version=version,
        max_results=0,
    )
    assert zero.capability.enabled and calls == 0

    def run():
        gate.wait()
        return service.retrieve(
            notebook_id,
            [source_a],
            parent_version=version,
            object_seeds={"ko-a1": 1.0},
        )

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _value: run(), range(2)))
    assert calls == 1
    assert sorted(result.cache_hit for result in results) == [False, True]


def test_publish_omits_incomplete_source_instead_of_guessing(repo):
    notebook_id, source_a, _source_b = _seed(repo)
    rows = repo._runtime.index_projections.source_graph_partition_rows(
        notebook_id, source_a
    )
    rows["source"] = {**rows["source"], "projection_status": "incomplete"}
    manifest = repo._runtime.scale_artifact_store.save_source_partitions(
        notebook_id,
        parent_version=["v1"],
        source_ids=[source_a],
        load_rows=lambda _source_id: rows,
    )
    assert manifest == {
        "format_version": 2,
        "parent_version": ["v1"],
        # No caller-supplied build id here (this store call publishes no main
        # root), so the pair falls back to matching on version alone.
        "parent_build_id": None,
        "published_sources": 0,
        "unavailable_sources": 1,
    }
    with pytest.raises(SourcePartitionUnavailable) as error:
        repo._runtime.scale_artifact_store.load_source_partitions(
            notebook_id,
            [source_a],
            expected_parent_version=["v1"],
            expected_source_signatures=_signature_map(repo, notebook_id, [source_a]),
        )
    assert error.value.reason == "source_partition_artifact_unavailable"


def test_full_scale_rebuild_publishes_matching_companion_and_invalidates_cache(repo):
    notebook_id, source_a, source_b = _seed(repo)
    repo.settings.source_partitioned_graph_artifacts_enabled = True
    manifest = repo._runtime.scale_builder.build(notebook_id)
    service = repo._runtime.source_partitioned_ppr
    first = service.retrieve(
        notebook_id,
        [source_a],
        parent_version=manifest["version"],
        object_seeds={"ko-a1": 1.0},
    )
    assert first.capability.enabled and service.cache_size == 1

    with repo._write() as db:
        db.execute(
            "UPDATE unified_kg_state SET kg_mutation_seq=kg_mutation_seq+1 "
            "WHERE notebook_id=?",
            (notebook_id,),
        )
        db.execute(
            "UPDATE knowledge_objects SET updated_at='9999-12-31T23:59:59Z' "
            "WHERE notebook_id=? AND id='ko-a1'",
            (notebook_id,),
        )
    repo._runtime.scale_artifacts.version_memo.pop(notebook_id, None)
    rebuilt = repo._runtime.scale_builder.build(notebook_id)

    assert rebuilt["version"] != manifest["version"]
    assert service.cache_size == 0
    loaded = repo._runtime.scale_artifact_store.load_source_partitions(
        notebook_id,
        [source_a, source_b],
        expected_parent_version=rebuilt["version"],
        expected_source_signatures=_signature_map(
            repo, notebook_id, [source_a, source_b]
        ),
    )
    assert [partition.source_id for partition in loaded] == [source_a, source_b]


def test_delta_fold_republishes_companion_for_new_main_identity(repo):
    notebook_id, source_a, _source_b = _seed(repo)
    repo.settings.source_partitioned_graph_artifacts_enabled = True
    initial = repo._runtime.scale_builder.build(notebook_id)
    service = repo._runtime.source_partitioned_ppr
    assert service.retrieve(
        notebook_id,
        [source_a],
        parent_version=initial["version"],
        object_seeds={"ko-a1": 1.0},
    ).capability.enabled
    assert service.cache_size == 1

    now = repo._runtime.seams.now()
    with repo._write() as db:
        db.execute(
            "INSERT INTO sources "
            "(id,notebook_id,title,source_type,status,parse_status,file_name,"
            "file_path,file_size,file_hash,summary,doc_type,created_at,updated_at) "
            "VALUES ('src-c',?,?, 'markdown','ready','parsed','c.md','c.md',1,"
            "'hash-c','','',?,?)",
            (notebook_id, "C", now, now),
        )
        db.execute(
            "INSERT INTO chunks "
            "(id,notebook_id,source_id,text,section_path,element_ids,created_at) "
            "VALUES ('chunk-c',?,'src-c','c','c','[]',?)",
            (notebook_id, now),
        )
    repo._runtime.scale_artifacts.version_memo.pop(notebook_id, None)
    folded = repo._runtime.scale_builder.fold(notebook_id)

    assert folded["version"] != initial["version"]
    assert service.cache_size == 0
    loaded = repo._runtime.scale_artifact_store.load_source_partitions(
        notebook_id,
        [source_a],
        expected_parent_version=folded["version"],
        expected_source_signatures=_signature_map(repo, notebook_id, [source_a]),
    )
    assert loaded[0].source_id == source_a
    unavailable = service.retrieve(
        notebook_id,
        ["src-c"],
        parent_version=folded["version"],
        object_seeds={"anything": 1.0},
    )
    assert unavailable.capability.reason == "source_partition_artifact_unavailable"


def test_runtime_wiring_and_default_flags(repo, monkeypatch):
    assert isinstance(repo._runtime.source_partitioned_ppr, SourcePartitionedPprService)
    monkeypatch.delenv("SOURCE_PARTITIONED_PPR_ENABLED")
    monkeypatch.delenv("SOURCE_PARTITIONED_GRAPH_ARTIFACTS_ENABLED", raising=False)
    defaults = Settings(_env_file=None)
    assert defaults.source_partitioned_graph_artifacts_enabled is True
    assert defaults.source_partitioned_ppr_enabled is True
