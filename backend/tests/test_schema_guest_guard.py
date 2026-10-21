"""Guard: no process except the server and the explicit migration command may
compose a repository that migrates or seeds.

Why a static scan rather than a process-role assertion on the seed path: the
seed path is reached by every default repository construction, and the test
suite and the scratch-database harnesses construct thousands of those on
throwaway databases. A runtime assertion would either need every one of them to
declare a role (noise that hides the real signal) or default to "allowed",
which guards nothing -- and a new CLI that forgot to declare itself would
default the same way. The scan instead looks at every repository construction
in production and tool code (``backend/app``, ``scripts``, ``examples``) and
accepts it only when it passes the guest keywords literally
(``migrate=False, seed=False`` or ``owns_schema=False``) or is one of the
schema owners listed below, each with the reason it owns its database. A new
CLI that calls ``create_application_repository(settings)`` -- under any alias --
fails here with its path and line. A listed owner that no longer exists fails
too, so the list cannot rot into a blanket exemption.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("backend/app", "scripts", "examples")
CONSTRUCTORS = frozenset({
    "create_repository",
    "create_application_repository",
    "SQLiteRepository",
    "PostgresRepository",
})
#: The API's lru-cached server repository (``app.api.deps.repository``).
SERVER_MODULE = "app.api.deps"

#: Files of the serving process itself (path prefix -> reason). The API layer
#: reaches the server's one lru-cached repository through ``deps.repository``.
SERVING_PROCESS = {
    "backend/app/api/":
        "the serving process: deps.repository() is its startup composition",
    "backend/app/main.py":
        "the serving process's application factory",
    "backend/app/services/startup_warmup.py":
        "the serving process's lifespan startup",
}

#: ``path::enclosing function`` -> why this construction owns its schema.
SCHEMA_OWNERS = {
    "backend/app/repositories/factory.py::create_repository":
        "the backend selector; forwards the caller's migrate/seed keywords",
    "backend/app/bootstrap.py::create_application_repository":
        "the composition root; forwards owns_schema (server: True, guests: False)",
    "backend/app/services/maintenance_cli.py::open_cli_repository":
        "calls the module's guest seam (create_repository = create_schema_guest_repository)",
    "scripts/smoke_backend.py::check_object_schemas":
        "smoke harness on its own temporary root",
    "scripts/smoke_backend.py::check_kg_store_ask_and_conversations":
        "smoke harness on its own temporary root",
    "scripts/smoke_backend.py::check_api_layer":
        "smoke harness on its own temporary root",
    "scripts/smoke_backend.py::check_pipeline_event_logging":
        "smoke harness on its own temporary root",
    "scripts/smoke_backend.py::main":
        "smoke harness on its own temporary root",
    "scripts/generate_repository_contract_fixtures.py::_new_offline_repo":
        "contract fixtures generated on a throwaway database",
    "scripts/bench_sqlite_writes.py::_make_repo":
        "write benchmark on its own explicit database path",
    "scripts/bench_scale_build_paging.py::_repository":
        "benchmark that refuses anything but a dedicated *_test database",
    "scripts/measure_sync_capture_cost.py::_measure_sqlite":
        "cost measurement on its own bench database",
    "scripts/measure_sync_capture_cost.py::_measure_postgres":
        "cost measurement on its own bench database",
    "scripts/verify_repository_snapshot.py::verify_snapshot":
        "models a real server start on a private restored copy",
}


def _keyword_is_false(call: ast.Call, name: str) -> bool:
    return any(
        keyword.arg == name
        and isinstance(keyword.value, ast.Constant)
        and keyword.value.value is False
        for keyword in call.keywords
    )


def _is_guest_call(call: ast.Call) -> bool:
    return _keyword_is_false(call, "owns_schema") or (
        _keyword_is_false(call, "migrate") and _keyword_is_false(call, "seed")
    )


def _local_constructor_names(tree: ast.AST) -> tuple[set[str], set[str]]:
    """Names that call a constructor directly, and names bound to the server
    module (for ``deps.repository()``)."""
    names = set(CONSTRUCTORS)
    server_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if alias.name in CONSTRUCTORS:
                    names.add(alias.asname or alias.name)
                if node.module == SERVER_MODULE and alias.name == "repository":
                    names.add(alias.asname or alias.name)
                if node.module == "app.api" and alias.name == "deps":
                    server_modules.add(alias.asname or alias.name)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == SERVER_MODULE and alias.asname:
                    server_modules.add(alias.asname)
    return names, server_modules


def _constructions(path: Path) -> list[tuple[str, ast.Call]]:
    """``(enclosing top-level function, call)`` for every construction; a
    finding is identified by its scope and source text, never a line number."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    names, server_modules = _local_constructor_names(tree)
    found: list[tuple[str, ast.Call]] = []

    def visit(node: ast.AST, scope: str) -> None:
        for child in ast.iter_child_nodes(node):
            child_scope = scope
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                child_scope = child.name if scope == "<module>" else scope
            if isinstance(child, ast.Call):
                func = child.func
                hit = (isinstance(func, ast.Name) and func.id in names) or (
                    isinstance(func, ast.Attribute)
                    and (
                        func.attr in CONSTRUCTORS
                        or (
                            func.attr == "repository"
                            and isinstance(func.value, ast.Name)
                            and func.value.id in server_modules
                        )
                    )
                )
                if hit:
                    found.append((child_scope, child))
            visit(child, child_scope)

    visit(tree, "<module>")
    return found


def _scan() -> tuple[list[str], set[str]]:
    violations: list[str] = []
    used: set[str] = set()
    for top in SCANNED:
        for path in sorted((ROOT / top).rglob("*.py")):
            relative = path.relative_to(ROOT).as_posix()
            if "/tests/" in f"/{relative}" or "__pycache__" in relative:
                continue
            for scope, call in _constructions(path):
                if _is_guest_call(call):
                    continue
                serving = next((p for p in SERVING_PROCESS if relative.startswith(p)), None)
                if serving is not None:
                    used.add(serving)
                    continue
                key = f"{relative}::{scope}"
                if key in SCHEMA_OWNERS:
                    used.add(key)
                    continue
                violations.append(f"{relative} (in {scope}): {ast.unparse(call)}")
    return violations, used


@pytest.mark.architecture_contract
def test_only_declared_schema_owners_compose_a_migrating_or_seeding_repository():
    violations, _ = _scan()
    assert not violations, (
        "these constructions would migrate and seed (re-hash the admin password) "
        "outside the server; open the database through "
        "app.services.maintenance_cli (open_cli_repository / "
        "open_maintenance_cli_repository / create_schema_guest_repository / "
        "create_bare_schema_guest_repository), or, if the process really owns a "
        "scratch database, add it to SCHEMA_OWNERS with the reason:\n  "
        + "\n  ".join(violations)
    )


@pytest.mark.architecture_contract
def test_every_declared_schema_owner_still_exists():
    _, used = _scan()
    stale = sorted((set(SCHEMA_OWNERS) | set(SERVING_PROCESS)) - used)
    assert not stale, f"remove stale SCHEMA_OWNERS entries: {stale}"


def test_the_cli_seam_is_the_guest_composition():
    from app.services import maintenance_cli

    assert maintenance_cli.create_repository is maintenance_cli.create_schema_guest_repository


def test_the_scan_catches_an_aliased_seeding_composition(tmp_path):
    """The guard's own mutation anchor: a new CLI under an alias is caught."""
    sample = tmp_path / "new_cli.py"
    sample.write_text(
        "from app.bootstrap import create_application_repository as make\n"
        "from app.api import deps\n"
        "def main():\n"
        "    make(object())\n"
        "    make(object(), owns_schema=False)\n"
        "    deps.repository()\n",
        encoding="utf-8",
    )
    flagged = [
        ast.unparse(call) for _, call in _constructions(sample) if not _is_guest_call(call)
    ]
    assert flagged == ["make(object())", "deps.repository()"]
