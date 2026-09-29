"""Every method a service calls on the Memory store seat exists on BOTH
backends' stores, with the same signature.

``MemoryStorePort`` does not declare every method its consumers call (the
ports file has a method-count ratchet that may only shrink, so the purge and
promotion helpers stay undeclared, following the established precedent). A
store missing one of them would otherwise only fail at the first delete,
exit or approval that reaches it. This introspection makes it fail here.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from app.repositories.postgres.memory_store import MemoryStore as PostgresMemoryStore
from app.repositories.sqlite.memory_store import MemoryStore as SqliteMemoryStore

SERVICES = Path(__file__).resolve().parents[1] / "app" / "services"


def _seat_attributes(tree: ast.AST) -> set[str]:
    """``self.<attr>`` names assigned from a parameter annotated
    ``MemoryStorePort`` (the seat, whatever the service calls it)."""
    seats: set[str] = set()
    for function in ast.walk(tree):
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        params = {
            arg.arg
            for arg in (*function.args.args, *function.args.kwonlyargs)
            if arg.annotation is not None
            and "MemoryStorePort" in ast.unparse(arg.annotation)
        }
        for node in ast.walk(function):
            if (
                isinstance(node, (ast.Assign, ast.AnnAssign))
                and isinstance(node.value, ast.Name)
                and node.value.id in params
            ):
                targets = node.targets if isinstance(node, ast.Assign) else [node.target]
                for target in targets:
                    if (
                        isinstance(target, ast.Attribute)
                        and isinstance(target.value, ast.Name)
                        and target.value.id == "self"
                    ):
                        seats.add(target.attr)
    return seats


def _seat_calls() -> dict[str, set[str]]:
    calls: dict[str, set[str]] = {}
    for path in sorted(SERVICES.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        if "MemoryStorePort" not in source:
            continue
        tree = ast.parse(source)
        seats = _seat_attributes(tree)
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Attribute)
                and isinstance(node.func.value.value, ast.Name)
                and node.func.value.value.id == "self"
                and node.func.value.attr in seats
            ):
                calls.setdefault(node.func.attr, set()).add(path.name)
    return calls


def test_the_seat_scan_finds_the_known_consumers():
    calls = _seat_calls()
    # The Memory purge (memory_service.py) and the promotion flow
    # (knowledge_governance.py) both reach undeclared store methods.
    assert {"memory_service.py"} <= calls["derived_memory_sources"]
    assert {"memory_service.py"} <= calls["member_exit_snapshot"]
    assert {"knowledge_governance.py"} <= calls["promotion_rows_on"]


@pytest.mark.parametrize("method", sorted(_seat_calls()))
def test_both_backends_implement_every_called_seat_method(method):
    sqlite_method = getattr(SqliteMemoryStore, method, None)
    postgres_method = getattr(PostgresMemoryStore, method, None)
    assert callable(sqlite_method), f"sqlite MemoryStore lacks {method}"
    assert callable(postgres_method), f"postgres MemoryStore lacks {method}"
    sqlite_params = [
        (p.name, p.kind, p.default)
        for p in inspect.signature(sqlite_method).parameters.values()
    ]
    postgres_params = [
        (p.name, p.kind, p.default)
        for p in inspect.signature(postgres_method).parameters.values()
    ]
    assert sqlite_params == postgres_params, method
