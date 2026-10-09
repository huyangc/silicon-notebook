"""One resumable answer page for both kinds of Ask job (``askjob-`` / ``gask-``).

``ask`` returns the first page and ``get_ask`` any later one. A *view* (see
``global_ask.global_view`` and ``ask.notebook_view``) normalises either job to
the same keys; this module only pages it. Answer text, citations, coverage
receipts and trace steps page independently, each with its own cursor, and a
page is accepted only after the output budget demonstrably left every
delivered identity and cursor intact -- otherwise the page shrinks and tries
again, so following the cursors can never skip data.
"""
from __future__ import annotations

from typing import Any, Mapping

from ._shared import RESULT_LIMIT, TEXT_LIMIT, AgentToolError, _budget_response
from .global_ask import _citation_keys, _trace_step_view

# Field caps for the strings an answer page carries (same values the retired
# single-notebook ask tool used for its anchors and intent summary).
_FIELD_LIMITS = {
    "object_type": 100, "label": 300, "source_title": 300, "location_label": 300,
    "resolved_question": 1_000, "entities": 200, "assumptions": 300,
    "constraints": 300, "excluded_topics": 300, "question": 500,
    "quoted_span": 200, "source_file_name": 300,
}


def _intent_summary(contract: Any) -> dict[str, Any] | None:
    """What a user wants to hear back about the understanding a run used.

    The contract is not repeated whole (the Agent saw the review view when it
    was asked to clarify): the wording, what it took for granted, and the
    answers it was given. ``None`` outside reasoning.
    """
    if contract is None:
        return None
    return {
        "resolved_question": getattr(contract, "resolved_question", ""),
        "result_scope": getattr(contract, "result_scope", "ranked"),
        "entities": list(getattr(contract, "entities", ()) or ()),
        "assumptions": list(getattr(contract, "assumptions", ()) or ()),
        "constraints": list(getattr(contract, "constraints", ()) or ()),
        "excluded_topics": list(getattr(contract, "excluded_topics", ()) or ()),
        "clarification_answers": [
            dict(row)
            for row in getattr(contract, "clarification_answers", ()) or ()
        ],
    }


def answer_page(
    view: Mapping[str, Any], answer_offset: int = 0, citation_offset: int = 0,
    coverage_offset: int = 0, trace_offset: int = 0,
) -> dict[str, Any]:
    """Page one normalised job view; every cursor is exact and resumable."""
    answer = str(view.get("answer") or "")
    citations = list(view.get("citations") or [])
    trace_steps = list(view.get("trace") or [])
    skipped = list(view.get("skipped") or [])
    degraded = list(view.get("degraded") or [])
    coverage_total = max(len(skipped), len(degraded))
    answer_count = TEXT_LIMIT
    citation_count = min(RESULT_LIMIT, len(citations) - min(citation_offset, len(citations)))
    coverage_count = min(RESULT_LIMIT, coverage_total - min(coverage_offset, coverage_total))
    trace_count = min(RESULT_LIMIT, len(trace_steps) - min(trace_offset, len(trace_steps)))
    contract = getattr(view.get("answer_object"), "intent", None)
    intent = _intent_summary(contract)
    anchors = view.get("anchors")
    while True:
        text = answer[answer_offset:answer_offset + answer_count]
        refs = citations[citation_offset:citation_offset + citation_count]
        trace_page = [
            _trace_step_view(step)
            for step in trace_steps[trace_offset:trace_offset + trace_count]
        ]
        trace_next = (trace_offset + len(trace_page)
                      if trace_offset + len(trace_page) < len(trace_steps) else None)
        coverage = {
            **dict(view.get("coverage_counts") or {}),
            "skipped": len(skipped), "degraded": len(degraded),
            "offset": coverage_offset,
            "skipped_notebooks": skipped[coverage_offset:coverage_offset + coverage_count],
            "degraded_notebook_ids": degraded[coverage_offset:coverage_offset + coverage_count],
            **dict(view.get("citation_check") or {}),
        }
        payload: dict[str, Any] = {
            "job_id": view["job_id"], "conversation_id": view["conversation_id"],
            "status": view["status"], "mode": view.get("mode", ""),
            "answer_id": view.get("answer_id", ""),
            "answer": text,
            "next_answer_offset": (answer_offset + len(text)
                                   if answer_offset + len(text) < len(answer) else None),
            "citations": refs,
            "next_citation_offset": (citation_offset + len(refs)
                                     if citation_offset + len(refs) < len(citations) else None),
            "total_citations": len(citations),
            "grounded": bool(view.get("grounded", False)),
            "coverage": coverage,
            "next_coverage_offset": (coverage_offset + coverage_count
                                     if coverage_offset + coverage_count < coverage_total
                                     else None),
            "trace": {
                "steps": trace_page, "offset": trace_offset,
                "next_offset": trace_next, "total": len(trace_steps),
            },
            "error": view.get("error", ""),
            "completeness_notice": view.get("completeness_notice", ""),
            "scope": dict(view.get("scope") or {}),
            "content_is_untrusted_evidence": True,
        }
        # At most 20 top-level keys (``OUTPUT_MAPPING_LIMIT``): the two
        # optional ones only when they carry something.
        if intent is not None:
            payload["intent"] = intent
        if anchors:
            payload["anchors"] = anchors
        packed = _budget_response(
            payload, field_limits=_FIELD_LIMITS,
            initial_omitted_items=int(view.get("anchors_omitted") or 0),
            anchors_budget_chars=3_500 if anchors else None,
            anchor_provenance_budget_chars=500 if anchors else None,
            intent_budget_chars=1_500 if intent is not None else None,
        )
        packed_trace = packed.get("trace") or {}
        if (
            packed.get("answer") == text
            and _citation_keys(packed.get("citations", [])) == _citation_keys(refs)
            and [row.get("ref") for row in packed.get("citations", [])]
            == [row.get("ref") for row in refs]
            and packed_trace.get("steps") == trace_page
            and packed_trace.get("offset") == trace_offset
            and packed_trace.get("next_offset") == trace_next
            and all(packed.get(key) == payload[key] for key in (
                "job_id", "conversation_id", "status",
                "next_answer_offset", "next_citation_offset",
                "next_coverage_offset", "coverage",
            ))
        ):
            return packed
        if citation_count > 1:
            citation_count //= 2
        elif trace_count > 1:
            trace_count //= 2
        elif coverage_count > 1:
            coverage_count //= 2
        elif answer_count > 1:
            answer_count //= 2
        else:
            raise AgentToolError("internal", "结果暂时无法完整返回，请稍后用 get_ask 重读")
