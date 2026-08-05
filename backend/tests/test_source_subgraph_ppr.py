from __future__ import annotations

import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import numpy as np
import pytest

import app.services.source_subgraph_ppr as ppr_module
from app.core.config import Settings
from app.services.retrieval import RetrievedChunk
from app.services.retrieval_enrichment import BaselineProtectedEnrichmentService
from app.services.source_subgraph import (
    SourceSubgraphChunk,
    SourceSubgraphNode,
    SourceSubgraphRelation,
)
from app.services.source_subgraph_ppr import (
    SourceSubgraphPprLimits,
    SourceSubgraphPprService,
)
from app.services.sqlite_repository import SQLiteRepository
from tests.test_source_subgraph import _seed


@pytest.fixture
def repo(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'ppr.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("SOURCE_SUBGRAPH_PPR_ENABLED", "true")
    return SQLiteRepository(Settings(_env_file=None))


def _snapshot(repo):
    notebook_id, source_a, source_b = _seed(repo)
    return (
        repo._runtime.source_subgraphs.snapshot(notebook_id, [source_a]),
        source_b,
    )


def _polluted(snapshot, source_b):
    foreign_node = SourceSubgraphNode(
        object_id="ko-b-foreign",
        object_type="concept",
        name="B foreign",
        source_ids=(source_b,),
    )
    foreign_chunk = SourceSubgraphChunk(
        chunk_id="chunk-b-foreign",
        source_id=source_b,
        text="foreign evidence",
        section_path="B",
        created_at="now",
        element_ids=("el-b-foreign",),
    )
    foreign_relation = SourceSubgraphRelation(
        relation_id="rel-b-foreign",
        source_id=source_b,
        source_object_id="ko-b-foreign",
        target_object_id="ko-a1",
        edge_type="part_of",
        evidence=({"source_id": source_b},),
    )
    return replace(
        snapshot,
        nodes=(*snapshot.nodes, foreign_node),
        chunks=(*snapshot.chunks, foreign_chunk),
        relations=(*snapshot.relations, foreign_relation),
        memberships=(
            *snapshot.memberships,
            ("ko-b-foreign", "chunk-b-foreign"),
        ),
        cluster_memberships=(
            *snapshot.cluster_memberships,
            ("canon-a", "ko-b-foreign"),
        ),
    )


def test_foreign_b_c_rows_cannot_change_a_transition_reset_or_ranking(repo):
    snapshot, source_b = _snapshot(repo)
    polluted = _polluted(snapshot, source_b)
    clean_service = SourceSubgraphPprService(settings=repo.settings)
    polluted_service = SourceSubgraphPprService(settings=repo.settings)

    clean = clean_service.retrieve(
        snapshot, object_seeds={"ko-a1": 1.0}, max_results=20
    )
    dirty = polluted_service.retrieve(
        polluted,
        object_seeds={"ko-a1": 1.0, "ko-b-foreign": 1_000_000.0},
        chunk_seeds={"chunk-b-foreign": 1_000_000.0},
        max_results=20,
    )
    clean_graph, _ = clean_service._graph(snapshot)
    dirty_graph, _ = polluted_service._graph(polluted)

    assert clean.capability.enabled and dirty.capability.enabled
    assert clean.node_count == dirty.node_count
    assert clean.logical_edge_count == dirty.logical_edge_count
    assert clean_graph.node_index == dirty_graph.node_index
    np.testing.assert_array_equal(
        clean_graph.transition.toarray(), dirty_graph.transition.toarray()
    )
    assert [(hit.chunk.chunk_id, hit.score) for hit in clean.hits] == [
        (hit.chunk.chunk_id, hit.score) for hit in dirty.hits
    ]
    assert {hit.chunk.source_id for hit in dirty.hits} == {"src-a"}


def test_relation_evidence_is_revalidated_inside_the_ppr_producer(repo):
    snapshot, source_b = _snapshot(repo)
    extra_node = SourceSubgraphNode(
        object_id="ko-a3",
        object_type="concept",
        name="A three",
        source_ids=("src-a",),
    )
    safe_shape = replace(snapshot, nodes=(*snapshot.nodes, extra_node))
    polluted = replace(
        safe_shape,
        relations=(
            *safe_shape.relations,
            SourceSubgraphRelation(
                relation_id="rel-evidence-pollution",
                source_id="src-a",
                source_object_id="ko-a1",
                target_object_id="ko-a3",
                edge_type="part_of",
                evidence=({"source_id": source_b},),
            ),
        ),
    )
    clean_service = SourceSubgraphPprService(settings=repo.settings)
    polluted_service = SourceSubgraphPprService(settings=repo.settings)

    clean_graph, _ = clean_service._graph(safe_shape)
    polluted_graph, _ = polluted_service._graph(polluted)

    assert clean_graph.logical_edge_count == polluted_graph.logical_edge_count
    np.testing.assert_array_equal(
        clean_graph.transition.toarray(), polluted_graph.transition.toarray()
    )


def test_scope_generation_cache_is_bounded_and_invalidates(repo):
    snapshot, _source_b = _snapshot(repo)
    service = SourceSubgraphPprService(
        settings=repo.settings,
        limits=SourceSubgraphPprLimits(cache_entries=2),
    )

    first = service.retrieve(snapshot, object_seeds={"ko-a1": 1.0})
    second = service.retrieve(snapshot, object_seeds={"ko-a1": 1.0})
    changed = replace(snapshot, kg_generation=snapshot.kg_generation + 1)
    third = service.retrieve(changed, object_seeds={"ko-a1": 1.0})
    another = replace(
        snapshot,
        kg_generation=snapshot.kg_generation + 2,
        cluster_generation=snapshot.cluster_generation + 1,
    )
    real_build = service._build
    retained_during_build = []

    def observing_build(value):
        retained_during_build.append(service.cache_size)
        return real_build(value)

    service._build = observing_build
    service.retrieve(another, object_seeds={"ko-a1": 1.0})

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert third.cache_hit is False
    assert service.cache_size == 2
    assert retained_during_build == [1]


def test_cold_transition_build_is_single_flight(repo, monkeypatch):
    snapshot, _source_b = _snapshot(repo)
    service = SourceSubgraphPprService(settings=repo.settings)
    real_build = ppr_module.build_transition
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def controlled_build(*args, **kwargs):
        calls.append("build")
        entered.set()
        assert release.wait(timeout=2)
        return real_build(*args, **kwargs)

    monkeypatch.setattr(ppr_module, "build_transition", controlled_build)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(service.retrieve, snapshot, object_seeds={"ko-a1": 1.0})
        assert entered.wait(timeout=2)
        second = pool.submit(service.retrieve, snapshot, object_seeds={"ko-a1": 1.0})
        release.set()
        results = (first.result(), second.result())

    assert calls == ["build"]
    assert sorted(result.cache_hit for result in results) == [False, True]


@pytest.mark.parametrize(
    ("limits", "reason"),
    [
        (SourceSubgraphPprLimits(max_nodes=2), "ppr_node_limit_exceeded"),
        (SourceSubgraphPprLimits(max_logical_edges=1), "ppr_edge_limit_exceeded"),
    ],
)
def test_online_graph_rails_fail_closed_with_explicit_reason(repo, limits, reason):
    snapshot, _source_b = _snapshot(repo)

    result = SourceSubgraphPprService(settings=repo.settings, limits=limits).retrieve(
        snapshot, object_seeds={"ko-a1": 1.0}
    )

    assert not result.capability.enabled
    assert result.capability.reason == reason
    assert result.hits == ()


def test_build_and_run_failures_are_local_capability_failures(repo, monkeypatch):
    snapshot, _source_b = _snapshot(repo)
    service = SourceSubgraphPprService(settings=repo.settings)
    monkeypatch.setattr(
        ppr_module,
        "build_transition",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("build")),
    )
    build_failed = service.retrieve(snapshot, object_seeds={"ko-a1": 1.0})
    assert build_failed.capability.reason == "ppr_build_failed"

    monkeypatch.undo()
    service = SourceSubgraphPprService(settings=repo.settings)
    monkeypatch.setattr(
        ppr_module,
        "personalized_ppr",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("run")),
    )
    run_failed = service.retrieve(snapshot, object_seeds={"ko-a1": 1.0})
    assert run_failed.capability.reason == "ppr_run_failed"
    assert run_failed.hits == ()


def test_empty_or_foreign_reset_returns_no_hits_without_disabling_graph(repo):
    snapshot, _source_b = _snapshot(repo)
    service = SourceSubgraphPprService(settings=repo.settings)

    result = service.retrieve(
        snapshot,
        object_seeds={"ko-foreign": 999.0},
        chunk_seeds={"chunk-foreign": "not-a-number"},
    )

    assert result.capability.enabled
    assert result.hits == ()
    assert result.iterations == 0


def test_disabled_or_zero_result_budget_is_zero_build(repo, monkeypatch):
    snapshot, _source_b = _snapshot(repo)
    service = SourceSubgraphPprService(settings=repo.settings)
    monkeypatch.setattr(
        service,
        "_graph",
        lambda *_args: pytest.fail("disabled scoped PPR built a transition"),
    )

    repo.settings.source_subgraph_ppr_enabled = False
    disabled = service.retrieve(snapshot, object_seeds={"ko-a1": 1.0})
    repo.settings.source_subgraph_ppr_enabled = True
    zero = service.retrieve(snapshot, object_seeds={"ko-a1": 1.0}, max_results=0)

    assert disabled.capability.reason == "ppr_disabled"
    assert zero.capability.enabled
    assert disabled.hits == zero.hits == ()


def test_ppr_ranks_only_g_and_protected_lane_keeps_b_unchanged(repo):
    snapshot, _source_b = _snapshot(repo)
    baseline = RetrievedChunk(
        chunk_id="baseline",
        source_id="src-a",
        source_title="A",
        section_path="baseline",
        text="historical baseline",
        element_ids=["el-baseline"],
        score=0.95,
        relevance=0.9,
    )
    ppr = SourceSubgraphPprService(settings=repo.settings).retrieve(
        snapshot, object_seeds={"ko-a1": 1.0}
    )

    def provider():
        return [
            RetrievedChunk(
                chunk_id=hit.chunk.chunk_id,
                source_id=hit.chunk.source_id,
                source_title="",
                section_path=hit.chunk.section_path,
                text=hit.chunk.text,
                element_ids=list(hit.chunk.element_ids),
                score=hit.score,
                relevance=hit.score,
                retrieval_supports=(hit.support,),
            )
            for hit in ppr.hits
        ]

    protected = BaselineProtectedEnrichmentService().run(
        [baseline],
        provider,
        max_enrichment_tokens=100,
        shadow_enabled=True,
    )

    assert ppr.hits
    assert protected.status == "completed"
    assert protected.visible_chunks[0].chunk_id == "baseline"
    assert protected.visible_chunks[0].score == 0.95
    assert protected.visible_chunks[0].relevance == 0.9
    assert protected.shadow_chunks[0].chunk_id == "baseline"
    assert all(chunk.chunk_id != "baseline" for chunk in protected.enrichment_chunks)


def test_repository_runtime_owns_scoped_ppr_service(repo):
    assert isinstance(repo._runtime.source_subgraph_ppr, SourceSubgraphPprService)


def test_scoped_ppr_has_an_independent_rollback_gate():
    assert Settings(_env_file=None).source_subgraph_ppr_enabled is True
    settings = Settings(_env_file=None, SOURCE_SUBGRAPH_PPR_ENABLED=False)
    assert settings.source_subgraph_ppr_enabled is False
