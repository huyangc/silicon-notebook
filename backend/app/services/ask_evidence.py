"""Pure helpers for the retrieval-only ask output (``output="evidence"``).

The synthesis context is one string with ``kN: ...`` entries plus an
``id_map`` describing each key.  These functions cut it back into per-key
items at the boundaries recorded while it was assembled (``context_spans``)
without touching the repository, a model or the request scope, so the chunk
and the reasoning paths share one splitting rule and one anchor source.
"""
from __future__ import annotations

import re
from typing import Any, Callable, Mapping, Sequence

from app.services.context_spans import Span, recorded_spans

from app.models.ask import (
    AnswerAnchor,
    AskEvidence,
    AskEvidenceCounts,
    AskEvidenceItem,
    AskEvidenceKindCount,
)
from app.services.spreadsheet_analysis import SPREADSHEET_RESULT_DEFINITION

# Key-number bands.  They mirror ``AskService._MIX_KG_KEY_BASE`` (1000),
# ``_MEMORY_KEY_BASE`` (3000), ``_ELEMENT_KEY_BASE`` (4000),
# ``_COLLECTION_KEY_BASE`` (5000), ``_EXTERNAL_KEY_BASE`` (6000) and
# ``_DOCUMENT_READ_KEY_BASE`` (7000); ``test_ask_evidence`` pins them equal.
# They are repeated here (not imported) so this module stays a leaf that
# ``ask_service`` can import.
_KG_BASE = 1000
_MEMORY_BASE = 3000
_ELEMENT_BASE = 4000
_COLLECTION_BASE = 5000
_EXTERNAL_BASE = 6000
_DOCUMENT_READ_BASE = 7000

# Sectioned synthesis shifts every band by ``i * OUTLINE_SECTION_KEY_STRIDE``
# (``outline_synthesis``); the band is read after removing that shift, exactly
# as ``AskService._assemble_reasoning_context`` partitions its counts.
_SECTION_KEY_STRIDE = 10000

# object_types that only one producer ever writes: they name the kind outright,
# whatever band the key is in.  ``"element"`` is NOT one of them: document
# overview / ``read_document`` excerpts, collection-preview rows and workbook
# results all write it too.
_OBJECT_TYPE_KINDS = frozenset({"chunk", "memory", "external"})

# The kind whose delivered/selected counts are reported only under by_kind.
MEMORY_KIND = "memory"


def kind_for_key(key: str, id_map: Mapping[str, Mapping[str, Any]]) -> str:
    """The evidence kind of ``key``, from the fields its producer wrote.

    Producers and what they write (the rule follows them, not a guess):

    * ``evidence_context`` chunk / memory / external evidence: ``object_type``
      "chunk" / "memory" / "external" -- decisive in any band;
    * KG objects (``render_subgraph_context``): their graph type ("claim",
      "entity", ...) from k1001 in mix, or from k1 in a reasoning round with
      no chunks -- typed, below the memory band;
    * element blocks (k4001+), collection-preview rows (k5001+,
      ``collection_enumeration_answer``: "element"/"source"/graph types),
      ``read_document`` excerpts (k7001+, "element") -- the band decides,
      because their ``object_type`` names the row, not the producer;
    * workbook results (k6001+, ``spreadsheet_prompt_block``: "element" or
      "source") share the band with external evidence and are told apart by
      the ``definition`` they always write;
    * a document overview in the chunk path numbers its elements from k1
      ("element" below the KG band).
    """
    entry = id_map.get(key) or {}
    object_type = str(entry.get("object_type") or "")
    if object_type in _OBJECT_TYPE_KINDS:
        return object_type
    match = re.fullmatch(r"k(\d+)", key)
    if match is None:
        return "context"
    band = int(match.group(1)) % _SECTION_KEY_STRIDE
    if band >= _DOCUMENT_READ_BASE:
        return "document_read"
    if band >= _EXTERNAL_BASE:
        if entry.get("definition") == SPREADSHEET_RESULT_DEFINITION:
            return "spreadsheet"
        return "external"
    if band >= _COLLECTION_BASE:
        return "collection"
    if band >= _ELEMENT_BASE:
        return "element"
    if band >= _MEMORY_BASE:
        return "memory"
    if band >= _KG_BASE:
        return "kg"
    if object_type == "element":
        return "element"
    # Below the KG band only an untyped entry is a chunk: a reasoning round
    # with no chunks numbers its KG objects from k1.
    return "kg" if object_type else "chunk"


def split_context_by_spans(
    context_block: str,
    spans: "Sequence[Span] | None",
    ordered_keys: Sequence[str],
) -> tuple[list[tuple[str, str]], list[str]]:
    """Cut ``context_block`` into ``(key, text)`` segments at the boundaries
    its assemblers recorded (``context_spans``), never at boundaries found in
    the text.

    A keyed span becomes ``(key, text)`` with the renderer's ``"kN: "`` head
    removed; its key must be one of ``ordered_keys`` (the id_map) and seen for
    the first time, otherwise the span is delivered as keyless content.
    Keyless content becomes ``("", text)``; neighbouring keyless pieces with
    only whitespace glue between them are one segment.  Glue an assembler
    registered (separators, section headings) is dropped -- nothing else is.
    Without a complete span record the whole block is one keyless segment.
    Keys of ``ordered_keys`` without a segment of their own are returned in
    the second list: they were budgeted out, or cannot be attributed.
    """
    wanted = set(ordered_keys)
    if not _covers(spans, len(context_block)):
        spans = (Span("", 0, len(context_block)),) if context_block else ()
    segments: list[tuple[str, str]] = []
    seen: set[str] = set()
    pending_glue = ""
    joinable = False
    for span in spans or ():
        piece = context_block[span.start:span.end]
        if span.glue:
            pending_glue += piece
            continue
        key = span.key if (
            span.key in wanted and span.key not in seen
            and piece.startswith(f"{span.key}:")
        ) else ""
        if key:
            seen.add(key)
            head = len(key) + (2 if piece.startswith(f"{key}: ") else 1)
            segments.append((key, piece[head:]))
            joinable = False
        elif joinable and not pending_glue.strip():
            segments[-1] = ("", segments[-1][1] + pending_glue + piece)
        else:
            segments.append(("", piece))
            joinable = True
        pending_glue = ""
    segments = [(key, text) for key, text in segments if key or text.strip()]
    return segments, [key for key in dict.fromkeys(ordered_keys) if key not in seen]


def _covers(spans: "Sequence[Span] | None", length: int) -> bool:
    """Whether ``spans`` tile ``[0, length)`` in order with no gap."""
    if not spans:
        return False
    position = 0
    for span in spans:
        if span.start != position or span.end < span.start:
            return False
        position = span.end
    return position == length


def build_ask_evidence(
    context_block: str,
    id_map: Mapping[str, Mapping[str, Any]],
    *,
    parse_anchors: Callable[[str, Mapping[str, Mapping[str, Any]]], list[AnswerAnchor]],
    mode: str,
    evidence_kind: str = "retrieval",
    relevance: Mapping[str, float] | None = None,
    recalled: int = 0,
    selected: int | None = None,
    budget_chars: int = 0,
    spans: "Sequence[Span] | None" = None,
    **fields: Any,
) -> AskEvidence:
    """Assemble an ``AskEvidence`` from the synthesis context and its id_map.

    ``parse_anchors`` is ``EvidenceContextService.parse_anchors`` (injected to
    avoid an import cycle); anchors are built from ``"[k1][k2]..."`` so every
    handle field comes from the same code the answer mode uses.  ``selected``
    defaults to delivered + omitted (non-memory).  Totals exclude memory;
    memory is reported only in ``counts.by_kind``.  ``fields`` carries the
    remaining ``AskEvidence`` fields (``retrieval_query``, ``intent``, ...).
    The boundaries are the ones recorded while ``context_block`` was
    assembled (``context_spans.recorded_spans``); ``spans`` overrides them.
    """
    if spans is None:
        spans = recorded_spans(context_block)
    segments, missing = split_context_by_spans(context_block, spans, list(id_map))
    anchors = {
        anchor.key: anchor
        for anchor in parse_anchors(
            "".join(f"[{key}]" for key, _ in segments if key), id_map
        )
    }
    relevance = relevance or {}
    items: list[AskEvidenceItem] = []
    by_kind: dict[str, AskEvidenceKindCount] = {}
    for key, text in segments:
        kind = kind_for_key(key, id_map) if key else "context"
        items.append(AskEvidenceItem(
            key=key,
            kind=kind,  # type: ignore[arg-type]
            text=text,
            anchor=anchors.get(key),
            relevance=relevance.get(key),
        ))
        if key:
            bucket = by_kind.setdefault(kind, AskEvidenceKindCount())
            bucket.delivered += 1
            bucket.selected += 1
    omitted_non_memory = 0
    for key in missing:
        kind = kind_for_key(key, id_map)
        by_kind.setdefault(kind, AskEvidenceKindCount()).selected += 1
        if kind != MEMORY_KIND:
            omitted_non_memory += 1
    delivered = sum(
        count.delivered for kind, count in by_kind.items() if kind != MEMORY_KIND
    )
    selected_total = delivered + omitted_non_memory if selected is None else selected
    counts = AskEvidenceCounts(
        recalled=recalled,
        selected=selected_total,
        delivered=delivered,
        omitted=omitted_non_memory,
        by_kind=by_kind,
    )
    return AskEvidence(
        mode=mode,
        evidence_kind=evidence_kind,  # type: ignore[arg-type]
        items=items,
        counts=counts,
        budget_chars=budget_chars,
        context_chars=len(context_block),
        **fields,
    )


class _EvidenceAssemblyClient:
    """Stand-in answer client for the evidence path.

    The reasoning structured-evidence assembly only reads ``.configured`` (and
    ``.model``) off the answer client as a gate; it must never call a model on
    the evidence path, so ``chat_json`` fails loudly instead.
    """

    configured = True
    model = ""

    def chat_json(self, *args: Any, **kwargs: Any) -> Any:
        raise RuntimeError("evidence output never calls the answer model")
