"""Executable coverage for consumer-owned repository protocols."""
from __future__ import annotations

import ast
from pathlib import Path

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
    receivers = {"retrieval", "_retrieval"}
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

    Retrieval is intentionally detected by data-flow-neutral seat names as
    well as annotations: production uses dataclass fields (``deps.retrieval``),
    local adapters (``retrieval = repo.retrieval``), and ``self.retrieval``.
    A new call therefore cannot evade the guard merely by dropping a type
    annotation at one call site.
    """
    if protocol_name != "RetrievalPort":
        raise ValueError(f"unsupported protocol audit: {protocol_name}")

    calls: set[tuple[str, int, str]] = set()
    for path, rel in _production_files():
        if rel in {
            "backend/app/services/retrieval_service.py",
            "backend/app/repositories/ports.py",
        }:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        receivers = _protocol_receivers(tree, protocol_name)
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
                continue
            receiver = _dotted(node.func.value)
            if receiver in receivers or receiver.rsplit(".", 1)[-1] in receivers:
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
    assert {
        "current_user", "start_ask_stream",
    } <= set(AskStreamPort.__dict__)
    assert {
        "notebook_languages", "chunk_plan", "retrieve_chunk_candidates",
        "graph_is_large",
    } <= set(AskCandidatePort.__dict__)
    assert {"federated_graph", "source_chunks"} <= set(AskGraphPort.__dict__)


def test_maintenance_port_covers_every_public_sqlite_adapter_method():
    from app.repositories.sqlite.maintenance import SQLiteMaintenanceAdapter

    adapter_methods = {
        name for name, value in SQLiteMaintenanceAdapter.__dict__.items()
        if callable(value) and not name.startswith("_")
    }
    assert adapter_methods <= set(SQLiteMaintenancePort.__dict__)
