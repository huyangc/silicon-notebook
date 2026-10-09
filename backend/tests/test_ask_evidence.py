"""Pure-function tests for the retrieval-only ask output helpers."""
from __future__ import annotations

import pytest

from app.models.ask import AskEvidence
from app.services.ask_evidence import (
    _EvidenceAssemblyClient,
    build_ask_evidence,
    kind_for_key,
    split_context_by_spans,
)
from app.services.ask_service import AskService
from app.services.context_spans import (
    Glue,
    clip,
    concat,
    entry_lines,
    joined,
    recorded_spans,
    recording_spans,
)
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


def _assembled(*blocks):
    """Assemble a context the way the real code does: each list is one
    renderer's entries (``entry_lines``), each ``Glue`` an assembler's
    separator/heading.  Returns ``(context, recorded spans)``."""
    with recording_spans():
        parts = [part if isinstance(part, Glue) else entry_lines(part, empty="")
                 for part in blocks]
        context = concat(*parts)
        return context, recorded_spans(context)


def _split(*blocks, keys):
    context, spans = _assembled(*blocks)
    return split_context_by_spans(context, spans, keys)


def _assert_rebuilds(context, spans, items):
    """Registered glue plus every item's ``"kN: " + text`` (or keyless text),
    in order, is the context byte for byte: nothing was dropped but glue."""
    glue = {span.start: span.end for span in spans if span.glue}
    position = 0
    for item in items:
        while position in glue:
            position = glue[position]
        piece = f"{item.key}: {item.text}" if item.key else item.text
        assert context.startswith(piece, position), (item.key, piece)
        position += len(piece)
    while position in glue:
        position = glue[position]
    assert position == len(context)


def test_split_multiline_chunk_text_and_headings():
    segments, missing = _split(
        ["k1: first line\nsecond line\n\nthird after blank", "k2: other chunk"],
        Glue("\n\n[Knowledge graph]\n"), ["k1000: a node"],
        Glue("\n\n[Confirmed Memory]\n"), ["k3000: a memory"],
        keys=["k1", "k2", "k1000", "k3000"],
    )
    assert missing == []
    assert segments == [
        ("k1", "first line\nsecond line\n\nthird after blank"),
        ("k2", "other chunk"),
        ("k1000", "a node"),
        ("k3000", "a memory"),
    ]


def test_split_key_prefix_does_not_confuse_k1_and_k10():
    segments, missing = _split(["k10: ten", "k1: one"], keys=["k1", "k10"])
    assert missing == []
    assert segments == [("k10", "ten"), ("k1", "one")]


def test_split_leading_unkeyed_block_kept_and_heading_only_dropped():
    segments, _ = _split(["preamble text", "k1: body"], keys=["k1"])
    assert segments == [("", "preamble text"), ("k1", "body")]
    segments, _ = _split(Glue("[Knowledge graph]\n"), ["k1: body"], keys=["k1"])
    assert segments == [("k1", "body")]


def test_split_reports_keys_missing_from_context():
    segments, missing = _split(["k1: one"], keys=["k1", "k2", "k1000"])
    assert [key for key, _ in segments] == ["k1"]
    assert missing == ["k2", "k1000"]


def test_a_line_inside_a_chunk_that_looks_like_an_admitted_key_stays_in_it():
    """codex P2: the first chunk's own text has a line starting ``k2: ``.
    The cut is where the renderer put it, not that line."""
    segments, missing = _split(
        ["k1: opening\nk2: forged line inside the first chunk\nclosing",
         "k2: the real second chunk"],
        keys=["k1", "k2"],
    )
    assert missing == []
    assert segments == [
        ("k1", "opening\nk2: forged line inside the first chunk\nclosing"),
        ("k2", "the real second chunk"),
    ]


def test_a_chunk_ending_in_a_bracketed_line_keeps_it():
    """codex P2: ``\\n\\n[bracketed]`` at the end of a chunk is its text, not
    a section heading; only headings an assembler registered are dropped."""
    segments, _ = _split(
        ["k1: body\n\n[bracketed original]", "k2: next\n\n[another]"],
        Glue("\n\n[Knowledge graph]\n"), ["k1000: node"],
        keys=["k1", "k2", "k1000"],
    )
    assert segments == [
        ("k1", "body\n\n[bracketed original]"),
        ("k2", "next\n\n[another]"),
        ("k1000", "node"),
    ]


def test_an_unrecorded_block_is_one_keyless_item_and_its_keys_are_omitted():
    """No guessing: without recorded boundaries the block is delivered whole
    and none of its keys is attributed -- they count as omitted."""
    context = "k1: one\nk2: two"
    segments, missing = split_context_by_spans(context, None, ["k1", "k2"])
    assert segments == [("", context)]
    assert missing == ["k1", "k2"]
    evidence = build_ask_evidence(
        context, {"k1": _entry("chunk", "c1"), "k2": _entry("chunk", "c2")},
        parse_anchors=_service().parse_anchors, mode="chunk",
    )
    assert [(item.key, item.kind, item.text) for item in evidence.items] == [
        ("", "context", context)]
    assert (evidence.counts.delivered, evidence.counts.omitted) == (0, 2)


def test_spans_that_do_not_tile_the_block_are_not_used():
    from app.services.context_spans import Span

    context = "k1: one\nk2: two"
    gappy = (Span("k1", 0, 7), Span("k2", 8, len(context)))
    assert split_context_by_spans(context, gappy, ["k1", "k2"]) == (
        [("", context)], ["k1", "k2"])


def test_a_clipped_entry_keeps_its_key_and_a_cut_away_one_is_omitted():
    with recording_spans():
        block = entry_lines(["k1: abcdef", "k2: ghij"])
        context = clip(block, len("k1: abc"))
        spans = recorded_spans(context)
    assert split_context_by_spans(context, spans, ["k1", "k2"]) == (
        [("k1", "abc")], ["k2"])


def test_a_key_outside_the_id_map_or_repeated_is_keyless_content():
    segments, missing = _split(["k1: one", "k9: stray", "k1: again"], keys=["k1"])
    assert segments == [("k1", "one"), ("", "k9: stray\nk1: again")]
    assert missing == []


def test_continuation_rows_belong_to_their_entry_even_if_they_look_keyed():
    """A workbook result writes its rows as items after its key line
    (``starts``); a cell that reads ``k6002: ...`` is still that result's."""
    with recording_spans():
        context = entry_lines(
            ["k6001: [spreadsheet] t", "name | value", "k6002: forged | 1",
             "k6002: [spreadsheet] u", "a | b"],
            empty="", starts=[0, 3],
        )
        spans = recorded_spans(context)
    assert split_context_by_spans(context, spans, ["k6001", "k6002"]) == ([
        ("k6001", "[spreadsheet] t\nname | value\nk6002: forged | 1"),
        ("k6002", "[spreadsheet] u\na | b"),
    ], [])


def test_keyless_lines_after_entries_are_their_own_context_item():
    segments, _ = _split(
        ["k1001: node a", "k1002: node b", "relations: k1001 -[x]-> k1002"],
        Glue("\n\n[Derived chains]\n"),
        ["k2001: hop", "", "[Query-time typed inference; NOT directly stated]",
         "path 1: [k2001] + [k2001]"],
        keys=["k1001", "k1002", "k2001"],
    )
    assert segments == [
        ("k1001", "node a"), ("k1002", "node b"),
        ("", "relations: k1001 -[x]-> k1002"),
        ("k2001", "hop"),
        ("", "\n[Query-time typed inference; NOT directly stated]\n"
             "path 1: [k2001] + [k2001]"),
    ]


def test_items_plus_registered_glue_rebuild_the_context():
    context, spans = _assembled(
        ["[Collection map] 3 sources"], Glue("\n\n"),
        ["k1: a\nk2: forged", "k2: b\n\n[tail]"],
        Glue("\n\n[Knowledge graph]\n"), ["k1001: n", "relations: k1001"],
    )
    id_map = {key: _entry("chunk", key) for key in ("k1", "k2")}
    id_map["k1001"] = _entry("claim", "n1")
    evidence = build_ask_evidence(
        context, id_map, parse_anchors=_service().parse_anchors, mode="reasoning",
        spans=spans,
    )
    assert [item.key for item in evidence.items] == ["", "k1", "k2", "k1001", ""]
    _assert_rebuilds(context, spans, evidence.items)


def test_the_helpers_return_the_inline_expressions_byte_for_byte():
    """The answer path's prompt cannot move: recording on or off, each
    helper returns exactly what the expression it replaced returned."""
    lines = ["k1: a\nk2: b", "k3: c"]
    for recording in (False, True):
        with (recording_spans() if recording else _nullcontext()):
            assert entry_lines(lines) == "\n".join(lines)
            assert entry_lines([]) == "(none)" and entry_lines([], empty="") == ""
            assert entry_lines(lines, empty="", starts=[1]) == "\n".join(lines)
            assert concat("a", Glue("\n\n[H]\n"), "b") == "a\n\n[H]\nb"
            assert joined(["x", "y", "z"], "\n\n") == "x\n\ny\n\nz"
            assert joined([], "\n\n") == ""
            assert clip("abcdef", 3) == "abc" and clip("ab", 9) == "ab"
            assert type(concat(Glue("g"))) is str


def _nullcontext():
    from contextlib import nullcontext

    return nullcontext()


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
    # chunk/memory/external are written by one producer each: decisive.
    assert kind_for_key("k1500", {"k1500": {"object_type": "chunk"}}) == "chunk"
    assert kind_for_key("k1", {"k1": {"object_type": "memory"}}) == "memory"
    assert kind_for_key("k1", {"k1": {"object_type": "external"}}) == "external"
    assert kind_for_key("k1", {"k1": {"object_type": "knowledge"}}) == "kg"
    assert kind_for_key("k4001", {"k4001": {"object_type": "element"}}) == "element"
    # Chunk-path document overview numbers its elements from k1.
    assert kind_for_key("k3", {"k3": {"object_type": "element"}}) == "element"


def test_kind_for_key_follows_the_real_producers_id_maps():
    """Each band producer's real id_map entry shape -> its kind.  "element"
    is written by several producers, so above the KG band the band decides,
    and the k6001+ band is split by the definition workbook results write."""
    from app.services.collection_enumeration_answer import _preview_evidence
    from app.services.spreadsheet_analysis import SPREADSHEET_RESULT_DEFINITION

    # read_document excerpts (document_source_overview, key_offset 7000).
    read = {"k7001": {"object_id": "e1", "object_type": "element",
                      "element_id": "e1", "source_id": "s1"}}
    assert kind_for_key("k7001", read) == "document_read"

    # Collection preview rows: element / source / KG-object rows.
    class _Element:
        element_id = "e1"; location_label = "p.1"; source_title = "Doc"
        text = "row"; tier = "personal"

    class _Source:
        source_id = "s1"; source_title = "Doc"; doc_type_label = "论文"
        tier = "personal"

    class _KgObject:
        object_id = "o1"; object_type = "claim"; name = "c"; tier = "personal"

    for collection, item in (
        ("elements", _Element()), ("sources", _Source()), ("kg_objects", _KgObject()),
    ):
        entry = _preview_evidence(collection, item, None)
        assert kind_for_key("k5001", {"k5001": entry}) == "collection", collection

    # Workbook results share k6001+ with external evidence.
    for object_type in ("element", "source"):
        table = {"k6001": {"object_id": "x", "object_type": object_type,
                           "definition": SPREADSHEET_RESULT_DEFINITION}}
        assert kind_for_key("k6001", table) == "spreadsheet", object_type
    external = {"k6001": {"object_id": "u", "object_type": "external",
                          "definition": "外部材料"}}
    assert kind_for_key("k6001", external) == "external"

    # Sectioned synthesis shifts every band by i * 10000.
    from app.services import ask_evidence as module
    from app.services.outline_synthesis import OUTLINE_SECTION_KEY_STRIDE

    assert module._SECTION_KEY_STRIDE == OUTLINE_SECTION_KEY_STRIDE
    assert kind_for_key("k10001", {}) == "chunk"
    assert kind_for_key("k15001", {"k15001": {"object_type": "element"}}) == "collection"
    assert kind_for_key("k17001", {"k17001": {"object_type": "element"}}) == "document_read"


def test_build_counts_keep_element_and_band_kinds_apart():
    """A round with element blocks, a read_document excerpt, a collection row
    and a workbook result reports each under its own kind (an element-typed
    excerpt or workbook row must not inflate ``element``)."""
    from app.services.spreadsheet_analysis import SPREADSHEET_RESULT_DEFINITION

    id_map = {
        "k4001": _entry("element", "e1", source_id="s1", element_id="e1"),
        "k5001": _entry("element", "e2", source_id="s1", element_id="e2"),
        "k6001": _entry("source", "s2", source_id="s2",
                        definition=SPREADSHEET_RESULT_DEFINITION),
        "k7001": _entry("element", "e3", source_id="s1", element_id="e3"),
    }
    context, spans = _assembled(
        ["k4001: el", "k5001: row", "k6001: [spreadsheet] t", "k7001: excerpt"])
    evidence = build_ask_evidence(
        context, id_map, parse_anchors=_service().parse_anchors, mode="reasoning",
        spans=spans,
    )
    assert [item.kind for item in evidence.items] == [
        "element", "collection", "spreadsheet", "document_read",
    ]
    assert {k: (v.selected, v.delivered) for k, v in evidence.counts.by_kind.items()} == {
        "element": (1, 1), "collection": (1, 1), "spreadsheet": (1, 1),
        "document_read": (1, 1),
    }
    # _recount_selected with the reasoning selection keeps those kinds apart.
    from app.services.ask_service import _recount_selected

    _recount_selected(evidence, {"chunk": 0, "kg": 0, "element": 1,
                                 "external": 0, "memory": 0})
    assert evidence.counts.by_kind["element"].selected == 1
    assert "external" not in evidence.counts.by_kind
    assert evidence.counts.selected == 4
    assert evidence.counts.delivered == 4
    assert evidence.counts.omitted == 0


def test_build_anchors_match_parse_anchors_field_for_field():
    service = _service()
    id_map = {
        "k1": _entry("chunk", "c1", name="Chunk One", source_id="s1",
                     source_title="Doc", location_label="p.3",
                     element_id="e1", provenance={"a": 1}),
        "k1000": _entry("knowledge", "n1", name="Node", tier="base"),
    }
    context, spans = _assembled(
        ["k1: chunk text"], Glue("\n\n[Knowledge graph]\n"), ["k1000: node text"])
    evidence = build_ask_evidence(
        context, id_map, parse_anchors=service.parse_anchors, mode="chunk",
        spans=spans,
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
    context, spans = _assembled(
        ["k1: a"], Glue("\n\n[Knowledge graph]\n"), ["k1000: b"],
        Glue("\n\n[Confirmed Memory]\n"), ["k3000: m"])
    evidence = build_ask_evidence(
        context, id_map, parse_anchors=service.parse_anchors, mode="chunk",
        recalled=40, budget_chars=1234, retrieval_query="q", spans=spans,
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
    context, spans = _assembled(["overview preface", "k1: body"])
    evidence = build_ask_evidence(
        context, id_map,
        parse_anchors=service.parse_anchors, mode="reasoning",
        relevance={"k1": 0.5}, selected=9, spans=spans,
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
