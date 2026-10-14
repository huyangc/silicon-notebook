"""Find hand-built child-process environments that drop the bytecode policy.

A test that builds a child's environment from scratch instead of extending
``os.environ`` loses the gate's ``PYTHONPYCACHEPREFIX``; any Python the child
runs then writes ``.pyc`` files next to the repository sources. Such an
environment must spread ``BYTECODE_ENV`` (``tests/child_env_testkit.py``) or set
``PYTHONPYCACHEPREFIX``/``PYTHONDONTWRITEBYTECODE`` itself.

Two shapes count as hand-built: a dict display with a ``"PATH"`` key (the base
every scrubbed environment starts from) that does not spread ``os.environ``,
and a dict display passed straight as ``env=`` to a ``subprocess`` call with
no spread at all. A dict that spreads some other mapping (``{**env, ...}``)
extends a base that is checked where it is built.
"""
from __future__ import annotations

import ast

from tests.architecture.semantic_source import qualified_scopes

BYTECODE_KEYS = frozenset({"PYTHONPYCACHEPREFIX", "PYTHONDONTWRITEBYTECODE"})
_SUBPROCESS_CALLS = frozenset({"run", "Popen", "call", "check_call", "check_output"})


def _string_keys(node: ast.Dict) -> set[str]:
    return {
        key.value
        for key in node.keys
        if isinstance(key, ast.Constant) and isinstance(key.value, str)
    }


def _module_bindings(tree: ast.Module) -> dict[str, ast.AST]:
    bindings: dict[str, ast.AST] = {}
    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            for target in statement.targets:
                if isinstance(target, ast.Name):
                    bindings[target.id] = statement.value
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.value is not None
        ):
            bindings[statement.target.id] = statement.value
    return bindings


def _carries_policy(spread: ast.AST, bindings: dict[str, ast.AST]) -> bool:
    text = ast.unparse(spread)
    if "environ" in text or "BYTECODE_ENV" in text:
        return True
    if isinstance(spread, ast.Name) and spread.id in bindings:
        return "environ" in ast.unparse(bindings[spread.id])
    return False


def _subprocess_env_dicts(tree: ast.AST) -> set[ast.Dict]:
    found: set[ast.Dict] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        name = func.attr if isinstance(func, ast.Attribute) else (
            func.id if isinstance(func, ast.Name) else ""
        )
        owner = func.value if isinstance(func, ast.Attribute) else None
        if name not in _SUBPROCESS_CALLS or not (
            isinstance(owner, ast.Name) and owner.id == "subprocess"
        ):
            continue
        for keyword in node.keywords:
            if keyword.arg == "env" and isinstance(keyword.value, ast.Dict):
                found.add(keyword.value)
    return found


def offenders(source: str, label: str) -> tuple[list[str], int]:
    """Offending scopes in ``source`` and how many hand-built environments it has."""
    tree = ast.parse(source)
    bindings = _module_bindings(tree)
    scopes = qualified_scopes(tree)
    passed_as_env = _subprocess_env_dicts(tree)
    found: list[str] = []
    checked = 0
    for node in ast.walk(tree):
        if not isinstance(node, ast.Dict):
            continue
        spreads = [value for key, value in zip(node.keys, node.values) if key is None]
        has_path = "PATH" in _string_keys(node)
        if not has_path and not (node in passed_as_env and not spreads):
            continue
        if any("environ" in ast.unparse(spread) for spread in spreads):
            continue  # extends the test process's environment, prefix included
        checked += 1
        if _string_keys(node) & BYTECODE_KEYS or any(
            _carries_policy(spread, bindings) for spread in spreads
        ):
            continue
        found.append(f"{label}: {scopes.get(node, '<module>')}")
    return found, checked
