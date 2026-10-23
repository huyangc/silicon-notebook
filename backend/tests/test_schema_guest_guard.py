"""Guard: no process except the server and the explicit migration command may
compose a repository that migrates or seeds, and no guest may skip the version
gate.

Why a static scan rather than a process-role assertion on the seed path: the
seed path is reached by every default repository construction, and the test
suite and the scratch-database harnesses construct thousands of those on
throwaway databases. A runtime assertion would either need every one of them to
declare a role (noise that hides the real signal) or default to "allowed",
which guards nothing -- and a new CLI that forgot to declare itself would
default the same way.

The scan looks at every repository construction in production and tool code
(``backend/app``, ``scripts``, ``examples``) and accepts it only in one of
three listed places, each with its reason:

* ``SERVING_PROCESS`` -- the server itself (every callable imported from
  ``app.api.deps``, and every ``deps.x()`` / ``app.api.deps.x()`` call,
  counts as a construction: they all reach its lru-cached repository);
* ``GUEST_ENTRIES`` -- the few functions that run the version gate first and
  then compose with the literal guest keywords (``owns_schema=False`` or
  ``migrate=False, seed=False``). Writing those keywords anywhere else is a
  violation too: it skips the migration and the seed but also the version gate,
  so a CLI must call a guest entry instead. The one entry whose gate lives in
  its callers (``open_scale_build_repository``) is checked separately: every
  function that calls it must call ``verify_migration_ledger`` first;
* ``SCHEMA_OWNERS`` -- the composition roots, the adapters (which build the
  persistence bundles, themselves counted as constructions) and the tools that
  own a scratch database or a private copy.

A new CLI that calls ``create_application_repository(settings)`` -- under an
import alias, or through ``deps.repository()`` -- fails here with its path,
scope and call text (never a line number). A listed place that no longer
constructs anything fails too, so the lists cannot rot into blanket exemptions.

Known blind spots, accepted: the scan follows names bound by ``import`` /
``from ... import`` only. It does not see a constructor re-bound by plain
assignment (``make = create_application_repository; make(s)``), wrapped in
``functools.partial``, or fetched with ``getattr(module, "...")``; and the
scale-entry caller check is per function and by name, so it cannot follow a
gate run in a different function than the call. Catching those needs data-flow
analysis for spellings nothing in the tree uses today; each is an obvious
detour in review, whereas the spellings above are the ones code is actually
written in.
"""
from __future__ import annotations

import ast
import os
from functools import lru_cache
from pathlib import Path

import pytest

from tests.architecture.source_trees import read_source_tree

ROOT = Path(__file__).resolve().parents[2]
SCANNED = ("backend/app", "scripts", "examples")
CONSTRUCTORS = frozenset({
    "create_repository",
    "create_application_repository",
    "SQLiteRepository",
    "PostgresRepository",
    # The persistence bundles underneath the adapters: the PostgreSQL one runs
    # migrate + seed itself, the SQLite one hands a database to a migrator.
    "PostgresPersistenceBundleFactory",
    "SqlitePersistenceBundleFactory",
})
#: The guest entry whose version gate lives in its callers, and that gate.
CALLER_GATED_ENTRY = "open_scale_build_repository"
CALLER_GATE = "verify_migration_ledger"
#: The API dependency module: everything it exports reaches the server's one
#: lru-cached repository, so every callable imported from it is a construction.
SERVER_MODULE = "app.api.deps"

#: Files of the serving process itself (path prefix -> reason).
SERVING_PROCESS = {
    "backend/app/api/":
        "the serving process: deps.repository() is its startup composition",
    "backend/app/main.py":
        "the serving process's application factory",
    "backend/app/services/startup_warmup.py":
        "the serving process's lifespan startup",
}

#: ``path::function`` -> the version gate that runs before its guest composition.
GUEST_ENTRIES = {
    "backend/app/services/maintenance_cli.py::create_schema_guest_repository":
        "require_current_schema, then create_application_repository(owns_schema=False)",
    "backend/app/services/maintenance_cli.py::create_bare_schema_guest_repository":
        "require_current_schema, then create_repository(migrate=False, seed=False)",
    "backend/app/services/scale_build_cli.py::open_scale_build_repository":
        "every caller runs verify_migration_ledger (the same schema check) first "
        "-- asserted by test_every_scale_build_entry_caller_runs_the_ledger_check_first",
}

#: ``path::function`` -> why this construction owns its schema.
SCHEMA_OWNERS = {
    "backend/app/services/repository_factory.py::create_repository":
        "the backend selector; forwards the caller's migrate/seed keywords",
    "backend/app/bootstrap.py::create_application_repository":
        "the composition root; forwards owns_schema (server: True, guests: False)",
    "backend/app/services/postgres_repository.py::__init__":
        "the PostgreSQL adapter builds its bundle with its caller's migrate/seed",
    "backend/app/services/sqlite_repository.py::__init__":
        "the SQLite adapter builds its bundle; its migrator gets the caller's migrate/seed",
    "backend/app/migration/sqlite_to_postgres.py::prepare_upgraded_snapshot":
        "upgrades a private working copy of the snapshot, never the source database",
    "backend/app/services/maintenance_cli.py::open_cli_repository":
        "calls the module's guest seam (create_repository = create_schema_guest_repository)",
    "backend/app/eval/sa_calibration.py::_run_arm":
        "main() points DATABASE_URL at a fresh temporary calibration database",
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
    "scripts/smoke_memory_mcp.py::run":
        "MCP smoke: run() points DATABASE_URL at a TemporaryDirectory database",
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
    """Names that call a constructor directly (including everything imported
    from the server module), and names bound to the server module itself."""
    names = set(CONSTRUCTORS)
    server_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                if alias.name in CONSTRUCTORS or node.module == SERVER_MODULE:
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
    return list(_tree_constructions(read_source_tree(path)))


@lru_cache(maxsize=4096)
def _tree_constructions(tree: ast.Module) -> tuple[tuple[str, ast.Call], ...]:
    # The shared tree is immutable; cache only its semantic projection. A path
    # lookup still discovers and reads current source before reaching this cache.
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
                            isinstance(func.value, ast.Name)
                            and func.value.id in server_modules
                        )
                        # ``import app.api.deps`` then ``app.api.deps.x()``
                        or _is_server_module(func.value)
                    )
                )
                if hit:
                    found.append((child_scope, child))
            visit(child, child_scope)

    visit(tree, "<module>")
    return tuple(found)


def _is_server_module(node: ast.AST) -> bool:
    # Avoid rendering every attribute expression in the repository just to
    # recognize the one dotted module spelling accepted by this guard.
    return (
        isinstance(node, ast.Attribute) and node.attr == "deps"
        and isinstance(node.value, ast.Attribute) and node.value.attr == "api"
        and isinstance(node.value.value, ast.Name) and node.value.value.id == "app"
    )


def _call_names_in_source_order(node: ast.AST) -> list[str]:
    """Called names (``f`` for ``f()``, ``attr`` for ``x.attr()``), pre-order,
    which for a statement sequence is source order."""
    names: list[str] = []

    def visit(parent: ast.AST) -> None:
        for child in ast.iter_child_nodes(parent):
            if isinstance(child, ast.Call):
                func = child.func
                if isinstance(func, ast.Name):
                    names.append(func.id)
                elif isinstance(func, ast.Attribute):
                    names.append(func.attr)
            visit(child)

    visit(node)
    return names


def _ungated_scale_entry_callers(path: Path) -> tuple[list[str], list[str]]:
    """``(callers, callers that never ran the gate first)`` of the one guest
    entry whose version gate lives in its callers."""
    callers, ungated = _tree_scale_entry_callers(read_source_tree(path))
    return list(callers), list(ungated)


@lru_cache(maxsize=4096)
def _tree_scale_entry_callers(tree: ast.Module) -> tuple[tuple[str, ...], tuple[str, ...]]:
    callers: list[str] = []
    ungated: list[str] = []
    nodes = tuple(ast.walk(tree))
    if not any(
        isinstance(node, ast.Call)
        and (
            isinstance(node.func, ast.Name) and node.func.id == CALLER_GATED_ENTRY
            or isinstance(node.func, ast.Attribute) and node.func.attr == CALLER_GATED_ENTRY
        )
        for node in nodes
    ):
        return (), ()
    for function in nodes:
        if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        if function.name == CALLER_GATED_ENTRY:
            continue
        calls = _call_names_in_source_order(function)
        if CALLER_GATED_ENTRY not in calls:
            continue
        callers.append(function.name)
        if CALLER_GATE not in calls[: calls.index(CALLER_GATED_ENTRY)]:
            ungated.append(function.name)
    return tuple(callers), tuple(ungated)


def _verdict(relative: str, scope: str, call: ast.Call) -> tuple[str | None, str | None]:
    """``(listed place that accepts it, violation text)`` -- exactly one is set."""
    serving = next((p for p in SERVING_PROCESS if relative.startswith(p)), None)
    if serving is not None:
        return serving, None
    key = f"{relative}::{scope}"
    if key in GUEST_ENTRIES:
        if _is_guest_call(call):
            return key, None
        return None, f"{key}: a guest entry composing without the guest keywords"
    if key in SCHEMA_OWNERS:
        return key, None
    if _is_guest_call(call):
        return None, f"{key}: guest keywords outside a guest entry skip the version gate"
    return None, f"{key}: would migrate and seed (re-hash the admin password)"


def _scan() -> tuple[list[str], set[str]]:
    violations: list[str] = []
    used: set[str] = set()
    for top in SCANNED:
        for path in sorted((ROOT / top).rglob("*.py")):
            relative = path.relative_to(ROOT).as_posix()
            if "/tests/" in f"/{relative}" or "__pycache__" in relative:
                continue
            for scope, call in _constructions(path):
                accepted, violation = _verdict(relative, scope, call)
                if accepted is not None:
                    used.add(accepted)
                else:
                    violations.append(f"{violation}: {ast.unparse(call)}")
    return violations, used


@pytest.mark.architecture_contract
@pytest.mark.xdist_group(name="schema_guest_scans")
def test_only_listed_places_compose_a_repository():
    violations, _ = _scan()
    assert not violations, (
        "open the database through app.services.maintenance_cli "
        "(open_cli_repository / open_maintenance_cli_repository / "
        "create_schema_guest_repository / create_bare_schema_guest_repository), "
        "or, if the process really owns a scratch database, add it to "
        "SCHEMA_OWNERS with the reason:\n  " + "\n  ".join(violations)
    )


@pytest.mark.architecture_contract
@pytest.mark.xdist_group(name="schema_guest_scans")
def test_every_listed_place_still_exists():
    _, used = _scan()
    listed = set(SCHEMA_OWNERS) | set(SERVING_PROCESS) | set(GUEST_ENTRIES)
    stale = sorted(listed - used)
    assert not stale, f"remove stale entries: {stale}"


@pytest.mark.architecture_contract
@pytest.mark.xdist_group(name="schema_guest_scans")
def test_every_scale_build_entry_caller_runs_the_ledger_check_first():
    """``open_scale_build_repository`` composes with the guest keywords but
    leaves the version gate to its callers; each of them must call
    ``verify_migration_ledger`` before it."""
    callers: list[str] = []
    ungated: list[str] = []
    for top in SCANNED:
        for path in sorted((ROOT / top).rglob("*.py")):
            relative = path.relative_to(ROOT).as_posix()
            if "/tests/" in f"/{relative}" or "__pycache__" in relative:
                continue
            found, bad = _ungated_scale_entry_callers(path)
            callers += [f"{relative}::{name}" for name in found]
            ungated += [f"{relative}::{name}" for name in bad]
    assert callers, "the scan must find the scale-build entry's callers"
    assert not ungated, f"call {CALLER_GATE} before {CALLER_GATED_ENTRY}: {ungated}"


def test_the_scan_catches_a_caller_that_skips_the_ledger_check(tmp_path):
    sample = tmp_path / "caller.py"
    sample.write_text(
        "def gated(settings):\n"
        "    verify_migration_ledger(settings.database_url)\n"
        "    with open_scale_build_repository(settings) as repo:\n"
        "        pass\n"
        "def late(settings):\n"
        "    with open_scale_build_repository(settings) as repo:\n"
        "        verify_migration_ledger(settings.database_url)\n",
        encoding="utf-8",
    )
    assert _ungated_scale_entry_callers(sample) == (["gated", "late"], ["late"])


def test_scan_projections_see_changed_contents_and_discovered_files(tmp_path, monkeypatch):
    monkeypatch.setattr(f"{__name__}.ROOT", tmp_path)
    sample = tmp_path / "scripts" / "tool.py"
    sample.parent.mkdir()
    sample.write_text("def run():\n    innocent_function()\n", encoding="utf-8")
    assert _scan() == ([], set())
    original_stat = sample.stat()
    replacement = "def run():\n    create_repository()\n"
    assert len(replacement.encode()) == original_stat.st_size
    sample.write_text(replacement, encoding="utf-8")
    os.utime(sample, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    violations, used = _scan()
    assert used == set()
    assert violations == [
        "scripts/tool.py::run: would migrate and seed (re-hash the admin password): create_repository()"
    ]
    sample.unlink()
    other = sample.with_name("other.py")
    other.write_text(replacement, encoding="utf-8")
    assert _scan()[0][0].startswith("scripts/other.py::run:")


def test_scale_projection_sees_reordered_same_size_gate_calls(tmp_path):
    sample = tmp_path / "caller.py"
    gated = (
        "def outer():\n"
        "    def inner():\n"
        "        verify_migration_ledger()\n"
        "        open_scale_build_repository()\n"
    )
    sample.write_text(gated, encoding="utf-8")
    assert _ungated_scale_entry_callers(sample) == (["outer", "inner"], [])
    original_stat = sample.stat()
    lines = gated.splitlines(keepends=True)
    sample.write_text("".join([*lines[:2], lines[3], lines[2]]), encoding="utf-8")
    os.utime(sample, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    assert _ungated_scale_entry_callers(sample) == (["outer", "inner"], ["outer", "inner"])


def test_the_scan_catches_a_dotted_deps_call_and_a_bundle_factory(tmp_path):
    sample = tmp_path / "scripts" / "tool.py"
    sample.parent.mkdir()
    sample.write_text(
        "import app.api.deps\n"
        "from app.repositories.postgres.bundle import PostgresPersistenceBundleFactory\n"
        "def main():\n"
        "    app.api.deps.repository()\n"
        "    PostgresPersistenceBundleFactory().create(settings=None)\n",
        encoding="utf-8",
    )
    flagged = [
        ast.unparse(call)
        for scope, call in _constructions(sample)
        if _verdict("scripts/tool.py", scope, call)[0] is None
    ]
    assert flagged == ["app.api.deps.repository()", "PostgresPersistenceBundleFactory()"]


def test_the_cli_seam_is_the_guest_composition():
    from app.services import maintenance_cli

    assert maintenance_cli.create_repository is maintenance_cli.create_schema_guest_repository


def test_the_scan_catches_aliased_server_and_gate_skipping_compositions(tmp_path):
    """The guard's own mutation anchor: an import alias, any deps callable and
    literal guest keywords outside a guest entry are all caught."""
    sample = tmp_path / "scripts" / "new_cli.py"
    sample.parent.mkdir()
    sample.write_text(
        "from app.bootstrap import create_application_repository as make\n"
        "from app.api import deps\n"
        "from app.api.deps import identity_repository\n"
        "def main():\n"
        "    make(object())\n"
        "    make(object(), owns_schema=False)\n"
        "    deps.notebook_store()\n"
        "    identity_repository()\n",
        encoding="utf-8",
    )
    verdicts = [
        _verdict("scripts/new_cli.py", scope, call)
        for scope, call in _constructions(sample)
    ]
    assert [accepted for accepted, _ in verdicts] == [None, None, None, None]
    assert "skip the version gate" in verdicts[1][1]
