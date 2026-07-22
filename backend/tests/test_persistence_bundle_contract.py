"""Architecture guard for the backend-neutral persistence bundle boundary."""
from __future__ import annotations

import ast
import inspect
from dataclasses import fields, is_dataclass
from pathlib import Path
from typing import get_type_hints


ROOT = Path(__file__).resolve().parents[2]
NEUTRAL_MODULES = (
    "backend/app/repositories/ports.py",
    "backend/app/repositories/bundle.py",
)
# Task 3 extends this guard after it extracts/injects the current SQLite
# composition root.  Keeping the next phase explicit prevents this Task 2
# guard from silently becoming a permanent partial boundary.
TASK_3_NEUTRAL_MODULES = (
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
    offenders = _forbidden_imports(_imports(ROOT / NEUTRAL_MODULES[0]))
    assert offenders == set(), offenders


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
        "self", "settings", "root_dir", "seams", "model_config_cache",
    )
    assert tuple(parameter.kind for parameter in parameters) == (
        inspect.Parameter.POSITIONAL_OR_KEYWORD,
        inspect.Parameter.KEYWORD_ONLY,
        inspect.Parameter.KEYWORD_ONLY,
        inspect.Parameter.KEYWORD_ONLY,
        inspect.Parameter.KEYWORD_ONLY,
    )
    assert all(parameter.default is inspect.Parameter.empty for parameter in parameters[1:])
    assert get_type_hints(PersistenceBundleFactory.create) == {
        "settings": Settings,
        "root_dir": Path,
        "seams": RepositorySeams,
        "model_config_cache": dict[str, object],
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
