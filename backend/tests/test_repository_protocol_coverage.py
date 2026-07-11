"""Executable coverage for consumer-owned repository protocols."""
from __future__ import annotations

import ast
import inspect
from pathlib import Path
from typing import get_type_hints

from app.repositories.ports import (
    AskCandidatePort,
    AskGraphPort,
    AskStreamPort,
    RetrievalPort,
    SQLiteMaintenancePort,
)


ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_ROOTS = (ROOT / "backend" / "app", ROOT / "scripts")
EXPECTED_REMEDIATION_SITES = set()


def _production_files():
    for base in PRODUCTION_ROOTS:
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" not in path.parts:
                yield path, str(path.relative_to(ROOT))


def _dotted(node: ast.AST) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
    return ".".join(reversed(parts))


def _annotation_name(node: ast.AST | None) -> str:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return node.value.strip("'\"")
    return _dotted(node) if node is not None else ""


def _protocol_receivers(tree: ast.AST, protocol_name: str) -> set[str]:
    receivers = {
        "RetrievalPort": {"retrieval", "_retrieval"},
        "AskCandidatePort": set(),
        "AskGraphPort": set(),
        "AskStreamPort": set(),
    }[protocol_name].copy()
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg in (*node.args.args, *node.args.kwonlyargs):
                if _annotation_name(arg.annotation).rsplit(".", 1)[-1] == protocol_name:
                    receivers.add(arg.arg)
        elif isinstance(node, ast.AnnAssign):
            if _annotation_name(node.annotation).rsplit(".", 1)[-1] == protocol_name:
                receivers.add(_dotted(node.target))

    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)) or node.value is None:
                continue
            source = _dotted(node.value)
            if source not in receivers and source.rsplit(".", 1)[-1] not in receivers:
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                dotted = _dotted(target)
                if dotted and dotted not in receivers:
                    receivers.add(dotted)
                    changed = True
    return receivers


def protocol_call_sites(protocol_name: str) -> set[tuple[str, int, str]]:
    """Return public calls made through the named production protocol seat.

    Each consumer-owned port is detected from its annotations and propagated
    assignments; established semantic seat names cover dataclass fields and
    local aliases. A new call cannot evade the guard merely by moving from a
    constructor argument to ``self.<seat>``.
    """
    if protocol_name not in {
        "RetrievalPort", "AskCandidatePort", "AskGraphPort", "AskStreamPort",
    }:
        raise ValueError(f"unsupported protocol audit: {protocol_name}")

    calls: set[tuple[str, int, str]] = set()
    for path, rel in _production_files():
        if rel in {
            "backend/app/services/retrieval_service.py",
            "backend/app/repositories/ports.py",
        }:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        if protocol_name == "AskStreamPort":
            for scope in ast.walk(tree):
                if not isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    continue
                stream_args = {
                    arg.arg
                    for arg in (*scope.args.args, *scope.args.kwonlyargs)
                    if _annotation_name(arg.annotation).rsplit(".", 1)[-1]
                    == protocol_name
                }
                for node in ast.walk(scope):
                    if (
                        stream_args
                        and isinstance(node, ast.Call)
                        and isinstance(node.func, ast.Attribute)
                        and _dotted(node.func.value) in stream_args
                    ):
                        calls.add((rel, node.lineno, node.func.attr))
            continue
        receivers = _protocol_receivers(tree, protocol_name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            receiver = _dotted(node.func.value)
            loose_match = (
                protocol_name == "RetrievalPort"
                and receiver.rsplit(".", 1)[-1] in receivers
            )
            if receiver in receivers or loose_match:
                calls.add((rel, node.lineno, node.func.attr))
    return calls


def protocol_calls(protocol_name: str) -> set[str]:
    return {call for _rel, _line, call in protocol_call_sites(protocol_name)}


def test_retrieval_port_declares_every_production_retrieval_call():
    missing = protocol_calls("RetrievalPort") - set(RetrievalPort.__dict__)
    missing_sites = {
        site for site in protocol_call_sites("RetrievalPort") if site[2] in missing
    }
    assert missing_sites == EXPECTED_REMEDIATION_SITES


def test_ask_ports_declare_the_executable_service_and_route_surface():
    for name, protocol in (
        ("AskCandidatePort", AskCandidatePort),
        ("AskGraphPort", AskGraphPort),
        ("AskStreamPort", AskStreamPort),
    ):
        declared = {
            member for member, value in protocol.__dict__.items()
            if callable(value) and not member.startswith("_")
        }
        assert protocol_calls(name) == declared


def test_maintenance_port_covers_every_public_sqlite_adapter_method():
    from app.repositories.sqlite.maintenance import SQLiteMaintenanceAdapter

    adapter_methods = {
        name for name, value in SQLiteMaintenanceAdapter.__dict__.items()
        if callable(value) and not name.startswith("_")
    }
    assert adapter_methods <= set(SQLiteMaintenancePort.__dict__)


def _parameter_contract(callable_):
    return [
        (parameter.name, parameter.kind, parameter.default)
        for parameter in inspect.signature(callable_).parameters.values()
        if parameter.name != "self"
    ]


def test_model_client_ports_match_concrete_call_signatures():
    from app.core.llm import OpenAICompatibleClient
    from app.repositories.ports import JsonChatClientPort, RerankClientPort
    from app.services.rerank_client import RerankClient

    assert _parameter_contract(JsonChatClientPort.chat_json) == _parameter_contract(
        OpenAICompatibleClient.chat_json
    )
    assert get_type_hints(JsonChatClientPort.chat_json)["messages"] == (
        get_type_hints(OpenAICompatibleClient.chat_json)["messages"]
    )
    assert _parameter_contract(RerankClientPort.rerank) == _parameter_contract(
        RerankClient.rerank
    )
    assert get_type_hints(RerankClientPort.rerank)["documents"] == (
        get_type_hints(RerankClient.rerank)["documents"]
    )


def test_batch_repository_returns_typed_consumer_projections():
    from app.repositories.ports import KGBuildResult, ScaleBuildManifest
    from app.services.batch_ingest import BatchIngestRepository

    hints = get_type_hints(BatchIngestRepository.build_notebook_kg)
    assert hints["return"] is KGBuildResult
    hints = get_type_hints(BatchIngestRepository.build_scale_index)
    assert hints["return"] is ScaleBuildManifest
    assert KGBuildResult.__required_keys__ >= {"built", "failed"}
    assert ScaleBuildManifest.__optional_keys__ == {"n_nodes"}
