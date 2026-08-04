from types import SimpleNamespace

import app.services.retrieval_baseline as baseline_module

from app.models.common import Evidence
from app.services.retrieval import (
    RetrievedChunk,
    RetrievedElement,
    RetrievedKnowledge,
)
from app.services.retrieval_baseline import (
    build_retrieval_baseline_manifest,
    merge_baseline_candidates,
)
from app.services.source_scope import source_scope_context


def _knowledge(object_id: str, text: str) -> RetrievedKnowledge:
    return RetrievedKnowledge(
        object_id=object_id,
        object_type="claim",
        payload={"name": object_id, "definition": text},
        evidence=[Evidence(
            source_id="source-secret",
            source_title="Private paper",
            element_id=f"element-{object_id}",
            element_type="paragraph",
            location_label="Methods",
            quoted_span=text,
            confidence=1.0,
        )],
        score=0.8,
        relevance=0.7,
    )


def _chunk(chunk_id: str, text: str) -> RetrievedChunk:
    return RetrievedChunk(
        chunk_id=chunk_id,
        source_id="source-secret",
        source_title="Private paper",
        section_path="Methods",
        text=text,
        element_ids=[f"element-{chunk_id}"],
        score=0.9,
        relevance=0.85,
    )


def _element(element_id: str, text: str) -> RetrievedElement:
    return RetrievedElement(
        element_id=element_id,
        source_id="source-secret",
        source_title="Private paper",
        location_label="Methods",
        element_type="paragraph",
        text=text,
        score=0.75,
    )


def _build(*, chunks=None):
    knowledge = [_knowledge("ko-1", "root cause evidence")]
    chunks = chunks or [_chunk("chunk-1", "debugger execution evidence")]
    elements = [_element("element-1", "raw source evidence")]
    return build_retrieval_baseline_manifest(
        notebook_id="nb",
        query="why did the debugger select this cause?",
        mode="reasoning",
        settings=SimpleNamespace(retrieval_top_n=20, chunk_recall=50),
        candidate_knowledge=knowledge,
        candidate_chunks=chunks,
        candidate_elements=elements,
        selected_knowledge=knowledge,
        selected_chunks=chunks,
        selected_elements=elements,
        final_context_block=(
            "k1: [claim] root cause evidence\n"
            "k2: [chunk] debugger execution evidence\n"
            "k3: [source-element] raw source evidence"
        ),
        final_id_map={
            "k1": {"object_type": "claim", "object_id": "ko-1"},
            "k2": {"object_type": "chunk", "object_id": "chunk-1"},
            "k3": {"object_type": "element", "object_id": "element-1"},
        },
        final_ordered_handles=("k1", "k2", "k3"),
        final_budget_chars=2_000,
        baseline_step_usage=3,
    )


def test_manifest_exists_only_for_genuinely_narrowed_scope():
    with source_scope_context(
        "nb", {"mode": "include", "source_ids": ["source-secret"], "narrowed": True}
    ):
        assert _build() is not None

    with source_scope_context(
        "nb", {"mode": "include", "source_ids": ["source-secret"], "narrowed": False}
    ):
        assert _build() is None

    assert _build() is None


def test_manifest_is_deterministic_and_order_sensitive():
    with source_scope_context(
        "nb", {"mode": "include", "source_ids": ["source-secret"], "narrowed": True}
    ):
        first = _build(chunks=[
            _chunk("chunk-1", "first"),
            _chunk("chunk-2", "second"),
        ])
        same = _build(chunks=[
            _chunk("chunk-1", "first"),
            _chunk("chunk-2", "second"),
        ])
        reordered = _build(chunks=[
            _chunk("chunk-2", "second"),
            _chunk("chunk-1", "first"),
        ])

    assert first.manifest_hash == same.manifest_hash
    assert first.manifest_hash != reordered.manifest_hash
    assert [row.item_id for row in first.selected_chunks] == ["chunk-1"]


def test_event_payload_is_redacted_and_counts_selected_evidence():
    with source_scope_context(
        "nb", {"mode": "include", "source_ids": ["source-secret"], "narrowed": True}
    ):
        manifest = _build()

    payload = manifest.event_payload("nb", site="test")
    rendered = str(payload)

    assert payload["kind"] == "selected_source_baseline"
    assert payload["selected_knowledge"] == 1
    assert payload["selected_chunks"] == 1
    assert payload["selected_elements"] == 1
    assert payload["citation_handles"] == 3
    assert payload["comparison_ready"] is False
    assert payload["baseline_steps"] == 3
    assert "source-secret" not in rendered
    assert "root cause evidence" not in rendered
    assert "why did the debugger" not in rendered
    assert "element-ko-1" not in rendered


def test_baseline_scaffold_has_no_operational_emit_path():
    assert not hasattr(baseline_module, "emit_retrieval_baseline")


def test_manifest_capture_marks_malformed_legacy_payload_partial():
    cyclic = {}
    cyclic["self"] = cyclic
    broken = _knowledge("ko-broken", "text")
    broken.payload = cyclic

    with source_scope_context(
        "nb", {"mode": "include", "source_ids": ["source-secret"], "narrowed": True}
    ):
        manifest = build_retrieval_baseline_manifest(
            notebook_id="nb",
            query="query",
            mode="reasoning",
            candidate_knowledge=[broken],
            selected_knowledge=[broken],
        )

    assert manifest is not None
    assert manifest.capture_status == "partial"
    assert manifest.capture_error_count == 2
    assert manifest.comparison_ready is False


def test_final_selection_uses_only_handles_that_survived_prompt_assembly():
    chunks = [_chunk("chunk-1", "first"), _chunk("chunk-2", "trimmed")]
    with source_scope_context(
        "nb", {"mode": "include", "source_ids": ["source-secret"], "narrowed": True}
    ):
        manifest = build_retrieval_baseline_manifest(
            notebook_id="nb",
            query="query",
            mode="chunk",
            candidate_chunks=chunks,
            selected_chunks=chunks,
            final_context_block="k7: [chunk] first",
            final_id_map={
                "k7": {"object_type": "chunk", "object_id": "chunk-1"},
                "k8": {"object_type": "chunk", "object_id": "chunk-2"},
            },
            final_ordered_handles=("k7",),
            final_budget_chars=20,
        )

    assert [row.item_id for row in manifest.candidate_chunks] == [
        "chunk-1", "chunk-2"
    ]
    assert [row.item_id for row in manifest.selected_chunks] == ["chunk-1"]
    assert manifest.selected_chunks[0].citation_handles == ("k7",)
    assert manifest.prompt.citation_handles == ("k7",)
    assert manifest.prompt.context_chars == len("k7: [chunk] first")


def test_generation_identity_is_required_before_shadow_comparison():
    with source_scope_context(
        "nb", {"mode": "include", "source_ids": ["source-secret"], "narrowed": True}
    ):
        manifest = build_retrieval_baseline_manifest(
            notebook_id="nb",
            query="query",
            mode="chunk",
            final_context_block="",
            final_id_map={},
            final_ordered_handles=(),
            generation_fingerprint="scope-gen:index-gen",
        )

    assert manifest.capture_status == "complete"
    assert manifest.generation_fingerprint == "scope-gen:index-gen"
    assert manifest.comparison_ready is True


def test_evidence_text_cannot_fabricate_an_admitted_handle():
    chunks = [_chunk("chunk-1", "first\nk2: user-authored text"), _chunk("chunk-2", "second")]
    with source_scope_context(
        "nb", {"mode": "include", "source_ids": ["source-secret"], "narrowed": True}
    ):
        manifest = build_retrieval_baseline_manifest(
            notebook_id="nb",
            query="query",
            mode="chunk",
            selected_chunks=chunks,
            final_context_block="k1: [chunk] first\nk2: user-authored text",
            final_id_map={
                "k1": {"object_type": "chunk", "object_id": "chunk-1"},
                "k2": {"object_type": "chunk", "object_id": "chunk-2"},
            },
            final_ordered_handles=("k1",),
        )

    assert manifest.prompt.citation_handles == ("k1",)
    assert [row.item_id for row in manifest.selected_chunks] == ["chunk-1"]


def test_finalize_preserves_prior_capture_errors_and_generation():
    broken = _knowledge("ko-broken", "text")
    cyclic = {}
    cyclic["self"] = cyclic
    broken.payload = cyclic
    with source_scope_context(
        "nb", {"mode": "include", "source_ids": ["source-secret"], "narrowed": True}
    ):
        prior = build_retrieval_baseline_manifest(
            notebook_id="nb",
            query="query",
            mode="reasoning",
            candidate_knowledge=[broken],
            generation_fingerprint="scope-gen:index-gen",
        )
        final = build_retrieval_baseline_manifest(
            notebook_id="nb",
            query="query",
            mode="reasoning",
            candidate_knowledge=prior.candidate_knowledge,
            final_context_block="",
            final_id_map={},
            final_ordered_handles=(),
            prior_manifest=prior,
        )

    assert prior.capture_status == "partial"
    assert final.capture_status == "partial"
    assert final.capture_error_count == prior.capture_error_count
    assert final.generation_fingerprint == "scope-gen:index-gen"
    assert final.comparison_ready is False


def test_candidate_union_keeps_prior_order_and_adds_new_directions_once():
    prior = [_chunk("chunk-1", "first"), _chunk("chunk-2", "second")]
    additional = [_chunk("chunk-2", "new duplicate"), _chunk("chunk-3", "third")]

    merged = merge_baseline_candidates("chunk", prior, additional)

    assert [item.chunk_id for item in merged] == ["chunk-1", "chunk-2", "chunk-3"]


def test_candidate_union_preserves_missing_id_for_partial_capture():
    malformed = _chunk("", "legacy row without an id")

    merged = merge_baseline_candidates("chunk", [], [malformed])

    assert merged == [malformed]
    with source_scope_context(
        "nb", {"mode": "include", "source_ids": ["source-secret"], "narrowed": True}
    ):
        manifest = build_retrieval_baseline_manifest(
            notebook_id="nb",
            query="query",
            mode="report",
            candidate_chunks=merged,
        )
    assert manifest.capture_status == "partial"
    assert manifest.capture_error_count == 1
