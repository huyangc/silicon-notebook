#!/usr/bin/env python3
"""Census of class members that production never reaches.

``scripts/audit_facade_callers.py`` audits ``RepositoryFacade``'s public
surface by name. This census covers every class: the facade layer itself, the
services and both adapters' stores it hands work to, the persistence ports in
``repositories/ports.py``, the extension hosts. Same "production / tests"
split, same "report, never edit" contract, but each reference is read through
its receiver when the receiver's type can be pinned (methodology point 2).

Usage
-----
    PYTHONPATH=backend python3 scripts/audit_component_callers.py [--json OUT] [--markdown OUT]
    PYTHONPATH=backend python3 scripts/audit_component_callers.py --check
    PYTHONPATH=backend python3 scripts/audit_component_callers.py --by-name --json OUT

``--check`` is the contracts-lane gate (``scripts/check_contracts.sh``): it
exits non-zero when the set of candidates differs from
``scripts/component_test_only_allowlist.json`` in either direction -- a new
test-only or unreferenced member that is not registered, or a registered
entry that is no longer a candidate (deleted, or production now calls it).
``--by-name`` reads every reference by name, the way the census worked before
receivers (S2-D), for comparison runs only; ``--check`` never uses it.

Methodology (read before trusting a row)
----------------------------------------
1. **Members.** Every ``def`` / ``async def`` directly in a class body
   (methods, properties and their setters, static/class methods, Protocol
   members) of every class under ``backend/app`` outside ``backend/app/api``
   (the route layer: route handlers are module functions and its classes are
   request schemas), the facade layer included (``RepositoryFacade``, its two
   adapter subclasses, the Protocols it is handed out as).  Public and
   single-underscore ("semi-public") names are both members; dunders are not.

2. **A reference is read through its receiver.**  In a scanned file, name
   ``m`` is referenced by: an attribute ``<expr>.m`` (a call, a bound-method
   reference, a property read or write, ``Class.m``); a bare name ``m``
   inside a class body (``alias = method``, a decorator or a default naming an
   earlier def -- the one scope where a member is a bare name; a bare name in
   a function or at module level is never a member); a ``getattr`` /
   ``hasattr`` / ``setattr`` / ``delattr`` literal; a string constant equal to
   ``m`` or a dotted / colon path with ``m`` as one segment
   (``"pkg.mod:Class.m"`` entry points, dispatch tables, ``__all__``); and a
   name built at run time from literal pieces (``f"_migration_{n}"``,
   ``f"delete_{chain}_page"``, ``"prefix" + x``) passed to those builtins or
   to ``attrgetter`` / ``methodcaller``.  Docstrings are not references.

   When the type of ``<expr>`` can be pinned, the reference reaches only the
   members that access can resolve to: the definer of ``m`` in the type's
   MRO; the definer in every app subclass's MRO (the value may be any
   subclass); for a Protocol-typed value, the definer in every app class that
   implements the Protocol (defines or inherits all of its methods and
   properties); the Protocol declarations a reached member implements (a
   declaration of live behaviour is a contract, not dead code; registered
   members keep theirs the same way); and the twin of an adapter class
   (``repositories/sqlite/x.py::C`` and ``repositories/postgres/x.py::C``,
   ``SQLiteRepository`` / ``PostgresRepository``).  A pinned access that
   resolves to a data attribute reaches no member.  ``FileContext`` pins a
   type from: ``self`` / ``cls`` of a method; an annotated parameter, local
   or attribute (``Optional``, unions, ``X | None``, string annotations, a
   ``TYPE_CHECKING`` import); an ``__init__`` parameter, from the production
   calls spelled with the class's name (widening its annotation, so a
   partial fake handed in as a Protocol counts) -- unannotated, it is
   unknown when the class can be constructed any other way (its class
   object handed on as a value: an alias, ``functools.partial``, a registry,
   ``default_factory``; ``type(obj)(...)``; a registering class decorator);
   ``x = Class(...)``; a call whose function or method has a return
   annotation (also in a module the census does not load, read from its
   source); ``self.attr`` assignments in the class's methods and
   ``<typed receiver>.attr`` stores anywhere in production; class-body
   annotations and dataclass fields; module globals, widened by every
   ``<module>.name = value`` store (unknown when the module rebinds the name
   under ``global``).

   Everything else counts by name, for every member of that name, and is
   reported under ``summary.references.by_name_reasons``: a receiver whose
   type cannot be pinned (a loop variable, ``Any``, an unannotated parameter
   of a plain function, a value read from a container, a subscript); a
   receiver pinned to nothing but ``None``; ``super()``; a class with
   ``__getattr__``; a name the pinned type does not have; a string constant
   or a name template.  A store ``<receiver>.attr = value`` through a
   receiver whose type cannot be pinned may bind ``attr`` on any class or
   module, so reads of ``attr`` through any pinned receiver count by name
   too (the attribute's type is unknown).  ``kept_by_name_only`` lists
   the members whose every live production reference is one of these: the
   residual same-name blind spot.  ``METADATA_FILES`` (the generated
   ownership manifest and the audit/guard scripts) name members as data and
   are not scanned; ``STRING_DATA_FILES`` (the contract-fixture generator,
   which drives a real repository) are scanned with their strings ignored.

3. **Buckets.**  Production = ``backend/app``, ``scripts``, ``examples``
   (the in-repo sample plugins stand in for out-of-tree ones) and
   ``fangan``, minus any ``tests`` directory.  Tests = ``backend/tests`` and
   every ``tests`` directory under ``examples`` / ``fangan``; test references
   are read by name (they only tell ``test-only`` from ``unreferenced``).  A
   member with no production reference is ``test-only`` when some test
   references its name and ``unreferenced`` otherwise.  References inside
   the member's own ``def`` (recursion, a setter writing its own property)
   never count for it.

4. **Transitive.**  A production reference that sits inside the body of a
   member that is itself a candidate is not a production caller: it only
   runs when a test runs that member.  The census iterates to a fixed point,
   so a store method whose one production caller is a test-only service
   method is filed too.  Bodies of registered members (any category, see 7)
   keep counting: a kept member's own implementation is part of it, so
   registering a service method also keeps every store method and port
   declaration it forwards to.

5. **Not candidates at all.**  A member that overrides a method of a class
   from outside ``app`` (``HTMLParser.handle_data``, a pydantic settings
   hook): the framework calls it.  A member with a decorator that registers
   it (a pydantic validator / serializer / ``computed_field``, a route or CLI
   command decorator on a receiver, ``*.register``): the decorator is the
   caller.  This is an allowlist of registering decorators: any other one
   (``contextmanager``, ``lru_cache``, ``cache``, ``wraps`` ...) only wraps
   the function, and its member is censused like any other.  A member of
   ``RepositoryFacade`` or its adapter subclasses whose name is in
   ``facade_surface.json``: the pinned public surface, retired through the
   facade audit and its ledger.  Classes nested in a function cannot be
   imported to check their MRO; their candidates are filed ``doubtful``
   instead of guessed.

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
import builtins
import functools
import importlib
import inspect
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
FACADE_ADAPTER_MODULES = ("app.services.sqlite_repository", "app.services.postgres_repository")
FACADE_SURFACE_PATH = (
    BACKEND_ROOT / "tests" / "fixtures" / "repository_contract" / "facade_surface.json"
)
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
    }
)
# Files whose string constants are data about the code (member names in its
# tables) but whose code runs the code: the contract-fixture generator drives
# a real repository (``repo.begin_ask_job(...)``) to record its fixtures. Its
# attributes are references; its strings are not.
STRING_DATA_FILES = frozenset({"scripts/generate_repository_contract_fixtures.py"})
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
# Class decorators that return the class they are given without constructing
# it or handing it on (matched on the last dotted segment); any other one may
# register the class somewhere that constructs it.
HARMLESS_CLASS_DECORATORS = frozenset({"dataclass", "final", "total_ordering", "runtime_checkable"})
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


_PARSED: dict[Path, ast.Module | None] = {}


def _parse(path: Path) -> ast.Module | None:
    """One parse per file per run: members, receivers and references read
    the same tree (the receiver index keys its scopes by node identity)."""
    path = path.resolve()
    if path not in _PARSED:
        try:
            _PARSED[path] = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        except (SyntaxError, UnicodeDecodeError):
            _PARSED[path] = None
    return _PARSED[path]


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
        members_of_tree(rel, tree, by_key, order)
    return [by_key[k] for k in order]


def members_of_tree(
    rel: str, tree: ast.Module, by_key: dict[str, Member], order: list[str]
) -> None:
    """Methodology point 1 for one parsed file: add its class members."""
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
    # Member keys the reference reaches once its receiver is resolved
    # (methodology point 2), or None when it counts for every member of that
    # name; ``fallback`` then says why the receiver could not be pinned.
    targets: frozenset[str] | None = None
    fallback: str = ""


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


SCOPE_NODES = (
    ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda,
    ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp,
)
COMPREHENSIONS = (ast.ListComp, ast.SetComp, ast.DictComp, ast.GeneratorExp)


class _RefCollector(ast.NodeVisitor):
    """Collect the references of one file (methodology point 2).

    ``receivers`` selects the census's default reading: an attribute is
    resolved through ``context`` (when given) to the members it reaches, and
    a bare name references a member only inside a class body (the one scope
    where a method name is a bare name).  ``receivers=False`` is the by-name
    reading S2-D shipped with: every attribute and every bare name counts for
    every member of that name (``--by-name``)."""

    def __init__(
        self,
        rel: str,
        module_keys: dict[tuple[str, str], str],
        *,
        context: "FileContext | None" = None,
        receivers: bool = True,
        strings_are_data: bool = False,
    ) -> None:
        self.rel = rel
        self.strings_are_data = strings_are_data
        self.module_keys = module_keys  # (class qualname, name) -> Member.key
        self.context = context
        self.receivers = receivers
        self.class_stack: list[str] = []
        self.func_stack: list[str] = []
        self.owner_stack: list[str | None] = [None]
        self.scope_stack: list[ast.AST] = []
        self.refs: list[Ref] = []
        self.docstrings: set[int] = set()
        self.handled_constants: set[int] = set()

    # -- scope tracking -------------------------------------------------
    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._mark_docstring(node)
        for item in node.bases + node.keywords + node.decorator_list:
            self.visit(item)
        prefix = self._qual_prefix()
        self.class_stack.append(f"{prefix}{node.name}")
        saved_funcs = self.func_stack
        self.func_stack = []
        self.scope_stack.append(node)
        for stmt in node.body:
            self.visit(stmt)
        self.scope_stack.pop()
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
        self.scope_stack.append(node)
        for stmt in node.body:
            self.visit(stmt)
        self.scope_stack.pop()
        self.func_stack = self.func_stack[:-1] if not directly_in_class else []
        self.owner_stack.pop()

    visit_FunctionDef = _visit_function
    visit_AsyncFunctionDef = _visit_function

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.visit(node.args)
        self.scope_stack.append(node)
        self.visit(node.body)
        self.scope_stack.pop()

    def _visit_comprehension(self, node: ast.AST) -> None:
        self.scope_stack.append(node)
        self.generic_visit(node)
        self.scope_stack.pop()

    visit_ListComp = _visit_comprehension
    visit_SetComp = _visit_comprehension
    visit_DictComp = _visit_comprehension
    visit_GeneratorExp = _visit_comprehension

    def _mark_docstring(self, node: ast.AST) -> None:
        body = getattr(node, "body", None)
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            self.docstrings.add(id(body[0].value))

    def visit_Module(self, node: ast.Module) -> None:
        self._mark_docstring(node)
        self.scope_stack.append(node)
        self.generic_visit(node)
        self.scope_stack.pop()

    # -- references -----------------------------------------------------
    def _add(
        self,
        name: str,
        lineno: int,
        pattern: re.Pattern[str] | None = None,
        *,
        targets: frozenset[str] | None = None,
        fallback: str = "",
    ) -> None:
        self.refs.append(
            Ref(name, pattern, self.rel, lineno, self.owner_stack[-1], targets, fallback)
        )

    def _scope(self) -> ast.AST | None:
        return self.scope_stack[-1] if self.scope_stack else None

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if self.context is not None and self.receivers:
            targets, fallback = self.context.attribute_targets(node, self._scope())
            self._add(node.attr, node.lineno, targets=targets, fallback=fallback)
        else:
            self._add(node.attr, node.lineno, fallback="no receiver resolution")
        self.generic_visit(node)

    def visit_Name(self, node: ast.Name) -> None:
        if not self.receivers:
            self._add(node.id, node.lineno, fallback="bare name")
            return
        # A method body cannot see its class's names; only a class body can
        # (``alias = method``, a decorator or default naming an earlier def).
        scope = self._scope()
        if isinstance(scope, ast.ClassDef) and self.class_stack:
            key = self.module_keys.get((self.class_stack[-1], node.id))
            self._add(node.id, node.lineno, targets=frozenset({key}) if key else frozenset())

    def visit_Constant(self, node: ast.Constant) -> None:
        if (
            not isinstance(node.value, str)
            or self.strings_are_data
            or id(node) in self.docstrings
            or id(node) in self.handled_constants
        ):
            return
        value = node.value
        if value.isidentifier():
            self._add(value, node.lineno, fallback="string constant")
        elif DOTTED_PATH_RE.match(value):
            for segment in re.split(r"[.:]", value):
                self._add(segment, node.lineno, fallback="string constant")

    def visit_Call(self, node: ast.Call) -> None:
        func = _dotted(node.func)
        tail = func.rsplit(".", 1)[-1]
        name_args: list[ast.AST] = []
        if func in NAME_BUILDER_CALLS and len(node.args) >= 2:
            name_args = [node.args[1]]
            literal = node.args[1]
            if (
                self.context is not None
                and self.receivers
                and isinstance(literal, ast.Constant)
                and isinstance(literal.value, str)
                and literal.value in self.context.index.member_names
            ):
                # ``getattr(receiver, "name")``: resolve the receiver like an
                # attribute; the literal is then not also a by-name string.
                types = self.context.expr_types(node.args[0], self._scope())
                targets, fallback = self.context.index.reached(types, literal.value)
                self._add(literal.value, node.lineno, targets=targets, fallback=fallback)
                self.handled_constants.add(id(literal))
                name_args = []
        elif tail in NAME_BUILDER_ATTR_CALLS:
            name_args = list(node.args)
        for arg in name_args if not self.strings_are_data else ():
            pattern = _template_regex(arg)
            if pattern is not None:
                self._add("", node.lineno, pattern, fallback="name template")
        self.generic_visit(node)


def collect_refs(
    roots: Iterable[str],
    tests: bool,
    member_index: dict[str, dict[tuple[str, str], str]],
    *,
    receivers: "ReceiverIndex | None" = None,
    by_name: bool = False,
) -> list[Ref]:
    """References of every scanned file under ``roots`` in one bucket.
    Production references resolve their receivers through ``receivers``;
    test references stay name-based (they only tell ``test-only`` from
    ``unreferenced``), but follow the same bare-name rule."""
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
            context = None
            if receivers is not None and not by_name:
                context = receivers.context(rel, tree)
                if context is not None:
                    tree = context.tree  # scopes and memos are keyed by its nodes
            collector = _RefCollector(
                rel, member_index.get(rel, {}), context=context, receivers=not by_name,
                strings_are_data=rel in STRING_DATA_FILES,
            )
            collector.visit(tree)
            refs.extend(collector.refs)
    return refs


# --------------------------------------------------------------------------
# Receivers (methodology point 2)
# --------------------------------------------------------------------------

_MISSING = object()


@dataclass(frozen=True)
class TypeRef:
    """What an expression evaluates to, as far as the census can tell: an
    instance of a class (``inst``), the class object itself (``cls``), a
    module (``mod``) or a function (``func``)."""

    kind: str
    obj: object


# An expression's possible values: a frozenset of TypeRef, or None when the
# census cannot tell (its references then count by name).
Types = "frozenset[TypeRef] | None"


def _is_app_object(obj: object) -> bool:
    module = getattr(obj, "__module__", None) or ""
    return module == "app" or module.startswith("app.")


def _is_protocol_class(cls: type) -> bool:
    return bool(getattr(cls, "_is_protocol", False)) and cls is not typing.Protocol


def _value_types(value: object) -> "Types":
    """Types of a run-time value the census holds (a module global, an
    imported name, a class attribute)."""
    if value is None:
        return frozenset()
    if isinstance(value, type):
        return frozenset({TypeRef("cls", value)})
    if isinstance(value, types.ModuleType):
        return frozenset({TypeRef("mod", value)})
    if isinstance(value, (types.FunctionType, types.BuiltinFunctionType, types.MethodType)):
        return frozenset({TypeRef("func", value)})
    if typing.get_origin(value) is not None or value is typing.Any:
        return None  # a typing construct, not a value
    return frozenset({TypeRef("inst", type(value))})


def _union(parts: Iterable["Types"]) -> "Types":
    out: set[TypeRef] = set()
    for part in parts:
        if part is None:
            return None
        out |= part
    return frozenset(out)


def _instances_of(annotation_obj: object) -> "Types":
    """Types of a value annotated with ``annotation_obj`` (a run-time
    annotation object: a class, ``Optional[X]``, ``X | None`` ...)."""
    if annotation_obj is None or annotation_obj is type(None):
        return frozenset()
    if annotation_obj is typing.Any or annotation_obj is object:
        return None
    origin = typing.get_origin(annotation_obj)
    if origin is typing.Union or origin is types.UnionType:
        return _union(_instances_of(arg) for arg in typing.get_args(annotation_obj))
    if origin in (typing.ClassVar, typing.Final, typing.Annotated):
        args = typing.get_args(annotation_obj)
        return _instances_of(args[0]) if args else None
    if origin is type:
        inner = typing.get_args(annotation_obj)
        if len(inner) == 1 and isinstance(inner[0], type):
            return frozenset({TypeRef("cls", inner[0])})
        return None
    if isinstance(origin, type):
        return frozenset({TypeRef("inst", origin)})
    if isinstance(annotation_obj, type):
        return frozenset({TypeRef("inst", annotation_obj)})
    return None


class FileContext:
    """Name bindings of one source file and the expression typing over them.

    Bindings are collected per scope (module, class, function, lambda,
    comprehension) the way Python binds them: an assignment to a plain name,
    an annotated name, a parameter (``self`` / ``cls`` of a method, or an
    annotated parameter), an import.  Any other binding of a name (a ``for``
    target, ``with ... as``, tuple unpacking, ``+=``, ``nonlocal`` ...) makes
    the name unknown in that scope.  A name bound several ways has the union
    of their types, and is unknown as soon as one of them is."""

    def __init__(self, index: "ReceiverIndex", rel: str, tree: ast.Module) -> None:
        self.index = index
        self.rel = rel
        self.tree = tree
        self.module_name = _module_name(rel) if rel.startswith("backend/app/") else None
        self.is_package = rel.endswith("/__init__.py")
        # Never imported here: a module the member census did not load (an
        # entry point such as ``app.main``) has import-time side effects, and
        # its names stay unresolved instead.
        self.module: types.ModuleType | None = (
            sys.modules.get(self.module_name) if self.module_name else None
        )
        self.parents: dict[int, ast.AST | None] = {}
        self.bindings: dict[int, dict[str, list[tuple]]] = {}
        self.global_names: dict[int, set[str]] = {}
        self.module_globals: set[str] = set()
        self.stmt_scope: dict[int, ast.AST] = {}
        # class node id -> attr -> [(scope, binding)] for ``self.attr`` stores
        self.self_stores: dict[int, dict[str, list[tuple]]] = {}
        self.classes: dict[str, ast.ClassDef] = {}
        self.class_quals: dict[int, str] = {}
        self._handled: set[int] = set()
        # Loads of a class name in a position that does not hand the class
        # object on: a call's function, an attribute's value, an annotation,
        # a base, an ``isinstance`` / ``issubclass`` / ``except`` type.
        self.passive: set[int] = set()
        self._expr_memo: dict[int, "Types"] = {}
        self._lookup_memo: dict[tuple[int, str], "Types"] = {}
        self._busy: set[object] = set()
        self._walk_scope(tree, None, "")

    # -- binding collection ---------------------------------------------
    def _bind(self, scope: ast.AST, name: str, binding: tuple) -> None:
        self.bindings[id(scope)].setdefault(name, []).append(binding)

    def _walk_scope(self, scope: ast.AST, parent: ast.AST | None, qual: str) -> None:
        self.parents[id(scope)] = parent
        self.bindings[id(scope)] = {}
        self.global_names[id(scope)] = set()
        if isinstance(scope, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            self._bind_params(scope, parent)
        if isinstance(scope, ast.Lambda):
            self._walk(scope.body, scope, qual)
        elif isinstance(scope, COMPREHENSIONS):
            for child in ast.iter_child_nodes(scope):
                self._walk(child, scope, qual)
        else:
            for stmt in scope.body:
                self._walk(stmt, scope, qual)

    def _bind_params(self, func: ast.AST, parent: ast.AST | None) -> None:
        args = func.args
        positional = list(args.posonlyargs) + list(args.args)
        method_of = (
            parent
            if isinstance(parent, ast.ClassDef) and not isinstance(func, ast.Lambda)
            else None
        )
        decorators = [] if isinstance(func, ast.Lambda) else [_dotted(d) for d in func.decorator_list]
        constructor = method_of is not None and getattr(func, "name", "") == "__init__"
        for arg in positional + list(args.kwonlyargs):
            if positional and arg is positional[0] and method_of is not None and "staticmethod" not in decorators:
                kind = "clsself" if "classmethod" in decorators else "self"
                self._bind(func, arg.arg, (kind, method_of))
            elif constructor:
                # Injected at construction: typed from every call site (and
                # its annotation, when it has one).
                self._bind(func, arg.arg, ("ctorparam", method_of, func, arg.arg, arg.annotation))
            elif arg.annotation is not None:
                self._bind(func, arg.arg, ("ann", arg.annotation))
            else:
                self._bind(func, arg.arg, ("unknown",))
        for arg in (args.vararg, args.kwarg):
            if arg is not None:
                self._bind(func, arg.arg, ("unknown",))

    def _import_module_name(self, node: ast.ImportFrom) -> str | None:
        if not node.level:
            return node.module
        if self.module_name is None:
            return None
        base = self.module_name if self.is_package else self.module_name.rpartition(".")[0]
        for _ in range(node.level - 1):
            base = base.rpartition(".")[0]
        return f"{base}.{node.module}" if node.module else base

    def _walk(self, node: ast.AST, scope: ast.AST, qual: str) -> None:
        kind = type(node)
        # Fast paths for the node types that dominate every file.
        if kind is ast.Name:
            if type(node.ctx) is not ast.Load:
                if id(node) not in self._handled:
                    self._bind(scope, node.id, ("unknown",))
            elif node.id in self.index.class_names:
                self.index.class_loads.setdefault(node.id, []).append((self, scope, node))
            return
        if kind is ast.Constant or kind is ast.Load or kind is ast.Store:
            return
        if kind is ast.Attribute:
            if type(node.ctx) is not ast.Load:
                if id(node) not in self._handled:
                    owner = self._self_class(scope, node.value)
                    if owner is not None:
                        self._self_store(owner, node.attr, scope, ("unknown",))
            elif node.attr in self.index.class_names:
                self.index.class_loads.setdefault(node.attr, []).append((self, scope, node))
            self.passive.add(id(node.value))
            self._walk(node.value, scope, qual)
            return
        if kind is ast.Call:
            callee = _dotted(node.func).rsplit(".", 1)[-1]
            if callee:
                self.index.calls.setdefault(callee, []).append((self, scope, node))
            self.passive.add(id(node.func))
            if callee in ("isinstance", "issubclass") and len(node.args) >= 2:
                self._mark_passive(node.args[1])
            if callee in ("setattr", "__setattr__") and len(node.args) >= 2:
                owner = self._self_class(scope, node.args[0])
                literal = node.args[1]
                if owner is not None and isinstance(literal, ast.Constant) and isinstance(literal.value, str):
                    self._self_store(owner, literal.value, scope, ("unknown",))
            for child in ast.iter_child_nodes(node):
                self._walk(child, scope, qual)
            return
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            self._bind(scope, node.name, ("def", node))
            for decorator in node.decorator_list:
                self._walk(decorator, scope, qual)
            if isinstance(node, ast.ClassDef):
                for base in node.bases:
                    self._mark_passive(base)
                for base in node.bases + node.keywords:
                    self._walk(base, scope, qual)
                child_qual = ""
                if isinstance(scope, (ast.Module, ast.ClassDef)) and (qual or isinstance(scope, ast.Module)):
                    child_qual = f"{qual}.{node.name}" if qual else node.name
                if child_qual:
                    self.classes[child_qual] = node
                    self.class_quals[id(node)] = child_qual
                self._walk_scope(node, scope, child_qual)
            else:
                for default in node.args.defaults + [d for d in node.args.kw_defaults if d is not None]:
                    self._walk(default, scope, qual)
                self._walk_scope(node, scope, "")
            return
        if isinstance(node, ast.Lambda):
            for default in node.args.defaults + [d for d in node.args.kw_defaults if d is not None]:
                self._walk(default, scope, qual)
            self._walk_scope(node, scope, "")
            return
        if isinstance(node, COMPREHENSIONS):
            self._walk_scope(node, scope, "")
            return
        if isinstance(node, ast.Assign):
            self.stmt_scope[id(node)] = scope
            for target in node.targets:
                if isinstance(target, ast.Name):
                    self._bind(scope, target.id, ("expr", node.value))
                    self._handled.add(id(target))
            self._record_external_stores(node, node.targets, scope, ("expr", node.value))
        elif isinstance(node, ast.AnnAssign):
            self._mark_passive(node.annotation)
            self.stmt_scope[id(node)] = scope
            if isinstance(node.target, ast.Name):
                self._bind(scope, node.target.id, ("ann", node.annotation))
                self._handled.add(id(node.target))
            self._record_external_stores(node, [node.target], scope, ("ann", node.annotation))
        elif isinstance(node, ast.NamedExpr):
            self._bind(scope, node.target.id, ("expr", node.value))
            self._handled.add(id(node.target))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.asname:
                    self._bind(scope, alias.asname, ("import", (alias.name, None)))
                else:
                    head = alias.name.split(".", 1)[0]
                    self._bind(scope, head, ("import", (head, None)))
        elif isinstance(node, ast.ImportFrom):
            module = self._import_module_name(node)
            for alias in node.names:
                if alias.name == "*":
                    continue
                binding = ("import", (module, alias.name)) if module else ("unknown",)
                self._bind(scope, alias.asname or alias.name, binding)
        elif isinstance(node, ast.Global):
            self.global_names[id(scope)].update(node.names)
            self.module_globals.update(node.names)
        elif isinstance(node, ast.Nonlocal):
            for name in node.names:
                self._bind(scope, name, ("unknown",))
                outer = self.parents.get(id(scope))
                while outer is not None and not isinstance(outer, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                    outer = self.parents.get(id(outer))
                if outer is not None:
                    self._bind(outer, name, ("unknown",))
        elif isinstance(node, ast.ExceptHandler):
            if node.type is not None:
                self._mark_passive(node.type)
            if node.name:
                self._bind(scope, node.name, ("unknown",))
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            self._bind(scope, node.name, ("unknown",))
        elif isinstance(node, ast.MatchMapping) and node.rest:
            self._bind(scope, node.rest, ("unknown",))
        for child in ast.iter_child_nodes(node):
            self._walk(child, scope, qual)

    def _mark_passive(self, node: ast.AST) -> None:
        for sub in ast.walk(node):
            self.passive.add(id(sub))

    def _record_external_stores(
        self, stmt: ast.AST, targets: list[ast.AST], scope: ast.AST, binding: tuple
    ) -> None:
        """``self.attr = value`` in a method goes to its class's attribute
        table.  ``<receiver>.attr = value`` anywhere else: the index types
        the receiver on demand and, when it is the class asked about (or a
        relative), counts the value as one of the attribute's bindings."""
        for target in targets:
            if not isinstance(target, ast.Attribute):
                continue
            owner = self._self_class(scope, target.value)
            if owner is not None:
                self._self_store(owner, target.attr, scope, binding)
                self._handled.add(id(target))
            else:
                self.index.external_stores.setdefault(target.attr, []).append(
                    (self, scope, target.value, binding)
                )

    def _self_class(self, scope: ast.AST, receiver: ast.AST) -> ast.ClassDef | None:
        """The class whose method's ``self`` / ``cls`` ``receiver`` is, or None."""
        if not isinstance(receiver, ast.Name):
            return None
        node: ast.AST | None = scope
        first = True
        while node is not None and not isinstance(node, ast.Module):
            if not (isinstance(node, ast.ClassDef) and not first):
                bindings = self.bindings.get(id(node), {}).get(receiver.id)
                if bindings is not None:
                    if len(bindings) == 1 and bindings[0][0] in ("self", "clsself"):
                        return bindings[0][1]
                    return None
            node = self.parents.get(id(node))
            first = False
        return None

    def _self_store(self, owner: ast.ClassDef, attr: str, scope: ast.AST, binding: tuple) -> None:
        self.self_stores.setdefault(id(owner), {}).setdefault(attr, []).append((scope, binding))

    # -- lookups ----------------------------------------------------------
    def _class_object(self, class_node: ast.ClassDef) -> type | None:
        qual = self.class_quals.get(id(class_node))
        if not qual or self.module is None:
            return None
        obj: object = self.module
        for part in qual.split("."):
            obj = getattr(obj, part, None)
            if obj is None:
                return None
        return obj if isinstance(obj, type) else None

    def module_object(self, name: str) -> object:
        """The run-time object a module-level name stands for, or _MISSING."""
        if self.module is not None and name in vars(self.module):
            return vars(self.module)[name]
        bindings = self.bindings[id(self.tree)].get(name)
        if bindings and len(bindings) == 1 and bindings[0][0] == "import":
            return self.index.resolve_import(*bindings[0][1])
        builtin = getattr(builtins, name, _MISSING)
        return builtin

    def _module_lookup(self, name: str) -> "Types":
        if name in self.module_globals:
            return None  # rebound from inside a function
        if self.module is not None and name in vars(self.module):
            return _value_types(vars(self.module)[name])
        bindings = self.bindings[id(self.tree)].get(name)
        if bindings:
            return self._combine(self.tree, name, bindings)
        builtin = getattr(builtins, name, _MISSING)
        return None if builtin is _MISSING else _value_types(builtin)

    def lookup(self, scope: ast.AST | None, name: str) -> "Types":
        node = scope
        first = True
        while node is not None and not isinstance(node, ast.Module):
            if isinstance(node, ast.ClassDef) and not first:
                node = self.parents.get(id(node))
                continue
            if name in self.global_names.get(id(node), ()):
                break
            bindings = self.bindings.get(id(node), {}).get(name)
            if bindings is not None:
                return self._combine(node, name, bindings)
            node = self.parents.get(id(node))
            first = False
        return self._module_lookup(name)

    def _combine(self, scope: ast.AST, name: str, bindings: list[tuple]) -> "Types":
        key = (id(scope), name)
        if key in self._lookup_memo:
            return self._lookup_memo[key]
        if key in self._busy:
            return None
        self._busy.add(key)
        try:
            result = _union(self.binding_types(scope, b) for b in bindings)
        finally:
            self._busy.discard(key)
        self._lookup_memo[key] = result
        return result

    def binding_types(self, scope: ast.AST, binding: tuple) -> "Types":
        kind = binding[0]
        if kind == "expr":
            return self.expr_types(binding[1], scope)
        if kind == "ann":
            return self.annotation_types(binding[1])
        if kind in ("self", "clsself"):
            cls = self._class_object(binding[1])
            if cls is None:
                return None
            return frozenset({TypeRef("inst" if kind == "self" else "cls", cls)})
        if kind == "import":
            return self.index.import_types(*binding[1])
        if kind == "def" and isinstance(scope, ast.Module):
            if self.module is not None:
                value = vars(self.module).get(binding[1].name, _MISSING)
                return None if value is _MISSING else _value_types(value)
            if isinstance(binding[1], (ast.FunctionDef, ast.AsyncFunctionDef)):
                # A module the census did not load: its functions are typed
                # from their source (``api/deps.py::repository() -> ...``).
                return frozenset({TypeRef("sfunc", (self, binding[1]))})
            return None
        if kind == "ctorparam":
            _kind, class_node, func, param, annotation = binding
            sites = self.index.constructor_argument_types(self, class_node, func, param)
            if annotation is None:
                return None if sites is None else _union(sites)
            # Annotated: the annotation, widened by every argument whose type
            # a construction site shows (a partial fake passed as a Protocol).
            declared = self.annotation_types(annotation)
            if declared is None:
                return None
            return _union([declared] + [s for s in (sites or []) if s is not None])
        return None

    # -- annotations ------------------------------------------------------
    def _annotation_object(self, node: ast.AST) -> object:
        if isinstance(node, ast.Name):
            return self.module_object(node.id)
        if isinstance(node, ast.Attribute):
            base = self._annotation_object(node.value)
            return _MISSING if base is _MISSING else getattr(base, node.attr, _MISSING)
        return _MISSING

    def annotation_types(self, node: ast.AST) -> "Types":
        if isinstance(node, ast.Constant):
            if node.value is None:
                return frozenset()
            if isinstance(node.value, str):
                parsed = _parse_annotation(node.value)
                return None if parsed is None else self.annotation_types(parsed)
            return None
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.BitOr):
            return _union([self.annotation_types(node.left), self.annotation_types(node.right)])
        if isinstance(node, ast.Subscript):
            base = self._annotation_object(node.value)
            elements = node.slice.elts if isinstance(node.slice, ast.Tuple) else [node.slice]
            if base is typing.Optional:
                return self.annotation_types(elements[0])
            if base is typing.Union:
                return _union(self.annotation_types(e) for e in elements)
            if base in (typing.ClassVar, typing.Final, typing.Annotated):
                return self.annotation_types(elements[0])
            if base in (type, typing.Type):
                inner = self.annotation_types(elements[0])
                if inner is None:
                    return None
                return frozenset(TypeRef("cls", t.obj) for t in inner if t.kind == "inst")
            if base is _MISSING:
                return None
            return _instances_of(base) if not isinstance(base, type) else frozenset({TypeRef("inst", base)})
        if isinstance(node, (ast.Name, ast.Attribute)):
            obj = self._annotation_object(node)
            return None if obj is _MISSING else _instances_of(obj)
        return None

    # -- expressions ------------------------------------------------------
    def expr_types(self, node: ast.AST, scope: ast.AST | None) -> "Types":
        key = id(node)
        if key in self._expr_memo:
            return self._expr_memo[key]
        if key in self._busy:
            return None
        self._busy.add(key)
        try:
            result = self._expr_types(node, scope)
        finally:
            self._busy.discard(key)
        self._expr_memo[key] = result
        return result

    def _expr_types(self, node: ast.AST, scope: ast.AST | None) -> "Types":
        if isinstance(node, ast.Name):
            return self.lookup(scope, node.id)
        if isinstance(node, ast.Attribute):
            if _is_super_call(node.value):
                return None
            base = self.expr_types(node.value, scope)
            if base is None:
                return None
            return _union(self.index.attribute_types(t, node.attr) for t in base)
        if isinstance(node, ast.Call):
            func = node.func
            if _dotted(func).rsplit(".", 1)[-1] == "cast" and node.args:
                return self.annotation_types(node.args[0])
            called = self.expr_types(func, scope)
            if called is None:
                return None
            parts: list[Types] = []
            for t in called:
                if t.kind == "cls":
                    parts.append(frozenset({TypeRef("inst", t.obj)}))
                elif t.kind == "func":
                    parts.append(self.index.return_types(t.obj))
                elif t.kind == "sfunc":
                    owner, fn = t.obj
                    parts.append(owner.annotation_types(fn.returns) if fn.returns else None)
                else:
                    return None
            return _union(parts)
        if isinstance(node, ast.Await):
            return self.expr_types(node.value, scope)
        if isinstance(node, ast.NamedExpr):
            return self.expr_types(node.value, scope)
        if isinstance(node, ast.IfExp):
            return _union([self.expr_types(node.body, scope), self.expr_types(node.orelse, scope)])
        if isinstance(node, ast.BoolOp):
            return _union(self.expr_types(value, scope) for value in node.values)
        if isinstance(node, ast.Constant):
            return frozenset() if node.value is None else frozenset({TypeRef("inst", type(node.value))})
        literal = {
            ast.JoinedStr: str, ast.List: list, ast.ListComp: list, ast.Tuple: tuple,
            ast.Dict: dict, ast.DictComp: dict, ast.Set: set, ast.SetComp: set,
        }.get(type(node))
        if literal is not None:
            return frozenset({TypeRef("inst", literal)})
        return None

    def attribute_targets(
        self, node: ast.Attribute, scope: ast.AST | None
    ) -> tuple[frozenset[str] | None, str]:
        if node.attr not in self.index.member_names:
            return None, "not a member name"
        if _is_super_call(node.value):
            return None, "super()"
        return self.index.reached(self.expr_types(node.value, scope), node.attr)


def _is_super_call(node: ast.AST) -> bool:
    return isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "super"


class ReceiverIndex:
    """Class-level facts the receiver resolution needs: which member keys a
    (class, name) pair owns, every app class, subclass and structural
    Protocol implementors, attribute types per class, and the twin of each
    adapter class (methodology point 2)."""

    def __init__(self, members: list[Member]) -> None:
        _ensure_backend_on_path()
        self.member_names = {m.name for m in members}
        self.keys: dict[tuple[str, str], dict[str, str]] = {}
        self.all_keys: set[str] = set()
        classes: dict[int, type] = {}
        self.key_class: dict[str, type] = {}
        for m in members:
            self.keys.setdefault((m.module, m.qualname), {})[m.name] = m.key
            self.all_keys.add(m.key)
            cls = _resolve_class(m)
            if cls is not None:
                classes[id(cls)] = cls
                self.key_class[m.key] = cls
        for module_name, module in list(sys.modules.items()):
            if module is None or not (module_name == "app" or module_name.startswith("app.")):
                continue
            for value in list(vars(module).values()):
                if isinstance(value, type) and value.__module__ == module_name:
                    classes[id(value)] = value
        self.classes = list(classes.values())
        self.class_names = {cls.__name__ for cls in self.classes}
        # Every production load of an app class's name, by that name, for
        # telling whether a class is constructed only where it is spelled.
        self.class_loads: dict[str, list[tuple]] = {}
        self._escapes: dict[int, bool] = {}
        self.contexts: dict[str, FileContext] = {}
        self.external_stores: dict[str, list[tuple]] = {}
        self._subclasses: dict[int, list[type]] = {}
        self._implementors: dict[int, list[type]] = {}
        self._dirs: dict[int, frozenset[str]] = {}
        self._attr_tables: dict[int, dict[str, list[tuple]]] = {}
        self._attr_memo: dict[tuple[int, str], "Types"] = {}
        self._busy: set[object] = set()
        self._imports: dict[tuple[str, str | None], object] = {}
        # Every production call by its callee's last name segment, for the
        # construction-site typing of ``__init__`` parameters.
        self.calls: dict[str, list[tuple]] = {}
        self._ctor_memo: dict[tuple[str, int, str], "list[Types] | None"] = {}
        self._declarations: dict[str, frozenset[str]] = {}
        self._protocols_by_name: dict[str, list[type]] | None = None

    # -- contexts ---------------------------------------------------------
    def prepare(self, roots: Iterable[str]) -> None:
        """Index every production file before any receiver is typed, so a
        store to ``obj.attr`` in one file is known when another file reads
        ``obj.attr``."""
        for root in roots:
            for path in _iter_py(REPO_ROOT / root):
                rel = _rel(path)
                if _is_test_path(rel) or rel in METADATA_FILES:
                    continue
                self.context(rel)

    def context(self, rel: str, tree: ast.Module | None = None) -> FileContext | None:
        if rel not in self.contexts:
            if tree is None:
                tree = _parse(REPO_ROOT / rel)
            if tree is None:
                return None
            self.contexts[rel] = FileContext(self, rel, tree)
        return self.contexts[rel]

    def context_for_module(self, module_name: str | None) -> FileContext | None:
        if not module_name or not (module_name == "app" or module_name.startswith("app.")):
            return None
        module = sys.modules.get(module_name)
        path = getattr(module, "__file__", None)
        if not path:
            return None
        try:
            rel = _rel(Path(path))
        except ValueError:
            return None
        return self.context(rel)

    def resolve_import(self, module_name: str | None, attr: str | None) -> object:
        if not module_name:
            return _MISSING
        key = (module_name, attr)
        if key in self._imports:
            return self._imports[key]
        # Only modules already loaded (by the member census or by them): this
        # never imports anything, see FileContext.
        value: object = _MISSING
        module = sys.modules.get(module_name)
        if module is not None:
            if attr is None:
                value = module
            else:
                value = getattr(module, attr, _MISSING)
                if value is _MISSING:
                    value = sys.modules.get(f"{module_name}.{attr}", _MISSING)
        self._imports[key] = value
        return value

    def import_types(self, module_name: str | None, attr: str | None) -> "Types":
        """Types of an imported name: the loaded object when there is one,
        else (an app module the census did not load) the name as that
        module's source binds it."""
        value = self.resolve_import(module_name, attr)
        if value is not _MISSING:
            return _value_types(value)
        if not module_name or attr is None or not module_name.startswith("app."):
            return None
        relative = Path("backend", *module_name.split("."))
        for candidate in (relative.with_suffix(".py"), relative / "__init__.py"):
            if (REPO_ROOT / candidate).is_file():
                ctx = self.context(candidate.as_posix())
                return ctx._module_lookup(attr) if ctx is not None else None
        return None

    def constructor_argument_types(
        self, ctx: FileContext, class_node: ast.ClassDef, func: ast.AST, param: str
    ) -> "list[Types] | None":
        """Per construction site, the type of what it passes for ``__init__``
        parameter ``param`` (its argument, else the default; None when a site
        omits a required one).  The sites are the production calls spelled
        with the name of the class or of a subclass inheriting its
        ``__init__`` (``Class(...)``, ``mod.Class(...)``, ``cls(...)``), so
        the answer is None -- the parameter is unknown -- whenever the class
        can be constructed any other way: its class object is handed on as a
        value (an alias, ``functools.partial(Class, ...)``, a registry entry,
        ``default_factory=Class``), it is constructed as ``type(obj)(...)`` /
        ``obj.__class__(...)`` from an instance, or a class decorator (other
        than ``dataclass`` / ``final`` / ``total_ordering``) receives it.
        Also None when a subclass defines its own ``__init__`` (it may pass
        anything to ``super().__init__``), when a call passes ``*args`` /
        ``**kwargs``, or when no construction site is found."""
        cls = ctx._class_object(class_node)
        if cls is None:
            return None
        key = ("ctor", id(cls), param)
        if key in self._ctor_memo:
            return self._ctor_memo[key]
        if key in self._busy:
            return None
        self._busy.add(key)
        try:
            result = self._constructor_argument_types(ctx, cls, func, param)
        finally:
            self._busy.discard(key)
        self._ctor_memo[key] = result
        return result

    def _constructor_argument_types(
        self, ctx: FileContext, cls: type, func: ast.AST, param: str
    ) -> "list[Types] | None":
        subclasses = self.subclasses(cls)
        if any("__init__" in vars(sub) for sub in subclasses):
            return None
        family = {id(cls)} | {id(sub) for sub in subclasses}
        if any(self.constructed_unspelled(klass) for klass in [cls] + subclasses):
            return None
        args = func.args
        positional = [a.arg for a in list(args.posonlyargs) + list(args.args)][1:]
        defaults: dict[str, ast.AST] = {}
        everything = list(args.posonlyargs) + list(args.args)
        for arg, default in zip(everything[len(everything) - len(args.defaults):], args.defaults):
            defaults[arg.arg] = default
        for arg, default in zip(args.kwonlyargs, args.kw_defaults):
            if default is not None:
                defaults[arg.arg] = default
        sites: list[tuple] = []
        for name in {cls.__name__, "cls"} | {sub.__name__ for sub in subclasses}:
            for site_ctx, scope, call in self.calls.get(name, []):
                called = site_ctx.expr_types(call.func, scope)
                if called is None:
                    # Spelled like the class but not resolved: count it
                    # (more sites only widen the parameter's type).
                    if name in (cls.__name__,):
                        sites.append((site_ctx, scope, call))
                    continue
                if any(t.kind == "cls" and id(t.obj) in family for t in called):
                    sites.append((site_ctx, scope, call))
        if not sites:
            return None
        parts: list[Types] = []
        for site_ctx, scope, call in sites:
            if any(isinstance(a, ast.Starred) for a in call.args) or any(
                k.arg is None for k in call.keywords
            ):
                return None
            expr = next((k.value for k in call.keywords if k.arg == param), None)
            if expr is None and param in positional:
                position = positional.index(param)
                if position < len(call.args):
                    expr = call.args[position]
            if expr is not None:
                parts.append(site_ctx.expr_types(expr, scope))
            elif param in defaults:
                parts.append(ctx.expr_types(defaults[param], ctx.parents.get(id(func))))
            else:
                parts.append(None)
        return parts

    def constructed_unspelled(self, cls: type) -> bool:
        """Whether production may construct ``cls`` (this class exactly)
        other than by a call spelled with its name: see
        ``constructor_argument_types``."""
        if id(cls) not in self._escapes:
            self._escapes[id(cls)] = self._constructed_unspelled(cls)
        return self._escapes[id(cls)]

    def _constructed_unspelled(self, cls: type) -> bool:
        ctx = self.context_for_module(cls.__module__)
        node = ctx.classes.get(cls.__qualname__) if ctx is not None else None
        if node is not None and any(
            _dotted(d).rsplit(".", 1)[-1] not in HARMLESS_CLASS_DECORATORS
            for d in node.decorator_list
        ):
            return True
        for load_ctx, scope, load in self.class_loads.get(cls.__name__, []):
            if id(load) in load_ctx.passive:
                continue
            loaded = load_ctx.expr_types(load, scope)
            # Spelled like the class but not resolved: it may be the class.
            if loaded is None or any(t.kind == "cls" and t.obj is cls for t in loaded):
                return True
        for callee in ("type", "__class__"):
            for call_ctx, scope, call in self.calls.get(callee, []):
                inner = call.func
                if callee == "type":
                    if not (
                        isinstance(inner, ast.Call)
                        and isinstance(inner.func, ast.Name)
                        and inner.func.id == "type"
                        and len(inner.args) == 1
                    ):
                        continue
                    instance = inner.args[0]
                else:
                    if not (isinstance(inner, ast.Attribute) and inner.attr == "__class__"):
                        continue
                    instance = inner.value
                held = call_ctx.expr_types(instance, scope)
                if held and any(t.kind == "inst" and t.obj is cls for t in held):
                    return True
        return False

    # -- class facts ------------------------------------------------------
    def subclasses(self, cls: type) -> list[type]:
        if id(cls) not in self._subclasses:
            found: dict[int, type] = {}
            stack = [cls]
            while stack:
                current = stack.pop()
                try:
                    children = type.__subclasses__(current)
                except TypeError:
                    children = []
                for child in children:
                    if id(child) not in found:
                        found[id(child)] = child
                        stack.append(child)
            self._subclasses[id(cls)] = [c for c in found.values() if _is_app_object(c)]
        return self._subclasses[id(cls)]

    def _dir(self, cls: type) -> frozenset[str]:
        if id(cls) not in self._dirs:
            self._dirs[id(cls)] = frozenset(dir(cls))
        return self._dirs[id(cls)]

    def implementors(self, protocol: type) -> list[type]:
        """App classes that define or inherit every method and property of
        ``protocol`` (and of the Protocols it extends)."""
        if id(protocol) not in self._implementors:
            required: set[str] = set()
            for klass in protocol.__mro__:
                if _is_protocol_class(klass):
                    required |= _own_members(klass)
            self._implementors[id(protocol)] = [
                cls for cls in self.classes if cls is not protocol and required <= self._dir(cls)
            ]
        return self._implementors[id(protocol)]

    def _key(self, cls: type, name: str) -> str | None:
        return self.keys.get((cls.__module__, cls.__qualname__), {}).get(name)

    @staticmethod
    def definer(cls: type, name: str) -> type | None:
        for klass in cls.__mro__:
            if name in vars(klass):
                return klass
        return None

    def _twin(self, key: str) -> str | None:
        rel, qual = key.split("::", 1)
        for one, other in (("/sqlite/", "/postgres/"), ("/postgres/", "/sqlite/")):
            if rel.startswith("backend/app/repositories") and one in rel:
                return f"{rel.replace(one, other)}::{qual}"
        pairs = {
            "backend/app/services/sqlite_repository.py::SQLiteRepository.":
                "backend/app/services/postgres_repository.py::PostgresRepository.",
            "backend/app/services/postgres_repository.py::PostgresRepository.":
                "backend/app/services/sqlite_repository.py::SQLiteRepository.",
        }
        for prefix, twin in pairs.items():
            if key.startswith(prefix):
                return twin + key[len(prefix):]
        return None

    def reached(self, types_: "Types", name: str) -> tuple[frozenset[str] | None, str]:
        """The member keys an access to ``name`` on a value of ``types_``
        reaches, or (None, why) when it has to count by name."""
        if types_ is None:
            return None, "receiver type unknown"
        if not types_:
            # Pinned to nothing but ``None``: every binding the census saw is
            # ``None``, so the value it holds when read came from somewhere
            # the census did not see.
            return None, "receiver type empty"
        keys: set[str] = set()
        for t in types_:
            if t.kind == "mod":
                value = getattr(t.obj, name, _MISSING)
                if value is _MISSING or inspect.ismethod(value):
                    return None, "module attribute"
                continue
            if t.kind in ("func", "sfunc"):
                return None, "attribute of a function"
            cls = t.obj
            found = False
            owner = self.definer(cls, name)
            if owner is not None:
                found = True
                keys.add(self._key(owner, name) or "")
            # The value may be any subclass, and a Protocol-typed value any
            # class that implements the Protocol: what each of them resolves
            # the name to is reached too.
            reachable = self.subclasses(cls)
            if _is_protocol_class(cls):
                reachable = reachable + self.implementors(cls)
            for sub in reachable:
                sub_owner = self.definer(sub, name)
                if sub_owner is not None:
                    found = True
                    keys.add(self._key(sub_owner, name) or "")
            if not found:
                if any("__getattr__" in vars(k) for k in cls.__mro__ if k is not object):
                    return None, "__getattr__"
                if self.is_data_attribute(cls, name):
                    continue
                return None, "not found on the resolved type"
        keys.discard("")
        for key in list(keys):
            keys |= self.declarations(key)
        for key in list(keys):
            twin = self._twin(key)
            if twin is not None and twin in self.all_keys:
                keys.add(twin)
        return frozenset(keys), ""

    def declarations(self, key: str) -> frozenset[str]:
        """The Protocol declarations of a reached member: every app Protocol
        its class implements (nominally or structurally) that declares the
        name.  A declaration describes behaviour that is live; dropping it
        from its Protocol is a contract change, not dead-code removal."""
        if key in self._declarations:
            return self._declarations[key]
        found: set[str] = set()
        cls = self.key_class.get(key)
        name = key.rsplit(".", 1)[1]
        if cls is not None:
            for protocol in self.protocols_declaring(name):
                if protocol is cls:
                    continue
                if protocol in cls.__mro__ or cls in self.implementors(protocol):
                    declared = self._key(protocol, name)
                    if declared:
                        found.add(declared)
        self._declarations[key] = frozenset(found)
        return self._declarations[key]

    def protocols_declaring(self, name: str) -> list[type]:
        if self._protocols_by_name is None:
            by_name: dict[str, list[type]] = {}
            for cls in self.classes:
                if _is_protocol_class(cls) and _is_app_object(cls):
                    for member in vars(cls):
                        by_name.setdefault(member, []).append(cls)
            self._protocols_by_name = by_name
        return self._protocols_by_name.get(name, [])

    # -- attribute and return types -----------------------------------
    def _attr_table(self, cls: type) -> dict[str, list[tuple]]:
        """``attr -> [(context, scope, binding)]`` for one app class: its
        class-body annotations and assignments, and every ``self.attr = ...``
        / ``self.attr: T = ...`` in its methods (any other store to
        ``self.attr`` -- a ``for`` target, unpacking, ``+=``, ``setattr`` --
        makes the attribute unknown; FileContext records them all)."""
        if id(cls) in self._attr_tables:
            return self._attr_tables[id(cls)]
        table: dict[str, list[tuple]] = {}
        self._attr_tables[id(cls)] = table
        if not _is_app_object(cls) or "<locals>" in cls.__qualname__:
            return table
        ctx = self.context_for_module(cls.__module__)
        node = ctx.classes.get(cls.__qualname__) if ctx is not None else None
        if node is None:
            return table
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                table.setdefault(stmt.target.id, []).append((ctx, node, ("ann", stmt.annotation)))
            elif isinstance(stmt, ast.Assign):
                for target in stmt.targets:
                    if isinstance(target, ast.Name):
                        table.setdefault(target.id, []).append((ctx, node, ("expr", stmt.value)))
        for attr, stores in ctx.self_stores.get(id(node), {}).items():
            table.setdefault(attr, []).extend((ctx, scope, binding) for scope, binding in stores)
        return table

    def is_data_attribute(self, cls: type, name: str) -> bool:
        for klass in cls.__mro__:
            if name in self._attr_table(klass) or name in getattr(klass, "__annotations__", {}):
                return True
        return any(name in self._attr_table(sub) for sub in self.subclasses(cls))

    def instance_attribute_types(self, cls: type, name: str) -> object:
        """Types of ``obj.name`` from the bindings above across the class, its
        bases and its subclasses, plus stores through a receiver typed as one
        of them anywhere in production; _MISSING when nothing binds it.
        Unknown when production stores ``<receiver>.name`` through a receiver
        whose type cannot be pinned: that store may be one of this class's
        bindings."""
        related = [k for k in cls.__mro__ if _is_app_object(k)] + self.subclasses(cls)
        infos: list[tuple] = []
        for klass in related:
            infos.extend(self._attr_table(klass).get(name, []))
        related_ids = {id(k) for k in related}
        for ctx, scope, receiver, binding in self.external_stores.get(name, []):
            receiver_types = ctx.expr_types(receiver, scope)
            if receiver_types is None:
                return None
            if any(t.kind == "inst" and id(t.obj) in related_ids for t in receiver_types):
                infos.append((ctx, scope, binding))
        if not infos:
            return _MISSING
        return _union(ctx.binding_types(scope, binding) for ctx, scope, binding in infos)

    def module_attribute_types(self, module: types.ModuleType, name: str) -> "Types":
        """Types of ``module.name``: the value the census holds, widened by
        every ``<module>.name = value`` store in production (the census runs
        before anything rebinds the global, so the value it holds may be a
        ``None`` placeholder).  Unknown when the module rebinds the name
        from a function (``global name``), when a store of ``.name`` goes
        through a receiver whose type cannot be pinned, or when nothing binds
        it."""
        key = (id(module), name)
        if key in self._attr_memo:
            return self._attr_memo[key]
        if key in self._busy:
            return None
        self._busy.add(key)
        try:
            result = self._module_attribute_types(module, name)
        finally:
            self._busy.discard(key)
        self._attr_memo[key] = result
        return result

    def _module_attribute_types(self, module: types.ModuleType, name: str) -> "Types":
        own = self.context_for_module(getattr(module, "__name__", None))
        if own is not None and name in own.module_globals:
            return None
        value = getattr(module, name, _MISSING)
        parts: list[Types] = [] if value is _MISSING else [_value_types(value)]
        for ctx, scope, receiver, binding in self.external_stores.get(name, []):
            receiver_types = ctx.expr_types(receiver, scope)
            if receiver_types is None:
                return None
            if any(r.kind == "mod" and r.obj is module for r in receiver_types):
                parts.append(ctx.binding_types(scope, binding))
        return _union(parts) if parts else None

    def attribute_types(self, t: TypeRef, name: str) -> "Types":
        if t.kind == "mod":
            return self.module_attribute_types(t.obj, name)
        if t.kind in ("func", "sfunc"):
            return None
        cls = t.obj
        key = (id(cls), name)
        if key in self._attr_memo:
            return self._attr_memo[key]
        if key in self._busy:
            return None
        self._busy.add(key)
        try:
            result = self._attribute_types(cls, name)
        finally:
            self._busy.discard(key)
        self._attr_memo[key] = result
        return result

    def _attribute_types(self, cls: type, name: str) -> "Types":
        try:
            static = inspect.getattr_static(cls, name, _MISSING)
        except Exception:
            return None
        if isinstance(static, property):
            return self.return_types(static.fget) if static.fget is not None else None
        if isinstance(static, functools.cached_property):
            return self.return_types(static.func)
        if isinstance(static, (staticmethod, classmethod)):
            return frozenset({TypeRef("func", static.__func__)})
        if isinstance(static, type):
            return frozenset({TypeRef("cls", static)})
        if inspect.isfunction(static) or inspect.ismethoddescriptor(static) or inspect.isbuiltin(static):
            return frozenset({TypeRef("func", static)})
        bound = self.instance_attribute_types(cls, name) if _is_app_object(cls) else _MISSING
        if bound is not _MISSING:
            return bound
        if static is not _MISSING:
            return _value_types(static)
        return None

    def return_types(self, func: object) -> "Types":
        annotations = getattr(func, "__annotations__", None)
        if not isinstance(annotations, dict) or "return" not in annotations:
            return None
        annotation = annotations["return"]
        if isinstance(annotation, str):
            ctx = self.context_for_module(getattr(func, "__module__", None))
            if ctx is None:
                return None
            parsed = _parse_annotation(annotation)
            return None if parsed is None else ctx.annotation_types(parsed)
        return _instances_of(annotation)


_ANNOTATIONS: dict[str, ast.AST | None] = {}


def _parse_annotation(text: str) -> ast.AST | None:
    if text not in _ANNOTATIONS:
        try:
            _ANNOTATIONS[text] = ast.parse(text, mode="eval").body
        except SyntaxError:
            _ANNOTATIONS[text] = None
    return _ANNOTATIONS[text]


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


def facade_classes() -> set[tuple[str, str]]:
    """``(module, qualname)`` of ``RepositoryFacade`` and its two adapter
    subclasses: the classes whose public surface ``facade_surface.json``
    pins (methodology point 5)."""
    _ensure_backend_on_path()
    facade_module = importlib.import_module(_module_name(FACADE_REL))
    facade = getattr(facade_module, FACADE_CLASS)
    classes = {(facade.__module__, facade.__qualname__)}
    for module_name in FACADE_ADAPTER_MODULES:
        module = importlib.import_module(module_name)
        for value in vars(module).values():
            if isinstance(value, type) and issubclass(value, facade) and value.__module__ == module_name:
                classes.add((value.__module__, value.__qualname__))
    return classes


def facade_surface_names(path: Path | None = None) -> frozenset[str]:
    return frozenset(json.loads((path or FACADE_SURFACE_PATH).read_text(encoding="utf-8")))


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
    # A kept member every one of whose live production references counts by
    # name (its receiver was never pinned): the census's residual blind spot.
    by_name_only: bool = False


def _refs_by_member(refs: Iterable[Ref], members: list[Member]) -> dict[str, list[Ref]]:
    """Every reference filed under each member key it counts for: its
    resolved targets, or every member of its name (a template: every member
    whose name fits) when it counts by name."""
    by_name: dict[str, list[str]] = {}
    for m in members:
        by_name.setdefault(m.name, []).append(m.key)
    by_key: dict[str, list[Ref]] = {}
    for ref in refs:
        if ref.pattern is not None:
            keys = [k for name, ks in by_name.items() if ref.pattern.match(name) for k in ks]
        elif ref.targets is None:
            keys = by_name.get(ref.name, [])
        else:
            keys = [k for k in ref.targets]
        for key in keys:
            by_key.setdefault(key, []).append(ref)
    return by_key


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
    prod_by_key = _refs_by_member(prod_refs, members)
    test_by_key = _refs_by_member(test_refs, members)

    def live(m: Member, dead: set[str]) -> list[Ref]:
        return [r for r in prod_by_key.get(m.key, []) if r.owner != m.key and r.owner not in dead]

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
        tests = [r for r in test_by_key.get(m.key, []) if r.owner != m.key]
        if production:
            verdicts[m.key] = Verdict(
                "keep", production_refs=len(production), test_refs=tests,
                by_name_only=all(r.targets is None for r in production),
            )
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


ATTRIBUTE_FALLBACKS = frozenset(
    {
        "receiver type unknown", "super()", "__getattr__", "module attribute",
        "attribute of a function", "not found on the resolved type", "no receiver resolution",
    }
)


def registered_declaration_refs(
    kept: set[str], members: list[Member], receivers: "ReceiverIndex"
) -> list[Ref]:
    """A registered member keeps the Protocol declarations it implements, the
    way a reached one does (``ReceiverIndex.declarations``): one synthetic
    production reference per registered member, owned by it."""
    refs: list[Ref] = []
    by_key = {m.key: m for m in members}
    for key in sorted(kept & by_key.keys()):
        declared = receivers.declarations(key)
        if declared:
            m = by_key[key]
            refs.append(Ref(m.name, None, m.rel, m.lineno, key, declared))
    return refs


def exemption_reason(
    m: Member, facade: set[tuple[str, str]], surface: frozenset[str]
) -> str | None:
    """Why a member with no production reference is still not a candidate
    (methodology point 5), or None."""
    registering = _registering_decorators(m)
    if registering:
        return "registered by decorator " + ", ".join(registering)
    if (m.module, m.qualname) in facade and m.name in surface:
        return "facade public surface (facade_surface.json)"
    cls = _resolve_class(m)
    hook = _external_override(cls, m.name) if cls is not None else None
    return f"overrides {hook}" if hook else None


def _reference_coverage(prod_refs: list[Ref], member_names: set[str]) -> dict[str, object]:
    """How the production references to member names were read: resolved
    through their receiver, or by name and why (methodology point 2)."""
    resolved = 0
    by_reason: dict[str, int] = {}
    for ref in prod_refs:
        if ref.pattern is None and ref.name not in member_names:
            continue
        if ref.targets is not None:
            resolved += 1
        else:
            reason = ref.fallback or "by name"
            by_reason[reason] = by_reason.get(reason, 0) + 1
    attributes = resolved + sum(n for r, n in by_reason.items() if r in ATTRIBUTE_FALLBACKS)
    return {
        "resolved": resolved,
        "by_name": sum(by_reason.values()),
        "by_name_reasons": dict(sorted(by_reason.items(), key=lambda kv: (-kv[1], kv[0]))),
        "attribute_coverage": round(resolved / attributes, 4) if attributes else 1.0,
    }


def build_report(
    allowlist: dict[str, dict[str, str]] | None = None, *, by_name: bool = False
) -> dict[str, object]:
    """The census (methodology points 1-7).  ``by_name`` reads every
    reference by name, the way S2-D shipped it, for comparison runs only."""
    _ensure_backend_on_path()
    allowlist = load_allowlist() if allowlist is None else allowlist
    members = collect_members()
    member_index: dict[str, dict[tuple[str, str], str]] = {}
    for m in members:
        member_index.setdefault(m.rel, {})[(m.qualname, m.name)] = m.key
    receivers = None
    if not by_name:
        receivers = ReceiverIndex(members)
        receivers.prepare(PRODUCTION_ROOTS)
    prod_refs = collect_refs(
        PRODUCTION_ROOTS, False, member_index, receivers=receivers, by_name=by_name
    )
    test_refs = collect_refs(
        TEST_ROOTS + ("examples", "fangan"), True, member_index, by_name=by_name
    )

    facade = facade_classes()
    surface = facade_surface_names()
    exemptions: dict[str, str | None] = {}

    def exemption(m: Member) -> str | None:
        if m.key not in exemptions:
            exemptions[m.key] = exemption_reason(m, facade, surface)
        return exemptions[m.key]

    kept = {k for k, e in allowlist.items() if e.get("category") in LIVE_CATEGORIES}
    coverage = _reference_coverage(prod_refs, {m.name for m in members})
    if receivers is not None:
        prod_refs.extend(registered_declaration_refs(kept, members, receivers))
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
    by_name_only = sorted(m.key for m in members if verdicts[m.key].by_name_only)
    summary = {
        "members": len(rows),
        "keep": sum(1 for r in rows if r.bucket == "keep"),
        "exempt": sum(1 for r in rows if r.bucket == "exempt"),
        "test_only": sum(1 for r in candidates if r.bucket == "test-only"),
        "unreferenced": sum(1 for r in candidates if r.bucket == "unreferenced"),
        "doubtful": sum(1 for r in candidates if r.bucket == "doubtful"),
        "extension_surface": sum(1 for r in candidates if r.surface),
        "kept_by_name_only": len(by_name_only),
        "references": coverage,
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
        "kept_by_name_only": by_name_only,
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
    parser.add_argument(
        "--by-name",
        action="store_true",
        help="read every reference by name (the S2-D reading) for a comparison run",
    )
    args = parser.parse_args()
    if args.check and args.by_name:
        parser.error("--check always reads references through their receivers")
    allowlist = load_allowlist()
    report = build_report(allowlist, by_name=args.by_name)
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
