"""Pure-function tests for the retrieval-only ask output helpers."""
from __future__ import annotations

import pytest

from app.models.ask import AskEvidence
from app.services.ask_evidence import (
    _EvidenceAssemblyClient,
    build_ask_evidence,
    kind_for_key,
    split_context_by_keys,
)
from app.services.ask_service import AskService
from app.services.evidence_context import EvidenceContextService


def _service() -> EvidenceContextService:
    service = object.__new__(EvidenceContextService)
    service.citation_source_info = lambda ids: {}  # type: ignore[method-assign]
    return service


def _entry(object_type: str, object_id: str, **extra) -> dict:
    return {
        "object_id": object_id,
        "object_type": object_type,
        "name": extra.pop("name", object_id),
        "source_id": extra.pop("source_id", ""),
        "tier": extra.pop("tier", "personal"),
        **extra,
    }


def test_band_constants_match_ask_service():
    from app.services import ask_evidence as module

    assert module._KG_BASE == AskService._MIX_KG_KEY_BASE
    assert module._MEMORY_BASE == AskService._MEMORY_KEY_BASE
    assert module._ELEMENT_BASE == AskService._ELEMENT_KEY_BASE
    assert module._COLLECTION_BASE == AskService._COLLECTION_KEY_BASE
    assert module._EXTERNAL_BASE == AskService._EXTERNAL_KEY_BASE
    assert module._DOCUMENT_READ_BASE == AskService._DOCUMENT_READ_KEY_BASE


def test_split_multiline_chunk_text_and_headings():
    context = (
        "k1: first line\nsecond line\n\nthird after blank\n"
        "k2: other chunk"
        "\n\n[Knowledge graph]\nk1000: a node"
        "\n\n[Confirmed Memory]\nk3000: a memory"
    )
    segments, missing = split_context_by_keys(
        context, ["k1", "k2", "k1000", "k3000"]
    )
    assert missing == []
    assert segments == [
        ("k1", "first line\nsecond line\n\nthird after blank"),
        ("k2", "other chunk"),
        ("k1000", "a node"),
        ("k3000", "a memory"),
    ]


def test_split_key_prefix_does_not_confuse_k1_and_k10():
    segments, missing = split_context_by_keys("k10: ten\nk1: one", ["k1", "k10"])
    assert missing == []
    assert segments == [("k10", "ten"), ("k1", "one")]


def test_split_leading_unkeyed_block_kept_and_heading_only_dropped():
    segments, _ = split_context_by_keys(
        "preamble text\nk1: body", ["k1"]
    )
    assert segments == [("", "preamble text"), ("k1", "body")]
    segments, _ = split_context_by_keys("[Knowledge graph]\nk1: body", ["k1"])
    assert segments == [("k1", "body")]


def test_split_reports_keys_missing_from_context():
    segments, missing = split_context_by_keys("k1: one", ["k1", "k2", "k1000"])
    assert [key for key, _ in segments] == ["k1"]
    assert missing == ["k2", "k1000"]


def test_kind_for_key_bands_and_object_type_priority():
    empty: dict = {}
    assert kind_for_key("k12", empty) == "chunk"
    assert kind_for_key("k1000", empty) == "kg"
    assert kind_for_key("k3001", empty) == "memory"
    assert kind_for_key("k4002", empty) == "element"
    assert kind_for_key("k5003", empty) == "collection"
    assert kind_for_key("k6004", empty) == "external"
    assert kind_for_key("k7005", empty) == "document_read"
    assert kind_for_key("weird", empty) == "context"
    # A KG-only reasoning round numbers its objects from k1.
    assert kind_for_key("k1", {"k1": {"object_type": "claim"}}) == "kg"
    assert kind_for_key("k1", {"k1": {"object_type": "chunk"}}) == "chunk"
    # object_type wins for chunk/memory/external/element; any other typed
    # entry below the KG band is a KG object.
    assert kind_for_key("k1500", {"k1500": {"object_type": "chunk"}}) == "chunk"
    assert kind_for_key("k1", {"k1": {"object_type": "memory"}}) == "memory"
    assert kind_for_key("k1", {"k1": {"object_type": "external"}}) == "external"
    assert kind_for_key("k1", {"k1": {"object_type": "knowledge"}}) == "kg"
    assert kind_for_key("k4001", {"k4001": {"object_type": "element"}}) == "element"


def test_build_anchors_match_parse_anchors_field_for_field():
    service = _service()
    id_map = {
        "k1": _entry("chunk", "c1", name="Chunk One", source_id="s1",
                     source_title="Doc", location_label="p.3",
                     element_id="e1", provenance={"a": 1}),
        "k1000": _entry("knowledge", "n1", name="Node", tier="base"),
    }
    context = "k1: chunk text\n\n[Knowledge graph]\nk1000: node text"
    evidence = build_ask_evidence(
        context, id_map, parse_anchors=service.parse_anchors, mode="chunk",
    )
    expected = {a.key: a for a in service.parse_anchors("[k1][k1000]", id_map)}
    assert [i.key for i in evidence.items] == ["k1", "k1000"]
    for item in evidence.items:
        assert item.anchor is not None
        assert item.anchor.model_dump() == expected[item.key].model_dump()
    assert [i.kind for i in evidence.items] == ["chunk", "kg"]
    assert evidence.context_chars == len(context)


def test_build_counts_omitted_and_memory_excluded_from_totals():
    service = _service()
    id_map = {
        "k1": _entry("chunk", "c1"),
        "k2": _entry("chunk", "c2"),          # budgeted out of the context
        "k1000": _entry("knowledge", "n1"),
        "k3000": _entry("memory", "m1"),
        "k3001": _entry("memory", "m2"),      # budgeted out of the context
    }
    context = "k1: a\n\n[Knowledge graph]\nk1000: b\n\n[Confirmed Memory]\nk3000: m"
    evidence = build_ask_evidence(
        context, id_map, parse_anchors=service.parse_anchors, mode="chunk",
        recalled=40, budget_chars=1234, retrieval_query="q",
    )
    counts = evidence.counts
    assert (counts.recalled, counts.selected, counts.delivered, counts.omitted) == (
        40, 3, 2, 1,
    )
    assert counts.by_kind["chunk"].model_dump() == {"selected": 2, "delivered": 1}
    assert counts.by_kind["kg"].model_dump() == {"selected": 1, "delivered": 1}
    assert counts.by_kind["memory"].model_dump() == {"selected": 2, "delivered": 1}
    assert [i.kind for i in evidence.items] == ["chunk", "kg", "memory"]
    assert evidence.budget_chars == 1234
    assert evidence.retrieval_query == "q"


def test_build_relevance_selected_override_and_unkeyed_item():
    service = _service()
    id_map = {"k1": _entry("chunk", "c1")}
    evidence = build_ask_evidence(
        "overview preface\nk1: body", id_map,
        parse_anchors=service.parse_anchors, mode="reasoning",
        relevance={"k1": 0.5}, selected=9,
    )
    assert [(i.key, i.kind) for i in evidence.items] == [
        ("", "context"), ("k1", "chunk"),
    ]
    assert evidence.items[0].anchor is None
    assert evidence.items[1].relevance == 0.5
    assert evidence.counts.selected == 9
    assert evidence.counts.delivered == 1
    assert isinstance(evidence, AskEvidence)


def test_assembly_client_gates_configured_but_never_calls_a_model():
    client = _EvidenceAssemblyClient()
    assert client.configured is True
    assert client.model == ""
    with pytest.raises(RuntimeError):
        client.chat_json("system", "user")
