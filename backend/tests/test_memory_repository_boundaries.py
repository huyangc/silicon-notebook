from __future__ import annotations

import ast
import inspect
from pathlib import Path

from app.repositories import ports
from app.repositories.ownership_manifest import DELEGATE_OWNER_OVERRIDES
from app.services.sqlite_repository import SQLiteRepository


ROOT = Path(__file__).resolve().parents[1]


def _imports(path: Path) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    return modules


def test_memory_ports_expose_store_and_lifecycle_contracts():
    store_methods = {
        "insert_memory",
        "create_candidate_with_initial_revision",
        "create_answer_with_initial_revision",
        "memory_for_user",
        "append_revision",
        "transition",
        "transition_with_revision",
        "update_fields",
        "update_with_revision",
        "list_memories",
        "answer_memory_links",
        "delete_memory",
        "bulk_delete_memories",
    }
    service_methods = {
        "create_memory_candidate",
        "create_memory_from_answer",
        "update_memory",
        "confirm_memory",
        "reject_memory",
        "deprecate_memory",
        "delete_memory",
        "bulk_delete_memories",
        "get_memory",
        "list_memories",
        "answer_memory_links",
        "memory_revisions",
        "transfer_memories",
    }
    assert store_methods <= set(ports.MemoryStorePort.__dict__)
    assert service_methods <= set(ports.MemoryRepository.__dict__)
    for name in store_methods:
        assert inspect.isfunction(getattr(ports.MemoryStorePort, name))


def test_memory_store_owns_sql_and_service_never_imports_facade():
    store_path = ROOT / "app" / "repositories" / "sqlite" / "memory_store.py"
    service_path = ROOT / "app" / "services" / "memory_service.py"
    memory_inputs_path = ROOT / "app" / "core" / "memory_inputs.py"
    store_source = store_path.read_text(encoding="utf-8")
    service_source = service_path.read_text(encoding="utf-8")

    assert ".execute(" in store_source
    assert ".execute(" not in service_source
    assert not any(
        module.startswith("app.services.sqlite_repository")
        for module in _imports(service_path)
    )
    assert not any(
        module.startswith("app.services.repository_runtime")
        for module in _imports(service_path)
    )
    assert "app.repositories.sqlite.memory_store" not in _imports(service_path)
    ports_path = ROOT / "app" / "repositories" / "ports.py"
    assert "app.repositories.sqlite.memory_store" not in _imports(ports_path)
    assert "app.models.memory" in _imports(service_path)
    assert "app.models.memory" in _imports(ports_path)
    assert "app.core.memory_inputs" in _imports(service_path)
    assert "app.core.memory_inputs" not in _imports(store_path)
    assert "app.services.memory_inputs" not in _imports(service_path)
    assert "app.services.memory_inputs" not in _imports(store_path)
    assert "app.core.json_safety" in _imports(store_path)
    assert "app.core.json_safety" in _imports(memory_inputs_path)


def test_runtime_owns_memory_components_and_facade_has_explicit_delegates(tmp_path):
    from app.core.config import Settings

    repo = SQLiteRepository(
        Settings(
            database_url=f"sqlite:///{tmp_path / 'boundary.db'}",
            storage_dir=str(tmp_path / "storage"),
            event_log_enabled=False,
            llm_log_enabled=False,
        )
    )
    runtime = repo._runtime
    assert runtime.memory_store.database is runtime.database
    assert runtime.memory_service.store is runtime.memory_store
    assert runtime.memory_service.ask_state is runtime.ask_state
    assert runtime.memory_service.notebooks is runtime.sharing
    assert runtime.memory_service.embedder is repo.embedder

    for name in (
        "create_memory_candidate",
        "create_memory_from_answer",
        "update_memory",
        "confirm_memory",
        "reject_memory",
        "deprecate_memory",
        "delete_memory",
        "bulk_delete_memories",
        "get_memory",
        "list_memories",
        "answer_memory_links",
        "memory_revisions",
        "transfer_memories",
    ):
        assert name in SQLiteRepository.__dict__
        assert DELEGATE_OWNER_OVERRIDES[name] == "MemoryService"
