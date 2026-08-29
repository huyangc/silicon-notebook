"""Architecture guard for the backend-neutral persistence bundle boundary."""
from __future__ import annotations

import ast
import inspect
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import get_type_hints

import pytest


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
    "groups": "GroupStorePort",
    "sources": "SourceStorePort",
    "chunks": "ChunkStorePort",
    "embeddings": "EmbeddingStorePort",
    "knowledge": "KnowledgeStorePort",
    "governance": "GovernanceStorePort",
    "index_projection": "IndexProjectionStorePort",
    "kg_build_jobs": "KgBuildJobStorePort",
    "catalog": "CatalogStorePort",
    "knowhow": "KnowhowStorePort",
    "knowhow_history": "KnowhowHistoryStorePort",
    "knowhow_transfer": "KnowhowTransferStorePort",
    "memory": "MemoryStorePort",
    "queries": "QueryStorePort",
    "reports": "ReportStorePort",
    "ask_state": "AskStateStorePort",
    "unified_kg": "UnifiedKgStorePort",
    "model_status": "ModelStatusStorePort",
    "agent_profile": "AgentProfileStorePort",
    "retrieval_experiences": "RetrievalExperienceStorePort",
    "agent_observations": "AgentObservationStorePort",
    "extension_toggles": "ExtensionToggleStorePort",
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


@pytest.mark.architecture_contract
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
