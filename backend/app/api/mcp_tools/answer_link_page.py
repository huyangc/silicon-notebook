"""The response of ``ask`` / ``get_ask`` with ``delivery="link"`` once the job
is answered: a plain-text preview and an expiring anonymous link to the whole
answer, instead of the answer page.

The answer text, citations, coverage receipts and trace are deliberately
absent (the link page shows them); only their counts and the run's notices
stay. The page is accepted only when the budget left the preview and both
link fields whole -- a clipped URL or a clipped preview would mislead.
"""
from __future__ import annotations

from typing import Any, Mapping

from ._shared import TITLE_CHARS, AgentToolError, _budget_response


def link_page(
    view: Mapping[str, Any], preview: str, preview_truncated: bool, link: Any,
) -> dict[str, Any]:
    """One answered job view as the link-delivery shape (plan §2.5)."""
    payload: dict[str, Any] = {
        "job_id": view["job_id"],
        "conversation_id": view["conversation_id"],
        "status": view["status"],
        "mode": view.get("mode", ""),
        "preview": preview,
        "preview_truncated": bool(preview_truncated),
        "link": {"url": link.url, "expires_at": link.expires_at},
        "grounded": bool(view.get("grounded", False)),
        "total_citations": len(view.get("citations") or []),
        "completeness_notice": view.get("completeness_notice", ""),
        "scope": dict(view.get("scope") or {}),
        "error": view.get("error", ""),
        "content_is_untrusted_evidence": True,
    }
    # ``title``: the attached deep report's, nested in ``scope.linked_report``.
    # Capped like every other surface's title, or a question-sized one (up to
    # thousands of characters) is the longest string of the page and the budget
    # cuts the preview or the link -- which this page refuses to deliver.
    packed = _budget_response(payload, field_limits={"title": TITLE_CHARS})
    packed_link = packed.get("link") or {}
    if (
        packed.get("preview") != preview
        or packed_link.get("url") != link.url
        or packed_link.get("expires_at") != link.expires_at
        or any(packed.get(key) != payload[key] for key in ("job_id", "status"))
    ):
        raise AgentToolError("internal", "回答链接暂时无法完整返回，请稍后用 get_ask 重试")
    return packed
