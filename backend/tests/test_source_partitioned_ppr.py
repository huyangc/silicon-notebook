from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor

import numpy as np
import pytest

import app.services.kg.source_partition_index as partition_module
import app.repositories.source_subgraph_projection as source_projection
from app.core.config import Settings
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


def _publish(repo, notebook_id, source_ids, version):
    return repo._runtime.scale_artifact_store.save_source_partitions(
        notebook_id,
        parent_version=version,
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

    def recording(
        root, *, source_id, expected_parent_version, expected_source_signature
    ):
        opened.append(source_id)
        return original(
            root,
            source_id=source_id,
            expected_parent_version=expected_parent_version,
            expected_source_signature=expected_source_signature,
        )

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
