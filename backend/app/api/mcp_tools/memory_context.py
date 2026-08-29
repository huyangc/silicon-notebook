"""Memory recall, formal context, Ask, and Memory proposal MCP tools."""

import logging
from typing import Any, Callable, Mapping, Sequence

import anyio
from mcp.server.fastmcp import Context, FastMCP

from app.domain.ask_engine import AskPluginEngineError
from app.services.ask_modes import UnknownAskMode

from app.core.memory_inputs import (
    normalize_client_request_id,
    normalize_content,
    normalize_evidence_refs,
    normalize_reason,
    normalize_tags,
    normalize_task_context,
    normalize_title,
)
from app.models.ask import ASK_QUESTION_MAX_CHARS, AskRequest
from app.services.agent_profile_block import resolve_agent_profile_names
from app.services.search_concurrency import run_under_search_gate

from ._shared import (
    RESULT_LIMIT,
    TEXT_LIMIT,
    _budget_response,
    _record_agent_call,
    _owner_request_context,
    _run_with_progress,
    _selected_notebook,
)


# Citations have no per-item cap of their own. Pre-fitting them keeps the shared
# output-budget convergence loop from halving the answer text first under a
# realistic CJK citation payload.
CITATIONS_BUDGET_CHARS = 1_800
# Mirrors AskIntentPreviewRequest.conversation_id.
CONVERSATION_ID_MAX_LENGTH = 200


def _validate_proposal_input(
    title: str,
    content_md: str,
    tags: Sequence[str] | None,
    reason: str,
    task_context: Mapping[str, Any],
    evidence_refs: Sequence[Mapping[str, Any]],
    client_request_id: str,
) -> tuple[str, str, list[str], str, dict[str, Any], list[dict[str, Any]], str]:
    """Validate the MCP write envelope before any provider lookup."""
    clean_title = normalize_title(title)
    clean_content = normalize_content(content_md)
    clean_reason = normalize_reason(reason)
    clean_request_id = normalize_client_request_id(client_request_id)
    if not clean_reason:
        raise ValueError("reason must be nonblank")
    clean_tags = normalize_tags(tags or [])
    clean_task_context = normalize_task_context(task_context)
    if not clean_task_context:
        raise ValueError("task_context must be nonblank")
    clean_evidence = normalize_evidence_refs(evidence_refs)
    return (
        clean_title,
        clean_content,
        clean_tags,
        clean_reason,
        clean_task_context,
        clean_evidence,
        clean_request_id,
    )


def _profile_names(service: Any, owner_id: str) -> dict[str, str]:
    return resolve_agent_profile_names(service.list_agent_profiles, owner_id)


logger = logging.getLogger(__name__)

# The gate and the advertised legal-mode list must never drift apart, so both
# read this one constant. ``graph`` and the retired ``fast``/``global`` aliases
# are deliberately absent: this Agent face offers only the two supported
# built-ins plus live plugin engines, unlike the browser registry.
_BUILTIN_ASK_MODES = ("chunk", "reasoning")

_ENGINE_UNAVAILABLE_TEXT = (
    "对应的扩展引擎暂不可用（部署未配置或未就绪），"
    "请改用 chunk/reasoning 或联系部署管理员"
)


def _validate_ask_mode(mode: str) -> None:
    """Allow the two built-in modes plus a registered, live-available plugin engine.

    Deliberately finer than the HTTP entry point: ``ask_routes`` folds
    "registered but unavailable" and "unregistered" into one ``UnknownAskMode``
    422, while an Agent needs the two told apart to act. It must speak in
    ``ValueError`` text -- the same reason ``ask_notebook`` validates
    ``question``/``conversation_id`` HERE rather than leaving it to
    ``AskRequest``'s validator: a pydantic ``ValidationError`` raised deep in
    ``repo.ask`` surfaces to an Agent as an opaque model dump.

    The ``app.bootstrap`` import is lazy (function-body, not module-level) to keep
    ``app.api.mcp_tools`` from acquiring a module-level dependency on the extension
    composition root. Any failure resolving the runtime this early (startup ordering,
    etc.) is treated as "mode not registered" rather than propagated -- the caller
    gets an actionable list of legal modes instead of an internal traceback.
    """
    if mode in _BUILTIN_ASK_MODES:
        return
    try:
        from app.bootstrap import application_extension_runtime
        host = application_extension_runtime().ask_engines
    except Exception as exc:
        # Silent fallback would make "plugins installed but runtime broken"
        # byte-identical to "no plugins installed"; log the class name only.
        logger.warning(
            "ask mode validation could not resolve the extension runtime (%s)",
            type(exc).__name__,
        )
        host = None
    if host is not None and host.mode(mode) is not None:
        if host.is_available(mode):
            return
        raise ValueError(f"mode '{mode}' {_ENGINE_UNAVAILABLE_TEXT}")
    legal_modes = [*_BUILTIN_ASK_MODES]
    if host is not None:
        legal_modes.extend(
            sorted(
                item.descriptor.mode_id
                for item in host.registrations()
                if host.is_available(item.descriptor.mode_id)
            )
        )
    raise ValueError(f"mode must be one of: {', '.join(legal_modes)}")


def _ask_actionable(repo: Any, notebook_id: str, payload: AskRequest) -> Any:
    """Translate ask failures the way ``ask_routes._plugin_engine_http_error``
    does for the browser -- an MCP tool error IS the Agent-facing copy, and a
    bare stable code (``plugin_engine_failed``) or a bare mode id (the
    ``UnknownAskMode`` message is just the mode string) is not actionable.
    ``AskCancelled`` and every other exception pass through untouched.
    """
    try:
        return repo.ask(notebook_id, payload)
    except UnknownAskMode:
        # Availability flipped between _validate_ask_mode and dispatch; same
        # wording as the pre-dispatch "registered but unavailable" rejection.
        raise ValueError(
            f"mode '{payload.mode}' {_ENGINE_UNAVAILABLE_TEXT}"
        ) from None
    except AskPluginEngineError as exc:
        if exc.code == "plugin_engine_unavailable":
            # The host's own availability re-check lost the same race the
            # UnknownAskMode branch above covers; the Agent needs the same
            # switch-modes guidance, not a generic retry.
            raise ValueError(
                f"mode '{payload.mode}' {_ENGINE_UNAVAILABLE_TEXT}"
            ) from None
        message = (
            "扩展引擎返回了无法核验的引用"
            if exc.code == "plugin_engine_unverified_citation"
            else "扩展引擎暂时无法完成回答，请重试"
        )
        raise ValueError(f"{message}（reason: {exc.code}）") from None


def register_memory_context_tools(
    server: FastMCP, repository_provider: Callable[[], Any]
) -> None:
    @server.tool(
        description=(
            "Search owner-private Memory in the selected notebook. Candidate "
            "entries are unconfirmed evidence and never formal notebook conclusions."
        )
    )
    async def search_agent_memory(
        query: str, ctx: Context, limit: int = 8
    ) -> dict[str, Any]:
        repo = repository_provider()
        principal, notebook_id = await anyio.to_thread.run_sync(
            _selected_notebook, ctx, repo, "memory:read"
        )

        def load() -> list[dict[str, Any]]:
            include_candidates = True
            try:
                repo.require_agent_access(
                    principal, "memory:read_candidates", notebook_id
                )
            except PermissionError:
                include_candidates = False
            hits = repo.agent_memory_hits(
                principal.owner_id,
                notebook_id,
                query,
                include_candidates=include_candidates,
                limit=min(limit, RESULT_LIMIT),
            )
            profiles = _profile_names(
                repo, principal.owner_id
            )
            rows: list[dict[str, Any]] = []
            for hit in hits:
                try:
                    record = repo.get_memory(hit.memory_id, principal.owner_id)
                except (KeyError, PermissionError):
                    # Retrieval and hydration are separate reads. A lifecycle
                    # transition/delete/access loss between them must fail
                    # closed for this hit without aborting the whole search.
                    continue
                if record.notebook_id != notebook_id or record.status not in {
                    "candidate", "confirmed"
                }:
                    continue
                if record.status == "candidate" and not include_candidates:
                    continue
                rows.append(
                    {
                        "memory_id": record.id,
                        "title": record.title,
                        "content": record.content_md,
                        "status": record.status,
                        "unconfirmed": record.status == "candidate",
                        "formal_notebook_conclusion": record.status == "confirmed",
                        "created_by_agent": profiles.get(
                            record.agent_profile_id or "", ""
                        ),
                        "score": round(float(hit.score), 6),
                        "authority": int(hit.authority),
                        "provenance": record.provenance,
                        "content_is_untrusted_evidence": True,
                    }
                )
            return rows

        rows = await _run_with_progress(
            ctx, load, label="search_agent_memory"
        )
        cap = max(1, min(int(limit), RESULT_LIMIT))
        return _budget_response(
            {"notebook_id": notebook_id, "items": rows[:cap]},
            initial_omitted_items=max(0, len(rows) - cap),
            field_limits={"title": 300, "content": TEXT_LIMIT,
                          "created_by_agent": 200},
            provenance_budget_chars=2_000,
        )

    @server.tool(
        description=(
            "Search source, KG, and confirmed Memory in the selected notebook. "
            "Candidate Memory is never returned."
        )
    )
    async def search_notebook_context(
        query: str, ctx: Context, limit: int = 12
    ) -> dict[str, Any]:
        repo = repository_provider()
        principal, notebook_id = await anyio.to_thread.run_sync(
            _selected_notebook, ctx, repo, "knowledge:read"
        )

        def load() -> list[dict[str, Any]]:
            with _owner_request_context(principal):
                response = repo.search_notebook(notebook_id, query)
            allow_memory = True
            try:
                repo.require_agent_access(
                    principal, "memory:read", notebook_id
                )
            except PermissionError:
                allow_memory = False
            rows: list[dict[str, Any]] = []
            for hit in response.hits:
                if hit.memory_id and not allow_memory:
                    continue
                rows.append(
                    {
                        "type": "memory" if hit.memory_id else hit.scope.lower(),
                        "label": hit.label,
                        "text": hit.text,
                        "memory_id": hit.memory_id,
                        "source_id": hit.source_id,
                        "element_id": hit.element_id,
                        "authority": (
                            "confirmed_memory" if hit.memory_id else "notebook_evidence"
                        ),
                        "provenance": hit.provenance,
                        "content_is_untrusted_evidence": True,
                    }
                )
            return rows

        # Z8 (P0 止血): 与 HTTP /notebooks/{id}/search 共用同一个进程级并发闸
        # (search_concurrency.search_concurrency_gate())。闸**在事件循环上、派发
        # 工作线程之前**拿——不是在 load 里面。在线程里阻塞式 acquire 会让每个
        # 等待者占住一个 anyio 工作线程 token,而那 40 个 token 是全站同步端点共享
        # 的,一次搜索突发就能把整个 API 面饿死(批 0 评审 P1)。这里等待的是一个
        # 挂起的协程,不占线程。
        #
        # 持闸期间跑 _run_with_progress,所以心跳照常:它跑在事件循环上,与工作线程
        # 无关。代价说明:排队等票的那段现在**不**发心跳(旧形态是在 load 里等,被
        # 心跳覆盖着)。这是自觉的取舍——等票的调用换来的是不再占住工作线程,而
        # 队列本身由 4 个在跑的搜索推进。
        #
        # 与 HTTP 入口同形地走 run_under_search_gate:Agent 断连或客户端超时会取消
        # 这个工具调用,而 load 所在的工作线程停不下来。票绑在工作上,线程真正跑完
        # 才归还(codex #627 R3 P1)。取消后 _run_with_progress 仍跑到底,它的心跳协程
        # 由 runner 的 finally 里那次 tg.cancel_scope.cancel() 收掉,不会泄漏。
        async def gated_search() -> list[dict[str, Any]]:
            return await _run_with_progress(
                ctx, load, label="search_notebook_context"
            )

        rows = await run_under_search_gate(gated_search)
        cap = max(1, min(int(limit), RESULT_LIMIT))
        return _budget_response(
            {"notebook_id": notebook_id, "items": rows[:cap]},
            initial_omitted_items=max(0, len(rows) - cap),
            field_limits={"type": 100, "label": 300, "text": TEXT_LIMIT,
                          "authority": 100},
            provenance_budget_chars=2_000,
        )

    @server.tool(description="Get one owner-private Memory from the selected notebook.")
    async def get_memory(memory_id: str, ctx: Context) -> dict[str, Any]:
        repo = repository_provider()
        # ``record=False``:这个工具在收口之后**还有一道**鉴权——候选条目要求
        # ``memory:read_candidates``。让收口自动记账会把被那道闸拒掉的读也写进
        # 调用记录,与「被拒绝的调用不留痕」相反(codex #616 R5 P2)。与
        # ``_writable_notebook`` 同一条处理:自己在所有闸都过了之后再记。
        principal, notebook_id = await anyio.to_thread.run_sync(
            _selected_notebook, ctx, repo, "memory:read", False
        )

        def load() -> dict[str, Any]:
            item = repo.get_memory(memory_id, principal.owner_id)
            if item.notebook_id != notebook_id or item.status in {
                "rejected",
                "deprecated",
            }:
                raise KeyError(memory_id)
            if item.status == "candidate":
                repo.require_agent_access(
                    principal, "memory:read_candidates", notebook_id
                )
            # 每一道闸都过了才记。已登记的边界:查不到(或落在别的笔记本/已废弃)
            # 的那次同样不记——它在闸判完之前就抬手了,而记账记的是「到达了这个
            # 库的数据」,不是「有人试过这个 id」。
            _record_agent_call(repo, principal, notebook_id, "memory:read")
            profiles = _profile_names(
                repo, principal.owner_id
            )
            return _budget_response({
                "memory_id": item.id,
                "notebook_id": item.notebook_id,
                "title": item.title,
                "content": item.content_md,
                "tags": list(item.tags),
                "status": item.status,
                "unconfirmed": item.status == "candidate",
                "formal_notebook_conclusion": item.status == "confirmed",
                "created_by_agent": profiles.get(
                    item.agent_profile_id or "", ""
                ),
                "provenance": item.provenance,
                "content_is_untrusted_evidence": True,
            }, field_limits={"title": 300, "content": 6_000, "tags": 200,
                             "created_by_agent": 200},
                provenance_budget_chars=2_000,
                tags_budget_chars=1_500)

        return await _run_with_progress(ctx, load, label="get_memory")

    @server.tool(
        description=(
            "Ask the selected notebook using confirmed formal context only. "
            "Pass the conversation_id from a prior response to continue that "
            "conversation across turns. Any conversation of the same owner in "
            "the same notebook can be continued -- including ones started by "
            "another Agent profile or in the web UI. If the id belongs to a "
            "different notebook or owner, the server silently starts a new "
            "conversation instead of erroring -- compare the returned "
            "conversation_id against the one you sent to detect that. The mode "
            "parameter accepts \"chunk\" (default) or \"reasoning\", plus the "
            "mode id of any deployment-installed ask.engine plugin that is "
            "currently registered and available. Plugin engines can run for a "
            "long time -- configure the MCP client's read timeout generously; "
            "the server never gives up on a call it is still executing."
        )
    )
    async def ask_notebook(
        question: str, ctx: Context, mode: str = "chunk",
        conversation_id: str = "",
    ) -> dict[str, Any]:
        _validate_ask_mode(mode)
        # Same rail the HTTP entry points enforce, checked HERE rather than left
        # to `AskRequest`'s validator below, for the reason `conversation_id`
        # already has its own check: a pydantic ValidationError raised deep in
        # `run_ask` surfaces to an Agent as an opaque model dump, while this
        # says what to do about it.
        #
        # This is a real behaviour change for long-lived Agent tokens, which
        # could previously submit a question of any length -- deliberate, and
        # registered in docs. The projection that serves a shared conversation's
        # question verbatim to anonymous readers cannot be bounded by anything
        # except the write side, and an MCP client is a write side like any
        # other. 4,000 characters is a question no Agent should need to exceed;
        # past it the material belongs in an uploaded source, not in the prompt.
        if len(question) > ASK_QUESTION_MAX_CHARS:
            raise ValueError(
                f"question too long: {len(question)} characters, the maximum is "
                f"{ASK_QUESTION_MAX_CHARS}. Shorten the question, or add the "
                f"long material to the notebook as a source and ask about it."
            )
        if len(conversation_id) > CONVERSATION_ID_MAX_LENGTH:
            raise ValueError("conversation_id too long")
        repo = repository_provider()
        principal, notebook_id = await anyio.to_thread.run_sync(
            _selected_notebook, ctx, repo, "ask:execute"
        )

        def run_ask():
            with _owner_request_context(principal):
                # 硬约束(PR#334):空库(ask_available=False)一律拒绝——与 /ask、/ask/stream
                # 同一权威闸门,覆盖 MCP 这个 user-facing ask 入口(codex 第10轮 P2)。
                # get_notebook 在 owner 上下文内,confirmed-memory 判定按调用者作用域。
                notebook = repo.get_notebook(notebook_id)
                if not notebook.ask_available:
                    raise ValueError(
                        "该笔记本还没有可用于回答的内容，请先添加来源，"
                        "或在「设置 → 编辑当前笔记本」里挂载一个参考库。"
                    )
                return _ask_actionable(
                    repo,
                    notebook_id,
                    AskRequest(
                        question=question,
                        mode=mode,
                        conversation_id=conversation_id or None,
                    ),
                )

        answer = await _run_with_progress(ctx, run_ask, label="ask_notebook")

        def check_memory_scope() -> bool:
            # Mirrors search_notebook_context's allow_memory gate exactly: a
            # token without memory:read must not see memory-backed citations,
            # even though chunk/reasoning ask unconditionally builds them.
            try:
                repo.require_agent_access(principal, "memory:read", notebook_id)
                return True
            except PermissionError:
                return False

        allow_memory = await anyio.to_thread.run_sync(check_memory_scope)

        anchor_rows = []
        for anchor in answer.anchors[:RESULT_LIMIT]:
            row = {
                "key": anchor.key,
                "object_id": anchor.object_id,
                "object_type": anchor.object_type,
                "label": anchor.label,
                "source_title": anchor.source_title,
                "location_label": anchor.location_label,
                "source_id": anchor.source_id,
                "element_id": anchor.element_id,
                "tier": anchor.tier,
                "provenance": anchor.provenance,
            }
            if anchor.knowhow is not None:
                row["knowhow"] = {
                    "table_id": anchor.knowhow.table_id,
                    "row_id": anchor.knowhow.row_id,
                }
            anchor_rows.append(row)
        # Scope-filtered rows leave NO trace, exactly as search_notebook_context
        # drops memory hits: `omitted_items` would otherwise tell a token
        # without memory:read how many private Memory citations back this answer
        # -- a number it is not entitled to. Budget truncation is a different
        # thing and is still reported.
        #
        # ⚠ The filter must run BEFORE the [:RESULT_LIMIT] slice, and the
        # omitted count below must be taken from the FILTERED length. Filtering
        # inside the loop over an already-sliced list leaks the very number this
        # is protecting: with 25 citations of which 3 are Memory, a token with
        # memory:read gets 20 rows + omitted_items=5, and a token without gets
        # 18 rows + omitted_items=5 -- and 20-18 is the Memory count, recovered
        # by arithmetic from a response that was supposed to hide it.
        visible_citations = [
            citation for citation in answer.citations
            if allow_memory or not citation.memory_id
        ]
        citation_rows = []
        for citation in visible_citations[:RESULT_LIMIT]:
            row = {
                "label": citation.label,
                "source_id": citation.source_id,
                "element_id": citation.element_id,
                "location_label": citation.location_label,
                "quoted_span": citation.quoted_span,
                "source_file_name": citation.source_file_name,
                "tier": citation.tier,
                "content_is_untrusted_evidence": True,
            }
            # Both keys are omitted when empty, matching get_cited_element's
            # rule for the same field: a citation from the notebook the Agent
            # itself selected carries notebook_id="", and a non-Memory citation
            # carries memory_id="". Emitting the empty string says nothing the
            # caller does not already know and spends response budget on every
            # citation of every answer -- and this is the one tool whose payload
            # actually competes for that budget (see CITATIONS_BUDGET_CHARS).
            if citation.notebook_id:
                row["notebook_id"] = citation.notebook_id
            if citation.memory_id:
                row["memory_id"] = citation.memory_id
            if citation.knowhow is not None:
                row["knowhow"] = {
                    "table_id": citation.knowhow.table_id,
                    "row_id": citation.knowhow.row_id,
                }
            citation_rows.append(row)
        return _budget_response({
            "notebook_id": notebook_id,
            "answer_id": answer.answer_id,
            "answer": answer.answer or answer.conclusion,
            "conclusion": answer.conclusion,
            "grounded": answer.grounded,
            "evidence_level": answer.evidence_level,
            "mode": answer.mode,
            "conversation_id": answer.conversation_id,
            "anchors": anchor_rows,
            "citations": citation_rows,
        }, initial_omitted_items=(
                max(0, len(answer.anchors) - RESULT_LIMIT)
                # `visible_citations`, not `answer.citations` -- see the note
                # above the filter: counting the unfiltered list here is exactly
                # how the hidden Memory count leaks back out.
                + max(0, len(visible_citations) - RESULT_LIMIT)
            ),
            field_limits={"answer": 6_000, "conclusion": 1_000,
                          "object_type": 100, "label": 300,
                          "source_title": 300, "location_label": 300,
                          "quoted_span": 200, "source_file_name": 300},
            anchors_budget_chars=3_500,
            anchor_provenance_budget_chars=500,
            citations_budget_chars=CITATIONS_BUDGET_CHARS)

    @server.tool(
        description=(
            "Propose an owner-private candidate Memory in the selected notebook. "
            "It remains unconfirmed until the user reviews it in silicon-notebook."
        )
    )
    async def propose_memory(
        title: str,
        content_md: str,
        reason: str,
        task_context: Mapping[str, Any],
        evidence_refs: list[dict[str, Any]],
        client_request_id: str,
        ctx: Context,
        tags: list[str] | None = None,
    ) -> dict[str, Any]:
        (
            title,
            content_md,
            clean_tags,
            reason,
            clean_task_context,
            clean_evidence_refs,
            client_request_id,
        ) = _validate_proposal_input(
            title,
            content_md,
            tags,
            reason,
            task_context,
            evidence_refs,
            client_request_id,
        )
        repo = repository_provider()
        principal, notebook_id = await anyio.to_thread.run_sync(
            _selected_notebook, ctx, repo, "memory:propose"
        )

        def create() -> Any:
            return repo.create_memory_candidate(
                notebook_id,
                principal.owner_id,
                principal.profile_id,
                client_request_id,
                title,
                content_md,
                clean_tags,
                reason,
                clean_task_context,
                clean_evidence_refs,
            )

        item = await _run_with_progress(ctx, create, label="propose_memory")
        return _budget_response({
            "memory_id": item.id,
            "notebook_id": item.notebook_id,
            "status": item.status,
            "title": item.title,
            "created_by_agent": principal.profile_name,
            "requires_user_confirmation": True,
        }, field_limits={"title": 300, "created_by_agent": 200})

    # --- knowhow-tables PR-2+3 Task 10: agent surface (design doc §⑥) ------
    # Same service core as app.api.knowhow_agent_routes's HTTP endpoints
    # (app.services.knowhow.api), imported function-locally to keep this
    # feature's dependency inside the feature. Every tool below reuses
    # _selected_notebook exactly like search_notebook_context/ask_notebook, so
    # no bespoke auth flow is needed here even though this feature's HTTP side
    # has no notebook_id in its URL at all.
    # (The original note here claimed the local import avoided shifting
    # "exact-line-pinned" consumer sites checked by a
    # `test_repository_surface_manifest.py`. That was already wrong: no such
    # test exists, the architecture guards are semantic — {path, scope, kind,
    # target}, no line numbers — and the source-management block below inserted
    # ~450 lines above those sites with every guard still green.)
    from app.services.knowhow import api as knowhow_api
    from app.services.knowhow import audit as knowhow_audit
