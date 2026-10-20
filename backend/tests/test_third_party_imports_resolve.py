"""Every third-party import in the backend and scripts resolves to a locked package.

Several dependencies are imported lazily inside a function and fail open: the
PDF fallback catches any exception around ``import pymupdf4llm`` and quietly
drops to pypdf plain text. A lock that pins a broken release (PyPI's
``pymupdf4llm`` 2.0 is an empty placeholder with no module at all) then never
fails a test — it only degrades parsing. This scan makes the environment the
gates run in answer for every such import:

* the module imports, and every name a ``from X import Y`` asks for exists;
* inside a function, every ``X.attr`` read on a lazily imported ``import X``
  exists (``pymupdf4llm.to_markdown``);
* the distribution that provides the top-level module is in the runtime lock
  (``backend/app``) or in one of the two locks (``scripts/``), so the import is
  satisfied by what CI and deployment install, not by a developer's extra
  packages.
"""
from __future__ import annotations

import ast
from dataclasses import dataclass, field
import importlib
from importlib import metadata
import importlib.util
from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "backend" / "app"
SCRIPTS = ROOT / "scripts"

_spec = importlib.util.spec_from_file_location(
    "python_lockfiles", ROOT / "scripts" / "python_lockfiles.py"
)
assert _spec is not None and _spec.loader is not None
lockfiles = importlib.util.module_from_spec(_spec)
sys.modules.setdefault(_spec.name, lockfiles)
_spec.loader.exec_module(lockfiles)

RUNTIME_LOCKED = frozenset(lockfiles.parse_lock(ROOT / "backend" / "requirements.txt"))
ANY_LOCKED = RUNTIME_LOCKED | frozenset(
    lockfiles.parse_lock(ROOT / "backend" / "requirements-dev.txt")
)


@dataclass
class ImportUse:
    module: str
    names: set[str] = field(default_factory=set)
    attributes: set[str] = field(default_factory=set)
    files: set[str] = field(default_factory=set)


def _first_party(root: Path) -> frozenset[str]:
    names = {"app", "tests", "__future__"}
    if root == SCRIPTS:
        names |= {path.stem for path in SCRIPTS.glob("*.py")}
        names |= {path.name for path in SCRIPTS.iterdir() if (path / "__init__.py").exists()}
    return frozenset(names)


def _third_party_imports(root: Path) -> dict[str, ImportUse]:
    skip = frozenset(sys.stdlib_module_names) | _first_party(root)
    uses: dict[str, ImportUse] = {}

    def use(module: str, path: Path) -> ImportUse | None:
        if module.split(".")[0] in skip:
            return None
        entry = uses.setdefault(module, ImportUse(module))
        entry.files.add(path.relative_to(ROOT).as_posix())
        return entry

    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                entry = use(node.module, path)
                if entry is not None:
                    entry.names.update(alias.name for alias in node.names if alias.name != "*")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    use(alias.name, path)
        # Attribute reads on a module imported inside the function that reads it.
        for function in ast.walk(tree):
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            bound: dict[str, str] = {}
            for node in ast.walk(function):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0] not in skip:
                            bound[alias.asname or alias.name.split(".")[0]] = (
                                alias.name if alias.asname else alias.name.split(".")[0]
                            )
            for node in ast.walk(function):
                if (
                    isinstance(node, ast.Attribute)
                    and isinstance(node.value, ast.Name)
                    and node.value.id in bound
                ):
                    entry = use(bound[node.value.id], path)
                    if entry is not None:
                        entry.attributes.add(node.attr)
    return uses


def _problems(root: Path, locked: frozenset[str]) -> list[str]:
    providers = metadata.packages_distributions()
    problems = []
    for module, entry in sorted(_third_party_imports(root).items()):
        where = sorted(entry.files)[0]
        try:
            imported = importlib.import_module(module)
        except Exception as exc:  # noqa: BLE001 - every failure is the finding
            problems.append(f"{module} ({where}): import failed: {exc!r}")
            continue
        for name in sorted(entry.names | entry.attributes):
            if hasattr(imported, name):
                continue
            try:
                importlib.import_module(f"{module}.{name}")
            except ImportError:
                problems.append(f"{module} ({where}): has no {name!r}")
        top = module.split(".")[0]
        dists = {lockfiles.canonical_name(dist) for dist in providers.get(top, [])}
        if not dists & locked:
            problems.append(
                f"{module} ({where}): provided by {sorted(dists) or 'no distribution'}, none of them locked"
            )
    return problems


def test_backend_third_party_imports_resolve_to_the_runtime_lock() -> None:
    uses = _third_party_imports(APP)
    # The scan must see the fail-open lazy imports it exists for.
    assert {"pymupdf4llm", "mammoth", "docx", "pptx", "pypdf", "xlrd", "igraph", "hnswlib"} <= set(uses)
    assert "to_markdown" in uses["pymupdf4llm"].attributes
    assert _problems(APP, RUNTIME_LOCKED) == []


def test_script_third_party_imports_resolve_to_a_lock() -> None:
    assert _problems(SCRIPTS, ANY_LOCKED) == []


def test_the_scan_reports_a_module_that_imports_but_lacks_a_name(tmp_path, monkeypatch) -> None:
    package = tmp_path / "pkg"
    (package / "app").mkdir(parents=True)
    (package / "app" / "lazy.py").write_text(
        "def load(path):\n"
        "    import json\n"
        "    import shellpkg\n"
        "    from shellpkg import present, absent\n"
        "    return shellpkg.convert(path), present, absent, json.dumps\n",
        encoding="utf-8",
    )
    (tmp_path / "shellpkg.py").write_text("present = 1\n", encoding="utf-8")
    monkeypatch.syspath_prepend(str(tmp_path))
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    uses = _third_party_imports(package)
    assert set(uses) == {"shellpkg"}
    assert uses["shellpkg"].names == {"present", "absent"}
    assert uses["shellpkg"].attributes == {"convert"}
    problems = _problems(package, frozenset({"shellpkg"}))
    assert any("has no 'absent'" in problem for problem in problems)
    assert any("has no 'convert'" in problem for problem in problems)
    assert any("none of them locked" in problem for problem in problems)
