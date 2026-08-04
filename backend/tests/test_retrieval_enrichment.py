from types import SimpleNamespace

from app.core.config import Settings
import app.services.retrieval_enrichment as enrichment_module
from app.services.retrieval import RetrievedChunk, RetrievalSupport
from app.services.retrieval_baseline import build_retrieval_baseline_manifest
from app.services.retrieval_enrichment import (
    BaselineProtectedEnrichmentService,
    ReasoningLaneBudget,
    baseline_evicted_count,
)
from app.services.source_scope import source_scope_context
from app.services.sqlite_repository import SQLiteRepository


def _chunk(
    chunk_id: str,
    text: str,
    *,
    score: float = 0.9,
    relevance: float = 0.8,
    supports=(),
) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        source_id="source-a",
        source_title="Paper A",
        section_path="Methods",
        text=text,
        element_ids=[f"element-{chunk_id}"],
        score=score,
        relevance=relevance,
        retrieval_supports=tuple(supports),
    )


def _manifest(chunks):
    with source_scope_context(
        "nb", {"mode": "include", "source_ids": ["source-a"], "narrowed": True}
    ):
        return build_retrieval_baseline_manifest(
            notebook_id="nb",
            query="question",
            mode="reasoning",
            settings=SimpleNamespace(retrieval_top_n=20),
            candidate_chunks=chunks,
            selected_chunks=chunks,
            final_context_block="k1: baseline",
            final_id_map={
                "k1": {"object_type": "chunk", "object_id": chunks[0].chunk_id}
            },
            final_ordered_handles=("k1",),
            generation_fingerprint="scope:index",
        )


def test_off_and_zero_budget_do_not_call_provider_or_change_baseline():
    baseline = [_chunk("b1", "baseline one"), _chunk("b2", "baseline two")]
    manifest = _manifest(baseline)
    calls = []

    def provider():
        calls.append("called")
        return [_chunk("g1", "graph")]

    service = BaselineProtectedEnrichmentService()
    off = service.run(
        baseline, provider, max_enrichment_tokens=100,
        baseline_manifest=manifest, shadow_enabled=False,
    )
    zero = service.run(
        baseline, provider, max_enrichment_tokens=0,
        baseline_manifest=manifest, shadow_enabled=True,
    )

    assert calls == []
    for result in (off, zero):
        assert result.status == "off"
        assert result.visible_chunks == tuple(baseline)
        assert result.shadow_chunks == tuple(baseline)
        assert result.baseline_manifest is manifest
        assert result.baseline_manifest.manifest_hash == manifest.manifest_hash


def test_shadow_appends_graph_evidence_under_an_independent_budget():
    baseline = [_chunk("b1", "x" * 400), _chunk("b2", "y" * 400)]
    graph = [_chunk("g1", "small"), _chunk("g2", "z" * 400), _chunk("g3", "fit")]
    manifest = _manifest(baseline)

    result = BaselineProtectedEnrichmentService().run(
        baseline, lambda: graph, max_enrichment_tokens=3,
        baseline_manifest=manifest, shadow_enabled=True,
    )

    assert result.status == "completed"
    assert [row.chunk_id for row in result.visible_chunks] == ["b1", "b2"]
    assert [row.chunk_id for row in result.shadow_chunks] == ["b1", "b2", "g1", "g3"]
    assert [row.chunk_id for row in result.enrichment_chunks] == ["g1", "g3"]
    assert result.enrichment_tokens == 3
    assert result.dropped_enrichment_count == 1
    assert result.baseline_evicted_count == 0
    assert result.baseline_manifest is manifest
    assert result.baseline_manifest.manifest_hash == manifest.manifest_hash


def test_duplicate_only_merges_provenance_in_shadow_copy():
    semantic = RetrievalSupport("semantic", "chunk", "b1", 0.9)
    relation = RetrievalSupport("relation", "relation", "r1", 0.7, "verified")
    baseline = [_chunk("b1", "baseline", supports=(semantic,))]
    duplicate = _chunk(
        "b1", "graph tried to replace text", score=0.1, relevance=0.2,
        supports=(relation,),
    )

    result = BaselineProtectedEnrichmentService().run(
        baseline, lambda: [duplicate],
        max_enrichment_tokens=100, shadow_enabled=True,
    )

    visible = result.visible_chunks[0]
    shadow = result.shadow_chunks[0]
    assert visible is not baseline[0]
    assert visible.retrieval_supports == (semantic,)
    assert shadow is not baseline[0]
    assert (shadow.text, shadow.score, shadow.relevance, shadow.element_ids) == (
        "baseline", 0.9, 0.8, ["element-b1"]
    )
    assert shadow.retrieval_supports == (semantic, relation)
    assert result.duplicate_chunk_ids == ("b1",)
    assert result.enrichment_chunks == ()


def test_provider_alias_cannot_mutate_the_frozen_visible_baseline():
    baseline = [_chunk("b1", "original")]

    def mutating_provider():
        baseline[0].text = "mutated through alias"
        baseline[0].element_ids.append("fabricated")
        return [baseline[0]]

    result = BaselineProtectedEnrichmentService().run(
        baseline, mutating_provider,
        max_enrichment_tokens=100, shadow_enabled=True,
    )

    assert result.status == "completed"
    assert result.visible_chunks[0].text == "original"
    assert result.visible_chunks[0].element_ids == ["element-b1"]
    assert result.baseline_evicted_count == 0


def test_failure_and_timeout_return_the_exact_baseline_manifest():
    baseline = [_chunk("b1", "baseline")]
    manifest = _manifest(baseline)

    def failed():
        raise RuntimeError("graph unavailable")

    def timed_out():
        raise TimeoutError("graph slow")

    service = BaselineProtectedEnrichmentService()
    failure = service.run(
        baseline, failed, max_enrichment_tokens=10,
        baseline_manifest=manifest, shadow_enabled=True,
    )
    timeout = service.run(
        baseline, timed_out, max_enrichment_tokens=10,
        baseline_manifest=manifest, shadow_enabled=True,
    )

    assert failure.status == "failed"
    assert timeout.status == "timeout"
    for result in (failure, timeout):
        assert result.visible_chunks == tuple(baseline)
        assert result.shadow_chunks == tuple(baseline)
        assert result.baseline_manifest is manifest
        assert result.baseline_manifest.manifest_hash == manifest.manifest_hash


def test_malformed_provider_rows_fail_open_to_baseline():
    baseline = [_chunk("b1", "baseline")]

    result = BaselineProtectedEnrichmentService().run(
        baseline, lambda: [object()],
        max_enrichment_tokens=10, shadow_enabled=True,
    )

    assert result.status == "failed"
    assert result.visible_chunks == tuple(baseline)
    assert result.shadow_chunks == tuple(baseline)


def test_baseline_guard_detects_reordering_removal_and_content_mutation():
    baseline = [_chunk("b1", "one"), _chunk("b2", "two")]
    assert baseline_evicted_count(baseline, baseline) == 0
    assert baseline_evicted_count(baseline, list(reversed(baseline))) == 2
    assert baseline_evicted_count(baseline, baseline[:1]) == 1
    assert baseline_evicted_count(
        baseline, [_chunk("b1", "changed"), baseline[1]]
    ) == 1


def test_nonzero_baseline_guard_discards_the_entire_graph_lane(monkeypatch):
    baseline = [_chunk("b1", "one")]
    monkeypatch.setattr(
        enrichment_module,
        "_guard_shadow",
        lambda baseline_rows, proposed: (baseline_rows, 1),
    )

    result = BaselineProtectedEnrichmentService().run(
        baseline, lambda: [_chunk("g1", "graph")],
        max_enrichment_tokens=10, shadow_enabled=True,
    )

    assert result.status == "baseline_guard"
    assert result.baseline_evicted_count == 1
    assert result.shadow_chunks == tuple(baseline)
    assert result.enrichment_chunks == ()


def test_reasoning_lanes_never_borrow_each_others_steps():
    budget = ReasoningLaneBudget(baseline_limit=2, enrichment_limit=1)

    assert budget.consume_enrichment()
    assert not budget.consume_enrichment()
    assert budget.enrichment_remaining == 0
    assert budget.baseline_remaining == 2
    assert budget.consume_baseline(2)
    assert not budget.consume_baseline()
    assert budget.baseline_remaining == 0
    assert budget.enrichment_used == 1


def test_repository_runtime_owns_the_shadow_enrichment_service(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'runtime.db'}")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    repo = SQLiteRepository(Settings(_env_file=None))

    assert isinstance(
        repo._runtime.source_graph_enrichment,
        BaselineProtectedEnrichmentService,
    )
