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
import os
from pathlib import Path
import sys

from tests.architecture.source_trees import read_source_tree


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
    """Top-level names that belong to the repository, never to a distribution.

    ``backend/app`` code imports ``app`` (and tests import ``tests``). Scripts
    run with ``scripts/`` on ``sys.path``, so a sibling module is imported by
    its bare name (``from diag_common import ...``, ``import python_env``):
    every ``scripts/*.py`` stem and every ``scripts/`` package directory is
    first-party there, and only there.
    """
    names = {"app", "tests", "__future__"}
    if root == SCRIPTS:
        names |= {path.stem for path in SCRIPTS.glob("*.py")}
        names |= {path.name for path in SCRIPTS.iterdir() if (path / "__init__.py").exists()}
    return frozenset(names)


def _third_party_imports(root: Path) -> dict[str, ImportUse]:
    """Every absolute import under ``root`` whose top-level name is neither the
    standard library (``sys.stdlib_module_names``, all platforms, so a
    Windows-only ``msvcrt`` fallback is not third-party) nor first-party.

    Policy: position does not matter. A module-level import, a lazy import in a
    function, one under ``if TYPE_CHECKING:`` and an optional one inside
    ``try: ... except ImportError:`` are all held to the same rule: they must
    import in the locked environment and their distribution must be locked.
    An optional import exists precisely so a missing package degrades quietly,
    which is the failure this scan is for. There is no allowlist today; should
    a dependency ever be optional on purpose, add an explicit allowlist here
    with the reason per entry rather than weakening the scan.
    """
    skip = frozenset(sys.stdlib_module_names) | _first_party(root)
    uses: dict[str, ImportUse] = {}

    def use(module: str, path: Path) -> ImportUse | None:
        if module.split(".")[0] in skip:
            return None
        entry = uses.setdefault(module, ImportUse(module))
        entry.files.add(path.relative_to(ROOT).as_posix())
        return entry

    for path in sorted(root.rglob("*.py")):
        tree = read_source_tree(path)
        nodes = tuple(ast.walk(tree))
        for node in nodes:
            if isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                entry = use(node.module, path)
                if entry is not None:
                    entry.names.update(alias.name for alias in node.names if alias.name != "*")
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    use(alias.name, path)
        # Attribute reads on a module imported inside the function that reads it.
        for function in nodes:
            if not isinstance(function, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            bound: dict[str, str] = {}
            attributes: list[ast.Attribute] = []
            for node in ast.walk(function):
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        if alias.name.split(".")[0] not in skip:
                            bound[alias.asname or alias.name.split(".")[0]] = (
                                alias.name if alias.asname else alias.name.split(".")[0]
                            )
                elif isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
                    attributes.append(node)
            for node in attributes:
                if node.value.id in bound:
                    entry = use(bound[node.value.id], path)
                    if entry is not None:
                        entry.attributes.add(node.attr)
    return uses


def _problems(uses: dict[str, ImportUse], locked: frozenset[str]) -> list[str]:
    providers = metadata.packages_distributions()
    problems = []
    for module, entry in sorted(uses.items()):
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
    assert _problems(uses, RUNTIME_LOCKED) == []


def test_script_third_party_imports_resolve_to_a_lock() -> None:
    uses = _third_party_imports(SCRIPTS)
    # Sibling script modules are first-party, the packages they use are not.
    assert not {"python_env", "diag_common", "python_lockfiles"} & set(uses)
    assert {"dotenv", "psycopg", "openpyxl"} <= set(uses)
    assert _problems(uses, ANY_LOCKED) == []


def test_the_scan_reports_a_module_that_imports_but_lacks_a_name(tmp_path, monkeypatch) -> None:
    package = tmp_path / "pkg"
    (package / "app").mkdir(parents=True)
    (package / "app" / "lazy.py").write_text(
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    import typedpkg\n"
        "try:\n"
        "    import optionalpkg\n"
        "except ImportError:\n"
        "    optionalpkg = None\n"
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
    assert set(uses) == {"shellpkg", "typedpkg", "optionalpkg"}
    assert uses["shellpkg"].names == {"present", "absent"}
    assert uses["shellpkg"].attributes == {"convert"}
    problems = _problems(uses, frozenset({"shellpkg"}))
    assert any("has no 'absent'" in problem for problem in problems)
    assert any("has no 'convert'" in problem for problem in problems)
    assert any("none of them locked" in problem for problem in problems)
    # TYPE_CHECKING-only and try/except ImportError imports get no exemption.
    assert any(problem.startswith("typedpkg ") and "import failed" in problem for problem in problems)
    assert any(problem.startswith("optionalpkg ") and "import failed" in problem for problem in problems)


def test_scan_keeps_nested_binding_semantics_and_reads_current_tree(tmp_path, monkeypatch):
    monkeypatch.setattr(sys.modules[__name__], "ROOT", tmp_path)
    sample = tmp_path / "lazy.py"
    source = (
        "def outer():\n"
        "    def inner():\n"
        "        import firstpkg as lazy\n"
        "        return lazy.inner\n"
        "    return lazy.outer\n"
    )
    sample.write_text(source, encoding="utf-8")
    uses = _third_party_imports(tmp_path)
    # The guard deliberately scans descendants of each function, including
    # imports in nested functions; both reads remain part of that contract.
    assert uses["firstpkg"].attributes == {"inner", "outer"}
    original_stat = sample.stat()
    sample.write_text(source.replace("firstpkg", "otherpkg"), encoding="utf-8")
    os.utime(sample, ns=(original_stat.st_atime_ns, original_stat.st_mtime_ns))
    fresh = _third_party_imports(tmp_path)
    assert set(fresh) == {"otherpkg"}
    assert fresh["otherpkg"].attributes == {"inner", "outer"}
    assert set(uses) == {"firstpkg"}
    sample.unlink()
    sample.with_name("new.py").write_text("from addedpkg import required\n", encoding="utf-8")
    assert set(_third_party_imports(tmp_path)) == {"addedpkg"}


def test_provider_checks_observe_current_environment(monkeypatch):
    uses = {"guard_probe": ImportUse("guard_probe", files={"probe.py"})}
    monkeypatch.setattr(importlib, "import_module", lambda name: object())
    monkeypatch.setattr(metadata, "packages_distributions", lambda: {"guard_probe": ["locked-provider"]})
    assert _problems(uses, frozenset({"locked-provider"})) == []
    monkeypatch.setattr(metadata, "packages_distributions", lambda: {"guard_probe": ["other-provider"]})
    assert _problems(uses, frozenset({"locked-provider"})) == [
        "guard_probe (probe.py): provided by ['other-provider'], none of them locked"
    ]
