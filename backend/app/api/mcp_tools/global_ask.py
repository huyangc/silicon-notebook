"""Global Ask helpers for the MCP ``ask`` / ``get_ask`` / ``read_reference``
tools: live authorization, the error translation, and a global job's view."""
import json
from typing import Any

from app.domain.agent_tools import (
    capability_tier, principal_has_capability, scope_missing_message,
)
from app.models.global_ask import (
    global_answer_citations, global_answer_text, global_answer_trace,
)
from app.services.global_citation_check import global_answer_check
from ._messages import has_cjk
from ._shared import AgentToolError, _live_principal
from .refs import global_element_ref


# ``GlobalAskError.status_code`` -> Agent error code. The service's message is
# already approved Chinese user copy, so it is passed through verbatim.
_GLOBAL_STATUS_CODES = {
    403: "notebook_unreadable",
    404: "not_found",
    409: "busy",
    422: "invalid_argument",
    429: "busy",
    503: "unavailable",
}
_GLOBAL_FALLBACK = "全局问答暂时无法完成，请稍后重试；如仍失败，请联系管理员"


def global_ask_tool_error(exc: BaseException) -> AgentToolError | None:
    """The Agent error for a ``GlobalAskError``, else ``None``."""
    try:
        from app.services.global_ask import REQUEST_KEY_REUSED, GlobalAskError
    except ImportError:  # pragma: no cover - discovery before runtime wiring
        return None
    if not isinstance(exc, GlobalAskError):
        return None
    if getattr(exc, "reason", "") == REQUEST_KEY_REUSED:
        # A client_request_id reused for another question: the caller's to
        # fix (409 on HTTP, but not "busy" -- retrying never helps).
        return AgentToolError(
            "invalid_argument",
            "这个 client_request_id 已用于另一个问题，请换一个新的 client_request_id",
        )
    code = _GLOBAL_STATUS_CODES.get(int(exc.status_code or 0), "internal")
    return AgentToolError(code, exc.message or _GLOBAL_FALLBACK)


def global_ask_service() -> Any:
    # Discovery constructs descriptors before repository/runtime startup.
    from app.api.deps import global_ask_service as provide
    return provide()


def _plain(value: Any) -> dict[str, Any]:
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else dict(value)


_INACTIVE = "Agent 凭证已失效，请在 Agent 接入中检查凭证状态"


def _authorize(repo: Any, *, token_id: str = "", answer: bool = True) -> Any:
    try:
        principal = repo.refresh_agent_principal(token_id) if token_id else _live_principal(repo)
    except PermissionError:
        raise AgentToolError("token_inactive", _INACTIVE) from None
    if principal is None:
        raise AgentToolError("token_inactive", _INACTIVE)
    # Starting/reading/cancelling an answer needs the ``ask`` tier alone;
    # opening a cited element's original text is a read (``read`` tier).
    capability = "ask:execute" if answer else "knowledge:read"
    if not principal_has_capability(principal, capability):
        raise AgentToolError(
            "scope_missing", scope_missing_message(capability_tier(capability))
        )
    return principal


def _citation_keys(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return [tuple(row.get(key) for key in ("notebook_id", "source_id", "element_id")) for row in rows]


# Per-step cap on the serialized ``detail`` blob a trace page carries. The
# browser's collapsible trace panel shows the whole thing; an MCP page keeps
# only the step's identity (kind/summary/duration) plus a capped, explicitly
# flagged slice, so one reasoning step with a large candidate/relation dump
# cannot by itself blow the page's share of the MCP output budget.
_TRACE_DETAIL_CHARS = 400
# The same for the step's ``summary``: it is usually one line, but some steps
# embed the question (a ``search_chunks`` summary can carry all 4,000
# characters of it), and a single step larger than the whole page budget would
# keep the page from ever converging.
_TRACE_SUMMARY_CHARS = 400


def _field(step: Any, name: str, default: Any = None) -> Any:
    """Read one attribute off a ``TraceStep`` or a plain dict alike.

    A job's trace is ``list[TraceStep]``, but a step that arrives through a
    replayed payload may still be a raw dict, so this is the one place both
    shapes are read.
    """
    return step.get(name, default) if isinstance(step, dict) else getattr(step, name, default)


def _trace_step_view(step: Any) -> dict[str, Any]:
    """One reasoning-trace step, bounded for an MCP page."""
    detail = _field(step, "detail", {})
    try:
        detail_text = json.dumps(detail, ensure_ascii=False, sort_keys=True)
    except (TypeError, ValueError):
        detail_text = str(detail)
    summary = str(_field(step, "summary", "") or "")
    return {
        "kind": _field(step, "step_type", ""),
        "summary": summary[:_TRACE_SUMMARY_CHARS],
        "summary_truncated": len(summary) > _TRACE_SUMMARY_CHARS,
        "duration_ms": _field(step, "duration_ms"),
        "detail": detail_text[:_TRACE_DETAIL_CHARS],
        "detail_truncated": len(detail_text) > _TRACE_DETAIL_CHARS,
    }


def _plain_citation(item: Any, job_id: str) -> dict[str, Any]:
    """A citation as a plain JSON-safe dict, whether it came out real or loose,
    carrying its ``read_reference`` handle when it points at an element."""
    row = dict(item.model_dump(mode="json") if hasattr(item, "model_dump") else item)
    ref = global_element_ref(job_id, str(row.get("element_id") or ""))
    if ref:
        row["ref"] = ref
    return row


def _citation_check_field(job: Any) -> dict[str, Any]:
    """``{"citation_check": summary}`` for a partly failed answer, else ``{}``.

    The one projection every global surface reads the stored summary through
    (``global_answer_check``); a legacy ``response`` answer predates the check.
    """
    summary = global_answer_check(job)
    return {"citation_check": summary} if summary else {}


#: A global job's lifecycle word -> the MCP ``ask`` page's ``status``.
JOB_STATUS = {
    "done": "answered", "running": "running", "failed": "failed",
    "interrupted": "failed", "cancelled": "cancelled",
}
FAILED_COPY = "这次回答没有完成，请稍后重试"


def global_view(job: Any) -> dict[str, Any]:
    """One global job in the shape ``ask_pages.answer_page`` pages.

    Both payload shapes read through the SAME projection every other reader
    of a global job goes through: a turn answered by the shared engine
    carries ``answer``, a turn written before the engine switch carries the
    legacy ``response``. ``legacy`` still supplies the fields the projections
    do not cover (``answer_id``/``grounded``/``completeness_notice``).
    """
    data = _plain(job)
    legacy = data.get("answer") or data.get("response") or {}
    citations = [
        _plain_citation(item, data["job_id"]) for item in global_answer_citations(job)
    ]
    error = str(data.get("error") or "")
    answer = getattr(job, "answer", None)
    return {
        "job_id": data["job_id"],
        "conversation_id": data["conversation_id"],
        "status": JOB_STATUS.get(str(data["status"]), "failed"),
        "mode": data.get("mode", "chunk"),
        "answer_id": legacy.get("answer_id", ""),
        "answer": global_answer_text(job),
        "citations": citations,
        "grounded": legacy.get("grounded", False),
        "coverage_counts": {
            "resolved": len(data.get("resolved_notebook_ids", [])),
            "searched": len(data.get("searched_notebook_ids", [])),
            "cited": len(data.get("cited_notebook_ids", [])),
        },
        "skipped": data.get("skipped_notebooks", []),
        "degraded": data.get("degraded_notebook_ids", []),
        "citation_check": _citation_check_field(job),
        "trace": global_answer_trace(job),
        "answer_object": answer,
        "anchors": None,
        # The service stores displayable Chinese guidance only; anything else
        # is replaced, never echoed.
        "error": error if has_cjk(error) else (FAILED_COPY if error else ""),
        "completeness_notice": legacy.get("completeness_notice", ""),
        "scope": {
            "kind": "global",
            "mode": (data.get("notebook_scope") or {}).get("mode", "all"),
            "notebook_ids": list(data.get("resolved_notebook_ids", [])),
        },
    }
