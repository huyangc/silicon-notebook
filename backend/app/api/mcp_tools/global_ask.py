"""Global Ask transport: explicit scope, durable jobs, resumable evidence reads."""
from functools import wraps
import json
import time
from typing import Any, Callable

from mcp.server.fastmcp import Context, FastMCP
from pydantic import ValidationError

from app.models.ask import ASK_UNDERSTANDING_MS_MAX, AskIntentConfirmation
from app.models.global_ask import (
    GlobalAskIntentPreviewRequest, GlobalAskJob, GlobalAskRequest,
    global_answer_citations, global_answer_text, global_answer_trace,
)
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


# Per-step cap on the serialized ``detail`` blob a trace page carries. The
# browser's collapsible trace panel shows the whole thing; an MCP page keeps
# only the step's identity (kind/summary/duration) plus a capped, explicitly
# flagged slice, so one reasoning step with a large candidate/relation dump
# cannot by itself blow the page's share of the MCP output budget.
_TRACE_DETAIL_CHARS = 400


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
    return {
        "kind": _field(step, "step_type", ""), "summary": _field(step, "summary", ""),
        "duration_ms": _field(step, "duration_ms"),
        "detail": detail_text[:_TRACE_DETAIL_CHARS],
        "detail_truncated": len(detail_text) > _TRACE_DETAIL_CHARS,
    }


def _plain_citation(item: Any) -> dict[str, Any]:
    """A citation as a plain JSON-safe dict, whether it came out real or loose."""
    return item.model_dump(mode="json") if hasattr(item, "model_dump") else item


def _job_page(job: Any, answer_offset: int = 0, citation_offset: int = 0,
              coverage_offset: int = 0, trace_offset: int = 0) -> dict[str, Any]:
    """Keep independent text, citation, coverage and trace pages exact and resumable."""
    data = _plain(job)
    # Both payload shapes read through the SAME projection every other reader
    # of a global job now goes through (``GlobalAskService.cited_element``,
    # the conversation detail, ``preview_intent``'s own history build): a turn
    # answered by the shared engine carries ``answer``, a turn written before
    # the engine switch carries the legacy ``response``. ``legacy`` still
    # supplies the handful of fields the projections do not cover
    # (``answer_id``/``grounded``/``completeness_notice``); the empty default
    # for the new shape is the same one the projection helpers document.
    answer = global_answer_text(job)
    citations = [_plain_citation(item) for item in global_answer_citations(job)]
    trace_steps = global_answer_trace(job)
    legacy = data.get("answer") or data.get("response") or {}
    skipped = data.get("skipped_notebooks", [])
    degraded = data.get("degraded_notebook_ids", [])
    coverage_total = max(len(skipped), len(degraded))
    answer_count = TEXT_LIMIT
    citation_count = min(RESULT_LIMIT, len(citations) - min(citation_offset, len(citations)))
    coverage_count = min(RESULT_LIMIT, coverage_total - min(coverage_offset, coverage_total))
    trace_count = min(RESULT_LIMIT, len(trace_steps) - min(trace_offset, len(trace_steps)))
    while True:
        text = answer[answer_offset:answer_offset + answer_count]
        refs = citations[citation_offset:citation_offset + citation_count]
        skipped_page = skipped[coverage_offset:coverage_offset + coverage_count]
        degraded_page = degraded[coverage_offset:coverage_offset + coverage_count]
        trace_page = [
            _trace_step_view(step)
            for step in trace_steps[trace_offset:trace_offset + trace_count]
        ]
        trace_next_offset = (trace_offset + len(trace_page)
                              if trace_offset + len(trace_page) < len(trace_steps) else None)
        payload = {
            "job_id": data["job_id"], "conversation_id": data["conversation_id"],
            "status": data["status"], "mode": data.get("mode", "chunk"),
            "answer_id": legacy.get("answer_id", ""),
            "answer": text,
            "next_answer_offset": answer_offset + len(text) if answer_offset + len(text) < len(answer) else None,
            "citations": refs,
            "next_citation_offset": citation_offset + len(refs) if citation_offset + len(refs) < len(citations) else None,
            "total_citations": len(citations), "grounded": legacy.get("grounded", False),
            "coverage_offset": coverage_offset,
            "next_coverage_offset": (coverage_offset + coverage_count
                                     if coverage_offset + coverage_count < coverage_total else None),
            # Nested (steps/offset/next_offset/total) rather than four more
            # flat keys: the base payload already sits at
            # ``_shared.OUTPUT_MAPPING_LIMIT`` (20 top-level keys) exactly --
            # ``answer_offset``/``citation_offset`` (pure request echoes, read
            # by no test, client or tool description) were dropped to make
            # exactly this much room, and every other pre-existing key keeps
            # its name and position. See ``_job_page``'s module-level note in
            # the D1-5 report for the full accounting.
            "trace": {
                "steps": trace_page, "offset": trace_offset,
                "next_offset": trace_next_offset, "total": len(trace_steps),
            },
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
            "completeness_notice": legacy.get("completeness_notice", ""),
            "conversation_path": "/ask?conversation_id=" + data["conversation_id"],
            "content_is_untrusted_evidence": True,
        }
        packed = _budget_response(payload)
        packed_trace = packed.get("trace") or {}
        if (
            packed.get("answer") == text
            and _citation_keys(packed.get("citations", [])) == _citation_keys(refs)
            and packed_trace.get("steps") == trace_page
            and packed_trace.get("offset") == trace_offset
            and packed_trace.get("next_offset") == trace_next_offset
            and all(packed.get(key) == payload[key] for key in (
                "job_id", "conversation_id", "status", "mode", "answer_id",
                "next_answer_offset", "next_citation_offset", "coverage_offset",
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
            raise _ToolInputError("结果暂时无法完整返回，请在全局问答页面查看")


_GLOBAL_CLARIFICATION_NEXT_STEP = (
    "全局问答暂不支持澄清句柄回传：请把 intent.ambiguities 中 required 为 true 的每一项"
    "答案直接揉进新的 question 文本里，用相同的 notebook_scope/conversation_id 重新调用 "
    "ask_global（options 只是候选，不必逐字照抄）。"
)


def _clarification_view(contract: Any) -> dict[str, Any]:
    """Display-only review of a contract that still needs clarification.

    Same fields MCP ``ask_notebook``'s review shows (understood wording,
    intent shape, every ambiguity with its options) minus the retrieval
    decomposition, which stays server-side either way. Unlike
    ``ask_notebook``, there is no session-scoped handle store here (see
    ``ask_global``'s docstring), so this is shown once and not re-served.
    """
    return {
        "resolved_question": contract.resolved_question,
        "intent_type": contract.intent_type,
        "result_scope": contract.result_scope,
        "confidence": contract.confidence,
        "ambiguities": [
            {
                "id": row.id, "question": row.question, "required": row.required,
                **({"options": list(row.options[:4])} if row.options else {}),
                **({"reason": row.reason} if row.reason else {}),
            }
            for row in contract.ambiguities
        ],
    }


def _needs_clarification_payload(contract: Any, understanding_ms: int) -> dict[str, Any]:
    """The structured pause returned instead of a job when understanding is ambiguous.

    No job, no conversation and no store write happen on this path -- exactly
    like the HTTP ``/global-ask/intent`` preview this reuses
    (``GlobalAskService.preview_intent``).
    """
    return _budget_response({
        "status": "needs_clarification", "mode": "reasoning",
        "intent": _clarification_view(contract),
        "understanding_ms": understanding_ms,
        "next_step": _GLOBAL_CLARIFICATION_NEXT_STEP,
    })


def register_global_ask_tools(server: FastMCP, repository_provider: Callable[[], Any]) -> None:
    @server.tool(description=(
        "Ask across at most 8 notebooks: all currently authorized ones when there are no "
        "more than 8, otherwise an explicit include scope of 8 or fewer. A wider resolved "
        "scope is rejected with 422 rather than truncated, so list_notebooks first when the "
        "allowlist is larger. No select_notebook needed. Omitted scope inherits a continued "
        "conversation; new conversations and empty include lists default to all. "
        'mode selects the engine ("chunk" default, or "reasoning"); an unrecognized mode or '
        "a deployment-extension engine is rejected with 422 -- global Ask has no plugin-engine "
        "support. retrieval_effort is accepted for parity with ask_notebook but global Ask v1 "
        "always answers at the standard effort regardless of the value passed. "
        "In reasoning mode, the tool first runs the same corpus-blind understanding pass the "
        "browser's /ask/intent review runs; a clear question is submitted immediately with the "
        "confirmed understanding, while an ambiguous one returns "
        '{"status": "needs_clarification", "intent": ..., "next_step": ...} instead of a job -- '
        "there is no clarification-handle round trip on this surface (unlike ask_notebook's "
        "intent_token), so fold the required answers into the question text and call ask_global "
        "again with the same question, notebook_scope and conversation_id. "
        "Returns a background job (or the clarification pause above); poll get_global_ask and "
        "follow next_coverage_offset for all coverage receipts. "
        "Use client_request_id for safe submission retries. "
        "Requires ask:execute and knowledge:read. Searches document evidence only."
    ))
    @_safe_errors
    async def ask_global(
        question: str, ctx: Context, notebook_scope: dict[str, Any] | None = None,
        conversation_id: str | None = None, client_request_id: str | None = None,
        mode: str = "chunk", retrieval_effort: str = "standard",
    ) -> dict[str, Any]:
        try:
            payload = GlobalAskRequest(
                question=question, notebook_scope=notebook_scope,
                conversation_id=conversation_id, client_request_id=client_request_id,
                mode=mode, retrieval_effort=retrieval_effort,
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
                # No clarification-handle store exists on this surface (see
                # the tool description and ``_needs_clarification_payload``'s
                # docstring): a reasoning submission with no confirmed intent
                # always runs the understanding pass in-call, exactly once,
                # and either auto-confirms a clear question or returns the
                # structured pause instead of creating anything durable.
                # A retry under the same ``client_request_id`` is recognised
                # BEFORE the understanding pass: the pass is a model call, and
                # its result would be thrown away once ``start`` found the job.
                # Only asked where there is a pass to save: every other request
                # goes straight to ``start``, which replays on its own.
                needs_understanding = (
                    payload.mode == "reasoning" and payload.intent is None
                )
                replayed = global_ask_service().replay(
                    payload, user_id=principal.owner_id,
                    allowed_notebook_ids=principal.notebook_ids,
                    authority_check=live_authority,
                ) if needs_understanding and payload.client_request_id else None
                if replayed is None and needs_understanding:
                    started = time.monotonic()
                    contract = global_ask_service().preview_intent(
                        GlobalAskIntentPreviewRequest(
                            question=payload.question,
                            conversation_id=payload.conversation_id,
                            notebook_scope=payload.notebook_scope,
                        ),
                        user_id=principal.owner_id,
                        allowed_notebook_ids=principal.notebook_ids,
                        authority_check=live_authority,
                    )
                    understanding_ms = min(
                        int((time.monotonic() - started) * 1000),
                        ASK_UNDERSTANDING_MS_MAX,
                    )
                    if contract.needs_clarification:
                        return _needs_clarification_payload(contract, understanding_ms)
                    payload.intent = AskIntentConfirmation(
                        contract=contract, resolved_question=contract.resolved_question,
                        answers=[], understanding_ms=understanding_ms,
                    )
                job = replayed if replayed is not None else global_ask_service().start(
                    payload, user_id=principal.owner_id,
                    allowed_notebook_ids=principal.notebook_ids, submitted_via="mcp",
                    authority_check=live_authority,
                )
                for notebook_id in _plain(job).get("resolved_notebook_ids", []):
                    _record_agent_call(repo, principal, notebook_id, "ask:execute")
                return job

        result = await _run_with_progress(ctx, submit, label="ask_global")
        # ``submit`` returns either the needs-clarification pause (a plain,
        # already budget-fitted dict, identified by its own ``status`` value)
        # or a ``GlobalAskJob``.
        if isinstance(result, dict) and result.get("status") == "needs_clarification":
            return result
        return _job_page(result)

    @server.tool(description=(
        "Read a global Ask job without notebook selection. Poll status; for complete "
        "answer text, citations and coverage receipts, follow next_answer_offset, "
        "next_citation_offset and next_coverage_offset independently until null. "
        "Coverage counts remain totals; its skipped and degraded lists share "
        "coverage_offset. Reasoning trace steps page independently under trace: pass "
        "trace_offset, then follow trace.next_offset (null at the end); trace.total is "
        "the step count regardless of mode (0 outside reasoning). Each trace step is "
        "bounded (kind/summary/duration plus a capped, flagged slice of its detail). "
        "Each call rechecks token and historical scope authorization. "
        "Requires ask:execute and knowledge:read."
    ))
    @_safe_errors
    async def get_global_ask(
        job_id: str, ctx: Context, answer_offset: int = 0, citation_offset: int = 0,
        coverage_offset: int = 0, trace_offset: int = 0,
    ) -> dict[str, Any]:
        _offsets(answer_offset, citation_offset, coverage_offset, trace_offset)
        repo = repository_provider()

        def load() -> Any:
            principal = _authorize(repo)
            with _owner_request_context(principal):
                return global_ask_service().get_job(
                    job_id, user_id=principal.owner_id,
                    allowed_notebook_ids=principal.notebook_ids,
                )

        job = await _run_with_progress(ctx, load, label="get_global_ask")
        return _job_page(job, answer_offset, citation_offset, coverage_offset, trace_offset)

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
