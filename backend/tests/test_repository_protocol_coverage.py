"""Executable coverage for consumer-owned repository protocols."""
from __future__ import annotations

import ast
from collections import defaultdict
from dataclasses import dataclass, field
import inspect
from pathlib import Path
from types import SimpleNamespace
from typing import get_type_hints

from app.repositories.ports import (
    AskCandidatePort,
    AskGraphPort,
    AskStreamPort,
    RetrievalPort,
    SQLiteMaintenancePort,
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
    ("knowhow_transfer", "knowhow_transfer_store", "KnowhowTransferStorePort"),
    ("memory", "memory_store", "MemoryStorePort"),
    ("queries", "queries", "QueryStorePort"),
    ("reports", "report_store", "ReportStorePort"),
    ("ask_state", "ask_state", "AskStateStorePort"),
    ("unified_kg", "unified_kg", "UnifiedKgStorePort"),
)
FACADE_PATH = ROOT / "backend" / "app" / "services" / "repository_facade.py"


@dataclass(frozen=True, order=True)
class ProtocolCallSite:
    path: str
    scope: str
    member: str
    count: int
    diagnostic_lines: tuple[int, ...] = field(compare=False)


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
    for path, rel in _production_files():
        if rel in {
            "backend/app/services/retrieval_service.py",
            "backend/app/repositories/ports.py",
        }:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=rel)
        scopes = qualified_scopes(tree)
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


def test_sqlite_stores_structurally_satisfy_every_persistence_bundle_port():
    from app.repositories import ports
    from app.repositories.bundle import PersistenceBundle
    from app.repositories.sqlite.ask_state_store import AskStateStore
    from app.repositories.sqlite.chunk_store import ChunkStore
    from app.repositories.sqlite.database import SqliteDatabase
    from app.repositories.sqlite.embedding_store import EmbeddingStore
    from app.repositories.sqlite.governance_store import GovernanceStore
    from app.repositories.sqlite.identity_store import IdentityStore
    from app.repositories.sqlite.index_projection_store import IndexProjectionStore
    from app.repositories.sqlite.kg_build_job_store import KgBuildJobStore
    from app.repositories.sqlite.knowhow_store import KnowhowStore
    from app.repositories.sqlite.knowhow_transfer_store import KnowhowTransferStore
    from app.repositories.sqlite.knowledge_store import KnowledgeStore
    from app.repositories.sqlite.memory_store import MemoryStore
    from app.repositories.sqlite.notebook_store import NotebookStore
    from app.repositories.sqlite.query_store import QueryStore
    from app.repositories.sqlite.report_store import ReportStore
    from app.repositories.sqlite.sharing_store import SharingStore
    from app.repositories.sqlite.source_store import SourceStore
    from app.repositories.sqlite.unified_kg_store import UnifiedKgStore

    sqlite_stores = {
        "database": SqliteDatabase,
        "identity": IdentityStore,
        "notebooks": NotebookStore,
        "sharing": SharingStore,
        "sources": SourceStore,
        "chunks": ChunkStore,
        "embeddings": EmbeddingStore,
        "knowledge": KnowledgeStore,
        "governance": GovernanceStore,
        "index_projection": IndexProjectionStore,
        "kg_build_jobs": KgBuildJobStore,
        "knowhow": KnowhowStore,
        "knowhow_transfer": KnowhowTransferStore,
        "memory": MemoryStore,
        "queries": QueryStore,
        "reports": ReportStore,
        "ask_state": AskStateStore,
        "unified_kg": UnifiedKgStore,
    }
    stores = {name: object.__new__(store) for name, store in sqlite_stores.items()}
    stores["identity"].model_config_cache = {}
    bundle = SimpleNamespace(
        **{
            bundle_name: stores[bundle_name]
            for bundle_name, _, _ in BUNDLE_STORE_SEATS
        }
    )

    assert set(get_type_hints(PersistenceBundle)) == {
        bundle_name for bundle_name, _, _ in BUNDLE_STORE_SEATS
    }
    assert isinstance(bundle, PersistenceBundle)
    for bundle_name, _, port_name in BUNDLE_STORE_SEATS:
        assert isinstance(getattr(bundle, bundle_name), getattr(ports, port_name))


def test_bundle_ports_cover_facade_store_calls_and_match_sqlite_signatures():
    from app.repositories import ports
    from app.repositories.sqlite.ask_state_store import AskStateStore
    from app.repositories.sqlite.chunk_store import ChunkStore
    from app.repositories.sqlite.database import SqliteDatabase
    from app.repositories.sqlite.embedding_store import EmbeddingStore
    from app.repositories.sqlite.governance_store import GovernanceStore
    from app.repositories.sqlite.identity_store import IdentityStore
    from app.repositories.sqlite.index_projection_store import IndexProjectionStore
    from app.repositories.sqlite.kg_build_job_store import KgBuildJobStore
    from app.repositories.sqlite.knowhow_store import KnowhowStore
    from app.repositories.sqlite.knowhow_transfer_store import KnowhowTransferStore
    from app.repositories.sqlite.knowledge_store import KnowledgeStore
    from app.repositories.sqlite.memory_store import MemoryStore
    from app.repositories.sqlite.notebook_store import NotebookStore
    from app.repositories.sqlite.query_store import QueryStore
    from app.repositories.sqlite.report_store import ReportStore
    from app.repositories.sqlite.sharing_store import SharingStore
    from app.repositories.sqlite.source_store import SourceStore
    from app.repositories.sqlite.unified_kg_store import UnifiedKgStore

    sqlite_stores = {
        "database": SqliteDatabase,
        "identity": IdentityStore,
        "notebook_store": NotebookStore,
        "sharing_store": SharingStore,
        "source_store": SourceStore,
        "chunk_store": ChunkStore,
        "embedding_store": EmbeddingStore,
        "knowledge": KnowledgeStore,
        "governance": GovernanceStore,
        "index_projections": IndexProjectionStore,
        "kg_build_jobs": KgBuildJobStore,
        "knowhow_store": KnowhowStore,
        "knowhow_transfer_store": KnowhowTransferStore,
        "memory_store": MemoryStore,
        "queries": QueryStore,
        "report_store": ReportStore,
        "ask_state": AskStateStore,
        "unified_kg": UnifiedKgStore,
    }
    calls = _bundle_facade_calls()
    missing: dict[str, set[str]] = {}
    for _, runtime_name, port_name in BUNDLE_STORE_SEATS:
        protocol_methods = _protocol_methods(getattr(ports, port_name))
        missing[runtime_name] = calls[runtime_name] - set(protocol_methods)
        for name, protocol_method in protocol_methods.items():
            store_method = getattr(sqlite_stores[runtime_name], name)
            assert _parameter_contract(protocol_method) == _parameter_contract(
                store_method
            ), (runtime_name, name)
    assert missing == {runtime_name: set() for _, runtime_name, _ in BUNDLE_STORE_SEATS}


def test_static_store_helpers_remain_static_and_keep_their_first_real_argument():
    from app.repositories import ports
    from app.repositories.sqlite.ask_state_store import AskStateStore
    from app.repositories.sqlite.chunk_store import ChunkStore
    from app.repositories.sqlite.database import SqliteDatabase
    from app.repositories.sqlite.embedding_store import EmbeddingStore
    from app.repositories.sqlite.governance_store import GovernanceStore
    from app.repositories.sqlite.identity_store import IdentityStore
    from app.repositories.sqlite.index_projection_store import IndexProjectionStore
    from app.repositories.sqlite.kg_build_job_store import KgBuildJobStore
    from app.repositories.sqlite.knowhow_store import KnowhowStore
    from app.repositories.sqlite.knowhow_transfer_store import KnowhowTransferStore
    from app.repositories.sqlite.knowledge_store import KnowledgeStore
    from app.repositories.sqlite.memory_store import MemoryStore
    from app.repositories.sqlite.notebook_store import NotebookStore
    from app.repositories.sqlite.query_store import QueryStore
    from app.repositories.sqlite.report_store import ReportStore
    from app.repositories.sqlite.sharing_store import SharingStore
    from app.repositories.sqlite.source_store import SourceStore
    from app.repositories.sqlite.unified_kg_store import UnifiedKgStore

    sqlite_stores = {
        "database": SqliteDatabase,
        "identity": IdentityStore,
        "notebook_store": NotebookStore,
        "sharing_store": SharingStore,
        "source_store": SourceStore,
        "chunk_store": ChunkStore,
        "embedding_store": EmbeddingStore,
        "knowledge": KnowledgeStore,
        "governance": GovernanceStore,
        "index_projections": IndexProjectionStore,
        "kg_build_jobs": KgBuildJobStore,
        "knowhow_store": KnowhowStore,
        "knowhow_transfer_store": KnowhowTransferStore,
        "memory_store": MemoryStore,
        "queries": QueryStore,
        "report_store": ReportStore,
        "ask_state": AskStateStore,
        "unified_kg": UnifiedKgStore,
    }

    def descriptor(cls, name):
        return next(base.__dict__[name] for base in cls.__mro__ if name in base.__dict__)

    def descriptor_kind(value):
        if isinstance(value, staticmethod):
            return "static"
        if isinstance(value, classmethod):
            return "class"
        return "instance"

    receiver = object()
    for _, runtime_name, port_name in BUNDLE_STORE_SEATS:
        protocol = getattr(ports, port_name)
        concrete = sqlite_stores[runtime_name]
        for name in _protocol_methods(protocol):
            concrete_descriptor = descriptor(concrete, name)
            protocol_descriptor = descriptor(protocol, name)
            kind = descriptor_kind(concrete_descriptor)
            assert descriptor_kind(protocol_descriptor) == kind, (
                protocol.__name__, name,
            )
            if kind in {"static", "class"}:
                bound_protocol = protocol_descriptor.__get__(receiver, protocol)
                bound_concrete = concrete_descriptor.__get__(receiver, concrete)
                assert _parameter_contract(bound_protocol) == _parameter_contract(
                    bound_concrete
                ), (protocol.__name__, name)
            else:
                assert _parameter_contract(
                    getattr(protocol, name)
                ) == _parameter_contract(getattr(concrete, name)), (
                    protocol.__name__, name,
                )
