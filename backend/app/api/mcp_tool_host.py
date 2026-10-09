"""The only bridge from the frozen core Agent tool catalog into FastMCP.

Three things happen here and nowhere else:

* every core tool is registered exactly once (``register_agent_tools``);
* every tool declares the permission tier it needs at registration, and
  ``tools/list`` shows a caller only the tools its token's LIVE tiers can use
  (``TieredFastMCP``) -- calling an unlisted tool still reaches the tool and
  is refused there with ``scope_missing``;
* every exception a tool raises is translated, once, into the Agent error
  model ``[<code>] <中文说明>`` (``agent_tool_error``).
"""
from __future__ import annotations

from dataclasses import dataclass
from functools import wraps
import logging
from types import MappingProxyType
from typing import Any, Callable, Mapping

import anyio
from mcp.server.fastmcp import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from pydantic import ValidationError

from app.api.mcp_tools._shared import (
    _MCP_PRINCIPAL,
    AgentToolError,
    MirroredNotebookError,
    OwnerOnlyError,
)
from app.api.mcp_tools._messages import GENERIC_INVALID, chinese_value_error
from app.api.mcp_tools.citations import register_citation_tools
from app.api.mcp_tools.ask import register_ask_tools
from app.api.mcp_tools.global_ask import global_ask_tool_error
from app.api.mcp_tools.knowhow import register_knowhow_tools
from app.api.mcp_tools.maintenance import register_maintenance_tools
from app.api.mcp_tools.memory_context import (
    register_memory_context_tools,
    register_memory_proposal_tools,
)
from app.api.mcp_tools.profiles import register_profile_tools
from app.api.mcp_tools.session import register_session_tools
from app.api.mcp_tools.sources import register_source_tools
from app.core.json_safety import JsonSafetyError
from app.core.memory_inputs import MemoryInputError
from app.domain.agent_tools import AGENT_TIERS, AgentAccessDenied
from app.domain.indexing_pipeline import (
    IndexingPipelineRebuildActiveError,
    IndexingPipelineUnavailableError,
)
from app.repositories.ports import ChunkLexicalSearchTimeout


logger = logging.getLogger(__name__)


_CORE_REGISTRARS = (
    register_session_tools,
    register_memory_context_tools,
    register_citation_tools,
    register_ask_tools,
    register_memory_proposal_tools,
    register_profile_tools,
    register_source_tools,
    register_maintenance_tools,
    register_knowhow_tools,
)


@dataclass(frozen=True, slots=True)
class _CoreTool:
    name: str
    description: str
    handler: Callable[..., object]
    tier: str | None


class _CoreToolCapture:
    def __init__(self) -> None:
        self.tools: list[_CoreTool] = []
        self._names: set[str] = set()

    def tool(self, *, description: str, tier: str | None):
        if type(description) is not str or not description:
            raise RuntimeError("invalid core Agent tool description")
        if tier is not None and tier not in AGENT_TIERS:
            raise RuntimeError("invalid core Agent tool tier")

        def register(handler: Callable[..., object]):
            name = getattr(handler, "__name__", "")
            if (
                not callable(handler)
                or type(name) is not str
                or not name
                or name in self._names
            ):
                raise RuntimeError("invalid or duplicate core Agent tool")
            self._names.add(name)
            self.tools.append(_CoreTool(name, description, handler, tier))
            return handler

        return register


def capture_core_agent_tools(
    repository_provider: Callable[[], Any],
) -> tuple[_CoreTool, ...]:
    capture = _CoreToolCapture()
    for registrar in _CORE_REGISTRARS:
        registrar(capture, repository_provider)
    return tuple(capture.tools)


def _poison_provider():
    raise AssertionError("core tool discovery must not resolve the repository")


def core_public_tool_names() -> tuple[str, ...]:
    return tuple(tool.name for tool in capture_core_agent_tools(_poison_provider))


def core_tool_tiers() -> Mapping[str, str | None]:
    """Tool name -> the tier it requires (``None`` = always listed).

    The one declaration both ``tools/list`` filtering and the documentation
    tests read."""
    return MappingProxyType(
        {tool.name: tool.tier for tool in capture_core_agent_tools(_poison_provider)}
    )


# ---------------------------------------------------------------- error model

#: Tool name -> the resource a bare ``KeyError`` from it means, for the
#: ``not_found`` copy. A KeyError's own text (an internal id/repr) is never
#: echoed.
_NOT_FOUND_RESOURCE = {
    "get_notebook": "笔记本",
    "read_reference": "引用（来源元素、Memory 或全局问答引用）",
    "list_sources": "来源",
    "reparse_source": "来源",
    "delete_source": "来源",
    "get_knowhow_discrimination": "Knowhow 表",
    "get_knowhow_row": "Knowhow 行",
    "put_knowhow_cell_code": "Knowhow 行或列",
    "ask": "会话或笔记本",
    "get_ask": "问答任务",
}
_INTERNAL = "服务内部错误，请稍后重试；如仍失败，请联系管理员"
_ACCESS_DENIED_CODES = {
    "inactive": "token_inactive",
    "scope_missing": "scope_missing",
    "notebook_not_allowed": "notebook_not_allowed",
    "notebook_unreadable": "notebook_unreadable",
}


_PIPELINE_UNAVAILABLE = "所选索引管线当前不可用；旧索引仍可读取。请切回内建管线后重试。"
_REBUILD_ACTIVE = "索引重建正在进行；等它完成后再重试。"
_SEARCH_TIMEOUT = "检索超时，请稍后重试；可以缩小问题或检索范围"


def _domain_refusal(exc: BaseException) -> AgentToolError | None:
    """Domain refusals the HTTP surface maps in ``main.py`` / its routes,
    mapped the same way here (the RuntimeError-based ones would otherwise
    read as ``internal``)."""
    if isinstance(exc, IndexingPipelineUnavailableError):
        return AgentToolError("unavailable", _PIPELINE_UNAVAILABLE)
    if isinstance(exc, IndexingPipelineRebuildActiveError):
        return AgentToolError("busy", _REBUILD_ACTIVE)
    if isinstance(exc, ChunkLexicalSearchTimeout) or type(exc).__name__ == "QueryCanceled":
        # A PostgreSQL statement timeout (or an administrative cancel): the
        # HTTP twin answers 503 ``query_timeout``.
        return AgentToolError("unavailable", _SEARCH_TIMEOUT)
    return None


def agent_tool_error(exc: BaseException, *, tool: str) -> AgentToolError:
    """Translate one tool exception into the Agent error model.

    Order matters: ``AgentAccessDenied`` and ``OwnerOnlyError`` are
    ``PermissionError``s, and a pydantic ``ValidationError`` is a
    ``ValueError`` whose text is an opaque model dump.
    """
    if isinstance(exc, AgentToolError):
        return exc
    if isinstance(exc, AgentAccessDenied):
        return AgentToolError(
            _ACCESS_DENIED_CODES.get(exc.reason, "scope_missing"), str(exc)
        )
    if isinstance(exc, OwnerOnlyError):
        return AgentToolError("owner_only", str(exc))
    if isinstance(exc, MirroredNotebookError):
        return AgentToolError("mirrored", str(exc))
    mapped = global_ask_tool_error(exc)
    if mapped is not None:
        return mapped
    resource = _NOT_FOUND_RESOURCE.get(tool, "资源")
    if isinstance(exc, (KeyError, PermissionError)):
        # A bare PermissionError from a repository read (no structured
        # reason) answers exactly like a missing id: no existence disclosure.
        return AgentToolError(
            "not_found",
            f"没有找到这个{resource}（不存在、已删除，或不在此凭证可访问的范围内）",
        )
    if isinstance(exc, ValidationError):
        fields = ", ".join(
            ".".join(str(part) for part in err.get("loc", ())) or "?"
            for err in exc.errors()[:5]
        )
        return AgentToolError("invalid_argument", f"参数格式不正确：{fields}")
    domain = _domain_refusal(exc)
    if domain is not None:
        return domain
    if isinstance(exc, ValueError):
        # Only a ValueError that is approved copy (Chinese) or a known
        # validator shape (``mcp_tools._messages``) is an argument error the
        # Agent can act on. Any other ValueError is incidental (an ``int()``
        # on bad data, a library's internal check): ``internal``, never echoed.
        message = chinese_value_error(str(exc))
        if message is None and isinstance(exc, (MemoryInputError, JsonSafetyError)):
            message = GENERIC_INVALID
        if message is not None:
            return AgentToolError("invalid_argument", message)
    # Never echo an unexpected exception: it may carry paths or SQL. Only
    # the class name reaches the log.
    logger.warning("MCP tool %s failed (%s)", tool, type(exc).__name__)
    return AgentToolError("internal", _INTERNAL)


def _agent_facing(name: str, handler: Callable[..., Any]) -> Callable[..., Any]:
    @wraps(handler)
    async def guarded(*args: Any, **kwargs: Any) -> Any:
        try:
            return await handler(*args, **kwargs)
        except Exception as exc:  # noqa: BLE001 - translated, never swallowed
            raise agent_tool_error(exc, tool=name) from None

    return guarded


# ------------------------------------------------------------ tier filtering


class TieredFastMCP(FastMCP):
    """``tools/list`` filtered by the caller's live token tiers.

    The caller is the principal ``AgentBearerMiddleware`` bound for this
    request/session; its tiers are re-read from the token row on every
    listing, so an in-place permission edit shows up on the next listing.
    Outside any MCP request (startup discovery, documentation tests) there is
    no principal and the full catalog is listed with zero repository work.
    """

    _silicon_tool_tiers: Mapping[str, str | None] = MappingProxyType({})
    _silicon_repository_provider: Callable[[], Any] | None = None

    async def call_tool(self, name: str, arguments: dict[str, Any]):  # type: ignore[override]
        """FastMCP's own argument validation and unknown-tool refusal, in
        the Agent error model (they happen before any tool body, so the
        registration wrapper never sees them)."""
        try:
            return await super().call_tool(name, arguments)
        except ToolError as exc:
            cause = exc.__cause__
            if isinstance(cause, ValidationError):
                raise ToolError(
                    f"Error executing tool {name}: "
                    + str(AgentToolError("invalid_argument", _argument_errors(cause)))
                ) from None
            if cause is None and str(exc).startswith("Unknown tool"):
                raise ToolError(str(AgentToolError(
                    "not_found", "没有这个工具；可用的工具以 tools/list 为准"
                ))) from None
            raise

    async def list_tools(self):  # type: ignore[override]
        tools = await super().list_tools()
        principal = _MCP_PRINCIPAL.get()
        provider = self._silicon_repository_provider
        if principal is None or provider is None:
            return tools
        tiers = await anyio.to_thread.run_sync(
            _live_tiers, provider, principal.token_id
        )
        return [
            tool for tool in tools
            if (required := self._silicon_tool_tiers.get(tool.name)) is None
            or required in tiers
        ]


def _argument_errors(exc: ValidationError) -> str:
    """``参数不合法：query（缺少）、limit（类型或格式不对）`` -- field names only,
    never the submitted values."""
    parts = []
    for err in exc.errors()[:5]:
        loc = [str(part) for part in err.get("loc", ()) if part != "Arguments"]
        field = ".".join(loc) or "参数"
        kind = "缺少" if err.get("type") == "missing" else "类型或格式不对"
        parts.append(f"{field}（{kind}）")
    return "参数不合法：" + "、".join(parts)


def _live_tiers(provider: Callable[[], Any], token_id: str) -> frozenset[str]:
    live = provider().refresh_agent_principal(token_id)
    return frozenset(live.scopes) if live is not None else frozenset()


def register_agent_tools(
    server: FastMCP,
    repository_provider: Callable[[], Any],
) -> tuple[str, ...]:
    """Register the frozen core Agent tool prefix exactly once."""
    core_tools = capture_core_agent_tools(repository_provider)
    names = tuple(tool.name for tool in core_tools)
    for tool in core_tools:
        server.add_tool(
            _agent_facing(tool.name, tool.handler),
            name=tool.name,
            description=tool.description,
        )
    if isinstance(server, TieredFastMCP):
        server._silicon_tool_tiers = MappingProxyType(
            {tool.name: tool.tier for tool in core_tools}
        )
        server._silicon_repository_provider = repository_provider
    return names
