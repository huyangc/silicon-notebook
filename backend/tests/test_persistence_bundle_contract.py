"""Architecture guard for the backend-neutral persistence bundle boundary."""
from __future__ import annotations

import ast
from contextlib import AbstractContextManager
import inspect
import json
import os
import subprocess
import sys
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import get_origin, get_type_hints


ROOT = Path(__file__).resolve().parents[2]
NEUTRAL_MODULES = (
    "backend/app/repositories/ports.py",
    "backend/app/repositories/bundle.py",
    "backend/app/services/repository_runtime.py",
    "backend/app/services/repository_facade.py",
)
BUNDLE_STORE_PORTS = {
    "database": "RepositoryDatabasePort",
    "identity": "IdentityStorePort",
    "notebooks": "NotebookStorePort",
    "sharing": "SharingStorePort",
    "sources": "SourceStorePort",
    "chunks": "ChunkStorePort",
    "embeddings": "EmbeddingStorePort",
    "knowledge": "KnowledgeStorePort",
    "governance": "GovernanceStorePort",
    "index_projection": "IndexProjectionStorePort",
    "kg_build_jobs": "KgBuildJobStorePort",
    "knowhow": "KnowhowStorePort",
    "knowhow_transfer": "KnowhowTransferStorePort",
    "memory": "MemoryStorePort",
    "queries": "QueryStorePort",
    "reports": "ReportStorePort",
    "ask_state": "AskStateStorePort",
    "unified_kg": "UnifiedKgStorePort",
}


def _imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def _forbidden_imports(modules: set[str]) -> set[str]:
    return {
        module
        for module in modules
        if module == "sqlite3"
        or module.startswith("psycopg")
        or module.startswith("app.repositories.sqlite")
        or module.startswith("app.repositories.postgres")
    }


def test_neutral_ports_do_not_import_database_backends():
    offenders = {
        module: _forbidden_imports(_imports(ROOT / module))
        for module in NEUTRAL_MODULES
        if (ROOT / module).exists()
    }
    assert offenders == {module: set() for module in NEUTRAL_MODULES}, offenders


def test_clean_neutral_runtime_and_facade_imports_load_no_backend_modules():
    script = """
import json
import sys
import app.services.repository_runtime
import app.services.repository_facade
print(json.dumps(sorted(
    name for name in sys.modules
    if name.startswith(('app.repositories.sqlite.', 'app.repositories.postgres.'))
)))
"""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT / "backend")
    completed = subprocess.run(
        [sys.executable, "-c", script],
        cwd=ROOT,
        env=env,
        text=True,
        capture_output=True,
        check=True,
    )
    assert json.loads(completed.stdout) == []


def test_neutral_database_and_facade_do_not_expose_sqlite_local_connection_cleanup():
    from app.repositories.ports import RepositoryDatabasePort
    from app.services.repository_facade import RepositoryFacade
    from app.services.sqlite_repository import SQLiteRepository

    assert "close_local" not in RepositoryDatabasePort.__dict__
    assert "close_local" not in RepositoryFacade.__dict__
    assert "close_local" in SQLiteRepository.__dict__


def test_bundle_is_neutral_and_declares_every_store_seat():
    bundle_path = ROOT / NEUTRAL_MODULES[1]
    assert bundle_path.exists()
    offenders = _forbidden_imports(_imports(bundle_path))
    assert offenders == set(), offenders

    from app.repositories.bundle import PersistenceBundle

    hints = get_type_hints(PersistenceBundle)
    assert set(hints) == set(BUNDLE_STORE_PORTS)
    assert {name: value.__name__ for name, value in hints.items()} == BUNDLE_STORE_PORTS


def test_persistence_bundle_factory_create_has_a_pinned_public_contract():
    from app.core.config import Settings
    from app.repositories.bundle import PersistenceBundle, PersistenceBundleFactory
    from app.repositories.ports import RepositorySeams

    signature = inspect.signature(PersistenceBundleFactory.create)
    parameters = tuple(signature.parameters.values())

    assert tuple(parameter.name for parameter in parameters) == (
        "self", "settings", "root_dir", "seams",
    )
    assert tuple(parameter.kind for parameter in parameters) == (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
        inspect.Parameter.KEYWORD_ONLY,
        inspect.Parameter.KEYWORD_ONLY,
    )
    assert all(parameter.default is inspect.Parameter.empty for parameter in parameters[1:])
    assert get_type_hints(PersistenceBundleFactory.create) == {
        "settings": Settings,
        "root_dir": Path,
        "seams": RepositorySeams,
        "return": PersistenceBundle,
    }


def test_ask_turn_result_is_neutral_and_preserves_the_conversation_history_shape():
    from app.repositories.ports import AskStateStorePort, PreparedAskTurn
    from app.repositories.sqlite.ask_state_store import AskStateStore

    assert is_dataclass(PreparedAskTurn)
    assert tuple(field.name for field in fields(PreparedAskTurn)) == (
        "conversation_id", "history",
    )
    assert get_type_hints(AskStateStorePort.prepare_turn)["return"] is PreparedAskTurn
    assert get_type_hints(AskStateStore.prepare_turn)["return"] is PreparedAskTurn
    assert get_type_hints(AskStateStorePort.prepare_turn_for_job)["return"] == (
        PreparedAskTurn | None
    )
    assert get_type_hints(AskStateStore.prepare_turn_for_job)["return"] == (
        PreparedAskTurn | None
    )


def test_mention_alias_scan_is_a_portable_store_operation():
    from app.repositories.ports import UnifiedKgStorePort
    from app.repositories.sqlite.unified_kg_store import UnifiedKgStore

    assert "mention_alias_candidate_batches" in UnifiedKgStorePort.__dict__
    port_signature = inspect.signature(UnifiedKgStorePort.mention_alias_candidate_batches)
    store_signature = inspect.signature(UnifiedKgStore.mention_alias_candidate_batches)
    assert port_signature.replace(return_annotation=inspect.Signature.empty) == (
        store_signature.replace(return_annotation=inspect.Signature.empty)
    )
    assert get_origin(
        get_type_hints(UnifiedKgStorePort.mention_alias_candidate_batches)["return"]
    ) is AbstractContextManager
    lifecycle_source = (ROOT / "backend/app/services/knowledge_lifecycle.py").read_text(
        encoding="utf-8"
    )
    assert "_close_local" not in lifecycle_source
    assert ".claim_name_rows(" not in lifecycle_source
    assert ".mention_scan_matches(" not in lifecycle_source


def test_newly_neutral_service_type_hints_resolve_without_backend_names():
    from app.repositories.ports import QueryStorePort
    from app.services.kg_mutation import KgMutationCoordinator
    from app.services.knowledge_governance import KnowledgeGovernanceService
    from app.services.knowledge_lifecycle import KnowledgeLifecycleService
    from app.services.model_provider import RuntimeModelProvider
    from app.services.notebook_catalog import NotebookCatalogService
    from app.services.notebook_sharing import NotebookSharingService
    from app.services.schema_registry import SchemaRegistryService
    from app.services.source_chunking import SourceChunkingService
    from app.services.source_embedding import SourceEmbeddingService
    from app.services.source_ingestion import SourceIngestionService

    constructors = (
        NotebookCatalogService,
        KnowledgeLifecycleService,
        KnowledgeGovernanceService,
        NotebookSharingService,
        SchemaRegistryService,
        RuntimeModelProvider,
        SourceIngestionService,
        SourceChunkingService,
        SourceEmbeddingService,
        KgMutationCoordinator,
    )
    hints_by_class = {cls: get_type_hints(cls.__init__) for cls in constructors}
    assert hints_by_class[NotebookCatalogService]["queries"] is QueryStorePort
    assert get_type_hints(KgMutationCoordinator.bump_cluster_mutation_seq)[
        "connection"
    ] is object
