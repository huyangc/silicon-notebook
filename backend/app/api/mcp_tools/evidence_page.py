"""The one response of ``ask(output="evidence")``: retrieval only, no synthesis.

``ask`` hands back the context the synthesis step would have been given, sized
by the synthesis budget rather than the 12,000-byte rail every other response
keeps (``EVIDENCE_TOTAL_TEXT_LIMIT`` is the hard cap), in one call and never
paged. Each item carries the same handle fields an answer's anchors carry, plus
the ``ref`` ``read_reference`` resolves (a Memory item's ``mem`` ref, a source
element's ``el`` ref).
"""
from __future__ import annotations

import json
from typing import Any, Callable

from app.models.ask import AskEvidence

from ._shared import (
    EVIDENCE_ITEM_OVERHEAD_BYTES,
    EVIDENCE_TOTAL_TEXT_LIMIT,
    RESULT_LIMIT,
    TOTAL_TEXT_LIMIT,
    _budget_response,
    _serialized_size,
)
from .ask_pages import _intent_summary
from .refs import element_ref, memory_ref

# Headroom the pre-trim leaves for what packing adds after it measured: the
# ``truncation`` block, an ellipsis per cut text, rounding of the ratio.
_EVIDENCE_PRETRIM_SLACK_BYTES = 4_096


def _is_memory_evidence(item: Any) -> bool:
    anchor = item.anchor
    return item.kind == "memory" or (
        anchor is not None and anchor.object_type == "memory"
    )


def _evidence_row(
    item: Any, notebook_id: str, anchor_row: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    """One evidence item: the synthesis text plus the anchor's resolvable handle.

    The handle fields come from ``anchor_row`` -- ``ask._anchor_row``, the
    builder the answer page uses -- so a key's handle is identical in both
    outputs; the ``ref`` is built the way an answer citation's is. Empty
    optional keys are omitted: every key here spends response budget on
    every item.
    """
    row: dict[str, Any] = {"key": item.key, "kind": item.kind, "text": item.text}
    anchor = item.anchor
    if anchor is not None:
        row.update(anchor_row(anchor))
        memory_id = anchor.object_id if anchor.object_type == "memory" else ""
        ref = (
            memory_ref(notebook_id, memory_id) if memory_id
            else element_ref(notebook_id, anchor.source_id, anchor.element_id)
        )
        if ref:
            row["ref"] = ref
        if anchor.notebook_id:
            row["notebook_id"] = anchor.notebook_id
        if memory_id:
            row["memory_id"] = memory_id
        if anchor.source_file_name:
            row["source_file_name"] = anchor.source_file_name
    if item.relevance is not None:
        row["relevance"] = item.relevance
    return row


def evidence_page(
    notebook_id: str, evidence: AskEvidence, allow_memory: bool,
    anchor_row: Callable[[Any], dict[str, Any]],
) -> dict[str, Any]:
    """``ask(output="evidence")``'s response.

    Memory is filtered BEFORE counting and truncation: the response must
    carry no trace of how many Memory rows the token could not see. The
    server totals already exclude Memory, so only ``by_kind.memory`` needs to
    go.
    """
    items = [
        item for item in evidence.items
        if allow_memory or not _is_memory_evidence(item)
    ]
    counts = evidence.counts.model_dump()
    if not allow_memory:
        counts["by_kind"] = {
            kind: value for kind, value in counts["by_kind"].items()
            if kind != "memory"
        }
    text_chars = max(
        [evidence.budget_chars, evidence.context_chars]
        + [len(item.text) for item in items]
    )
    total = min(
        EVIDENCE_TOTAL_TEXT_LIMIT,
        TOTAL_TEXT_LIMIT + 3 * text_chars
        + EVIDENCE_ITEM_OVERHEAD_BYTES * len(items),
    )
    rows = [_evidence_row(item, notebook_id, anchor_row) for item in items]
    intent = _intent_summary(evidence.intent)
    payload = {
        "notebook_id": notebook_id,
        "status": "retrieved",
        "output": "evidence",
        "mode": evidence.mode,
        "evidence_kind": evidence.evidence_kind,
        "conversation_id": evidence.conversation_id,
        "retrieval_query": evidence.retrieval_query,
        "counts": counts,
        "budget": {
            "budget_chars": evidence.budget_chars,
            "context_chars": evidence.context_chars,
        },
        "content_is_untrusted_evidence": True,
        "items": rows,
        "notice": evidence.notice,
        "skipped_libraries": [
            {"notebook_id": row.notebook_id, "name": row.name}
            for row in evidence.skipped_libraries
        ],
        "index_required": evidence.index_required,
        **({"intent": intent} if intent is not None else {}),
    }
    return _budget_response(
        payload, initial_omitted_characters=_pretrim_evidence_text(
            payload, rows, total),
        field_limits={"text": text_chars, "object_type": 100, "kind": 40,
                      "label": 300, "source_title": 300, "location_label": 300,
                      "source_file_name": 300, "notice": 1_000,
                      "retrieval_query": 1_000, "resolved_question": 1_000,
                      "entities": 200, "assumptions": 300, "constraints": 300,
                      "excluded_topics": 300, "question": 500},
        anchor_provenance_budget_chars=500, provenance_list_key="items",
        intent_budget_chars=1_500, total_budget_bytes=total,
        list_limit=max(RESULT_LIMIT, len(items)))


def _pretrim_evidence_text(
    payload: dict[str, Any], rows: list[dict[str, Any]], total: int
) -> int:
    """Cut every item's ``text`` by one common ratio so the payload lands
    near ``total`` in a single pass, and return the characters removed.

    ``_budget_response``'s convergence loop re-serializes the whole response
    and halves one string per step: on a payload far above the 524,288-byte
    cap that is thousands of half-megabyte dumps.  One proportional cut first
    leaves the loop a few steps of residue.  Every cut is reported (the caller
    passes the count as ``initial_omitted_characters``) and ends in the same
    "…" the loop writes.  Within budget nothing is touched.
    """
    excess = _serialized_size(payload) - total
    if excess <= 0:
        return 0
    sizes = [
        len(json.dumps(row["text"], ensure_ascii=False).encode("utf-8"))
        for row in rows
    ]
    text_bytes = sum(sizes)
    if not text_bytes:
        return 0
    ratio = max(
        0.0, (text_bytes - excess - _EVIDENCE_PRETRIM_SLACK_BYTES) / text_bytes
    )
    omitted = 0
    for row in rows:
        text = row["text"]
        keep = int(len(text) * ratio)
        if keep >= len(text):
            continue
        row["text"] = text[: max(0, keep - 1)] + "…"
        omitted += max(0, len(text) - len(row["text"]))
    return omitted
