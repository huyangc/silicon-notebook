#!/usr/bin/env python3
"""Census of component-layer members that production never reaches.

``scripts/audit_facade_callers.py`` audits ``RepositoryFacade``'s public
surface only. The components the facade hands work to -- the services, the
two adapters' stores, the persistence ports in ``repositories/ports.py``, the
extension hosts -- can accumulate members that only tests call (or that
nothing calls at all), and the facade audit cannot see them. This script is
the companion census for that layer: same "production / tests" split, same
"report, never edit" contract, but name-based rather than receiver-based,
because component receivers are spelled every possible way
(``self._store``, ``store``, ``self.deps.ask_state``, ``ports.ask_state`` ...).

Usage
-----
    PYTHONPATH=backend python3 scripts/audit_component_callers.py [--json OUT] [--markdown OUT]
    PYTHONPATH=backend python3 scripts/audit_component_callers.py --check

``--check`` is the contracts-lane gate (``scripts/check_contracts.sh``): it
exits non-zero when the set of candidates differs from
``scripts/component_test_only_allowlist.json`` in either direction -- a new
test-only or unreferenced member that is not registered, or a registered
entry that is no longer a candidate (deleted, or production now calls it).

Methodology (read before trusting a row)
----------------------------------------
1. **Members.** Every ``def`` / ``async def`` directly in a class body
   (methods, properties and their setters, static/class methods, Protocol
   members) of every class under ``backend/app`` outside ``backend/app/api``
   (the route layer: route handlers are module functions and its classes are
   request schemas).  Public and single-underscore ("semi-public") names are
   both members; dunders are not.  The facade layer is not: ``RepositoryFacade``,
   its two adapter subclasses and every Protocol the facade is handed out as
   (``check_architecture_boundaries.facade_protocol_bindings``, plus the
   in-repo Protocols those extend) belong to the facade audit and the facade
   surface fixture.  Their bodies still count as production callers here, so a
   component member reached only through a facade wrapper that nothing calls is
   invisible to this census -- retiring that wrapper is the facade audit's job.

2. **A reference is any spelling of the name, on any receiver.**  In a
   scanned file, name ``m`` is referenced by: an attribute ``<expr>.m`` (a
   call, a bound-method reference, a property read or write,
   ``super().m``, ``Class.m``); a bare name ``m`` (a class-body alias); a
   string constant equal to ``m`` (``getattr`` / ``hasattr`` / ``setattr``
   literals, ``operator.attrgetter``, dispatch tables keyed by method name,
   ``__all__``); a string constant that is a dotted or colon path with ``m``
   as one segment (``"pkg.mod:Class.m"`` entry points); and a ``getattr`` /
   ``hasattr`` / ``setattr`` / ``delattr`` / ``attrgetter`` / ``methodcaller``
   name built at run time from literal pieces (``f"_migration_{n}"``,
   ``f"delete_{chain}_page"``, ``"prefix" + x``), which references every
   member whose name fits the template.  Docstrings are not references.
   Because the receiver is ignored, a name that some other production class
   also uses is counted as used: the census can miss a test-only member whose
   name collides with a live one, but it never files a live member as
   test-only on that account.  ``METADATA_FILES`` (the generated ownership
   manifest and the audit/guard scripts) name members as data and are not
   scanned.

3. **Buckets.**  Production = ``backend/app``, ``scripts``, ``examples``
   (the in-repo sample plugins stand in for out-of-tree ones) and
   ``fangan``, minus any ``tests`` directory.  Tests = ``backend/tests`` and
   every ``tests`` directory under ``examples`` / ``fangan``.  A member with
   no production reference is ``test-only`` when some test references its
   name and ``unreferenced`` otherwise.  References inside the member's own
   ``def`` (recursion, a setter writing its own property) never count for it.

4. **Transitive.**  A production reference that sits inside the body of a
   member that is itself a candidate is not a production caller: it only
   runs when a test runs that member.  The census iterates to a fixed point,
   so a store method whose one production caller is a test-only service
   method is filed too.  Bodies of registered members (any category, see 7)
   keep counting: a kept member's own implementation is part of it, so
   registering a service method also keeps every same-named store method and
   port declaration it forwards to.

5. **Not candidates at all.**  A member that overrides a method of a class
   from outside ``app`` (``HTMLParser.handle_data``, a pydantic settings
   hook): the framework calls it.  A member with a decorator that registers
   it (a pydantic validator / serializer / ``computed_field``, a route or CLI
   command decorator on a receiver, ``*.register``): the decorator is the
   caller.  This is an allowlist of registering decorators: any other one
   (``contextmanager``, ``lru_cache``, ``cache``, ``wraps`` ...) only wraps
   the function, and its member is censused like any other.
   Classes nested in a function cannot be imported to check their MRO; their
   candidates are filed ``doubtful`` instead of guessed.

6. **Extension surface.**  Plugins live out of tree and only reach the SDK
   (``app.extension_sdk``) and the plugin contracts in ``app.domain`` /
   ``app.extensions``.  A member is ``extension_surface`` when its class is a
   Protocol defined there, is re-exported by an ``app.extension_sdk``
   module, or structurally implements (defines or inherits every method of)
   such a Protocol declaring the name.  Sharing one method name with a
   contract is not enough.  The census still lists these (an
   out-of-tree caller is invisible to it), but they are registered, never
   deleted.  The persistence ports in ``repositories/ports.py`` are not
   extension surface: plugins never get a store.

7. **Registration.**  Every candidate must be in the allowlist with one of
   ``test-observation`` (a test-only observation or construction point; the
   ``def`` carries a「测试观察点」comment or docstring, which ``--check``
   verifies), ``extension-port`` (section 6), ``runtime-reference``
   (production reaches it in a way this scan cannot see; the reason names
   the path) or ``pending-decision`` (its fate is a product or architecture
   call that has not been made).

This is a census for human review, not a type checker: a ``test-only`` row
is a lead to verify with ``grep`` before anything is deleted.
"""
from __future__ import annotations

import argparse
import ast
import importlib
import json
import re
import sys
import types
import typing
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
APP_ROOT = BACKEND_ROOT / "app"
ALLOWLIST_PATH = REPO_ROOT / "scripts" / "component_test_only_allowlist.json"

FACADE_REL = "backend/app/services/repository_facade.py"
FACADE_CLASS = "RepositoryFacade"
EXCLUDED_MEMBER_DIRS = ("backend/app/api/",)
PRODUCTION_ROOTS = ("backend/app", "scripts", "examples", "fangan")
TEST_ROOTS = ("backend/tests",)
# Files that name members as data about the code, never to call them: the
# generated facade ownership manifest (imported only by tests and the fixture
# generator) and the audit/guard scripts themselves.
METADATA_FILES = frozenset(
    {
        "backend/app/repositories/ownership_manifest.py",
        "scripts/audit_component_callers.py",
        "scripts/audit_facade_callers.py",
        "scripts/check_architecture_boundaries.py",
        "scripts/generate_repository_contract_fixtures.py",
    }
)
EXTENSION_MODULE_PREFIXES = ("app.extension_sdk", "app.extensions", "app.domain")
SDK_PREFIX = "app.extension_sdk"

CATEGORIES = ("test-observation", "extension-port", "runtime-reference", "pending-decision")
# Registered members are kept, so for the transitive pass their bodies are
# what keeps their own implementation alive: a registered observation point's
# helpers are part of it, not test-only members of their own.
LIVE_CATEGORIES = frozenset(CATEGORIES)

# Decorators that register the function with a framework, which then calls
# it: pydantic validators / serializers / computed fields, route and CLI
# command decorators (``router.get``, ``app.command`` ...) and
# ``*.register``. Matched on the last dotted segment; the route / command
# names only when spelled on a receiver (``router.get``, not a bare ``get``).
# Any other decorator (``contextmanager``, ``lru_cache``, ``cache``,
# ``wraps`` ...) only wraps the function, so its member is censused as usual.
REGISTERING_DECORATORS = frozenset(
    {
        "field_validator",
        "model_validator",
        "validator",
        "root_validator",
        "field_serializer",
        "model_serializer",
        "computed_field",
        "register",
    }
)
RECEIVER_REGISTERING_DECORATORS = frozenset(
    {
        "get", "post", "put", "patch", "delete", "head", "options",
        "api_route", "websocket", "route", "command", "callback",
    }
)
PROPERTY_ACCESSOR_SUFFIXES = (".setter", ".getter", ".deleter")
NAME_BUILDER_CALLS = frozenset({"getattr", "hasattr", "setattr", "delattr"})
NAME_BUILDER_ATTR_CALLS = frozenset({"attrgetter", "methodcaller"})
DOTTED_PATH_RE = re.compile(r"^[A-Za-z_][\w]*(?:[.:][A-Za-z_][\w]*)+$")
IGNORED_MRO = (object, typing.Protocol, typing.Generic)


# --------------------------------------------------------------------------
# Source walking
# --------------------------------------------------------------------------


def _rel(path: Path) -> str:
    return path.resolve().relative_to(REPO_ROOT).as_posix()


def _is_test_path(rel: str) -> bool:
    return rel.startswith(TEST_ROOTS) or "/tests/" in f"/{rel}"


def _iter_py(root: Path) -> Iterator[Path]:
    if not root.exists():
        return
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts or "node_modules" in path.parts:
            continue
        yield path


def _parse(path: Path) -> ast.Module | None:
    try:
        return ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (SyntaxError, UnicodeDecodeError):
        return None


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    return ""


def _module_name(rel: str) -> str:
    parts = Path(rel).with_suffix("").parts
    # backend/app/x/y.py -> app.x.y
    parts = parts[1:]
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


# --------------------------------------------------------------------------
# Members
# --------------------------------------------------------------------------


@dataclass
class Member:
    rel: str
    module: str
    qualname: str  # class qualname, e.g. "Outer.Inner"
    name: str
    lineno: int
    kinds: set[str] = field(default_factory=set)
    decorators: list[str] = field(default_factory=list)
    in_function: bool = False  # class nested inside a function body
    is_protocol: bool = False

    @property
    def key(self) -> str:
        return f"{self.rel}::{self.qualname}.{self.name}"


def _decorator_names(node: ast.FunctionDef | ast.AsyncFunctionDef) -> list[str]:
    return [_dotted(d) for d in node.decorator_list]


def _is_dunder(name: str) -> bool:
    return name.startswith("__") and name.endswith("__")


def _class_is_protocol(node: ast.ClassDef) -> bool:
    return any(_dotted(base).rsplit(".", 1)[-1] == "Protocol" for base in node.bases)


def collect_members() -> list[Member]:
    by_key: dict[str, Member] = {}
    order: list[str] = []
    for path in _iter_py(APP_ROOT):
        rel = _rel(path)
        if rel.startswith(EXCLUDED_MEMBER_DIRS):
            continue
        tree = _parse(path)
        if tree is None:
            continue
        module = _module_name(rel)

        def visit(body: list[ast.stmt], prefix: str, in_function: bool) -> None:
            for stmt in body:
                if isinstance(stmt, ast.ClassDef):
                    qual = f"{prefix}{stmt.name}"
                    protocol = _class_is_protocol(stmt)
                    for child in stmt.body:
                        if not isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                            continue
                        if _is_dunder(child.name):
                            continue
                        member = Member(rel, module, qual, child.name, child.lineno,
                                        in_function=in_function, is_protocol=protocol)
                        existing = by_key.get(member.key)
                        if existing is None:
                            by_key[member.key] = existing = member
                            order.append(member.key)
                        decorators = _decorator_names(child)
                        existing.decorators.extend(decorators)
                        if any(d.endswith(PROPERTY_ACCESSOR_SUFFIXES) for d in decorators):
                            existing.kinds.add("setter")
                        elif "property" in decorators or "cached_property" in decorators:
                            existing.kinds.add("property")
                        elif "staticmethod" in decorators:
                            existing.kinds.add("staticmethod")
                        elif "classmethod" in decorators:
                            existing.kinds.add("classmethod")
                        else:
                            existing.kinds.add("async" if isinstance(child, ast.AsyncFunctionDef) else "method")
                    visit(stmt.body, f"{qual}.", in_function)
                elif isinstance(stmt, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    visit(stmt.body, f"{prefix}{stmt.name}.<locals>.", True)
                elif isinstance(stmt, (ast.If, ast.Try, ast.With, ast.For, ast.While)):
                    for sub in ("body", "orelse", "finalbody", "handlers"):
                        block = getattr(stmt, sub, None) or []
                        for item in block:
                            if isinstance(item, ast.ExceptHandler):
                                visit(item.body, prefix, in_function)
                        visit([b for b in block if isinstance(b, ast.stmt)], prefix, in_function)

        visit(tree.body, "", False)
    return [by_key[k] for k in order]


# --------------------------------------------------------------------------
# References
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Ref:
    name: str  # literal name, or "" for a template
    pattern: re.Pattern[str] | None
    rel: str
    lineno: int
    owner: str | None  # Member.key of the enclosing class member, if any


def _template_regex(node: ast.AST) -> re.Pattern[str] | None:
    """Regex for an attribute name built at run time from literal pieces, or
    None when the expression carries no literal piece at all."""
    if isinstance(node, ast.JoinedStr):
        pieces: list[str] = []
        literal = False
        for value in node.values:
            if isinstance(value, ast.Constant) and isinstance(value.value, str):
                pieces.append(re.escape(value.value))
                literal = literal or bool(value.value)
            else:
                pieces.append(r".+")
        return re.compile("^" + "".join(pieces) + "$") if literal else None
    if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
        left = _template_regex(node.left) if not isinstance(node.left, ast.Constant) else None
        right = _template_regex(node.right) if not isinstance(node.right, ast.Constant) else None
        lpart = re.escape(node.left.value) if isinstance(node.left, ast.Constant) and isinstance(node.left.value, str) else (left.pattern[1:-1] if left else r".+")
        rpart = re.escape(node.right.value) if isinstance(node.right, ast.Constant) and isinstance(node.right.value, str) else (right.pattern[1:-1] if right else r".+")
        if lpart == r".+" and rpart == r".+":
            return None
        return re.compile(f"^{lpart}{rpart}$")
    return None


class _RefCollector(ast.NodeVisitor):
    def __init__(self, rel: str, module_keys: dict[tuple[str, str], str]) -> None:
        self.rel = rel
        self.module_keys = module_keys  # (class qualname, name) -> Member.key
        self.class_stack: list[str] = []
        self.func_stack: list[str] = []
        self.owner_stack: list[str | None] = [None]
        self.refs: list[Ref] = []
        self.docstrings: set[int] = set()

    # -- scope tracking -------------------------------------------------
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._mark_docstring(node)
        for item in node.bases + node.keywords + node.decorator_list:
            self.visit(item)
        prefix = self._qual_prefix()
        self.class_stack.append(f"{prefix}{node.name}")
        saved_funcs = self.func_stack
        self.func_stack = []
        for stmt in node.body:
            self.visit(stmt)
        self.func_stack = saved_funcs
        self.class_stack.pop()

    def _qual_prefix(self) -> str:
        if self.func_stack:
            return ".".join(self.func_stack) + ".<locals>."
        if self.class_stack:
            return self.class_stack[-1] + "."
        return ""

    def _visit_function(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> None:
        self._mark_docstring(node)
        directly_in_class = bool(self.class_stack) and not self.func_stack
        for decorator in node.decorator_list:
            # ``@name.setter`` on the def of ``name`` defines, it does not use.
            dotted = _dotted(decorator)
            if dotted.startswith(f"{node.name}.") and dotted.endswith(PROPERTY_ACCESSOR_SUFFIXES):
                continue
            self.visit(decorator)
        for item in (node.args, node.returns):
            if item is not None:
                self.visit(item)
        owner = self.owner_stack[-1]
        if directly_in_class:
            owner = self.module_keys.get((self.class_stack[-1], node.name), owner)
        self.owner_stack.append(owner)
        if directly_in_class:
            self.func_stack = [self.class_stack[-1], node.name]
        else:
            self.func_stack = self.func_stack + [node.name]
        for stmt in node.body:
            self.visit(stmt)
        self.func_stack = self.func_stack[:-1] if not directly_in_class else []
        self.owner_stack.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.generic_visit(node)

    def _mark_docstring(self, node: ast.AST) -> None:
        body = getattr(node, "body", None)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            self.docstrings.add(id(body[0].value))

    def visit_Module(self, node: ast.Module) -> None:
        self._mark_docstring(node)
        self.generic_visit(node)

    # -- references -----------------------------------------------------
    def _add(self, name: str, lineno: int, pattern: re.Pattern[str] | None = None) -> None:
        self.refs.append(Ref(name, pattern, self.rel, lineno, self.owner_stack[-1]))

    def visit_Attribute(self, node: ast.Attribute) -> None:
        self._add(node.attr, node.lineno)
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        self._add(node.id, node.lineno)

    def visit_Constant(self, node: ast.Constant) -> None:
        if not isinstance(node.value, str) or id(node) in self.docstrings:
            return
        value = node.value
        if value.isidentifier():
            self._add(value, node.lineno)
        elif DOTTED_PATH_RE.match(value):
            for segment in re.split(r"[.:]", value):
                self._add(segment, node.lineno)

    def visit_Call(self, node: ast.Call) -> None:
        func = _dotted(node.func)
        tail = func.rsplit(".", 1)[-1]
        name_args: list[ast.AST] = []
        if func in NAME_BUILDER_CALLS and len(node.args) >= 2:
            name_args = [node.args[1]]
        elif tail in NAME_BUILDER_ATTR_CALLS:
            name_args = list(node.args)
        for arg in name_args:
            pattern = _template_regex(arg)
            if pattern is not None:
                self._add("", node.lineno, pattern)
        self.generic_visit(node)


def collect_refs(
    roots: Iterable[str], tests: bool, member_index: dict[str, dict[tuple[str, str], str]]
) -> list[Ref]:
    refs: list[Ref] = []
    seen: set[Path] = set()
    for root in roots:
        for path in _iter_py(REPO_ROOT / root):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            rel = _rel(resolved)
            if _is_test_path(rel) != tests or rel in METADATA_FILES:
                continue
            tree = _parse(resolved)
            if tree is None:
                continue
            collector = _RefCollector(rel, member_index.get(rel, {}))
            collector.visit(tree)
            refs.extend(collector.refs)
    return refs


# --------------------------------------------------------------------------
# Runtime checks: framework overrides, extension surface
# --------------------------------------------------------------------------


def _ensure_backend_on_path() -> None:
    if str(BACKEND_ROOT) not in sys.path:
        sys.path.insert(0, str(BACKEND_ROOT))


_CLASS_CACHE: dict[tuple[str, str], type | None] = {}


def _resolve_class(member: Member) -> type | None:
    if member.in_function:
        return None
    cache_key = (member.module, member.qualname)
    if cache_key in _CLASS_CACHE:
        return _CLASS_CACHE[cache_key]
    resolved: type | None = None
    try:
        obj: object = importlib.import_module(member.module)
    except Exception:  # pragma: no cover - reported as doubtful
        obj = None
    for part in member.qualname.split("."):
        if obj is None:
            break
        obj = getattr(obj, part, None)
    if isinstance(obj, type):
        resolved = obj
    _CLASS_CACHE[cache_key] = resolved
    return resolved


def _external_override(cls: type, name: str) -> str | None:
    for klass in cls.__mro__[1:]:
        if klass in IGNORED_MRO or klass.__module__.startswith("app.") or klass.__module__ == "app":
            continue
        if name in vars(klass):
            return f"{klass.__module__}.{klass.__qualname__}"
    return None


def is_registering_decorator(dotted: str) -> bool:
    """Whether a decorator (its dotted name, call parentheses dropped) hands
    the function to a framework that calls it (see REGISTERING_DECORATORS)."""
    tail = dotted.rsplit(".", 1)[-1]
    if tail in REGISTERING_DECORATORS:
        return True
    return "." in dotted and tail in RECEIVER_REGISTERING_DECORATORS


def _registering_decorators(member: Member) -> list[str]:
    return [d for d in member.decorators if is_registering_decorator(d)]


def _own_members(cls: type) -> set[str]:
    """Methods and properties the class body defines (not typing internals)."""
    return {
        name
        for name, value in vars(cls).items()
        if not _is_dunder(name)
        and isinstance(value, (types.FunctionType, property, staticmethod, classmethod))
    }


def extension_surface() -> tuple[list[type], set[int]]:
    """Protocols plugins implement or receive, and the ids of every class an
    ``app.extension_sdk`` module defines or re-exports."""
    _ensure_backend_on_path()
    protocols: list[type] = []
    sdk_classes: set[int] = set()
    for path in _iter_py(APP_ROOT):
        module = _module_name(_rel(path))
        if not module.startswith(EXTENSION_MODULE_PREFIXES):
            continue
        try:
            mod = importlib.import_module(module)
        except Exception:  # pragma: no cover
            continue
        for value in vars(mod).values():
            if not isinstance(value, type) or not getattr(value, "__module__", "").startswith("app."):
                continue
            if module.startswith(SDK_PREFIX):
                sdk_classes.add(id(value))
            if value.__module__ == module and getattr(value, "_is_protocol", False):
                protocols.append(value)
    return protocols, sdk_classes


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


@dataclass
class Row:
    member: Member
    bucket: str  # "test-only" | "unreferenced" | "doubtful" | "keep" | "exempt"
    surface: bool = False
    reason: str = ""
    production_refs: int = 0
    test_refs: int = 0
    test_sites: list[str] = field(default_factory=list)


def facade_layer_classes() -> set[tuple[str, str]]:
    """``(module, qualname)`` of the facade layer, which the facade audit and
    the facade surface fixture govern: ``RepositoryFacade``, its two adapter
    subclasses, and every Protocol the facade is handed out as
    (``check_architecture_boundaries.facade_protocol_bindings``) together with
    the in-repo Protocols those extend."""
    _ensure_backend_on_path()
    scripts_dir = str(REPO_ROOT / "scripts")
    if scripts_dir not in sys.path:
        sys.path.insert(0, scripts_dir)
    from check_architecture_boundaries import facade_protocol_bindings

    layer: set[tuple[str, str]] = set()
    facade_module = importlib.import_module(_module_name(FACADE_REL))
    facade = getattr(facade_module, FACADE_CLASS)
    roots: list[type] = [facade]
    for rel, _function, protocol in facade_protocol_bindings(REPO_ROOT):
        module = importlib.import_module(_module_name(rel))
        bound = getattr(module, protocol, None)
        if isinstance(bound, type):
            roots.append(bound)
    for path in _iter_py(APP_ROOT):
        module_name = _module_name(_rel(path))
        if not module_name.startswith(("app.services.sqlite_repository", "app.services.postgres_repository")):
            continue
        module = importlib.import_module(module_name)
        for value in vars(module).values():
            if isinstance(value, type) and issubclass(value, facade) and value.__module__ == module_name:
                roots.append(value)
    for root in roots:
        for klass in root.__mro__:
            if klass is root or (
                klass.__module__.startswith("app.") and getattr(klass, "_is_protocol", False)
            ):
                layer.add((klass.__module__, klass.__qualname__))
    return layer


def load_allowlist(path: Path = ALLOWLIST_PATH) -> dict[str, dict[str, str]]:
    if not path.exists():
        return {}
    data = json.loads(path.read_text(encoding="utf-8"))
    return {entry["member"]: entry for entry in data["members"]}


@dataclass
class Verdict:
    bucket: str  # "keep" | "exempt" | "test-only" | "unreferenced" | "doubtful"
    reason: str = ""
    production_refs: int = 0
    test_refs: list[Ref] = field(default_factory=list)


def _refs_by_name(refs: Iterable[Ref], names: set[str]) -> dict[str, list[Ref]]:
    by_name: dict[str, list[Ref]] = {}
    for ref in refs:
        if ref.pattern is None:
            if ref.name in names:
                by_name.setdefault(ref.name, []).append(ref)
            continue
        for name in names:
            if ref.pattern.match(name):
                by_name.setdefault(name, []).append(ref)
    return by_name


def classify_members(
    members: list[Member],
    prod_refs: Iterable[Ref],
    test_refs: Iterable[Ref],
    *,
    exempt_reason,
    importable,
    kept: set[str],
) -> dict[str, Verdict]:
    """Pure classification (methodology points 3-5). ``exempt_reason(member)``
    gives why a member is not a candidate at all (or None), ``importable``
    whether its class can be checked, ``kept`` the registered keys whose
    bodies keep counting as production callers.

    Fixed point: a production reference inside a candidate's own body is not
    a production caller -- it only runs when a test runs that candidate -- so
    removing such references can make further members candidates, until
    nothing changes. A reference inside the member's own ``def`` never counts
    for it."""
    names = {m.name for m in members}
    prod_by_name = _refs_by_name(prod_refs, names)
    test_by_name = _refs_by_name(test_refs, names)

    def live(m: Member, dead: set[str]) -> list[Ref]:
        return [r for r in prod_by_name.get(m.name, []) if r.owner != m.key and r.owner not in dead]

    dead: set[str] = set()
    while True:
        new_dead = {
            m.key
            for m in members
            if not live(m, dead)
            and exempt_reason(m) is None
            and importable(m)  # doubtful: its body keeps counting
            and m.key not in kept
        }
        if new_dead == dead:
            break
        dead = new_dead

    verdicts: dict[str, Verdict] = {}
    for m in members:
        production = live(m, dead)
        tests = [r for r in test_by_name.get(m.name, []) if r.owner != m.key]
        if production:
            verdicts[m.key] = Verdict("keep", production_refs=len(production), test_refs=tests)
            continue
        reason = exempt_reason(m)
        if reason is not None:
            verdicts[m.key] = Verdict("exempt", reason, test_refs=tests)
        elif not importable(m):
            verdicts[m.key] = Verdict(
                "doubtful", "class cannot be imported to check its MRO", test_refs=tests
            )
        else:
            verdicts[m.key] = Verdict("test-only" if tests else "unreferenced", test_refs=tests)
    return verdicts


def _is_extension_surface(
    m: Member, cls: type | None, protocol_members, sdk_classes: set[int]
) -> bool:
    """Methodology point 6: a Protocol in the plugin-contract modules, a class
    an SDK module exports, or a class that structurally implements (defines
    every method of) a plugin-contract Protocol declaring this name."""
    if cls is None:
        return False
    if id(cls) in sdk_classes:
        return True
    if m.is_protocol and m.module.startswith(EXTENSION_MODULE_PREFIXES):
        return True
    available = _own_members(cls) | set(dir(cls))
    return any(
        m.name in proto_names and proto_names <= available
        for _proto, proto_names in protocol_members
    )


def build_report(allowlist: dict[str, dict[str, str]] | None = None) -> dict[str, object]:
    _ensure_backend_on_path()
    allowlist = load_allowlist() if allowlist is None else allowlist
    facade_layer = facade_layer_classes()
    members = [m for m in collect_members() if (m.module, m.qualname) not in facade_layer]
    member_index: dict[str, dict[tuple[str, str], str]] = {}
    for m in members:
        member_index.setdefault(m.rel, {})[(m.qualname, m.name)] = m.key
    prod_refs = collect_refs(PRODUCTION_ROOTS, False, member_index)
    test_refs = collect_refs(TEST_ROOTS + ("examples", "fangan"), True, member_index)

    exemptions: dict[str, str | None] = {}

    def exemption(m: Member) -> str | None:
        """Why a member with no production reference is still not a
        candidate (methodology point 5), or None."""
        if m.key not in exemptions:
            registering = _registering_decorators(m)
            reason: str | None = None
            if registering:
                reason = "registered by decorator " + ", ".join(registering)
            else:
                cls = _resolve_class(m)
                hook = _external_override(cls, m.name) if cls is not None else None
                if hook:
                    reason = f"overrides {hook}"
            exemptions[m.key] = reason
        return exemptions[m.key]

    kept = {k for k, e in allowlist.items() if e.get("category") in LIVE_CATEGORIES}
    verdicts = classify_members(
        members, prod_refs, test_refs,
        exempt_reason=exemption,
        importable=lambda m: _resolve_class(m) is not None,
        kept=kept,
    )

    protocols, sdk_classes = extension_surface()
    protocol_members = [(p, _own_members(p)) for p in protocols]

    rows: list[Row] = []
    for m in members:
        verdict = verdicts[m.key]
        row = Row(
            m, verdict.bucket, reason=verdict.reason,
            production_refs=verdict.production_refs, test_refs=len(verdict.test_refs),
        )
        if verdict.bucket in {"test-only", "unreferenced"}:
            row.surface = _is_extension_surface(
                m, _resolve_class(m), protocol_members, sdk_classes
            )
        row.test_sites = sorted({f"{r.rel}:{r.lineno}" for r in verdict.test_refs})[:5]
        rows.append(row)

    candidates = [r for r in rows if r.bucket in {"test-only", "unreferenced", "doubtful"}]
    summary = {
        "members": len(rows),
        "keep": sum(1 for r in rows if r.bucket == "keep"),
        "exempt": sum(1 for r in rows if r.bucket == "exempt"),
        "test_only": sum(1 for r in candidates if r.bucket == "test-only"),
        "unreferenced": sum(1 for r in candidates if r.bucket == "unreferenced"),
        "doubtful": sum(1 for r in candidates if r.bucket == "doubtful"),
        "extension_surface": sum(1 for r in candidates if r.surface),
    }
    return {
        "summary": summary,
        "candidates": [
            {
                "member": r.member.key,
                "line": r.member.lineno,
                "kind": "+".join(sorted(r.member.kinds)),
                "bucket": r.bucket,
                "extension_surface": r.surface,
                "reason": r.reason,
                "test_refs": r.test_refs,
                "test_sites": r.test_sites,
                "registered": allowlist.get(r.member.key, {}).get("category"),
            }
            for r in candidates
        ],
        "exempt": [
            {"member": r.member.key, "reason": r.reason} for r in rows if r.bucket == "exempt"
        ],
    }


OBSERVATION_MARKER = "测试观察点"


def carries_observation_marker(key: str) -> bool:
    """Whether the ``def`` behind ``key`` says it is a test observation point:
    a comment line directly above it (or its decorators), or its docstring."""
    rel, qual = key.split("::")
    class_qual, name = qual.rsplit(".", 1)
    path = REPO_ROOT / rel
    source = path.read_text(encoding="utf-8")
    lines = source.splitlines()
    tree = ast.parse(source, filename=rel)
    for node in ast.walk(tree):
        if not (isinstance(node, ast.ClassDef) and node.name == class_qual.rsplit(".", 1)[-1]):
            continue
        for child in node.body:
            if not (isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child.name == name):
                continue
            if OBSERVATION_MARKER in (ast.get_docstring(child) or ""):
                return True
            index = (child.decorator_list[0].lineno if child.decorator_list else child.lineno) - 2
            while index >= 0 and lines[index].strip().startswith("#"):
                if OBSERVATION_MARKER in lines[index]:
                    return True
                index -= 1
    return False


def check(report: dict[str, object], allowlist: dict[str, dict[str, str]]) -> list[str]:
    problems: list[str] = []
    candidates = {c["member"]: c for c in report["candidates"]}
    for key, entry in sorted(allowlist.items()):
        category = entry.get("category")
        if category not in CATEGORIES:
            problems.append(f"{key}: unknown category {category!r} (one of {', '.join(CATEGORIES)})")
        if not str(entry.get("reason", "")).strip():
            problems.append(f"{key}: registered without a reason")
        if key not in candidates:
            problems.append(
                f"{key}: registered but no longer a candidate (deleted, renamed or now called "
                "by production) -- drop it from scripts/component_test_only_allowlist.json"
            )
        elif category == "extension-port" and not candidates[key]["extension_surface"]:
            problems.append(f"{key}: registered as extension-port but is not extension surface")
        elif category == "test-observation" and not carries_observation_marker(key):
            problems.append(
                f"{key}: registered as test-observation but its def carries no "
                f"「{OBSERVATION_MARKER}」comment or docstring"
            )
    for key, cand in sorted(candidates.items()):
        if key in allowlist:
            continue
        problems.append(
            f"{key}: {cand['bucket']} component member"
            + (" (extension surface)" if cand["extension_surface"] else "")
            + (f" -- {cand['reason']}" if cand["reason"] else "")
            + ". Delete it (rewrite its tests through the production path), or register it in "
            "scripts/component_test_only_allowlist.json"
        )
    return problems


def render_markdown(report: dict[str, object]) -> str:
    lines = [
        "| 成员 | 行 | 种类 | 档 | 扩展面 | 测试引用 | 登记 |",
        "|---|---|---|---|---|---|---|",
    ]
    for c in sorted(report["candidates"], key=lambda c: c["member"]):
        lines.append(
            f"| `{c['member']}` | {c['line']} | {c['kind']} | {c['bucket']} | "
            f"{'是' if c['extension_surface'] else ''} | {c['test_refs']} | {c['registered'] or ''} |"
        )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--json", type=Path, default=None, help="write the full JSON report here")
    parser.add_argument("--markdown", type=Path, default=None, help="write the candidate table here")
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail unless the candidates equal scripts/component_test_only_allowlist.json",
    )
    args = parser.parse_args()
    allowlist = load_allowlist()
    report = build_report(allowlist)
    exit_code = 0
    if args.check:
        problems = check(report, allowlist)
        if problems:
            print("audit_component_callers: component members production never reaches:", file=sys.stderr)
            for problem in problems:
                print(f"  - {problem}", file=sys.stderr)
            exit_code = 1
        else:
            print(
                "audit_component_callers: OK ("
                f"{len(report['candidates'])} component members production never reaches, "
                "all registered)"
            )
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    if args.markdown:
        args.markdown.write_text(render_markdown(report), encoding="utf-8")
    if not args.check and not args.json and not args.markdown:
        print(json.dumps(report["summary"], indent=2, ensure_ascii=False))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
