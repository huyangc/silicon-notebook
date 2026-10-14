"""Find G1 tests whose own code starts a real child process.

A test that starts a child process checks a process-level contract and carries
``process_contract`` so that it runs in G2 only (docs/development.md). This
scan reads one test module and reports every test that is still selected by
G1 and that starts a process itself: through ``subprocess`` (``run``,
``Popen``, ``call``, ``check_call``, ``check_output``, also imported by name),
``multiprocessing`` (``Process``, ``Pool``, ``get_context``), a
``ProcessPoolExecutor``, ``os.fork``/``os.spawn*``/``os.posix_spawn*`` or
``asyncio.create_subprocess_*``. The test's own body counts, and so does every
module-level helper it calls by name and every module-level fixture it takes
as a parameter, transitively.

It is a source scan: a process started inside application code, a testkit in
another module or a conftest fixture is not seen.
"""
from __future__ import annotations

import ast
from collections.abc import Collection

_SUBPROCESS_CALLS = frozenset({"run", "Popen", "call", "check_call", "check_output"})
_MULTIPROCESSING_CALLS = frozenset({"Process", "Pool", "get_context"})
_OS_CALLS = frozenset({"fork", "forkpty", "posix_spawn", "posix_spawnp"})
_ASYNCIO_CALLS = frozenset({"create_subprocess_exec", "create_subprocess_shell"})


def _imported_names(tree: ast.Module) -> dict[str, str]:
    """Local name -> "module.attr" for ``from <module> import <attr> [as x]``."""
    names: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                names[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    return names


def _module_aliases(tree: ast.Module) -> dict[str, str]:
    """Local name -> module for ``import <module> [as x]``."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                aliases[alias.asname or alias.name.split(".")[0]] = (
                    alias.name if alias.asname else alias.name.split(".")[0]
                )
    return aliases


def _starts_a_process(call: ast.Call, modules: dict[str, str], imported: dict[str, str]) -> bool:
    func = call.func
    if isinstance(func, ast.Attribute) and func.attr == "ProcessPoolExecutor":
        return True
    if isinstance(func, ast.Attribute) and isinstance(func.value, ast.Name):
        module = modules.get(func.value.id, func.value.id)
        attr = func.attr
        return (
            (module == "subprocess" and attr in _SUBPROCESS_CALLS)
            or (module == "multiprocessing" and attr in _MULTIPROCESSING_CALLS)
            or (module == "os" and (attr in _OS_CALLS or attr.startswith("spawn")))
            or (module == "asyncio" and attr in _ASYNCIO_CALLS)
            or attr == "ProcessPoolExecutor"
        )
    if isinstance(func, ast.Name):
        origin = imported.get(func.id, "")
        module, _, attr = origin.rpartition(".")
        return (
            (module == "subprocess" and attr in _SUBPROCESS_CALLS)
            or (module.startswith("multiprocessing") and attr in _MULTIPROCESSING_CALLS)
            or (module == "os" and (attr in _OS_CALLS or attr.startswith("spawn")))
            or (module == "asyncio" and attr in _ASYNCIO_CALLS)
            or attr == "ProcessPoolExecutor"
        )
    return False


def _is_fixture(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    def plain(expr: ast.expr) -> bool:
        return (isinstance(expr, ast.Name) and expr.id == "fixture") or (
            isinstance(expr, ast.Attribute)
            and expr.attr == "fixture"
            and isinstance(expr.value, ast.Name)
            and expr.value.id == "pytest"
        )

    return any(
        plain(decorator) or (isinstance(decorator, ast.Call) and plain(decorator.func))
        for decorator in node.decorator_list
    )


def _marker_names(expressions: list[ast.expr]) -> set[str]:
    """``pytest.mark.<name>`` names anywhere in the given expressions."""
    names: set[str] = set()
    for expression in expressions:
        for node in ast.walk(expression):
            if (
                isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Attribute)
                and node.value.attr == "mark"
            ):
                names.add(node.attr)
    return names


def _pytestmark(body: list[ast.stmt]) -> list[ast.expr]:
    values: list[ast.expr] = []
    for node in body:
        if isinstance(node, ast.Assign) and any(
            getattr(target, "id", "") == "pytestmark" for target in node.targets
        ):
            values.append(node.value)
        elif (
            isinstance(node, ast.AnnAssign)
            and getattr(node.target, "id", "") == "pytestmark"
            and node.value is not None
        ):
            values.append(node.value)
    return values


def g1_process_tests(
    source: str,
    filename: str,
    *,
    g2_markers: Collection[str],
    g2_tests: Collection[str] = (),
) -> list[str]:
    """Names of the G1-selected tests in this module that start a child process.

    ``g2_markers`` are the marker names that move a test to G2; ``g2_tests``
    are test names conftest moves to G2 by name (its marker-driving tables).
    Class methods are reported as ``Class.test``.
    """
    tree = ast.parse(source, filename=filename)
    modules = _module_aliases(tree)
    imported = _imported_names(tree)
    functions = {
        node.name: node
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    }
    fixtures = {name for name, node in functions.items() if _is_fixture(node)}

    def direct(node: ast.AST) -> bool:
        return any(
            isinstance(sub, ast.Call) and _starts_a_process(sub, modules, imported)
            for sub in ast.walk(node)
        )

    def uses(node: ast.FunctionDef | ast.AsyncFunctionDef) -> set[str]:
        called = {
            sub.func.id for sub in ast.walk(node)
            if isinstance(sub, ast.Call) and isinstance(sub.func, ast.Name)
        }
        params = {arg.arg for arg in node.args.args + node.args.kwonlyargs}
        return (called & (set(functions) - fixtures)) | (params & fixtures)

    spawners = {name for name, node in functions.items() if direct(node)}
    changed = True
    while changed:
        changed = False
        for name, node in functions.items():
            if name not in spawners and uses(node) & spawners:
                spawners.add(name)
                changed = True

    module_markers = _marker_names(_pytestmark(tree.body))
    if module_markers & set(g2_markers):
        return []

    found: list[str] = []

    def check(node, qualified: str, inherited: set[str]) -> None:
        if not node.name.startswith("test_") or _is_fixture(node):
            return
        if node.name in g2_tests:
            return
        markers = inherited | _marker_names(node.decorator_list)
        if markers & set(g2_markers):
            return
        if direct(node) or uses(node) & spawners:
            found.append(qualified)

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            check(node, node.name, set())
        elif isinstance(node, ast.ClassDef):
            inherited = _marker_names(node.decorator_list) | _marker_names(
                _pytestmark(node.body)
            )
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    check(sub, f"{node.name}.{sub.name}", inherited)
    return found
