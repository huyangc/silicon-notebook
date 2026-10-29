"""Answer-path isolation of app.domain.kg_attrs (spec 2026-10-28 §7.5).

Guard 1 — the module is imported only as ``from app.domain import kg_attrs``
(no ``as``): no from-import of its names, no ``import app.domain.kg_attrs``,
no ``import_module`` / ``__import__`` literal. Calls therefore always read
``kg_attrs.<name>`` at call time, which is what a runtime spy patches.

Guard 2 — every use of that bound name sits in a whitelisted place, matched on
the resolved import target (not on the bare text ``kg_attrs``, so an unrelated
local called ``kg_attrs`` cannot hide a real one and vice versa). The whitelist
grows only together with the call site it admits (plan G0-T1 / P2-4). Also:
nothing in ``backend/app`` imports the Gate 0 calibration tool.

Each guard is a pure function over (path, source) so the mutation cases below
prove it turns red on the spellings it exists to stop.
"""
from __future__ import annotations

import ast
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[1]
_APP = _BACKEND / "app"
_TARGET = "app.domain.kg_attrs"
_CALIBRATION = "app.eval.kg_attrs_calibration"

# "<path under backend/>" admits a whole module; "<path>::<Qualname>" admits
# one function (nested helpers are NOT covered by their parent's entry).
CALL_SITE_WHITELIST: frozenset[str] = frozenset({
    # Gate 0 calibration tool (manual, never imported by app; see below).
    "app/eval/kg_attrs_calibration.py",
})


def _module_name(rel: str) -> tuple[str, bool]:
    parts = rel[: -len(".py")].split("/")
    is_package = parts[-1] == "__init__"
    if is_package:
        parts = parts[:-1]
    return ".".join(parts), is_package


def _from_target(rel: str, node: ast.ImportFrom) -> str:
    if not node.level:
        return node.module or ""
    module, is_package = _module_name(rel)
    base = module.split(".")
    if not is_package:
        base = base[:-1]
    base = base[: len(base) - (node.level - 1)]
    return ".".join([*base, node.module] if node.module else base)


def _is_dynamic_import(call: ast.Call) -> bool:
    func = call.func
    name = func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""
    return name in ("import_module", "__import__")


def _literal_args(call: ast.Call) -> list[str]:
    return [a.value for a in call.args if isinstance(a, ast.Constant) and isinstance(a.value, str)]


def import_violations(rel: str, source: str) -> list[str]:
    """Guard 1 plus the calibration-tool import ban."""
    out: list[str] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == _TARGET or alias.name.startswith(_TARGET + "."):
                    out.append(f"{rel}: import {alias.name}")
                if alias.name == _CALIBRATION and rel != "app/eval/kg_attrs_calibration.py":
                    out.append(f"{rel}: imports the calibration tool")
        elif isinstance(node, ast.ImportFrom):
            target = _from_target(rel, node)
            if target == _TARGET or target.startswith(_TARGET + "."):
                out.append(f"{rel}: from {target} import ...")
            for alias in node.names:
                full = f"{target}.{alias.name}"
                if full == _TARGET and alias.asname is not None:
                    out.append(f"{rel}: kg_attrs imported as {alias.asname}")
                if full == _CALIBRATION or target == _CALIBRATION:
                    out.append(f"{rel}: imports the calibration tool")
        elif isinstance(node, ast.Call) and _is_dynamic_import(node):
            for literal in _literal_args(node):
                if "kg_attrs" in literal:
                    out.append(f"{rel}: dynamic import of {literal}")
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr":
            if any(literal == "kg_attrs" for literal in _literal_args(node)):
                out.append(f"{rel}: getattr(..., 'kg_attrs')")
        elif isinstance(node, ast.Attribute) and node.attr == "kg_attrs":
            # `from app import domain; domain.kg_attrs`, `app.domain.kg_attrs.x`:
            # the module reached through an attribute chain, not the one spelling.
            out.append(f"{rel}: attribute access .kg_attrs")
    return out


def _bound_names(rel: str, tree: ast.AST) -> set[str]:
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            target = _from_target(rel, node)
            for alias in node.names:
                if f"{target}.{alias.name}" == _TARGET:
                    names.add(alias.asname or alias.name)
    return names


class _Uses(ast.NodeVisitor):
    def __init__(self, bound: set[str]) -> None:
        self.bound = bound
        self.stack: list[str] = []
        self.sites: list[tuple[str, str]] = []

    def _scoped(self, node) -> None:
        self.stack.append(node.name)
        self.generic_visit(node)
        self.stack.pop()

    visit_FunctionDef = visit_AsyncFunctionDef = visit_ClassDef = _scoped

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if isinstance(node.value, ast.Name) and node.value.id in self.bound:
            self.sites.append((".".join(self.stack) or "<module>", f"kg_attrs.{node.attr}"))
            return
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if node.id in self.bound and isinstance(node.ctx, ast.Load):
            # the module object itself escaping (passed, stored, aliased)
            self.sites.append((".".join(self.stack) or "<module>", node.id))


def call_site_violations(rel: str, source: str, whitelist: frozenset[str]) -> list[str]:
    """Guard 2: uses of the resolved ``app.domain.kg_attrs`` binding outside the whitelist."""
    if rel == "app/domain/kg_attrs.py":
        return []
    tree = ast.parse(source)
    bound = _bound_names(rel, tree)
    if not bound:
        return []
    visitor = _Uses(bound)
    visitor.visit(tree)
    return [
        f"{rel}::{qualname}: {what}"
        for qualname, what in visitor.sites
        if rel not in whitelist and f"{rel}::{qualname}" not in whitelist
    ]


def _app_sources():
    for path in sorted(_APP.rglob("*.py")):
        yield path.relative_to(_BACKEND).as_posix(), path.read_text(encoding="utf-8")


# --- the real tree ----------------------------------------------------------

def test_kg_attrs_is_imported_only_as_the_module_attribute_form():
    found = [v for rel, src in _app_sources() for v in import_violations(rel, src)]
    assert found == [], found


def test_kg_attrs_is_used_only_at_whitelisted_call_sites():
    found = [v for rel, src in _app_sources() for v in call_site_violations(rel, src, CALL_SITE_WHITELIST)]
    assert found == [], found


def test_whitelist_entries_still_name_a_real_call_site():
    for entry in CALL_SITE_WHITELIST:
        rel, _, qualname = entry.partition("::")
        source = (_BACKEND / rel).read_text(encoding="utf-8")
        tree = ast.parse(source)
        visitor = _Uses(_bound_names(rel, tree))
        visitor.visit(tree)
        sites = {q for q, _ in visitor.sites}
        assert (sites if not qualname else sites & {qualname}), f"stale whitelist entry {entry}"


# --- mutation cases: each spelling must turn the guard red ------------------

@pytest.mark.parametrize("rel, source", [
    ("app/services/x.py", "from app.domain.kg_attrs import derive_concept_aliases\n"),
    ("app/services/x.py", "from app.domain import kg_attrs as ka\n"),
    ("app/services/x.py", "import app.domain.kg_attrs\n"),
    ("app/services/x.py", "import app.domain.kg_attrs as ka\n"),
    ("app/services/x.py", "import importlib\nimportlib.import_module('app.domain.kg_attrs')\n"),
    ("app/services/x.py", "__import__('app.domain.kg_attrs')\n"),
    ("app/domain/other.py", "from . import kg_attrs as k\n"),
    ("app/domain/other.py", "from .kg_attrs import normalize_latex\n"),
    ("app/services/x.py", "from app.eval import kg_attrs_calibration\n"),
    ("app/services/x.py", "import app.eval.kg_attrs_calibration\n"),
    ("app/services/x.py", "from app.eval.kg_attrs_calibration import main\n"),
    ("app/services/x.py", "from app import domain\ndomain.kg_attrs.normalize_latex('a')\n"),
    ("app/services/x.py", "import app\napp.domain.kg_attrs.normalize_latex('a')\n"),
    ("app/services/x.py", "import app.domain\nf = app.domain.kg_attrs\n"),
    ("app/services/x.py", "from app import domain\nm = getattr(domain, 'kg_attrs')\n"),
])
def test_guard1_turns_red(rel, source):
    assert import_violations(rel, source)


def test_guard1_accepts_the_one_allowed_spelling():
    assert import_violations("app/services/x.py", "from app.domain import kg_attrs\n") == []
    assert import_violations("app/domain/other.py", "from . import kg_attrs\n") == []
    assert import_violations("app/services/x.py", "from app.domain import kg_attrs_contracts as c\n") == []


_SERVICE = """
from app.domain import kg_attrs

class Svc:
    def allowed(self, name, items):
        return kg_attrs.derive_concept_aliases(name, items, exclude_names=())

    def helper(self, name, items):
        return kg_attrs.derive_concept_aliases(name, items, exclude_names=())
"""


def test_guard2_admits_only_the_whitelisted_function():
    whitelist = frozenset({"app/services/x.py::Svc.allowed"})
    found = call_site_violations("app/services/x.py", _SERVICE, whitelist)
    # mutation: move the call into a new helper of a whitelisted module.
    assert found == ["app/services/x.py::Svc.helper: kg_attrs.derive_concept_aliases"]


@pytest.mark.parametrize("source", [
    "from app.domain import kg_attrs\nx = kg_attrs.normalize_latex('a')\n",
    "from app.domain import kg_attrs\ndef f():\n    g = kg_attrs\n    return g\n",
    "from app.domain import kg_attrs\nclass Svc:\n    def allowed(self):\n"
    "        def inner():\n            return kg_attrs.attrs_eligible('a', 'b')\n        return inner()\n",
])
def test_guard2_turns_red_outside_the_whitelist(source):
    whitelist = frozenset({"app/services/x.py::Svc.allowed"})
    assert call_site_violations("app/services/x.py", source, whitelist)


def test_guard2_matches_the_resolved_import_not_the_bare_name():
    unrelated = "kg_attrs = object()\ndef f():\n    return kg_attrs.thing\n"
    assert call_site_violations("app/services/x.py", unrelated, frozenset()) == []
    renamed_module_attr = "from app.domain import kg_attrs\ndef f():\n    return kg_attrs.thing\n"
    assert call_site_violations("app/services/x.py", renamed_module_attr, frozenset())


def test_whole_module_entry_admits_every_function():
    whitelist = frozenset({"app/services/x.py"})
    assert call_site_violations("app/services/x.py", _SERVICE, whitelist) == []
