"""Notebook search and Memory proposal MCP tools."""

from typing import Any, Callable, Mapping, Sequence

import anyio
from mcp.server.fastmcp import Context, FastMCP

from app.core.memory_inputs import (
    normalize_client_request_id,
    normalize_content,
    normalize_evidence_refs,
    normalize_reason,
    normalize_tags,
    normalize_task_context,
    normalize_title,
)
from app.services.agent_profile_block import resolve_agent_profile_names
from app.services.search_concurrency import run_under_search_gate
from app.services.source_scope import memory_access_context

from ._shared import (
    RESULT_LIMIT,
    TEXT_LIMIT,
    AgentToolError,
    _budget_response,
    _owner_request_context,
    _run_with_progress,
    _selected_notebook,
)
from .refs import element_ref, memory_ref


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
        raise AgentToolError("invalid_argument", "reason 不能为空")
    clean_tags = normalize_tags(tags or [])
    clean_task_context = normalize_task_context(task_context)
    if not clean_task_context:
        raise AgentToolError("invalid_argument", "task_context 不能为空")
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


def _memory_read_allowed(repo: Any, principal: Any, notebook_id: str) -> bool:
    """Whether this token holds ``memory:read`` on the notebook right now.

    The one gate both context tools read: its answer opens or closes the
    private-Memory channel for the whole run (``memory_access_context``) AND
    decides which answer items may reach the wire (``_strip_memory_items``).
    A ``PermissionError`` is a deny, never an error.
    """
    try:
        repo.require_agent_access(principal, "memory:read", notebook_id)
    except PermissionError:
        return False
    return True


def _memory_rows(
    repo: Any, principal: Any, notebook_id: str, query: str, limit: int
) -> list[dict[str, Any]]:
    """``search(include="memory")``: the owner's private Memory, candidate +
    confirmed (candidates only with ``memory:read_candidates``)."""
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
    profiles = _profile_names(repo, principal.owner_id)
    rows: list[dict[str, Any]] = []
    for hit in hits:
        try:
            record = repo.get_memory(hit.memory_id, principal.owner_id)
        except (KeyError, PermissionError):
            # Retrieval and hydration are separate reads. A lifecycle
            # transition/delete/access loss between them must fail closed for
            # this hit without aborting the whole search.
            continue
        if record.notebook_id != notebook_id or record.status not in {
            "candidate", "confirmed"
        }:
            continue
        if record.status == "candidate" and not include_candidates:
            continue
        rows.append(
            {
                "type": "memory",
                "ref": memory_ref(notebook_id, record.id),
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


def _formal_rows(
    repo: Any, principal: Any, notebook_id: str, query: str
) -> list[dict[str, Any]]:
    """``search(include="formal")``: source, KG, and confirmed Memory.

    The channel is closed IN this worker thread, before the search runs:
    without memory:read the Memory retriever is never called. That covers
    Memory ITEMS only.  This search installs no retrieval ceiling, and its
    knowledge-graph leg does not filter Memory-derived objects (the owner's
    or another member's): the channel does not reach that leg's store query.
    The per-row ``memory_id`` check below is not an enforcement point either:
    with the channel closed no Memory item reaches it.
    """
    allow_memory = _memory_read_allowed(repo, principal, notebook_id)
    with _owner_request_context(principal), memory_access_context(allow_memory):
        response = repo.search_notebook(notebook_id, query)
    rows: list[dict[str, Any]] = []
    for hit in response.hits:
        if hit.memory_id and not allow_memory:
            continue
        rows.append(
            {
                "type": "memory" if hit.memory_id else hit.scope.lower(),
                "ref": (
                    memory_ref(notebook_id, hit.memory_id) if hit.memory_id
                    else element_ref(notebook_id, hit.source_id, hit.element_id)
                ),
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


def register_memory_context_tools(
    server: FastMCP, repository_provider: Callable[[], Any]
) -> None:
    @server.tool(
        description=(
            "Search one notebook (notebook_id omitted = the token's default "
            "notebook). include=\"formal\" (default) searches source, KG, and "
            "confirmed Memory -- candidate Memory is never returned. "
            "include=\"memory\" searches the token owner's private Memory "
            "only: candidate entries are unconfirmed evidence and never formal "
            "notebook conclusions. Every hit carries a `ref` (null when the hit "
            "has no readable target): pass it to read_reference for the full "
            "text. Requires the read permission."
        ),
        tier="read",
    )
    async def search(
        query: str, ctx: Context, notebook_id: str = "", include: str = "formal",
        limit: int = 12,
    ) -> dict[str, Any]:
        if include not in ("formal", "memory"):
            raise AgentToolError(
                "invalid_argument", "include 只能是 formal 或 memory"
            )
        repo = repository_provider()
        principal, notebook_id = await anyio.to_thread.run_sync(
            _selected_notebook, repo, notebook_id,
            "knowledge:read" if include == "formal" else "memory:read",
        )

        def load() -> list[dict[str, Any]]:
            if include == "memory":
                return _memory_rows(repo, principal, notebook_id, query, limit)
            return _formal_rows(repo, principal, notebook_id, query)

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
        #
        # 私有 Memory 检索(include="memory")不经过这道闸:它从来不走 notebook
        # 搜索的重型通道,旧的 search_agent_memory 同样不排队。
        async def gated_search() -> list[dict[str, Any]]:
            return await _run_with_progress(ctx, load, label="search")

        rows = await (
            run_under_search_gate(gated_search) if include == "formal"
            else gated_search()
        )
        cap = max(1, min(int(limit), RESULT_LIMIT))
        return _budget_response(
            {"notebook_id": notebook_id, "include": include, "items": rows[:cap]},
            initial_omitted_items=max(0, len(rows) - cap),
            field_limits={"type": 100, "label": 300, "text": TEXT_LIMIT,
                          "authority": 100, "title": 300, "content": TEXT_LIMIT,
                          "created_by_agent": 200},
            provenance_budget_chars=2_000,
        )


def register_memory_proposal_tools(
    server: FastMCP, repository_provider: Callable[[], Any]
) -> None:
    @server.tool(
        description=(
            "Propose an owner-private candidate Memory in a notebook "
            "(notebook_id omitted = the token's default notebook). It remains "
            "unconfirmed until the user reviews it in silicon-notebook. "
            "Requires the contribute permission."
        ),
        tier="contribute",
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
        notebook_id: str = "",
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
            _selected_notebook, repo, notebook_id, "memory:propose"
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
            "ref": memory_ref(item.notebook_id, item.id),
            "notebook_id": item.notebook_id,
            "status": item.status,
            "title": item.title,
            "created_by_agent": principal.profile_name,
            "requires_user_confirmation": True,
        }, field_limits={"title": 300, "created_by_agent": 200})
