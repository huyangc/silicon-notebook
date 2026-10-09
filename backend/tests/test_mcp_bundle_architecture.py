from __future__ import annotations

import ast
import inspect
from pathlib import Path
import re

import pytest
from mcp.server.fastmcp import FastMCP

from app.api import mcp_server
from app.api.mcp_tools.citations import register_citation_tools
from app.api.mcp_tools.ask import register_ask_tools
from app.api.mcp_tools.knowhow import register_knowhow_tools
from app.api.mcp_tools.maintenance import register_maintenance_tools
from app.api.mcp_tools.memory_context import (
    register_memory_context_tools,
    register_memory_proposal_tools,
)
from app.api.mcp_tools.profiles import register_profile_tools
from app.api.mcp_tools.session import register_session_tools
from app.api.mcp_tools.sources import register_source_tools
from app.domain.agent_tools import AGENT_SCOPES, AGENT_TIERS


_REGISTRARS = (
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


class _CaptureServer:
    def __init__(self) -> None:
        self.names: list[str] = []
        self.tiers: dict[str, str | None] = {}

    def tool(self, *, description: str, tier: str | None):
        assert type(description) is str and description
        assert tier is None or tier in AGENT_TIERS

        def register(function):
            self.names.append(function.__name__)
            self.tiers[function.__name__] = tier
            return function

        return register


class _LegacyAdapter:
    """A plain FastMCP behind the registrars' ``tool(description, tier)``
    seat: the descriptor oracle knows nothing about tiers."""

    def __init__(self, server: FastMCP) -> None:
        self.server = server

    def tool(self, *, description: str, tier: str | None):
        return self.server.tool(description=description)


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
        registrar(_LegacyAdapter(legacy), _poison_provider)
    unified, _app = mcp_server.create_memory_mcp(_poison_provider)
    legacy_tools = await legacy.list_tools()
    unified_tools = await unified.list_tools()
    assert [tool.model_dump(mode="json") for tool in unified_tools] == [
        tool.model_dump(mode="json") for tool in legacy_tools
    ]


def test_every_tool_declares_its_tier_once_and_only_discovery_is_tierless() -> None:
    capture = _CaptureServer()
    for registrar in _REGISTRARS:
        registrar(capture, _poison_provider)
    assert dict(mcp_server.TOOL_TIERS) == capture.tiers
    assert [name for name, tier in capture.tiers.items() if tier is None] == [
        "list_notebooks"
    ]
    # Every tier opens at least one tool: a tier a token can hold but no tool
    # reads would be a permission without a door.
    assert {tier for tier in capture.tiers.values() if tier} == set(AGENT_TIERS)


@pytest.mark.anyio
async def test_tools_list_is_filtered_by_the_live_tiers_of_the_bound_principal() -> None:
    from types import SimpleNamespace

    from app.api.mcp_tools._shared import _MCP_PRINCIPAL

    live = {"scopes": ["read"]}
    reads: list[str] = []

    def refresh(token_id):
        reads.append(token_id)
        return SimpleNamespace(scopes=list(live["scopes"])) if live["scopes"] else None

    repo = SimpleNamespace(refresh_agent_principal=refresh)
    server, _app = mcp_server.create_memory_mcp(lambda: repo)
    marker = _MCP_PRINCIPAL.set(SimpleNamespace(token_id="token-1"))
    try:
        read_only = [tool.name for tool in await server.list_tools()]
        live["scopes"] = ["ask", "delete"]
        edited = [tool.name for tool in await server.list_tools()]
        live["scopes"] = []
        inactive = [tool.name for tool in await server.list_tools()]
    finally:
        _MCP_PRINCIPAL.reset(marker)
    tiers = mcp_server.TOOL_TIERS
    assert read_only == [
        name for name in mcp_server.PUBLIC_TOOLS if tiers[name] in (None, "read")
    ]
    assert edited == [
        name for name in mcp_server.PUBLIC_TOOLS
        if tiers[name] in (None, "ask", "delete")
    ]
    assert inactive == ["list_notebooks"]
    assert reads == ["token-1"] * 3


def test_tool_exceptions_map_to_one_chinese_error_model() -> None:
    from pydantic import BaseModel, ValidationError

    from app.api.mcp_tool_host import agent_tool_error
    from app.api.mcp_tools._shared import (
        AgentToolError, MirroredNotebookError, OwnerOnlyError,
    )
    from app.domain.agent_tools import AgentAccessDenied
    from app.core.memory_inputs import MemoryInputError
    from app.domain.indexing_pipeline import (
        IndexingPipelineRebuildActiveError,
        IndexingPipelineUnavailableError,
    )
    from app.repositories.ports import ChunkLexicalSearchTimeout
    from app.services.global_ask import GlobalAskError

    class _Shape(BaseModel):
        value: int

    try:
        _Shape(value="x")
    except ValidationError as exc:
        validation = exc

    cases = [
        (AgentAccessDenied("inactive"), "token_inactive", "已失效"),
        (AgentAccessDenied("scope_missing", tier="manage"), "scope_missing", "「管理」"),
        (AgentAccessDenied("notebook_not_allowed"), "notebook_not_allowed", "白名单"),
        (AgentAccessDenied("notebook_unreadable"), "notebook_unreadable", "无权访问"),
        (OwnerOnlyError(), "owner_only", "拥有的笔记本"),
        (MirroredNotebookError("prod-a"), "mirrored", "prod-a"),
        (KeyError("private-id-sentinel"), "not_found", "来源"),
        (PermissionError("private-sentinel"), "not_found", "来源"),
        (ValueError("中文的参数说明"), "invalid_argument", "中文的参数说明"),
        (ValueError("title must not be blank"), "invalid_argument", "title 不能为空"),
        (ValueError("tags may contain at most 20 values"), "invalid_argument", "最多 20 项"),
        # An incidental English ValueError is not an argument error: internal,
        # never echoed (the sentence would otherwise reach the Agent).
        (ValueError("invalid literal for int() with base 10: 'x'"), "internal", "服务内部错误"),
        # Containing a CJK character is not approved copy; starting with one is.
        (ValueError("invalid literal for int() with base 10: '一'"), "internal", "服务内部错误"),
        (MemoryInputError("some new validator wording"), "invalid_argument", "参数不合法"),
        (AgentToolError("forbidden", "此操作不被允许"), "forbidden", "不被允许"),
        (IndexingPipelineUnavailableError("plugin.x"), "unavailable", "索引管线当前不可用"),
        (IndexingPipelineRebuildActiveError(), "busy", "索引重建正在进行"),
        (ChunkLexicalSearchTimeout("private-sentinel"), "unavailable", "检索超时"),
        (validation, "invalid_argument", "value"),
        (GlobalAskError(404, "问答任务不存在"), "not_found", "问答任务不存在"),
        (GlobalAskError(409, "仍在回答"), "busy", "仍在回答"),
        (GlobalAskError(422, "范围不对"), "invalid_argument", "范围不对"),
        (GlobalAskError(503, "正在关闭"), "unavailable", "正在关闭"),
        (RuntimeError("private-sentinel /etc/secret"), "internal", "服务内部错误"),
        (AgentToolError("busy", "原样"), "busy", "原样"),
    ]
    for exc, code, fragment in cases:
        mapped = agent_tool_error(exc, tool="list_sources")
        assert mapped.code == code, exc
        assert fragment in mapped.message, (exc, mapped.message)
        assert str(mapped) == f"[{code}] {mapped.message}"
        assert "private" not in mapped.message, exc
        assert "sentinel" not in mapped.message, exc


@pytest.mark.anyio
async def test_the_registration_wrapper_translates_what_a_tool_raises() -> None:
    from types import SimpleNamespace

    from mcp.shared.memory import create_connected_server_and_client_session

    from app.api.mcp_tools._shared import _MCP_PRINCIPAL

    server, _app = mcp_server.create_memory_mcp(
        lambda: SimpleNamespace(refresh_agent_principal=lambda _token: None)
    )
    # No bound principal: every data tool refuses as an inactive token.
    async with create_connected_server_and_client_session(server._mcp_server) as client:
        result = await client.call_tool("search", {"query": "x"})
    assert result.isError
    assert result.content[0].text.endswith(
        "[token_inactive] 此凭证已失效（已撤销、已过期或所属 Agent 已停用），请在 Agent 接入页检查"
    )
    assert _MCP_PRINCIPAL.get() is None


def test_composition_has_exactly_one_core_tool_registration_seat() -> None:
    signature = inspect.signature(mcp_server.create_memory_mcp)
    assert tuple(signature.parameters) == (
        "repository_provider",
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
    ) == 1
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
    frontend_scopes = re.findall(r'value:\s*"([a-z_]+)"', block)
    assert len(frontend_scopes) == len(set(frontend_scopes))
    assert set(frontend_scopes) == set(AGENT_SCOPES)


def test_every_suppressed_ledger_record_is_re_booked_in_the_same_module() -> None:
    """调用记账的抑制与补记必须成对出现。

    「所有鉴权都过了才记」这条不变式的实现方式是:带后置鉴权的工具把收口的
    自动记账关掉(``record=False``),自己在那道闸之后补记。危险的方向是**关掉
    却忘了补**——那不会报任何错,只会让这条路径从此静默不记,而这正是单一收口
    最初要消灭的失败形态(codex #616 R2/R5/R6 连着三轮都在这条线上)。

    判据是构造性的:任何一个模块只要出现 ``record=False``(或以位置参数形式
    传的 ``False``),同一模块里就必须出现 ``_record_agent_call`` 调用。反过来
    不作要求——``_shared`` 自己就是那个收口。

    覆盖边界(如实说明):本守卫不判断补记的**位置**是否真的在那道闸之后,那需要
    控制流分析;位置由用例(``test_memory_mcp.py`` 里那几条「被拒不留痕」)与人工
    评审保证。它只钉住「关掉了就必须补」。
    """
    tools_dir = Path(mcp_server.__file__).parent / "mcp_tools"
    offenders = []
    for path in sorted(tools_dir.glob("*.py")):
        if path.name == "_shared.py":
            continue
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        suppresses = False
        rebooks = False
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            target = getattr(node.func, "id", "") or getattr(node.func, "attr", "")
            # 判据是**调用**而不是名字出现过:只留一行 import 也算补记的话,
            # 摘掉真正那次调用不会让这条守卫报红(自证时实测过)。
            if target == "_record_agent_call":
                rebooks = True
            # 关键字形态:record=False
            if any(
                keyword.arg == "record"
                and isinstance(keyword.value, ast.Constant)
                and keyword.value.value is False
                for keyword in node.keywords
            ):
                suppresses = True
            # 位置形态:run_sync(_selected_notebook, ctx, repo, scope, False)
            #
            # ⚠ 判的是 ``record`` **那一个位置**(run_sync 的第 5 个实参 = 被调
            # helper 的第 4 个形参),不是「实参里出现过任何一个 False」。后者曾经
            # 等价,因为那两个 helper 当时只有 ``record`` 一个布尔形参;
            # ``_writable_notebook`` 长出第二个布尔(``fenced``,镜像写入围栏的
            # 豁免开关)之后就不再等价——一个 ``fenced=False`` 会被宽判据误读成
            # 「关掉了记账」,逼着调用点为了绕开守卫而改写法,而守卫本身仍然什么都
            # 没看对。位置判据既不误报,也照样抓得住真正在 ``record`` 位置传 False。
            if target == "run_sync" and node.args:
                first = getattr(node.args[0], "id", "")
                record_arg = node.args[4] if len(node.args) > 4 else None
                if first in {"_selected_notebook", "_writable_notebook"} and (
                    isinstance(record_arg, ast.Constant)
                    and record_arg.value is False
                ):
                    suppresses = True
        if suppresses and not rebooks:
            offenders.append(path.name)
    assert offenders == [], (
        f"这些模块关掉了收口的自动记账却没有补记:{offenders}——"
        "那条路径会从此静默不记,而且不会有任何东西报错"
    )
