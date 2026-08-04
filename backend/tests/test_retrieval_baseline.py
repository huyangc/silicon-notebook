from types import SimpleNamespace

from app.models.common import Evidence
from app.services.retrieval import (
    RetrievedChunk,
    RetrievedElement,
    RetrievedKnowledge,
)
from app.services.retrieval_baseline import (
    build_retrieval_baseline_manifest,
    emit_retrieval_baseline,
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
    assert [row.item_id for row in first.selected_chunks] == ["chunk-1", "chunk-2"]


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
    assert payload["baseline_steps"] == 3
    assert "source-secret" not in rendered
    assert "root cause evidence" not in rendered
    assert "why did the debugger" not in rendered
    assert "element-ko-1" not in rendered


def test_emit_is_fail_soft():
    class BrokenLog:
        logger = None

        def emit(self, _event):
            raise RuntimeError("logging unavailable")

    with source_scope_context(
        "nb", {"mode": "include", "source_ids": ["source-secret"], "narrowed": True}
    ):
        manifest = _build()

    emit_retrieval_baseline(BrokenLog(), manifest, "nb", site="test")


def test_manifest_capture_is_fail_soft_for_malformed_legacy_payload():
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

    assert manifest is None
