from __future__ import annotations

import ast
import inspect
from pathlib import Path
import re

import pytest
from mcp.server.fastmcp import FastMCP

from app.api import mcp_server
from app.api.mcp_tools.citations import register_citation_tools
from app.api.mcp_tools.knowhow import register_knowhow_tools
from app.api.mcp_tools.maintenance import register_maintenance_tools
from app.api.mcp_tools.memory_context import register_memory_context_tools
from app.api.mcp_tools.profiles import register_profile_tools
from app.api.mcp_tools.session import register_session_tools
from app.api.mcp_tools.sources import register_source_tools
from app.domain.agent_tools import AGENT_SCOPES


_REGISTRARS = (
    register_session_tools,
    register_memory_context_tools,
    register_knowhow_tools,
    register_citation_tools,
    register_source_tools,
    register_maintenance_tools,
    register_profile_tools,
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
    seen: set[str] = set()
    for registrar in _REGISTRARS:
        capture = _CaptureServer()
        registrar(capture, _poison_provider)
        assert capture.names, registrar.__name__
        assert not seen.intersection(capture.names), registrar.__name__
        seen.update(capture.names)
        combined.extend(capture.names)

    assert tuple(combined) == mcp_server.CORE_TOOLS
    assert len(combined) == len(set(combined))


@pytest.mark.anyio
async def test_server_construction_and_tool_listing_do_zero_repository_work() -> None:
    server, _app = mcp_server.create_memory_mcp(_poison_provider)
    tools = await server.list_tools()
    assert tuple(tool.name for tool in tools) == mcp_server.PUBLIC_TOOLS


@pytest.mark.anyio
async def test_unified_host_preserves_every_core_tool_descriptor() -> None:
    legacy = FastMCP("legacy descriptor oracle")
    for registrar in _REGISTRARS:
        registrar(legacy, _poison_provider)
    unified, _app = mcp_server.create_memory_mcp(_poison_provider)
    legacy_tools = await legacy.list_tools()
    unified_tools = await unified.list_tools()
    assert [tool.model_dump(mode="json") for tool in unified_tools] == [
        tool.model_dump(mode="json") for tool in legacy_tools
    ]


def test_composition_has_one_point_specific_tool_provider_seat() -> None:
    signature = inspect.signature(mcp_server.create_memory_mcp)
    assert tuple(signature.parameters) == (
        "repository_provider",
        "agent_tool_provider_host",
        "agent_tool_audit_sink",
        "allowed_origins",
        "public_url",
        "require_https",
    )

    tools_dir = Path(mcp_server.__file__).with_name("mcp_tools")
    paths = (Path(mcp_server.__file__), *sorted(tools_dir.glob("*.py")))
    forbidden = ("extension_sdk", "app.extensions", "registry")
    for path in paths:
        tree = ast.parse(path.read_text(encoding="utf-8"))
        imported: list[str] = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imported.append(node.module or "")
                assert all(alias.name != "*" for alias in node.names), path
            elif isinstance(node, ast.Import):
                imported.extend(alias.name for alias in node.names)
        assert not any(
            marker in target for target in imported for marker in forbidden
        ), path
        declarations = {
            node.name.lower()
            for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
        }
        assert not declarations.intersection(
            {"tool_registry", "tool_descriptor"}
        ), path
        if path.parent == tools_dir:
            assert not any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "FastMCP"
                for node in ast.walk(tree)
            ), path

    host_path = Path(mcp_server.__file__).with_name("mcp_tool_host.py")
    host_tree = ast.parse(host_path.read_text(encoding="utf-8"))
    assert sum(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_tool"
        for node in ast.walk(host_tree)
    ) == 2
    server_tree = ast.parse(Path(mcp_server.__file__).read_text(encoding="utf-8"))
    assert not any(
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "add_tool"
        for node in ast.walk(server_tree)
    )


def test_each_public_handler_has_one_progress_wrapped_main_body() -> None:
    tools_dir = Path(mcp_server.__file__).with_name("mcp_tools")
    handlers: dict[str, ast.AsyncFunctionDef] = {}
    for path in tools_dir.glob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name in mcp_server.CORE_TOOLS:
                handlers[node.name] = node

    assert tuple(name for name in mcp_server.CORE_TOOLS if name in handlers) == (
        mcp_server.CORE_TOOLS
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


def test_frontend_scope_options_equal_the_core_scope_vocabulary() -> None:
    root = Path(__file__).resolve().parents[2]
    source = (root / "frontend/app/agent-token-model.ts").read_text(
        encoding="utf-8"
    )
    block = source.split("export const AGENT_SCOPE_OPTIONS = [", 1)[1].split(
        "] as const;", 1
    )[0]
    frontend_scopes = re.findall(r'value:\s*"([a-z_]+:[a-z_]+)"', block)
    assert len(frontend_scopes) == len(set(frontend_scopes))
    assert set(frontend_scopes) == set(AGENT_SCOPES)


def test_provider_adapter_has_exactly_one_progress_wrapper() -> None:
    host_path = Path(mcp_server.__file__).with_name("mcp_tool_host.py")
    tree = ast.parse(host_path.read_text(encoding="utf-8"))
    adapter = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_plugin_adapter"
    )
    progress_calls = [
        node
        for node in ast.walk(adapter)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Name)
        and node.func.id == "_run_with_progress"
    ]
    assert len(progress_calls) == 1
