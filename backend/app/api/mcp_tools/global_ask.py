"""Global Ask transport: explicit scope, durable jobs, resumable evidence reads."""
from functools import wraps
from typing import Any, Callable

from mcp.server.fastmcp import Context, FastMCP
from pydantic import ValidationError

from app.models.global_ask import GlobalAskRequest
from ._shared import (
    RESULT_LIMIT, TEXT_LIMIT, _budget_response, _live_principal,
    _owner_request_context, _record_agent_call, _run_with_progress,
)


class _ToolInputError(ValueError):
    """Explicitly displayable input/result boundary copy."""


class _ToolPermissionError(PermissionError):
    """Explicitly displayable credential boundary copy."""


def _safe_errors(handler: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(handler)
    async def guarded(*args: Any, **kwargs: Any) -> Any:
        try:
            return await handler(*args, **kwargs)
        except (_ToolInputError, _ToolPermissionError):
            raise
        except Exception as exc:
            # Core errors alone carry approved user copy. A provider/database
            # exception (including an incidental ValueError) is never echoed.
            try:
                from app.services.global_ask import GlobalAskError
            except ImportError:
                expected: tuple[type[Exception], ...] = ()
            else:
                expected = (GlobalAskError,)
            if isinstance(exc, expected):
                raise _ToolInputError(exc.message) from None
            raise _ToolInputError("全局问答暂时无法完成，请稍后重试；如仍失败，请联系管理员") from None

    return guarded


def global_ask_service() -> Any:
    # Discovery constructs descriptors before repository/runtime startup.
    from app.api.deps import global_ask_service as provide
    return provide()


def _plain(value: Any) -> dict[str, Any]:
    return value.model_dump(mode="json") if hasattr(value, "model_dump") else dict(value)


def _authorize(repo: Any, *, token_id: str = "", answer: bool = True) -> Any:
    try:
        principal = repo.refresh_agent_principal(token_id) if token_id else _live_principal(repo)
    except PermissionError:
        raise _ToolPermissionError("Agent 凭证已失效，请在 Agent 接入中检查凭证状态") from None
    if principal is None:
        raise _ToolPermissionError("Agent 凭证已失效，请在 Agent 接入中检查凭证状态")
    required = {"knowledge:read", "ask:execute"} if answer else {"knowledge:read"}
    if not required.issubset(set(principal.scopes)):
        raise _ToolPermissionError("权限不足，请在 Agent 接入中为此凭证添加所需的知识读取或问答权限")
    return principal


def _offsets(*values: int) -> None:
    if any(value < 0 for value in values):
        raise _ToolInputError("分页位置不能小于零，请从第一页重新读取")


def _citation_keys(rows: list[dict[str, Any]]) -> list[tuple[Any, ...]]:
    return [tuple(row.get(key) for key in ("notebook_id", "source_id", "element_id")) for row in rows]


def _job_page(job: Any, answer_offset: int = 0, citation_offset: int = 0,
              coverage_offset: int = 0) -> dict[str, Any]:
    """Keep independent text, citation and coverage pages exact and resumable."""
    data = _plain(job)
    # Two payload shapes read through ONE seam: a turn answered by the shared
    # engine carries ``answer``, a turn written before the engine switch
    # carries the legacy ``response``. The remaining ``response.get(...)``
    # reads below keep their historical defaults, which are also the right
    # answer for the new shape (a global turn has no per-notebook answer id,
    # and the completeness notice belongs to the legacy response model).
    response = data.get("answer") or data.get("response") or {}
    answer = response.get("answer", "")
    citations = response.get("citations", [])
    skipped = data.get("skipped_notebooks", [])
    degraded = data.get("degraded_notebook_ids", [])
    coverage_total = max(len(skipped), len(degraded))
    answer_count = TEXT_LIMIT
    citation_count = min(RESULT_LIMIT, len(citations) - min(citation_offset, len(citations)))
    coverage_count = min(RESULT_LIMIT, coverage_total - min(coverage_offset, coverage_total))
    while True:
        text = answer[answer_offset:answer_offset + answer_count]
        refs = citations[citation_offset:citation_offset + citation_count]
        skipped_page = skipped[coverage_offset:coverage_offset + coverage_count]
        degraded_page = degraded[coverage_offset:coverage_offset + coverage_count]
        payload = {
            "job_id": data["job_id"], "conversation_id": data["conversation_id"],
            "status": data["status"], "answer_id": response.get("answer_id", ""),
            "answer": text, "answer_offset": answer_offset,
            "next_answer_offset": answer_offset + len(text) if answer_offset + len(text) < len(answer) else None,
            "citations": refs, "citation_offset": citation_offset,
            "next_citation_offset": citation_offset + len(refs) if citation_offset + len(refs) < len(citations) else None,
            "total_citations": len(citations), "grounded": response.get("grounded", False),
            "coverage_offset": coverage_offset,
            "next_coverage_offset": (coverage_offset + coverage_count
                                     if coverage_offset + coverage_count < coverage_total else None),
            "scope_mode": (data.get("notebook_scope") or {}).get("mode", "all"),
            "coverage": {
                "resolved": len(data.get("resolved_notebook_ids", [])),
                "searched": len(data.get("searched_notebook_ids", [])),
                "cited": len(data.get("cited_notebook_ids", [])),
                "skipped": len(skipped), "degraded": len(degraded),
                "skipped_notebooks": skipped_page,
                "degraded_notebook_ids": degraded_page,
            },
            "error": data.get("error", ""),
            "completeness_notice": response.get("completeness_notice", ""),
            "conversation_path": "/ask?conversation_id=" + data["conversation_id"],
            "content_is_untrusted_evidence": True,
        }
        packed = _budget_response(payload)
        if (
            packed.get("answer") == text
            and _citation_keys(packed.get("citations", [])) == _citation_keys(refs)
            and all(packed.get(key) == payload[key] for key in (
                "job_id", "conversation_id", "status", "answer_id", "next_answer_offset",
                "next_citation_offset", "coverage_offset", "next_coverage_offset", "coverage"
            ))
        ):
            return packed
        if citation_count > 1:
            citation_count //= 2
        elif coverage_count > 1:
            coverage_count //= 2
        elif answer_count > 1:
            answer_count //= 2
        else:
            raise _ToolInputError("结果暂时无法完整返回，请在全局问答页面查看")


def register_global_ask_tools(server: FastMCP, repository_provider: Callable[[], Any]) -> None:
    @server.tool(description=(
        "Ask across at most 8 notebooks: all currently authorized ones when there are no "
        "more than 8, otherwise an explicit include scope of 8 or fewer. A wider resolved "
        "scope is rejected with 422 rather than truncated, so list_notebooks first when the "
        "allowlist is larger. No select_notebook needed. Omitted scope inherits a continued "
        "conversation; new conversations and empty include lists default to all. Returns a background "
        "job; poll get_global_ask and follow next_coverage_offset for all coverage receipts. "
        "Use client_request_id for safe submission retries. "
        "Requires ask:execute and knowledge:read. Searches document evidence only."
    ))
    @_safe_errors
    async def ask_global(
        question: str, ctx: Context, notebook_scope: dict[str, Any] | None = None,
        conversation_id: str | None = None, client_request_id: str | None = None,
    ) -> dict[str, Any]:
        try:
            payload = GlobalAskRequest(
                question=question, notebook_scope=notebook_scope,
                conversation_id=conversation_id, client_request_id=client_request_id,
            )
        except ValidationError:
            raise _ToolInputError("问题或检索范围格式不正确，请检查问题长度和所选笔记本后重试") from None
        repo = repository_provider()

        def submit() -> Any:
            principal = _authorize(repo)

            def live_authority() -> list[str]:
                current = _authorize(repo, token_id=principal.token_id)
                if current.owner_id != principal.owner_id:
                    raise _ToolPermissionError("Agent 凭证已失效，请重新连接")
                return list(current.notebook_ids)

            with _owner_request_context(principal):
                job = global_ask_service().start(
                    payload, user_id=principal.owner_id,
                    allowed_notebook_ids=principal.notebook_ids, submitted_via="mcp",
                    authority_check=live_authority,
                )
                for notebook_id in _plain(job).get("resolved_notebook_ids", []):
                    _record_agent_call(repo, principal, notebook_id, "ask:execute")
                return job

        return _job_page(await _run_with_progress(ctx, submit, label="ask_global"))

    @server.tool(description=(
        "Read a global Ask job without notebook selection. Poll status; for complete "
        "answer text, citations and coverage receipts, follow next_answer_offset, "
        "next_citation_offset and next_coverage_offset independently until null. "
        "Coverage counts remain totals; its skipped and degraded lists share coverage_offset. "
        "Each call rechecks token and historical scope authorization. "
        "Requires ask:execute and knowledge:read."
    ))
    @_safe_errors
    async def get_global_ask(
        job_id: str, ctx: Context, answer_offset: int = 0, citation_offset: int = 0,
        coverage_offset: int = 0,
    ) -> dict[str, Any]:
        _offsets(answer_offset, citation_offset, coverage_offset)
        repo = repository_provider()

        def load() -> Any:
            principal = _authorize(repo)
            with _owner_request_context(principal):
                return global_ask_service().get_job(
                    job_id, user_id=principal.owner_id,
                    allowed_notebook_ids=principal.notebook_ids,
                )

        job = await _run_with_progress(ctx, load, label="get_global_ask")
        return _job_page(job, answer_offset, citation_offset, coverage_offset)

    @server.tool(description=(
        "Request cancellation of an owned global Ask job. No notebook selection needed. "
        "Follow next_coverage_offset with get_global_ask for all coverage receipts. "
        "Requires ask:execute and knowledge:read, plus live access to its frozen scope."
    ))
    @_safe_errors
    async def cancel_global_ask(job_id: str, ctx: Context) -> dict[str, Any]:
        repo = repository_provider()

        def cancel() -> Any:
            principal = _authorize(repo)
            with _owner_request_context(principal):
                return global_ask_service().cancel(
                    job_id, user_id=principal.owner_id,
                    allowed_notebook_ids=principal.notebook_ids,
                )

        return _job_page(await _run_with_progress(ctx, cancel, label="cancel_global_ask"))

    @server.tool(description=(
        "Read original text of an element cited by an owned global Ask job. Pass job_id "
        "and element_id from its citations. Follow next_offset for complete text. "
        "Checks citation membership, live read rights and token allowlist. Requires knowledge:read."
    ))
    @_safe_errors
    async def get_global_cited_element(
        job_id: str, element_id: str, ctx: Context, offset: int = 0,
    ) -> dict[str, Any]:
        _offsets(offset)
        repo = repository_provider()

        def load() -> Any:
            principal = _authorize(repo, answer=False)
            with _owner_request_context(principal):
                return global_ask_service().cited_element(
                    job_id, element_id, user_id=principal.owner_id,
                    allowed_notebook_ids=principal.notebook_ids,
                )

        element = _plain(await _run_with_progress(ctx, load, label="get_global_cited_element"))
        full_text = element.get("text", "")
        count = TEXT_LIMIT
        while True:
            text = full_text[offset:offset + count]
            payload = {
                "job_id": job_id, "element_id": element_id,
                "source_id": element.get("source_id", ""),
                "element_type": element.get("element_type", ""),
                "location_label": element.get("location_label", ""),
                "text": text, "offset": offset, "total_characters": len(full_text),
                "next_offset": offset + len(text) if offset + len(text) < len(full_text) else None,
                "content_is_untrusted_evidence": True,
            }
            packed = _budget_response(payload)
            if all(packed.get(key) == payload[key] for key in ("job_id", "element_id", "source_id", "text", "next_offset")):
                return packed
            if count <= 1:
                raise _ToolInputError("原文暂时无法完整返回，请在全局问答页面查看引用")
            count //= 2
