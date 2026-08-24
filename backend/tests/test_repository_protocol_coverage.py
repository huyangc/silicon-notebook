"""Executable coverage for consumer-owned repository protocols."""
from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass, field
from functools import lru_cache
import inspect
from pathlib import Path
from typing import get_type_hints

from app.repositories.ports import (
    AskCandidatePort,
    AskGraphPort,
    AskStreamPort,
    RetrievalPort,
)
from tests.architecture.semantic_source import qualified_scopes


ROOT = Path(__file__).resolve().parents[2]
PRODUCTION_ROOTS = (ROOT / "backend" / "app", ROOT / "scripts")
EXPECTED_REMEDIATION_SITES = set()
BUNDLE_STORE_SEATS = (
    ("database", "database", "RepositoryDatabasePort"),
    ("identity", "identity", "IdentityStorePort"),
    ("notebooks", "notebook_store", "NotebookStorePort"),
    ("sharing", "sharing_store", "SharingStorePort"),
    ("sources", "source_store", "SourceStorePort"),
    ("chunks", "chunk_store", "ChunkStorePort"),
    ("embeddings", "embedding_store", "EmbeddingStorePort"),
    ("knowledge", "knowledge", "KnowledgeStorePort"),
    ("governance", "governance", "GovernanceStorePort"),
    ("index_projection", "index_projections", "IndexProjectionStorePort"),
    ("kg_build_jobs", "kg_build_jobs", "KgBuildJobStorePort"),
    ("knowhow", "knowhow_store", "KnowhowStorePort"),
    ("knowhow_history", "knowhow_history_store", "KnowhowHistoryStorePort"),
    ("knowhow_transfer", "knowhow_transfer_store", "KnowhowTransferStorePort"),
    ("memory", "memory_store", "MemoryStorePort"),
    ("queries", "queries", "QueryStorePort"),
    ("reports", "report_store", "ReportStorePort"),
    ("ask_state", "ask_state", "AskStateStorePort"),
    ("unified_kg", "unified_kg", "UnifiedKgStorePort"),
    ("model_status", "model_status_store", "ModelStatusStorePort"),
)
FACADE_PATH = ROOT / "backend" / "app" / "services" / "repository_facade.py"

# `database.write` is an opaque backend boundary: RepositoryDatabasePort declares
# a generic `write(self)` on purpose (see its docstring — it "intentionally does
# not pretend that SQLite and PostgreSQL share a connection API") because the two
# backends specialise it divergently — SQLite adds a keyword-only `operation=`
# for write-lock instrumentation, PostgreSQL adds `isolation_level=`. Structural
# Protocol conformance already permits an impl to carry extra optional kwargs, so
# this seam is exempt from the exact port↔sqlite signature match rather than
# forcing a fake shared parameter onto the neutral port.
_BACKEND_DIVERGENT_SIGNATURES = frozenset({("database", "write")})


@dataclass(frozen=True, order=True)
class ProtocolCallSite:
    path: str
    scope: str
    member: str
    count: int
    diagnostic_lines: tuple[int, ...] = field(compare=False)


@lru_cache(maxsize=1)
def _production_syntax() -> tuple[tuple[str, ast.AST, dict[ast.AST, str]], ...]:
    """Parse the production corpus once for all protocol audits in this worker."""
    parsed: list[tuple[str, ast.AST, dict[ast.AST, str]]] = []
    for base in PRODUCTION_ROOTS:
        for path in sorted(base.rglob("*.py")):
            if "__pycache__" in path.parts:
                continue
            rel = str(path.relative_to(ROOT))
            tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
            parsed.append((rel, tree, qualified_scopes(tree)))
    return tuple(parsed)


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


@lru_cache(maxsize=None)
def protocol_call_sites(protocol_name: str) -> frozenset[ProtocolCallSite]:
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

    diagnostic_lines_by_site: dict[tuple[str, str, str], list[int]] = defaultdict(
        list
    )
    for rel, tree, scopes in _production_syntax():
        if rel in {
            "backend/app/services/retrieval_service.py",
            "backend/app/repositories/ports.py",
        }:
            continue
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
                        diagnostic_lines_by_site[
                            (rel, scopes[node], node.func.attr)
                        ].append(node.lineno)
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
                diagnostic_lines_by_site[
                    (rel, scopes[node], node.func.attr)
                ].append(node.lineno)
    return frozenset(
        ProtocolCallSite(
            path=path,
            scope=scope,
            member=member,
            count=len(lines),
            diagnostic_lines=tuple(sorted(lines)),
        )
        for (path, scope, member), lines in diagnostic_lines_by_site.items()
    )


def protocol_calls(protocol_name: str) -> set[str]:
    return {site.member for site in protocol_call_sites(protocol_name)}


def test_retrieval_port_declares_every_production_retrieval_call():
    missing = protocol_calls("RetrievalPort") - set(RetrievalPort.__dict__)
    missing_sites = {
        site
        for site in protocol_call_sites("RetrievalPort")
        if site.member in missing
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


def _parameter_contract(callable_):
    return [
        (parameter.name, parameter.kind, parameter.default)
        for parameter in inspect.signature(callable_).parameters.values()
        if parameter.name != "self"
    ]


def _bundle_facade_calls() -> dict[str, set[str]]:
    """Methods that Task 3 must preserve when it injects the bundle."""
    tree = ast.parse(FACADE_PATH.read_text(encoding="utf-8"), filename=str(FACADE_PATH))
    calls = {runtime_name: set() for _, runtime_name, _ in BUNDLE_STORE_SEATS}
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not isinstance(node.func, ast.Attribute):
            continue
        receiver = node.func.value
        if not (
            isinstance(receiver, ast.Attribute)
            and isinstance(receiver.value, ast.Attribute)
            and isinstance(receiver.value.value, ast.Name)
            and receiver.value.value.id == "self"
            and receiver.value.attr == "_runtime"
            and receiver.attr in calls
        ):
            continue
        calls[receiver.attr].add(node.func.attr)
    return calls


def _protocol_methods(protocol) -> dict[str, object]:
    methods: dict[str, object] = {}
    for base in reversed(protocol.__mro__):
        for name, value in base.__dict__.items():
            if (
                callable(value) or isinstance(value, (staticmethod, classmethod))
            ) and not name.startswith("__"):
                methods[name] = getattr(protocol, name)
    return methods


def test_model_client_ports_match_concrete_call_signatures():
    from app.core.llm import OpenAICompatibleClient
    from app.repositories.ports import JsonChatClientPort, RerankClientPort
    from app.services.model_provider import ScheduledJsonChatClient
    from app.services.rerank_client import RerankClient

    assert _parameter_contract(JsonChatClientPort.chat_json) == _parameter_contract(
        ScheduledJsonChatClient.chat_json
    )
    raw_chat_contract = [
        parameter
        for parameter in _parameter_contract(OpenAICompatibleClient.chat_json)
        if parameter[0] != "thinking_mode"
    ]
    assert _parameter_contract(JsonChatClientPort.chat_json) == raw_chat_contract
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
