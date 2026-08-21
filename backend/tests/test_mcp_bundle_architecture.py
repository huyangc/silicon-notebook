from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app.api import mcp_server
from app.api.mcp_tools.citations import register_citation_tools
from app.api.mcp_tools.knowhow import register_knowhow_tools
from app.api.mcp_tools.maintenance import register_maintenance_tools
from app.api.mcp_tools.memory_context import register_memory_context_tools
from app.api.mcp_tools.profiles import register_profile_tools
from app.api.mcp_tools.session import register_session_tools
from app.api.mcp_tools.sources import register_source_tools


_BUNDLES = (
    (register_session_tools, ("list_notebooks", "select_notebook")),
    (
        register_memory_context_tools,
        (
            "search_agent_memory",
            "search_notebook_context",
            "get_memory",
            "ask_notebook",
            "propose_memory",
        ),
    ),
    (
        register_knowhow_tools,
        (
            "list_knowhow_tables",
            "get_knowhow_discrimination",
            "get_knowhow_row",
            "put_knowhow_cell_code",
        ),
    ),
    (register_citation_tools, ("get_cited_element",)),
    (
        register_source_tools,
        (
            "add_source_text",
            "add_source_url",
            "get_source_status",
            "reparse_source",
            "delete_source",
        ),
    ),
    (
        register_maintenance_tools,
        ("build_kg", "build_retrieval_index", "get_build_status"),
    ),
    (register_profile_tools, ("get_notebook_profile", "add_observation")),
)


class _CaptureServer:
    def __init__(self) -> None:
        self.names: list[str] = []

    def tool(self, *, description: str):
        assert type(description) is str and description

        def register(function):
            self.names.append(function.__name__)
            return function

        return register


def _poison_provider():
    raise AssertionError("tool registration must not resolve the repository")


def test_fixed_builtin_bundles_partition_the_ordered_public_surface() -> None:
    combined: list[str] = []
    for registrar, expected in _BUNDLES:
        capture = _CaptureServer()
        registrar(capture, _poison_provider)
        assert tuple(capture.names) == expected
        combined.extend(capture.names)

    assert tuple(combined) == mcp_server.PUBLIC_TOOLS
    assert len(combined) == len(set(combined))


@pytest.mark.anyio
async def test_server_construction_and_tool_listing_do_zero_repository_work() -> None:
    server, _app = mcp_server.create_memory_mcp(_poison_provider)
    tools = await server.list_tools()
    assert tuple(tool.name for tool in tools) == mcp_server.PUBLIC_TOOLS


def test_composition_has_no_generic_tool_provider_or_extension_seat() -> None:
    signature = inspect.signature(mcp_server.create_memory_mcp)
    assert tuple(signature.parameters) == (
        "repository_provider",
        "allowed_origins",
        "public_url",
        "require_https",
    )

    source = inspect.getsource(mcp_server)
    tree = ast.parse(source)
    forbidden = ("extension_sdk", "app.extensions", "tool_provider", "registry")
    imports = [
        node.module or ""
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom)
    ]
    imports.extend(
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    )
    assert not any(
        marker in imported for imported in imports for marker in forbidden[:2]
    )
    assert "tool_provider" not in source


def test_each_public_handler_has_one_progress_wrapped_main_body() -> None:
    tools_dir = Path(mcp_server.__file__).with_name("mcp_tools")
    handlers: dict[str, ast.AsyncFunctionDef] = {}
    for path in tools_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name in mcp_server.PUBLIC_TOOLS:
                handlers[node.name] = node

    assert tuple(name for name in mcp_server.PUBLIC_TOOLS if name in handlers) == (
        mcp_server.PUBLIC_TOOLS
    )
    for name, handler in handlers.items():
        progress_calls = [
            node
            for node in ast.walk(handler)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "_run_with_progress"
        ]
        assert len(progress_calls) == 1, name
