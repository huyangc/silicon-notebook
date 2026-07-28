from __future__ import annotations

import ast
import inspect
from pathlib import Path

from app.repositories import ports


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
