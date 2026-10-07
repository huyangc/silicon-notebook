# backend/tests/test_memory_chunk_write_guard.py
"""E4-1b static guard: every write of the ``chunks`` table refuses a Memory
source or is on a reviewed list, and nothing but an INSERT writes
``sources.source_type``.

Passages (chunks) are a SHARED index while Memory is private per user, so the
invariant is "no chunk row under a Memory source". It is kept in two ways:

1. Every path that WRITES ``chunks`` refuses a Memory source itself, by calling
   ``_refuse_memory_source`` (with non-constant arguments) BEFORE the write, on
   every path to it, in the same function: the chunk store's two write methods,
   the KG build publish, the Knowhow transfer insert, the sync import. Or it is on
   ``REVIEWED`` with the reason it needs no refusal. A new write site that is
   neither fails, and the failure names the helper to call.
2. A probe protects a write only at the moment the source IS a Memory source; it
   cannot protect against the type changing LATER (measured on PostgreSQL under
   READ COMMITTED, with and without row locks). So the invariant also rests on
   ``sources.source_type`` never changing after insert: no statement other than an
   INSERT may write that column. A generic writer whose table can be ``sources``
   (the sync import's ``ON CONFLICT (id) DO UPDATE SET <all columns>``, the shadow
   replicator) is listed on ``GENERIC_UPDATERS`` with a checked reason; the sync
   import refuses a type change to or from Memory at run time
   (``import_._preflight_memory_sources``, pinned by
   ``test_memory_chunk_write_refusals.py``).

What is scanned: the Python sources under ``backend/app`` (both repository
backends, the migration packages, the services), ``scripts/`` (``merge_dbs.py``
merges two real libraries into one; the benchmark and fixture generators that
seed a scratch database of their own are listed as such) and ``examples/``, and
every ``.sql`` file there outside a ``migrations`` directory (a script a module
could read and run: no function can refuse before its statements, so a
``chunks`` write in one must be on ``REVIEWED``, and the ``sources`` rules apply).
Schema migrations are not scanned: PostgreSQL's are ``.sql`` files under
``repositories/postgres/migrations`` and run once at upgrade as the schema's own
history, not as a write path (SQLite's are Python and are scanned).

How a write is found, by AST (so a comment or a docstring never counts):

* every string expression (a literal, an f-string, a ``+`` / ``%`` chain, a
  ``str.format`` / ``psycopg.sql.SQL(...).format`` template) is folded into one
  statement text with its interpolated parts as placeholders; a write is
  ``INSERT [OR ..] INTO`` / ``REPLACE INTO`` / ``MERGE INTO`` / ``UPDATE ... SET`` /
  ``COPY ... FROM`` of a table, schema-qualified or not. An English sentence that
  merely mentions "UPDATE chunks" is not an ``UPDATE ... SET``;
* an interpolated table name is EVALUATED: module and class constants, imported
  constants, loop variables over constant tuples/dicts, local constant
  assignments and identifier-quoting wrappers resolve to the set of tables the
  statement can write. A table that is the enclosing function's own parameter
  makes that function a table helper, and then EVERY call site of the helper in
  the scanned tree (by name, through imports, through the late-bound seats in
  ``SEAM_ALIASES``) is checked the same way, transitively; a reference to a helper
  that is not a call (a callback) must be listed. Anything that cannot be
  evaluated must be listed. ``self.X`` / ``cls.X`` resolves to this module's class
  constant only when no other scanned module defines a class constant ``X`` (a
  subclass there could override it);
* a statement hoisted into a module or class constant is attributed to the
  function that uses the constant, at the place where it is used;
* "before the write" means the refusal call is an expression statement of its
  own (``_refuse_memory_source(...)`` on its line: inside an expression such as
  ``flag and refuse(...)``, ``refuse(...) if flag else None``, a comprehension or
  a lambda it may not run), in the same function as the write, in a statement
  list that encloses the write, before the statement that holds it: a refusal in
  a ``try`` / ``if`` / ``else`` / loop / ``with`` body the write is outside of or
  in a nested function does not count (it may not run, or its error may be
  swallowed). A statement whose text is assigned to a local
  name is written where that name is next used (a statement builder), and a
  nested function writes at every place it is called. So a write moved into a
  private method called after the refusal is a write site of that method, not
  of its caller (control P5 of the second re-review is red by design: the method
  must refuse itself, or take its table as a parameter so its callers are
  checked);
* a ``sources`` UPDATE / upsert whose SET list is composed is checked through
  every string that can FLOW into the composed part (local assignments, additive
  mutations of a local list, loop and comprehension variables, constants; a
  comprehension over an unknown value is enumerable through ``if var in
  <enumerable>``): ``source_type`` among them is a violation, a part the guard
  cannot enumerate (a parameter, ``**kwargs``, a mapping's keys) makes the site a
  generic updater that must be on ``GENERIC_UPDATERS``.

What the guard cannot see: WHICH id a refusal checks. A refusal with a
non-constant argument counts even when that argument is the wrong one (the
notebook id instead of the source id; experiment X8 of the second re-review);
that is a semantic property the behaviour tests of each path pin
(``test_memory_chunk_write_refusals.py``), not something a static scan proves. A
statement a PL/pgSQL function builds at run time (``EXECUTE format(...)``) is out
of its reach as well.

The copy path (``NotebookCopyService.copy_notebook`` -> ``insert_copy_rows("chunks")``)
does not call the refusal. Which chunk rows it copies is decided by the copy
statement set of ``sharing_store.py``, and the guard reports the path as "guarded
by the copy snapshot predicate" exactly when, on BOTH backends, the chunks copy
statement set that is used when the notebook holds a Memory source carries a
``memory_sql`` Memory-derived predicate (``NOT memory_sql.memory_derived_*(<the
chunks alias>)`` as an ``AND``-conjunct of the query's outermost WHERE clause),
else as "unguarded" (``copy_path_reason``). Two shapes are recognised, by value
and control flow rather than by a variable name: ONE statement set used for
every copy (``_COPY_SNAPSHOT_QUERIES`` on master, whose ``chunks`` query would
carry ``memory_derived_object``), or several sets chosen per copy, where a set
may lack the predicate only when it is returned solely on the branch where a
Memory probe found NO row -- the probe being exactly
``if <conn>.execute(PROBE, ...).fetchone():`` (or the same call as the test of a
conditional expression) with ``PROBE`` a module-level statement carrying
``memory_sql.memory_source_type_predicate(...)``, so the set returned when it
finds a row is the one checked -- and when it is otherwise used only by a
constant index to a non-``chunks`` entry (the root row) or, at module level, as a
projection to its table names or to construct another statement set. The second
is task E5-1's seam: ``SharingStore._copy_queries`` returns
``_MEMORY_COPY_SNAPSHOT_QUERIES`` (``chunks`` query with ``NOT
memory_sql.memory_derived_in_notebook("c")``) when ``_COPY_DIRTY_SQL`` finds a
Memory source or a dirty flag, and the pre-M2 ``_COPY_SNAPSHOT_QUERIES`` otherwise.

The scan parses each file once per test session (``lru_cache`` keyed by path and
text, all tests of this file in one xdist group). The guard is entered through
``guard_problems(tree)`` everywhere: the repository test passes the real tree,
the controls pass the real tree with one file changed (every surviving
experiment of the reviews of this task is a control) or a small fake tree.
"""
from __future__ import annotations

import ast
import functools
import importlib
import itertools
import re
import sys
import types
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

import pytest

pytestmark = pytest.mark.xdist_group(name="memory_chunk_write_guard")

ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS = ("backend/app", "scripts", "examples")
HELPER = "_refuse_memory_source"

# Late-bound call seats: an attribute call ``x.<seat>(...)`` is a call of the named
# helper. ``SharingStore.insert_row`` is filled by both bundles with
# ``SharingStore.insert_row_values`` (and rebound to the facade's ``_insert_row``,
# which forwards to it); both references are on ``REVIEWED`` as SEAM.
SEAM_ALIASES = {"insert_row": "insert_row_values"}

# Single-argument calls that only quote/render an identifier.
_IDENTITY_WRAPPERS = frozenset(
    {"_ident", "Identifier", "_quote", "_quote_ident", "quote_ident", "_q", "ident", "str"}
)
_SEQUENCE_WRAPPERS = frozenset({"sorted", "tuple", "list", "reversed", "set", "frozenset"})


# ============================================================= tree and parsing
def _is_scanned_sql(path: str) -> bool:
    """A ``.sql`` file outside every ``migrations`` directory (a schema migration
    runs once at upgrade, as the schema's own history, not as a write path)."""
    return path.endswith(".sql") and "migrations" not in path.split("/")[:-1]


def _read_tree(root: Path) -> dict[str, str]:
    tree: dict[str, str] = {}
    for directory in SCAN_DIRS:
        base = root / directory
        if not base.is_dir():
            continue
        for file in sorted([*base.rglob("*.py"), *base.rglob("*.sql")]):
            if "__pycache__" in file.parts:
                continue
            path = file.relative_to(root).as_posix()
            if path.endswith(".sql") and not _is_scanned_sql(path):
                continue
            tree[path] = file.read_text(encoding="utf-8")
    return tree


@functools.lru_cache(maxsize=1)
def repo_tree() -> Mapping[str, str]:
    return types.MappingProxyType(_read_tree(ROOT))


def overlay(base: Mapping[str, str], changes: Mapping[str, str]) -> dict[str, str]:
    out = dict(base)
    out.update(changes)
    return out


PH = "\u27ea{}\u27eb"  # placeholder marker in a folded statement text
_PH_RE = re.compile("\u27ea(\\d+)\u27eb")


@dataclass(eq=False)
class Scope:
    """One function. ``outer`` is the outermost enclosing function's qual."""

    module: "Module"
    qual: str
    outer: str
    node: ast.AST
    parent: "Scope | None"
    params: list[str]
    implicit_first: bool  # a method called through an instance/class

    @property
    def name(self) -> str:
        return self.qual.rsplit(".", 1)[-1]


@dataclass(eq=False)
class Statement:
    node: ast.AST
    text: str
    exprs: list[ast.AST | None]
    scope: Scope | None


class Module:
    def __init__(self, path: str, text: str) -> None:
        self.path = path
        self.tree = ast.parse(text)
        self.parent: dict[ast.AST, ast.AST] = {}
        self.scope_of: dict[ast.AST, Scope | None] = {}
        self.scopes: list[Scope] = []
        self.consts: dict[str, ast.AST | None] = {}
        self.class_consts: dict[str, ast.AST | None] = {}
        self.const_assign_node: dict[ast.AST, str] = {}  # value node -> constant name
        self.imports: dict[str, tuple[str, str | None]] = {}
        self.docstrings: set[int] = set()
        self.calls: list[ast.Call] = []
        self.name_loads: dict[str, list[ast.Name]] = {}
        self.attr_loads: dict[str, list[ast.Attribute]] = {}
        self._mark_docstring(self.tree)
        self._walk(self.tree, None, "", in_class=False)
        self.calls_by_name: dict[str, list[ast.Call]] = {}
        for call in self.calls:
            func = call.func
            callee = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            self.calls_by_name.setdefault(callee, []).append(call)
        self.statements = self._statements()

    # ------------------------------------------------------------- building
    def _mark_docstring(self, node: ast.AST) -> None:
        body = getattr(node, "body", None)
        if (
            isinstance(body, list) and body and isinstance(body[0], ast.Expr)
            and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str)
        ):
            self.docstrings.add(id(body[0].value))

    def _record_const(self, target_map: dict, node: ast.AST) -> None:
        targets: list[ast.AST] = []
        value = None
        if isinstance(node, ast.Assign):
            targets, value = node.targets, node.value
        elif isinstance(node, ast.AnnAssign) and node.value is not None:
            targets, value = [node.target], node.value
        for target in targets:
            if isinstance(target, ast.Name):
                name = target.id
                target_map[name] = value if name not in target_map else None
                if value is not None:
                    self.const_assign_node[value] = name

    def _walk(self, node: ast.AST, scope: Scope | None, qual: str, *, in_class: bool) -> None:
        for child in ast.iter_child_nodes(node):
            self.parent[child] = node
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                inner_qual = f"{qual}.{child.name}" if qual else child.name
                decorators = {
                    d.id if isinstance(d, ast.Name) else getattr(d, "attr", "")
                    for d in child.decorator_list
                }
                args = child.args
                params = [a.arg for a in (*args.posonlyargs, *args.args)]
                params += [a.arg for a in args.kwonlyargs]
                inner = Scope(
                    module=self, qual=inner_qual,
                    outer=scope.outer if scope is not None else inner_qual,
                    node=child, parent=scope, params=params,
                    implicit_first=in_class and "staticmethod" not in decorators,
                )
                self.scopes.append(inner)
                self.scope_of[child] = scope
                self._mark_docstring(child)
                for default in (*args.defaults, *[d for d in args.kw_defaults if d is not None]):
                    self.parent[default] = child
                self._walk(child, inner, inner_qual, in_class=False)
                continue
            if isinstance(child, ast.ClassDef):
                self.scope_of[child] = scope
                self._mark_docstring(child)
                if scope is None:
                    for stmt in child.body:
                        self._record_const(self.class_consts, stmt)
                self._walk(child, scope, f"{qual}.{child.name}" if qual else child.name, in_class=True)
                continue
            self.scope_of[child] = scope
            if scope is None and not in_class and node is self.tree:
                self._record_const(self.consts, child)
            if isinstance(child, ast.ImportFrom):
                base = self._resolve_relative(child)
                for alias in child.names:
                    self.imports[alias.asname or alias.name] = (base, alias.name)
            elif isinstance(child, ast.Import):
                for alias in child.names:
                    self.imports[alias.asname or alias.name.split(".")[0]] = (
                        alias.name if alias.asname else alias.name.split(".")[0], None
                    )
            elif isinstance(child, ast.Call):
                self.calls.append(child)
            elif isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                self.name_loads.setdefault(child.id, []).append(child)
            elif isinstance(child, ast.Attribute) and isinstance(child.ctx, ast.Load):
                self.attr_loads.setdefault(child.attr, []).append(child)
            self._walk(child, scope, qual, in_class=in_class)

    def _resolve_relative(self, node: ast.ImportFrom) -> str:
        if not node.level:
            return node.module or ""
        parts = self.path[:-3].split("/")
        if parts[0] == "backend":
            parts = parts[1:]
        base = parts[: len(parts) - node.level]
        return ".".join(base + ([node.module] if node.module else []))

    # ----------------------------------------------------------- statements
    def _stringish(self, node: ast.AST) -> bool:
        if isinstance(node, ast.Constant):
            return isinstance(node.value, str) and id(node) not in self.docstrings
        if isinstance(node, ast.JoinedStr):
            return True
        if isinstance(node, ast.BinOp) and isinstance(node.op, (ast.Add, ast.Mod)):
            return self._stringish(node.left) or (
                isinstance(node.op, ast.Add) and self._stringish(node.right)
            )
        return False

    def _statements(self) -> list[Statement]:
        out: list[Statement] = []
        for node, parent in list(self.parent.items()):
            if not self._stringish(node):
                continue
            if isinstance(parent, ast.JoinedStr) or isinstance(parent, ast.FormattedValue):
                continue
            if (
                isinstance(parent, ast.BinOp) and isinstance(parent.op, (ast.Add, ast.Mod))
                and self._stringish(parent)
            ):
                continue
            exprs: list[ast.AST | None] = []
            text = self._fold(node, exprs)
            text = self._apply_format(node, text, exprs)
            out.append(Statement(node, text, exprs, self.scope_of.get(node)))
        return out

    @staticmethod
    def _placeholder(exprs: list, expr: ast.AST | None) -> str:
        exprs.append(expr)
        return PH.format(len(exprs) - 1)

    def _fold(self, node: ast.AST, exprs: list) -> str:
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            return node.value
        if isinstance(node, ast.JoinedStr):
            parts = []
            for value in node.values:
                if isinstance(value, ast.Constant):
                    parts.append(str(value.value))
                elif isinstance(value, ast.FormattedValue):
                    parts.append(self._placeholder(exprs, value.value))
            return "".join(parts)
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Add):
            left = self._fold(node.left, exprs) if self._stringish(node.left) else self._placeholder(exprs, node.left)
            right = self._fold(node.right, exprs) if self._stringish(node.right) else self._placeholder(exprs, node.right)
            return left + right
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Mod):
            template = self._fold(node.left, exprs)
            if isinstance(node.right, ast.Tuple):
                args = list(node.right.elts)
            elif isinstance(node.right, ast.Dict):
                args = []
            else:
                args = [node.right]
            named = {}
            if isinstance(node.right, ast.Dict):
                named = {
                    k.value: v for k, v in zip(node.right.keys, node.right.values)
                    if isinstance(k, ast.Constant)
                }
            counter = iter(args)

            def sub(match: re.Match) -> str:
                if match.group(1):
                    return self._placeholder(exprs, named.get(match.group(1)))
                return self._placeholder(exprs, next(counter, None))

            return re.sub(r"%(?:\((\w+)\))?s", sub, template)
        return self._placeholder(exprs, node)

    def _apply_format(self, node: ast.AST, text: str, exprs: list) -> str:
        """``"...{}...".format(x)`` and ``sql.SQL("...{}...").format(x)``: the braces
        become placeholders bound to the format arguments; without an attached
        ``.format`` a ``{name}`` in a plain literal is an unknown placeholder."""
        holder = node
        parent = self.parent.get(holder)
        if (
            isinstance(parent, ast.Call) and parent.args and parent.args[0] is holder
            and (getattr(parent.func, "attr", None) == "SQL" or getattr(parent.func, "id", None) == "SQL")
        ):
            holder = parent
            parent = self.parent.get(holder)
        args: list[ast.AST] = []
        keywords: dict[str, ast.AST] = {}
        if isinstance(parent, ast.Attribute) and parent.attr == "format":
            call = self.parent.get(parent)
            if isinstance(call, ast.Call) and call.func is parent:
                args = list(call.args)
                keywords = {k.arg: k.value for k in call.keywords if k.arg}
        if not isinstance(node, (ast.Constant, ast.BinOp)) and not args:
            return text
        counter = iter(range(len(args)))

        def sub(match: re.Match) -> str:
            key = match.group(1)
            if key == "":
                index = next(counter, None)
                return self._placeholder(exprs, args[index] if index is not None else None)
            if key.isdigit():
                index = int(key)
                return self._placeholder(exprs, args[index] if index < len(args) else None)
            return self._placeholder(exprs, keywords.get(key))

        return re.sub(r"(?<!\{)\{(\w*)\}(?!\})", sub, text)


@functools.lru_cache(maxsize=None)
def _module(path: str, text: str) -> Module:
    return Module(path, text)


_SQL_COMMENT = re.compile(r"--[^\n]*|/\*.*?\*/", re.S)


@functools.lru_cache(maxsize=None)
def _sql_statements(text: str) -> tuple[Statement, ...]:
    """The statements of a scanned ``.sql`` file (comments blanked, split at ``;``)."""
    body = _SQL_COMMENT.sub(" ", text)
    return tuple(
        Statement(node=ast.Constant(part), text=part, exprs=[], scope=None)
        for part in body.split(";") if part.strip()
    )


# ======================================================= statement recognition
_Q, _QE = r"[\"`\[]?", r"[\"`\]]?"
_NAME = rf"{_Q}(?:\w|\u27ea\d+\u27eb)+{_QE}"
_TABLE = rf"(?P<table>(?:{_NAME}\s*\.\s*)?{_NAME})"
_WRITE_PATTERNS = (
    ("insert", re.compile(
        rf"\b(?:INSERT\s+(?:OR\s+(?P<orx>\w+)\s+)?INTO|(?P<rep>REPLACE)\s+INTO|MERGE\s+INTO)\s+{_TABLE}"
        r"(?=\s*(?:\(|VALUES\b|SELECT\b|DEFAULT\b|WITH\b|OVERRIDING\b|AS\b|USING\b|\u27ea|$))",
        re.I | re.S,
    )),
    ("update", re.compile(
        # SQLite's ``UPDATE OR <conflict> t``, PostgreSQL's ``UPDATE ONLY t``
        rf"\bUPDATE\s+(?:OR\s+\w+\s+)?(?:ONLY\s+)?{_TABLE}"
        r"(?=\s*\*?\s*(?:(?:AS\s+)?(?!SET\b)\w+\s+)?SET\b|\s*\u27ea|\s*$)",
        re.I | re.S,
    )),
    ("copy", re.compile(
        rf"\bCOPY\s+{_TABLE}(?=\s*(?:\([^)]*\)\s*)?(?:FROM\b|\u27ea|$))",
        re.I | re.S,
    )),
)
# a piece that ends with the verb: the table comes from whatever follows it
_TRAILING_VERB = re.compile(r"(?-i:\b(?:INSERT\s+(?:OR\s+[A-Z]+\s+)?INTO|REPLACE\s+INTO)\s*$)")
_UPSERT = re.compile(r"\bDO\s+UPDATE\b|\bON\s+DUPLICATE\s+KEY\b|\bOR\s+REPLACE\b|^\s*REPLACE\s+INTO\b", re.I)
_SEG_END = r"(?=\bWHERE\b|\bFROM\b|\bRETURNING\b|$)"


def _assigns(segment: str, column: str) -> bool:
    """``segment`` (a SET list) assigns ``column``: ``col =``, ``t.col =`` or a row
    form ``(a, col) = (...)``."""
    simple = rf"(?:^|,)\s*(?:\w+\.)?[\"`]?{column}[\"`]?\s*="
    row = rf"\(\s*[^()]*\b{column}\b[^()]*\)\s*="
    return bool(re.search(simple, segment, re.I) or re.search(row, segment, re.I))


@dataclass
class Write:
    verb: str          # insert | update | copy
    table: str | None  # literal table (lower-case, unqualified) or None if interpolated
    table_expr: ast.AST | None
    table_pattern: str  # the raw token, placeholders kept
    segment: str       # SET list for an UPDATE / DO UPDATE
    replace: bool       # REPLACE INTO / INSERT OR REPLACE
    upsert: bool       # the statement itself is an upsert


def _writes(statement: Statement) -> list[Write]:
    text = statement.text
    found: list[Write] = []
    for verb, pattern in _WRITE_PATTERNS:
        for match in pattern.finditer(text):
            token = match.group("table")
            last = re.split(r"\s*\.\s*", token)[-1].strip("\"`[]")
            placeholders = _PH_RE.findall(last)
            table = None if placeholders else last.lower()
            expr = None
            if placeholders and _PH_RE.fullmatch(last):
                expr = statement.exprs[int(placeholders[0])]
            segment = ""
            rest = text[match.end():]
            if verb == "update":
                seg = re.search(rf"\bSET\b(.*?){_SEG_END}", rest, re.I | re.S)
                segment = seg.group(1) if seg else rest
            elif verb == "insert":
                seg = re.search(rf"\bDO\s+UPDATE\s+SET\b(.*?)(?=\bWHERE\b|\bRETURNING\b|$)", rest, re.I | re.S)
                segment = seg.group(1) if seg else ""
            replace = bool(match.groupdict().get("rep")) or (
                (match.groupdict().get("orx") or "").upper() == "REPLACE"
            )
            found.append(Write(
                verb=verb, table=table, table_expr=expr, table_pattern=last,
                segment=segment, replace=replace,
                upsert=bool(re.search(r"\bDO\s+UPDATE\b|\bON\s+DUPLICATE\s+KEY\b", rest, re.I)) or replace,
            ))
    if not found and _TRAILING_VERB.search(text):
        found.append(Write("insert", None, None, "", "", False, False))
    return found


def _could_be(pattern: str, target: str) -> bool:
    """Can a table token with placeholders (``⟪0⟫_facts``) spell ``target``?"""
    parts = _PH_RE.split(pattern)
    regex = "".join(re.escape(p) if i % 2 == 0 else ".*" for i, p in enumerate(parts))
    return bool(re.fullmatch(regex, target, re.I))


# ================================================================== evaluation
class Unknown(Exception):
    pass


@dataclass(frozen=True)
class Param:
    name: str


_MAX_VALUES = 512


def _dedupe(values: Iterable[Any]) -> list[Any]:
    seen: dict[str, Any] = {}
    for value in values:
        seen.setdefault(repr(value), value)
        if len(seen) > _MAX_VALUES:
            raise Unknown("too many values")
    return list(seen.values())


class Evaluator:
    def __init__(self, modules: Mapping[str, Module]) -> None:
        self.modules = modules
        self._by_dotted: dict[str, Module] = {}
        # class constant name -> the modules that define a class constant of that
        # name: ``self.X`` / ``cls.X`` resolves to this module's class constant only
        # when no other module can override it in a subclass
        self._class_const_modules: dict[str, set[str]] = {}
        for path, module in modules.items():
            for name in module.class_consts:
                self._class_const_modules.setdefault(name, set()).add(path)
        for path, module in modules.items():
            dotted = path[:-3].replace("/", ".")
            if dotted.startswith("backend."):
                dotted = dotted[len("backend."):]
            if dotted.endswith(".__init__"):
                dotted = dotted[: -len(".__init__")]
            self._by_dotted[dotted] = module

    def module_for(self, dotted: str) -> Module | None:
        return self._by_dotted.get(dotted)

    # -------------------------------------------------------------- public
    def values(self, expr: ast.AST | None, module: Module, scope: Scope | None) -> list[Any] | Param:
        """Every value ``expr`` can take, a ``Param`` when it is the scope's own
        parameter (possibly quoted by an identity wrapper), else ``Unknown``."""
        if expr is None:
            raise Unknown("no expression")
        return self._ev(expr, module, scope, frozenset())

    # ------------------------------------------------------------ internals
    def _ev(self, expr: ast.AST, module: Module, scope: Scope | None, seen: frozenset) -> Any:
        key = (module.path, id(expr))
        if key in seen:
            raise Unknown("cycle")
        seen = seen | {key}
        if isinstance(expr, ast.Constant):
            return [expr.value]
        if isinstance(expr, ast.JoinedStr):
            parts = []
            for value in expr.values:
                if isinstance(value, ast.Constant):
                    parts.append([str(value.value)])
                else:
                    got = self._ev(value.value, module, scope, seen)
                    if isinstance(got, Param):
                        raise Unknown("param inside f-string")
                    parts.append([str(v) for v in got])
            return _dedupe("".join(combo) for combo in itertools.product(*parts))
        if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
            items: list[Any] = []
            for elt in expr.elts:
                if isinstance(elt, ast.Starred):
                    got = self._ev(elt.value, module, scope, seen)
                    if isinstance(got, Param) or len(got) != 1:
                        raise Unknown("starred")
                    items.extend(got[0])
                    continue
                got = self._ev(elt, module, scope, seen)
                if isinstance(got, Param) or len(got) != 1:
                    raise Unknown("ambiguous element")
                items.append(got[0])
            return [tuple(items)]
        if isinstance(expr, ast.Dict):
            out = {}
            for k, v in zip(expr.keys, expr.values):
                if k is None:
                    raise Unknown("dict splat")
                kv, vv = self._ev(k, module, scope, seen), self._ev(v, module, scope, seen)
                if isinstance(kv, Param) or isinstance(vv, Param) or len(kv) != 1 or len(vv) != 1:
                    raise Unknown("ambiguous dict")
                out[kv[0]] = vv[0]
            return [out]
        if isinstance(expr, ast.Name):
            return self._name(expr, module, scope, seen)
        if isinstance(expr, ast.Attribute):
            base = expr.value
            if isinstance(base, ast.Name):
                if expr.attr in module.class_consts and (
                    base.id in ("self", "cls") or base.id[:1].isupper()
                ):
                    value = module.class_consts[expr.attr]
                    if value is None:
                        raise Unknown("reassigned class constant")
                    if base.id in ("self", "cls") and (
                        self._class_const_modules.get(expr.attr, set()) - {module.path}
                    ):
                        raise Unknown("class constant a subclass in another module may override")
                    return self._ev(value, module, None, seen)
                imported = module.imports.get(base.id)
                if imported and imported[1] is None:
                    target = self.module_for(imported[0])
                    if target and target.consts.get(expr.attr) is not None:
                        return self._ev(target.consts[expr.attr], target, None, seen)
            raise Unknown("attribute")
        if isinstance(expr, ast.Subscript):
            containers = self._ev(expr.value, module, scope, seen)
            keys = self._ev(expr.slice, module, scope, seen)
            if isinstance(containers, Param) or isinstance(keys, Param):
                raise Unknown("subscript of a parameter")
            out = []
            for container in containers:
                for key in keys:
                    try:
                        out.append(container[key])
                    except (KeyError, IndexError, TypeError):
                        raise Unknown("subscript") from None
            return _dedupe(out)
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
            left = self._ev(expr.left, module, scope, seen)
            right = self._ev(expr.right, module, scope, seen)
            if isinstance(left, Param) or isinstance(right, Param):
                raise Unknown("param in +")
            try:
                return _dedupe(a + b for a in left for b in right)
            except TypeError:
                raise Unknown("+") from None
        if isinstance(expr, ast.IfExp):
            body = self._ev(expr.body, module, scope, seen)
            other = self._ev(expr.orelse, module, scope, seen)
            if isinstance(body, Param) or isinstance(other, Param):
                raise Unknown("param in if-expression")
            return _dedupe([*body, *other])
        if isinstance(expr, ast.Call):
            func = expr.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if name in _IDENTITY_WRAPPERS and len(expr.args) == 1 and not expr.keywords:
                return self._ev(expr.args[0], module, scope, seen)
            if (
                isinstance(func, ast.Name) and name in _SEQUENCE_WRAPPERS
                and len(expr.args) == 1
            ):
                got = self._ev(expr.args[0], module, scope, seen)
                if isinstance(got, Param):
                    raise Unknown("sequence of a parameter")
                return _dedupe(tuple(self._iterate(v)) for v in got)
            if isinstance(func, ast.Attribute) and name in ("items", "keys", "values") and not expr.args:
                got = self._ev(func.value, module, scope, seen)
                if isinstance(got, Param):
                    raise Unknown("mapping parameter")
                out = []
                for value in got:
                    if not isinstance(value, dict):
                        raise Unknown("not a dict")
                    out.append(tuple(getattr(value, name)()))
                return _dedupe(out)
        raise Unknown(type(expr).__name__)

    @staticmethod
    def _iterate(value: Any) -> list[Any]:
        if isinstance(value, dict):
            return list(value.keys())
        if isinstance(value, (tuple, list)):
            return list(value)
        raise Unknown("not iterable")

    def _bind(self, target: ast.AST, name: str, item: Any) -> tuple[bool, Any]:
        if isinstance(target, ast.Name):
            return (target.id == name, item)
        if isinstance(target, (ast.Tuple, ast.List)):
            if not isinstance(item, (tuple, list)):
                raise Unknown("destructure")
            elts = target.elts
            star = next((i for i, e in enumerate(elts) if isinstance(e, ast.Starred)), None)
            for index, elt in enumerate(elts):
                if isinstance(elt, ast.Starred):
                    if isinstance(elt.value, ast.Name) and elt.value.id == name:
                        return (True, tuple(item[index: len(item) - (len(elts) - index - 1)]))
                    continue
                position = index if star is None or index < star else len(item) - (len(elts) - index)
                if position >= len(item):
                    raise Unknown("destructure")
                hit, value = self._bind(elt, name, item[position])
                if hit:
                    return (True, value)
        return (False, None)

    def _binds(self, target: ast.AST, name: str) -> bool:
        return any(isinstance(n, ast.Name) and n.id == name for n in ast.walk(target))

    def _name(self, expr: ast.Name, module: Module, scope: Scope | None, seen: frozenset) -> Any:
        name = expr.id
        # 1. an enclosing loop / comprehension of this scope binds it
        node: ast.AST = expr
        stop = scope.node if scope is not None else module.tree
        while node is not stop and node in module.parent:
            parent = module.parent[node]
            generators: list[ast.comprehension] = []
            if isinstance(parent, (ast.For, ast.AsyncFor)) and node in parent.body:
                generators = [ast.comprehension(target=parent.target, iter=parent.iter, ifs=[], is_async=0)]
            elif isinstance(parent, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
                generators = parent.generators
            for generator in generators:
                if self._binds(generator.target, name):
                    iterables = self._ev(generator.iter, module, scope, seen)
                    if isinstance(iterables, Param):
                        raise Unknown("loop over a parameter")
                    out = []
                    for iterable in iterables:
                        for item in self._iterate(iterable):
                            hit, value = self._bind(generator.target, name, item)
                            if hit:
                                out.append(value)
                    return _dedupe(out)
            node = parent
        # 2. local assignments, 3. parameters, 4. enclosing scopes
        current = scope
        first = True
        while current is not None:
            assigned = self._local_assignments(current, name)
            if assigned is not None:
                if name in current.params:
                    raise Unknown("reassigned parameter")
                out: list[Any] = []
                for value in assigned:
                    got = self._ev(value, module, current, seen)
                    if isinstance(got, Param):
                        raise Unknown("parameter through a local")
                    out.extend(got)
                return _dedupe(out)
            if name in current.params:
                if first:
                    return Param(name)
                raise Unknown("closure parameter")
            current = current.parent
            first = False
        # 5. module constants, 6. imported constants
        if name in module.consts:
            value = module.consts[name]
            if value is None:
                raise Unknown("reassigned module constant")
            return self._ev(value, module, None, seen)
        imported = module.imports.get(name)
        if imported and imported[1] is not None:
            target = self.module_for(imported[0])
            if target is not None and target.consts.get(imported[1]) is not None:
                return self._ev(target.consts[imported[1]], target, None, seen)
        raise Unknown(f"name {name}")

    @staticmethod
    def _bound(target: ast.AST) -> Iterable[str]:
        """Names a binding target rebinds (a subscript or attribute target rebinds none)."""
        if isinstance(target, ast.Name):
            yield target.id
        elif isinstance(target, (ast.Tuple, ast.List)):
            for elt in target.elts:
                yield from Evaluator._bound(elt)
        elif isinstance(target, ast.Starred):
            yield from Evaluator._bound(target.value)

    @staticmethod
    def _local_assignments(
        scope: Scope, name: str, *, augmented_ok: bool = False
    ) -> list[ast.AST] | None:
        """The values assigned to ``name`` in ``scope``; any other rebinding is
        ``Unknown`` (``+=`` too, unless ``augmented_ok``: ``_SetFlow`` follows it)."""
        found: list[ast.AST] = []
        other = False
        stack = list(ast.iter_child_nodes(scope.node))
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name) and target.id == name:
                        found.append(node.value)
                    elif name in Evaluator._bound(target):
                        other = True
            elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) and node.target.id == name:
                if node.value is not None:
                    found.append(node.value)
            elif isinstance(node, (ast.AugAssign, ast.NamedExpr)):
                if name in Evaluator._bound(node.target) and not (
                    augmented_ok and isinstance(node, ast.AugAssign)
                ):
                    other = True
            elif isinstance(node, (ast.For, ast.AsyncFor, ast.comprehension)):
                pass  # loop targets are resolved from the loop itself
            elif isinstance(node, (ast.With, ast.AsyncWith)):
                for item in node.items:
                    if item.optional_vars is not None and name in Evaluator._bound(item.optional_vars):
                        other = True
            elif isinstance(node, ast.ExceptHandler) and node.name == name:
                other = True
            stack.extend(ast.iter_child_nodes(node))
        if other:
            raise Unknown("rebound")
        return found or None


class _SetFlow:
    """The strings that can flow into an interpolated part of a SET list.

    The result is a set of atoms: the possible values of a string expression, and
    for a sequence (a list being built, a ``join``) the possible values of its
    elements. It follows local assignments and the additive mutations of a local
    list (``append`` / ``extend`` / ``insert`` / ``+=``), loop and comprehension
    variables over enumerable iterables, and module / imported constants. A
    comprehension variable over a value it cannot enumerate is enumerable only
    through a filter ``if var in <enumerable>``. Anything else (a parameter,
    ``**kwargs``, a mapping's keys, a call it does not know) raises ``Unknown``."""

    _ADDITIVE = frozenset({"append", "extend", "insert", "add", "update", "setdefault"})

    def __init__(self, ev: Evaluator, module: Module) -> None:
        self.ev = ev
        self.module = module
        self._seen: set[tuple[str, int]] = set()

    def strings(self, expr: ast.AST, scope: Scope | None, module: Module | None = None) -> set[str]:
        module = module or self.module
        key = (module.path, id(expr))
        if key in self._seen:
            raise Unknown("cycle")
        self._seen.add(key)
        try:
            return self._strings(expr, scope, module)
        finally:
            self._seen.discard(key)

    def _product(self, parts: list[set[str]]) -> set[str]:
        out = {""}
        for part in parts:
            out = {a + b for a in out for b in part}
            if len(out) > _MAX_VALUES:
                raise Unknown("too many values")
        return out

    def _strings(self, expr: ast.AST, scope: Scope | None, module: Module) -> set[str]:
        s = self.strings
        if isinstance(expr, ast.Constant):
            return {"" if expr.value is None else str(expr.value)}
        if isinstance(expr, ast.JoinedStr):
            return self._product([
                {str(v.value)} if isinstance(v, ast.Constant) else s(v.value, scope, module)
                for v in expr.values
            ])
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, (ast.Add, ast.Mod)):
            left, right = s(expr.left, scope, module), s(expr.right, scope, module)
            joined = self._product([left, right]) if isinstance(expr.op, ast.Add) else set()
            return left | right | joined
        if isinstance(expr, (ast.Tuple, ast.List, ast.Set)):
            out: set[str] = set()
            for elt in expr.elts:
                out |= s(elt.value if isinstance(elt, ast.Starred) else elt, scope, module)
            return out
        if isinstance(expr, ast.Dict):
            out = set()
            for k, v in zip(expr.keys, expr.values):
                out |= s(v, scope, module) | (s(k, scope, module) if k is not None else set())
            return out
        if isinstance(expr, (ast.ListComp, ast.SetComp, ast.GeneratorExp)):
            return s(expr.elt, scope, module)
        if isinstance(expr, ast.DictComp):
            return s(expr.key, scope, module) | s(expr.value, scope, module)
        if isinstance(expr, ast.IfExp):
            return s(expr.body, scope, module) | s(expr.orelse, scope, module)
        if isinstance(expr, ast.Starred):
            return s(expr.value, scope, module)
        if isinstance(expr, ast.Call):
            func = expr.func
            name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")
            if expr.keywords and any(k.arg is None for k in expr.keywords):
                raise Unknown("call with **")
            args = [*expr.args, *[k.value for k in expr.keywords]]
            if isinstance(func, ast.Attribute) and name in ("join", "format"):
                out = s(func.value, scope, module)
                for arg in args:
                    out |= s(arg, scope, module)
                return out
            if name in _IDENTITY_WRAPPERS | _SEQUENCE_WRAPPERS | {"SQL"} and len(args) == 1:
                return s(args[0], scope, module)
            raise Unknown(f"call {name}")
        if isinstance(expr, ast.Name):
            return self._name(expr, scope, module)
        try:
            got = self.ev.values(expr, module, scope)
        except Unknown:
            raise Unknown(type(expr).__name__) from None
        if isinstance(got, Param):
            raise Unknown("parameter")
        return self._flatten(got)

    @staticmethod
    def _flatten(values: Iterable[Any]) -> set[str]:
        out: set[str] = set()
        for value in values:
            if isinstance(value, dict):
                out |= _SetFlow._flatten([*value.keys(), *value.values()])
            elif isinstance(value, (tuple, list, set, frozenset)):
                out |= _SetFlow._flatten(value)
            else:
                out.add("" if value is None else str(value))
        return out

    def _name(self, expr: ast.Name, scope: Scope | None, module: Module) -> set[str]:
        name = expr.id
        s = self.strings
        # a loop or comprehension of this scope binds it
        node: ast.AST = expr
        stop = scope.node if scope is not None else module.tree
        while node is not stop and node in module.parent:
            parent = module.parent[node]
            generators: list[ast.comprehension] = []
            if isinstance(parent, (ast.For, ast.AsyncFor)) and node in parent.body:
                generators = [ast.comprehension(target=parent.target, iter=parent.iter, ifs=[], is_async=0)]
            elif isinstance(parent, (ast.ListComp, ast.SetComp, ast.GeneratorExp, ast.DictComp)):
                generators = parent.generators
            for generator in generators:
                if not self.ev._binds(generator.target, name):
                    continue
                if not isinstance(generator.target, ast.Name):
                    raise Unknown("destructured loop variable")
                try:
                    return s(generator.iter, scope, module)
                except Unknown:
                    for test in generator.ifs:
                        if (
                            isinstance(test, ast.Compare) and isinstance(test.left, ast.Name)
                            and test.left.id == name and len(test.ops) == 1
                            and isinstance(test.ops[0], ast.In)
                        ):
                            return s(test.comparators[0], scope, module)
                    raise
            node = parent
        current = scope
        while current is not None:
            if name in current.params or name in self._star_params(current):
                raise Unknown(f"parameter {name}")
            assigned = Evaluator._local_assignments(current, name, augmented_ok=True)
            mutations = self._mutations(current, name, module)
            if assigned is not None or mutations:
                out: set[str] = set()
                for value in assigned or []:
                    out |= s(value, current, module)
                for value in mutations:
                    out |= s(value, current, module)
                return out
            current = current.parent
        if name in module.consts:
            value = module.consts[name]
            if value is None:
                raise Unknown("reassigned module constant")
            return s(value, None, module)
        imported = module.imports.get(name)
        if imported and imported[1] is not None:
            target = self.ev.module_for(imported[0])
            if target is not None and target.consts.get(imported[1]) is not None:
                return s(target.consts[imported[1]], None, target)
        raise Unknown(f"name {name}")

    @staticmethod
    def _star_params(scope: Scope) -> list[str]:
        args = getattr(scope.node, "args", None)
        if args is None:
            return []
        return [a.arg for a in (args.vararg, args.kwarg) if a is not None]

    def _mutations(self, scope: Scope, name: str, module: Module) -> list[ast.AST]:
        """The values a local ``name`` gains after its assignment: the arguments of
        its additive methods and of ``name += ...``. A store into ``name[...]`` is
        not enumerable."""
        found: list[ast.AST] = []
        stack = list(ast.iter_child_nodes(scope.node))
        while stack:
            node = stack.pop()
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef, ast.Lambda)):
                continue
            if (
                isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
                and isinstance(node.func.value, ast.Name) and node.func.value.id == name
                and node.func.attr in self._ADDITIVE
            ):
                args = node.args[1:] if node.func.attr == "insert" else node.args
                found += [*args, *[k.value for k in node.keywords]]
            elif (
                isinstance(node, ast.AugAssign) and isinstance(node.target, ast.Name)
                and node.target.id == name
            ):
                found.append(node.value)
            elif (
                isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store)
                and isinstance(node.value, ast.Name) and node.value.id == name
            ):
                raise Unknown(f"item store into {name}")
            stack.extend(ast.iter_child_nodes(node))
        return found


# ==================================================================== analysis
@dataclass
class Site:
    """One place that writes ``target`` and needs a decision."""

    target: str                 # "chunks" | "sources"
    path: str
    qual: str                   # enclosing function, or "<module>"
    kind: str                   # literal | dynamic | call | reference
    key: tuple
    detail: str
    pos: tuple[int, int]
    writes: list[Write] = field(default_factory=list)
    refused: bool = False


@dataclass
class Report:
    sites: list[Site]
    helpers: list[tuple[str, str, str, str]]   # (target, path, qual, parameter)
    violations: list[str]                        # always wrong, no list can excuse them
    copy_reason: str


def _normal(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    return re.split(r"\s*\.\s*", value.strip())[-1].strip("\"`[]").lower()


def _pos(node: ast.AST) -> tuple[int, int]:
    return (getattr(node, "lineno", 0), getattr(node, "col_offset", 0))


class Analysis:
    def __init__(self, tree: Mapping[str, str]) -> None:
        self.modules = {
            path: _module(path, text) for path, text in tree.items() if path.endswith(".py")
        }
        self.sql_files = {
            path: _sql_statements(text) for path, text in tree.items() if _is_scanned_sql(path)
        }
        self.ev = Evaluator(self.modules)
        self.sites: list[Site] = []
        self.violations: list[str] = []
        self.helpers: dict[tuple[str, str, str, str], Scope] = {}
        self._queue: list[tuple[str, Scope, str]] = []
        self._scopes_by_name: dict[str, list[Scope]] = {}
        for module in self.modules.values():
            for scope in module.scopes:
                self._scopes_by_name.setdefault(scope.name, []).append(scope)
        self._run()

    # ----------------------------------------------------------- positions
    # An anchor is ``(scope, node)``: a place in a function where a write happens.
    def _lift(self, module: Module, scope: Scope | None, node: ast.AST) -> list[tuple[Scope | None, ast.AST]]:
        """A write in a nested function happens where that function is called:
        every call of it in its enclosing function (transitively). A nested
        function that is never called there keeps its own anchor."""
        out: list[tuple[Scope | None, ast.AST]] = []
        work: list[tuple[Scope | None, ast.AST]] = [(scope, node)]
        seen: set[int] = set()
        while work:
            current, anchor = work.pop()
            calls = []
            if current is not None and current.parent is not None:
                calls = [
                    c for c in module.calls_by_name.get(current.name, [])
                    if isinstance(c.func, ast.Name) and self._within(module, c, current.parent)
                ]
            if not calls:
                out.append((current, anchor))
                continue
            for call in calls:
                if id(call) not in seen:
                    seen.add(id(call))
                    work.append((module.scope_of.get(call), call))
        return out

    @staticmethod
    def _within(module: Module, node: ast.AST, scope: Scope) -> bool:
        owner = module.scope_of.get(node)
        while owner is not None:
            if owner is scope:
                return True
            owner = owner.parent
        return False

    def _anchors(self, module: Module, scope: Scope | None, node: ast.AST) -> list[tuple[Scope | None, ast.AST]]:
        """Where ``node`` (a statement text or a builder call) is written: at the
        next use of the local name it is assigned to, else where it stands; then
        lifted out of nested functions to their call sites."""
        anchor = node
        current = node
        while current in module.parent and not isinstance(current, ast.stmt):
            current = module.parent[current]
        if (
            scope is not None and isinstance(current, ast.Assign) and len(current.targets) == 1
            and isinstance(current.targets[0], ast.Name)
        ):
            name = current.targets[0].id
            later = [
                n for n in module.name_loads.get(name, [])
                if module.scope_of.get(n) is scope and _pos(n) > _pos(current)
            ]
            if later:
                anchor = min(later, key=_pos)
        return self._lift(module, scope, anchor)

    @staticmethod
    def _block_of(module: Module, stmt: ast.stmt) -> tuple[tuple[int, str], int] | None:
        """``((id of the owning node, field), index)`` of ``stmt`` in its statement list."""
        parent = module.parent.get(stmt)
        if parent is None:
            return None
        for name, value in ast.iter_fields(parent):
            if isinstance(value, list):
                for index, item in enumerate(value):
                    if item is stmt:
                        return (id(parent), name), index
        return None

    def _statement_chain(self, module: Module, node: ast.AST, stop: ast.AST) -> dict[tuple[int, str], int]:
        """Every statement list that encloses ``node`` up to ``stop`` (the function),
        with the index of the statement in it that contains ``node``."""
        chain: dict[tuple[int, str], int] = {}
        current = node
        while current is not stop and current in module.parent:
            if isinstance(current, ast.stmt):
                block = self._block_of(module, current)
                if block is not None:
                    chain.setdefault(block[0], block[1])
            current = module.parent[current]
        return chain

    @staticmethod
    def _refusal_statement(module: Module, call: ast.Call) -> ast.stmt | None:
        """The statement a refusal call IS: the call must stand alone as an
        expression statement. Inside any expression (``flag and refuse(...)``,
        ``refuse(...) if flag else None``, a comprehension, a lambda) it may not
        run, so it refuses nothing."""
        parent = module.parent.get(call)
        return parent if isinstance(parent, ast.Expr) else None

    def _dominated(self, module: Module, scope: Scope | None, anchor: ast.AST) -> bool:
        """A refusal with non-constant arguments runs, in the same function, in a
        statement list that encloses the write, before the statement holding it.
        So a refusal inside a ``try`` / ``if`` / loop body the write is not in, in a
        nested function or in a lambda, does not count."""
        if scope is None:
            return False
        chain = self._statement_chain(module, anchor, scope.node)
        for call in module.calls_by_name.get(HELPER, []):
            if module.scope_of.get(call) is not scope:
                continue
            if any(
                isinstance(a, ast.Constant) and isinstance(a.value, str)
                for a in (*call.args, *[k.value for k in call.keywords])
            ):
                continue  # a refusal of a constant id refuses nothing this write writes
            stmt = self._refusal_statement(module, call)
            block = self._block_of(module, stmt) if stmt is not None else None
            if block is not None and block[0] in chain and block[1] < chain[block[0]]:
                return True
        return False

    def _refused(self, module: Module, anchors: list[tuple[Scope | None, ast.AST]]) -> bool:
        return bool(anchors) and all(self._dominated(module, s, a) for s, a in anchors)

    # ----------------------------------------------------------------- run
    def _run(self) -> None:
        for module in self.modules.values():
            updating_scopes = {
                id(s.scope) for s in module.statements
                if s.scope is not None and _UPSERT.search(s.text)
            }
            for statement in module.statements:
                for write in _writes(statement):
                    self._statement_write(module, statement, write, id(statement.scope) in updating_scopes)
        for path, statements in self.sql_files.items():
            for statement in statements:
                for write in _writes(statement):
                    self._sql_file_write(path, statement, write)
        seen: set[tuple[str, str, str, str]] = set()
        while self._queue:
            target, scope, param = self._queue.pop()
            key = (target, scope.module.path, scope.qual, param)
            if key in seen:
                continue
            seen.add(key)
            self._helper_call_sites(target, scope, param)

    def _uses_of_constant(self, module: Module, name: str) -> list[tuple[Module, ast.AST]]:
        uses: list[tuple[Module, ast.AST]] = [
            (module, n) for n in module.name_loads.get(name, [])
        ]
        uses += [
            (module, a) for a in module.attr_loads.get(name, [])
            if isinstance(a.value, ast.Name) and (a.value.id in ("self", "cls") or a.value.id[:1].isupper())
        ]
        dotted = self.ev
        for other in self.modules.values():
            if other is module:
                continue
            for alias, (base, imported) in other.imports.items():
                if imported == name and dotted.module_for(base) is module:
                    uses += [(other, n) for n in other.name_loads.get(alias, [])]
        return uses

    def _statement_write(self, module: Module, statement: Statement, write: Write, updating: bool) -> None:
        # a statement hoisted into a module/class constant is written where it is used
        places: list[tuple[Module, Scope | None, ast.AST]] = []
        root = statement.node
        const_name = module.const_assign_node.get(root)
        if const_name is not None and statement.scope is None:
            uses = self._uses_of_constant(module, const_name)
            places = [(m, m.scope_of.get(n), n) for m, n in uses]
        if not places:
            places = [(module, statement.scope, root)]
        for where, scope, node in places:
            self._place_write(where, scope, node, statement, write, updating)

    def _place_write(self, module, scope, node, statement, write, updating) -> None:
        qual = scope.qual if scope is not None else "<module>"
        anchors = self._anchors(module, scope, node)
        excerpt = " ".join(statement.text.split())[:90]
        # ---- sources: INSERT-only source_type
        if write.table == "sources":
            self._sources_literal(module, scope, qual, statement, write)
        # ---- chunks (and dynamic tables)
        if write.table is not None:
            if write.table == "chunks":
                self._chunk_move(module.path, qual, write, excerpt)
                self._add_site("chunks", module, scope, qual, "literal", (module.path, qual), excerpt, anchors, write)
            return
        self._dynamic(module, scope, qual, write, anchors, excerpt, updating)

    def _chunk_move(self, path: str, qual: str, write: Write, excerpt: str) -> None:
        if write.verb == "update" and (
            _assigns(write.segment, "source_id") or _assigns(write.segment, "notebook_id")
        ):
            self.violations.append(
                f"{path}::{qual} moves a chunk row to another source or notebook "
                f"(UPDATE chunks SET source_id/notebook_id): {excerpt}"
            )

    def _sql_file_write(self, path: str, statement: Statement, write: Write) -> None:
        """A write in a scanned ``.sql`` file: no function can refuse before it, so
        a ``chunks`` write must be on ``REVIEWED``; the ``sources`` rules apply as
        in Python."""
        holder = types.SimpleNamespace(path=path)
        excerpt = " ".join(statement.text.split())[:90]
        if write.table == "sources":
            self._sources_literal(holder, None, "<sql>", statement, write)
        elif write.table == "chunks":
            self._chunk_move(path, "<sql>", write, excerpt)
            self._add_site("chunks", holder, None, "<sql>", "sql file", (path, "<sql>"), excerpt, [], write)

    def _dynamic(self, module, scope, qual, write, anchors, excerpt, updating) -> None:
        targets = [t for t in ("chunks", "sources") if not write.table_pattern or _could_be(write.table_pattern, t)]
        if not targets:
            return
        is_updater = write.verb == "update" or write.upsert or updating or write.replace
        try:
            if write.table_expr is None or (write.table_pattern and not _PH_RE.fullmatch(write.table_pattern)):
                raise Unknown("no table expression")
            got = self.ev.values(write.table_expr, module, scope)
        except Unknown:
            got = None
        for target in targets:
            if target == "sources" and not is_updater:
                continue
            if got is None:
                self._add_site(target, module, scope, qual, "dynamic", (module.path, qual), excerpt, anchors, write)
            elif isinstance(got, Param):
                if target == "chunks" and self._refused(module, anchors):
                    continue
                self._queue.append((target, scope, got.name))
                self.helpers[(target, module.path, qual, got.name)] = scope
            elif target in {_normal(v) for v in got}:
                self._add_site(target, module, scope, qual, "dynamic", (module.path, qual), excerpt, anchors, write)

    def _add_site(self, target, module, scope, qual, kind, key, detail, anchors, write=None) -> None:
        refused = target == "chunks" and self._refused(module, anchors)
        pos = min((_pos(a) for _s, a in anchors), default=(0, 0))
        for site in self.sites:
            if site.target == target and site.key == key:
                site.refused = site.refused and refused
                if write is not None:
                    site.writes.append(write)
                return
        self.sites.append(Site(
            target=target, path=module.path, qual=qual, kind=kind, key=key, detail=detail,
            pos=pos, writes=[write] if write is not None else [], refused=refused,
        ))

    # ------------------------------------------------------ sources literal
    def _sources_literal(self, module, scope, qual, statement, write) -> None:
        where = f"{module.path}::{qual}"
        if write.replace:
            self.violations.append(f"{where} REPLACEs a sources row (rewrites source_type)")
            return
        segment = write.segment
        if not segment:
            return
        if _assigns(segment, "source_type"):
            self.violations.append(f"{where} writes sources.source_type outside an INSERT")
            return
        placeholders = [int(i) for i in _PH_RE.findall(segment)]
        if not placeholders:
            return
        # a composed SET list: every string that can FLOW into its interpolated
        # parts (not every string the function holds: a comparison or an error
        # message never reaches the SET list). A part fed by a value the guard
        # cannot enumerate (a parameter, ``**kwargs``, a mapping's keys) makes the
        # site a generic updater that must be listed.
        try:
            atoms: set[str] = set()
            for index in placeholders:
                expr = statement.exprs[index]
                if expr is None:
                    raise Unknown("no expression")
                atoms |= _SetFlow(self.ev, module).strings(expr, scope)
        except Unknown as exc:
            self._add_site(
                "sources", module, scope, qual, "set-list", (module.path, qual),
                f"SET list from a value the guard cannot enumerate ({exc})",
                [(scope, statement.node)],
            )
            return
        if any(re.search(r"\bsource_type\b", atom, re.I) for atom in atoms):
            self.violations.append(
                f"{where} builds the SET list of a sources UPDATE/upsert from pieces that "
                "include source_type"
            )

    # ------------------------------------------------------------- helpers
    def _resolves_to(self, module: Module, call_name: str, helper: Scope) -> bool:
        """A plain-name call/reference ``call_name`` in ``module`` means ``helper``."""
        if module is helper.module:
            return call_name == helper.name and call_name not in module.imports
        imported = module.imports.get(call_name)
        return bool(
            imported and imported[1] == helper.name
            and self.ev.module_for(imported[0]) is helper.module
        )

    def _helper_call_sites(self, target: str, helper: Scope, param: str) -> None:
        name = helper.name
        seats = {seat for seat, real in SEAM_ALIASES.items() if real == name}
        index = helper.params.index(param)
        for module in self.modules.values():
            candidates = [*module.calls_by_name.get(name, [])]
            for seat in seats:
                candidates += module.calls_by_name.get(seat, [])
            for alias, (_base, imported) in module.imports.items():
                if imported == name and alias != name:
                    candidates += module.calls_by_name.get(alias, [])
            for call in candidates:
                func = call.func
                through_seat = False
                if isinstance(func, ast.Name):
                    if not self._resolves_to(module, func.id, helper):
                        continue
                    offset = 0
                elif isinstance(func, ast.Attribute):
                    if func.attr == name:
                        if not helper.implicit_first and not (
                            helper.parent is None and "." in helper.qual
                        ):
                            # a module-level function reached through a module alias
                            base = func.value
                            imported = module.imports.get(base.id) if isinstance(base, ast.Name) else None
                            if not (imported and imported[1] is None and self.ev.module_for(imported[0]) is helper.module):
                                continue
                        offset = 1 if helper.implicit_first else 0
                    elif func.attr in seats:
                        through_seat, offset = True, 0
                    else:
                        continue
                else:
                    continue
                self._check_call(target, module, call, helper, param, index - offset, through_seat)
            # references that are not calls
            for node in module.name_loads.get(name, []):
                parent = module.parent.get(node)
                if isinstance(parent, ast.Call) and parent.func is node:
                    continue
                if not self._resolves_to(module, name, helper):
                    continue
                self._reference(target, module, node, name)
            if helper.implicit_first or (helper.parent is None and "." in helper.qual):
                for node in module.attr_loads.get(name, []):
                    parent = module.parent.get(node)
                    if isinstance(parent, ast.Call) and parent.func is node:
                        continue
                    self._reference(target, module, node, name)

    def _reference(self, target: str, module: Module, node: ast.AST, name: str) -> None:
        scope = module.scope_of.get(node)
        qual = scope.qual if scope is not None else "<module>"
        self._add_site(
            target, module, scope, qual, "reference", (module.path, qual, name, "<reference>"),
            f"{name} referenced, not called", [(scope, node)],
        )

    def _check_call(self, target, module, call, helper, param, index, through_seat) -> None:
        scope = module.scope_of.get(call)
        qual = scope.qual if scope is not None else "<module>"
        arg: ast.AST | None = None
        if any(isinstance(a, ast.Starred) for a in call.args) or any(k.arg is None for k in call.keywords):
            arg = None
        elif 0 <= index < len(call.args):
            arg = call.args[index]
        else:
            arg = next((k.value for k in call.keywords if k.arg == param), None)
            if arg is None:
                defaults = helper.node.args.defaults
                positional = [a.arg for a in (*helper.node.args.posonlyargs, *helper.node.args.args)]
                if param in positional:
                    offset = len(positional) - len(defaults)
                    at = positional.index(param)
                    if at >= offset:
                        arg = defaults[at - offset]
        callee = helper.name if not through_seat else getattr(call.func, "attr", helper.name)
        source = ast.unparse(arg) if arg is not None else "<unknown>"
        key = (module.path, qual, callee, source)
        anchors = self._anchors(module, scope, call)
        try:
            got = self.ev.values(arg, module if arg is not None else module, scope)
        except Unknown:
            got = None
        if got is None:
            if target == "chunks" and self._refused(module, anchors):
                return
            self._add_site(target, module, scope, qual, "call", key, f"{callee}({source})", anchors)
            return
        if isinstance(got, Param):
            if target == "chunks" and self._refused(module, anchors):
                return
            self._queue.append((target, scope, got.name))
            self.helpers[(target, module.path, qual, got.name)] = scope
            return
        if target in {_normal(v) for v in got}:
            self._add_site(target, module, scope, qual, "call", key, f"{callee}({source})", anchors)


# ============================================================== the copy path
COPY_GUARDED = "copy path, guarded by the copy snapshot predicate"
COPY_UNGUARDED = (
    "copy path, unguarded: the copy statement set used when the notebook holds a Memory "
    "source does not exclude Memory-derived chunk rows on both backends, so the copy "
    "carries every chunk row of the notebook"
)
COPY_BY_SNAPSHOT = "copy path (reason decided by the copy snapshot)"


def _statement_sets(namespace: Mapping[str, Any]) -> dict[str, tuple]:
    """Every module-level copy statement set: a sequence of ``(table, SELECT
    statement)`` string pairs that has a ``chunks`` entry. Found by value, not by
    name (a sequence of ``(table, WHERE fragment)`` pairs, such as the copy's
    validation extras, is not a statement set)."""
    out: dict[str, tuple] = {}
    for name, value in namespace.items():
        if not isinstance(value, (tuple, list)) or not value:
            continue
        if not all(
            isinstance(pair, tuple) and len(pair) == 2
            and all(isinstance(part, str) for part in pair)
            and re.match(r"\s*(?:WITH|SELECT)\b", pair[1], re.I)
            for pair in value
        ):
            continue
        if any(table == "chunks" for table, _query in value):
            out[name] = tuple(value)
    return out


def _is_memory_probe(test: ast.AST, namespace: Mapping[str, Any], memory_sql: Any) -> bool:
    """``test`` is exactly ``<conn>.execute(PROBE, ...).fetchone()``: true when the
    probe found a row. ``PROBE`` is a module-level string that carries the Memory
    source-type predicate ``memory_sql.memory_source_type_predicate(...)``. Any
    other shape (``... is None``, ``not ...``, a boolean combination) is not a
    probe, so the direction of the branch is never guessed."""
    sentinel = "COLUMN_SENTINEL"
    pattern = re.escape(memory_sql.memory_source_type_predicate(sentinel)).replace(
        sentinel, r"[\w.\"]+"
    )
    if not (
        isinstance(test, ast.Call) and not test.args and isinstance(test.func, ast.Attribute)
        and test.func.attr == "fetchone"
    ):
        return False
    execute = test.func.value
    if not (
        isinstance(execute, ast.Call) and isinstance(execute.func, ast.Attribute)
        and execute.func.attr == "execute" and execute.args
        and isinstance(execute.args[0], ast.Name)
    ):
        return False
    statement = namespace.get(execute.args[0].id)
    return isinstance(statement, str) and bool(re.search(pattern, statement))


def _returned_sets(tree: ast.Module, sets: Mapping[str, tuple]) -> list[tuple[ast.AST, str]]:
    """``(the node standing for the return, set name)`` for every function return of
    a statement set (``return S``, or either arm of ``return A if t else B``)."""
    out: list[tuple[ast.AST, str]] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Return) or node.value is None:
            continue
        value = node.value
        if isinstance(value, ast.Name) and value.id in sets:
            out.append((node, value.id))
        elif isinstance(value, ast.IfExp):
            for arm in (value.body, value.orelse):
                if isinstance(arm, ast.Name) and arm.id in sets:
                    out.append((arm, arm.id))
    return out


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    parent: dict[ast.AST, ast.AST] = {}
    for node in ast.walk(tree):
        for child in ast.iter_child_nodes(node):
            parent[child] = node
    return parent


def _returns(stmt: ast.stmt) -> bool:
    return isinstance(stmt, (ast.Return, ast.Raise))


def _under_no_memory(node: ast.AST, parent: Mapping[ast.AST, ast.AST], is_probe: Callable[[ast.AST], bool]) -> bool:
    """``node`` is reached only when a Memory probe found nothing: it is the
    ``else`` arm of ``A if <probe> else B``, in the ``else`` of ``if <probe>:``, or
    after an ``if <probe>: ... return`` in the same statement list."""
    current = node
    while current in parent:
        above = parent[current]
        if isinstance(above, ast.IfExp) and current is above.orelse and is_probe(above.test):
            return True
        if isinstance(above, ast.If) and current in above.orelse and is_probe(above.test):
            return True
        for _name, value in ast.iter_fields(above):
            if isinstance(value, list) and any(item is current for item in value):
                index = next(i for i, item in enumerate(value) if item is current)
                for earlier in value[:index]:
                    if (
                        isinstance(earlier, ast.If) and not earlier.orelse and earlier.body
                        and _returns(earlier.body[-1]) and is_probe(earlier.test)
                    ):
                        return True
        if isinstance(above, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
            return False
        current = above
    return False


def _top_level_where_conjuncts(query: str) -> list[str] | None:
    """The conjuncts of the outermost WHERE clause of ``query`` (split at ``AND``
    outside parentheses and quotes, up to a top-level ORDER/GROUP BY, LIMIT,
    HAVING or the end); ``None`` when there is no such WHERE or it has a top-level
    ``OR`` (then no conjunct is guaranteed to hold for every row)."""
    depth, quoted, i = 0, False, 0
    upper = query.upper()
    where = end = None
    splits: list[tuple[int, int]] = []
    has_or = False
    while i < len(query):
        ch = query[i]
        if quoted:
            if ch == "'":
                quoted = False
        elif ch == "'":
            quoted = True
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
        elif depth == 0 and (i == 0 or not (query[i - 1].isalnum() or query[i - 1] == "_")):
            for word in ("WHERE", "AND", "OR", "ORDER", "GROUP", "LIMIT", "HAVING"):
                stop = i + len(word)
                if upper.startswith(word, i) and (stop == len(query) or not (query[stop].isalnum() or query[stop] == "_")):
                    if word == "WHERE" and where is None:
                        where = stop
                    elif where is not None and end is None:
                        if word == "AND":
                            splits.append((i, stop))
                        elif word == "OR":
                            has_or = True
                        elif word in ("ORDER", "GROUP", "LIMIT", "HAVING"):
                            end = i
                    break
        i += 1
    if where is None or has_or:
        return None
    end = len(query) if end is None else end
    parts, cursor = [], where
    for a, b in splits:
        if a < end:
            parts.append(query[cursor:a])
            cursor = b
    parts.append(query[cursor:end])
    return [" ".join(part.split()) for part in parts]


def _chunks_query_excludes_memory(queries: tuple, memory_sql: Any) -> bool:
    """The ``chunks`` query's outermost WHERE clause has ``NOT <p>(alias)`` as an
    ``AND``-conjunct (not merely somewhere in its text: ``... OR NOT p`` excludes
    nothing) for a Memory-derived predicate ``p`` of ``memory_sql``
    (``memory_derived_object``, ``memory_derived_in_notebook``, any
    ``memory_derived_*`` of that module), ``alias`` being the query's own alias of
    ``chunks``."""
    predicates = [
        getattr(memory_sql, name) for name in dir(memory_sql)
        if name.startswith("memory_derived") and callable(getattr(memory_sql, name))
    ]
    for table, query in queries:
        if table != "chunks":
            continue
        match = re.search(r"\bFROM\s+chunks(?:\s+(?:AS\s+)?(?!WHERE\b|ORDER\b)(\w+))?", query, re.I)
        alias = (match.group(1) if match else None) or "chunks"
        conjuncts = _top_level_where_conjuncts(query)
        if not conjuncts:
            return False
        for predicate in predicates:
            try:
                text = " ".join(predicate(alias).split())
            except Exception:  # noqa: BLE001 -- a predicate of another shape
                continue
            if f"NOT {text}" in conjuncts or f"NOT ({text})" in conjuncts:
                return True
        return False
    return False


def _projects_table_names(node: ast.Name, parent: Mapping[ast.AST, ast.AST]) -> bool:
    """``node`` is the iterable of ``t for t, _q in S`` whose element is the table
    name alone."""
    generator = parent.get(node)
    if not (isinstance(generator, ast.comprehension) and generator.iter is node):
        return False
    comprehension = parent.get(generator)
    target = generator.target
    return (
        isinstance(comprehension, (ast.GeneratorExp, ast.ListComp, ast.SetComp))
        and len(comprehension.generators) == 1 and not generator.ifs
        and isinstance(target, ast.Tuple) and len(target.elts) == 2
        and isinstance(target.elts[0], ast.Name) and isinstance(comprehension.elt, ast.Name)
        and comprehension.elt.id == target.elts[0].id
    )


def _builds_another_set(node: ast.Name, parent: Mapping[ast.AST, ast.AST], sets: Mapping[str, tuple]) -> bool:
    """``node`` stands in the value of a module-level assignment whose target is
    another statement set."""
    current: ast.AST = node
    while current in parent and not isinstance(current, ast.stmt):
        current = parent[current]
    targets: list[ast.AST] = []
    if isinstance(current, ast.Assign):
        targets = current.targets
    elif isinstance(current, ast.AnnAssign):
        targets = [current.target]
    return (
        isinstance(parent.get(current), ast.Module) and len(targets) == 1
        and isinstance(targets[0], ast.Name) and targets[0].id in sets
        and targets[0].id != node.id
    )


@functools.lru_cache(maxsize=None)
def _copy_snapshot_excludes_memory(backend: str, path: str, text: str) -> bool:
    """The chunks copy statement set that is used when the notebook holds a Memory
    source carries a ``memory_sql`` Memory-derived predicate.

    Both shapes of ``sharing_store.py`` are recognised, by value and control flow,
    not by variable name: ONE statement set used for every copy (it must carry the
    predicate), or several sets chosen per copy, where a set may lack the predicate
    only if it is returned solely when a Memory probe (a statement carrying
    ``memory_sql.memory_source_type_predicate(...)``) found nothing -- E5-1's
    ``_copy_queries``: ``_MEMORY_COPY_SNAPSHOT_QUERIES`` when ``_COPY_DIRTY_SQL``
    finds a row, ``_COPY_SNAPSHOT_QUERIES`` otherwise -- and that is its only road
    to a copy: it is otherwise used only through a constant index whose entry is
    not ``chunks`` (the root row, ``[0]``), or at module level as a projection to
    its table names or to construct another statement set. The probe must be the
    positive ``<conn>.execute(PROBE, ...).fetchone()`` (``_is_memory_probe``), and
    the predicate an ``AND``-conjunct of the WHERE clause
    (``_chunks_query_excludes_memory``)."""
    name = f"_memory_chunk_write_guard_copy_probe_{backend}"
    module = types.ModuleType(name)
    module.__file__ = str(ROOT / path)
    sys.modules[name] = module
    try:
        exec(compile(text, path, "exec"), module.__dict__)
    finally:
        sys.modules.pop(name, None)
    namespace = module.__dict__
    memory_sql = namespace.get("memory_sql")
    expected = f"app.repositories.{backend}.memory_sql"
    if getattr(memory_sql, "__name__", None) != expected:
        memory_sql = importlib.import_module(expected)
    sets = _statement_sets(namespace)
    if not sets:
        return False
    tree = ast.parse(text)
    parent = _parents(tree)

    def is_probe(test: ast.AST) -> bool:
        return _is_memory_probe(test, namespace, memory_sql)

    returned = _returned_sets(tree, sets)
    no_memory_only = {set_name for _node, set_name in returned}
    for node, set_name in returned:
        if not _under_no_memory(node, parent, is_probe):
            no_memory_only.discard(set_name)
    # A set exempted that way must not reach a copy by any other road. Besides
    # those returns it may only be indexed by a constant for an entry other than
    # ``chunks`` (the root row, ``S[0]``), projected to its table names at module
    # level (``frozenset(t for t, _q in S)``), or used at module level to construct
    # another statement set (``_MEMORY_... = tuple(... for t, q in S)``, which is
    # judged on its own). Any other use (a fetch loop over ``S[1:]``, a module dict
    # built from it) revokes the exemption.
    returned_names = {
        id(node.value if isinstance(node, ast.Return) else node) for node, _name in returned
    }
    for node in ast.walk(tree):
        if not (isinstance(node, ast.Name) and node.id in no_memory_only):
            continue
        if id(node) in returned_names or not isinstance(node.ctx, ast.Load):
            continue
        above, in_function = node, False
        while above in parent:
            above = parent[above]
            if isinstance(above, (ast.FunctionDef, ast.AsyncFunctionDef, ast.Lambda)):
                in_function = True
                break
        if not in_function and (
            _projects_table_names(node, parent) or _builds_another_set(node, parent, sets)
        ):
            continue
        holder = parent.get(node)
        if (
            isinstance(holder, ast.Subscript) and holder.value is node
            and isinstance(holder.slice, ast.Constant) and isinstance(holder.slice.value, int)
            and -len(sets[node.id]) <= holder.slice.value < len(sets[node.id])
            and sets[node.id][holder.slice.value][0] != "chunks"
        ):
            continue
        no_memory_only.discard(node.id)
    used_with_memory = [set_name for set_name in sets if set_name not in no_memory_only]
    return bool(used_with_memory) and all(
        _chunks_query_excludes_memory(sets[set_name], memory_sql) for set_name in used_with_memory
    )


def copy_path_reason(tree: Mapping[str, str]) -> str:
    """``COPY_GUARDED`` when, on BOTH backends, the chunks copy statement set used
    when the notebook holds a Memory source excludes Memory-derived chunk rows
    (``_copy_snapshot_excludes_memory``), else ``COPY_UNGUARDED``."""
    for backend in ("sqlite", "postgres"):
        path = f"backend/app/repositories/{backend}/sharing_store.py"
        text = tree.get(path)
        if text is None or not _copy_snapshot_excludes_memory(backend, path, text):
            return COPY_UNGUARDED
    return COPY_GUARDED


# ============================================================ the reviewed list
MOVES = "moves existing rows only"
CHANGES = "changes existing rows only"
SCRATCH_SCRIPT = "seeds a scratch database the script is pointed at, never a library in use"
SEAM = "wires the helper into a late-bound seat whose calls are checked through SEAM_ALIASES"

_MIRROR_PATHS = re.compile(
    r"^(backend/app/migration/(shadow/[^/]+\.py|sqlite_to_postgres\.py)|scripts/merge_dbs\.py)$"
)
_UPDATE_ONLY_PATHS = re.compile(
    r"^backend/app/repositories/(sqlite|postgres)/(maintenance|chunk_store)\.py$"
)
_COPY_PATHS = re.compile(r"^backend/app/services/notebook_sharing\.py$")
_SCRATCH_SCRIPT_PATHS = re.compile(
    r"^scripts/(bench_scale_build_paging|generate_repository_contract_fixtures"
    r"|measure_sync_capture_cost)\.py$"
)
_SEAM_PATHS = re.compile(
    r"^backend/app/(repositories/(sqlite|postgres)/bundle|services/repository_facade)\.py$"
)
_PLACEMENT = {
    MOVES: _MIRROR_PATHS,
    CHANGES: _UPDATE_ONLY_PATHS,
    COPY_GUARDED: _COPY_PATHS,
    COPY_UNGUARDED: _COPY_PATHS,
    COPY_BY_SNAPSHOT: _COPY_PATHS,
    SCRATCH_SCRIPT: _SCRATCH_SCRIPT_PATHS,
    SEAM: _SEAM_PATHS,
}

_SQ, _PG = "backend/app/repositories/sqlite", "backend/app/repositories/postgres"

# A key is ``(path, function)`` for a statement in that function, or
# ``(path, function, callee, table argument as written)`` for one call of a table
# helper (``"<reference>"`` for a reference that is not a call).
REVIEWED: dict[tuple, tuple[str, str]] = {
    # -- changes existing rows only: UPDATE of chunks that already exist, never
    #    of source_id / notebook_id (checked) --------------------------------------
    (f"{_SQ}/maintenance.py", "SQLiteMaintenanceAdapter.apply_image_backfill"):
        (CHANGES, "UPDATE chunks SET element_ids for chunks that already exist"),
    (f"{_PG}/maintenance.py", "PostgresMaintenanceAdapter.apply_image_backfill"):
        (CHANGES, "UPDATE chunks SET element_ids for chunks that already exist"),
    (f"{_SQ}/chunk_store.py", "ChunkStore.replace_chunk_questions"):
        (CHANGES, "UPDATE chunks SET question_indexed_at on an existing chunk"),
    (f"{_PG}/chunk_store.py", "ChunkStore.replace_chunk_questions"):
        (CHANGES, "UPDATE chunks SET question_indexed_at on an existing chunk"),
    # -- moves existing rows only: whole-database mirrors of a database at this
    #    schema version (its migration removes a Memory source's passages) --------
    ("backend/app/migration/sqlite_to_postgres.py", "copy_snapshot_to_postgres", "_copy_table", "table"):
        (MOVES, "COPY of the SQLite snapshot, upgraded to the paired schema first, into the empty PostgreSQL target"),
    ("backend/app/migration/shadow/bulk_copy.py", "_copy_batch"):
        (MOVES, "batch copy of the primary's rows into the shadow"),
    ("backend/app/migration/shadow/replicator.py", "ShadowReplicator._apply_statement"):
        (MOVES, "replays the primary's own change onto the shadow"),
    ("backend/app/migration/shadow/replicator.py", "ShadowReplicator._park_unique_cycles"):
        (MOVES, "re-keys rows the shadow already holds"),
    ("scripts/merge_dbs.py", "merge_core", "_run", "t"):
        (MOVES, "offline merge of two libraries: both inputs are first migrated to this "
                "schema (migrate_to_current), then the secondary's notebooks are copied "
                "verbatim by plain INSERT ... SELECT; a sources row is only ever inserted"),
    # -- the copy path: reason decided by the copy snapshot ------------------------
    ("backend/app/services/notebook_sharing.py", "NotebookCopyService.copy_notebook",
     "insert_copy_rows", "'chunks'"):
        (COPY_BY_SNAPSHOT, "insert_copy_rows(\"chunks\", ...) of the notebook copy"),
    # -- late-bound seats: calls through SharingStore.insert_row are checked as calls
    #    of insert_row_values (SEAM_ALIASES) -----------------------------------------
    (f"{_SQ}/bundle.py", "SqlitePersistenceBundleFactory.create", "insert_row_values", "<reference>"):
        (SEAM, "insert_row=SharingStore.insert_row_values"),
    (f"{_PG}/bundle.py", "PostgresPersistenceBundleFactory.create", "insert_row_values", "<reference>"):
        (SEAM, "insert_row=SharingStore.insert_row_values"),
    ("backend/app/services/repository_facade.py", "RepositoryFacade.__init__", "_insert_row", "table"):
        (SEAM, "insert_row=lambda db, table, data: self._insert_row(db, table, data)"),
    # -- scripts that seed a scratch / fixture database ------------------------------
    ("scripts/bench_scale_build_paging.py", "_seed_notebook"):
        (SCRATCH_SCRIPT, "benchmark seed into the dedicated BENCH_POSTGRES_URL database"),
    ("scripts/generate_repository_contract_fixtures.py", "_seed_ask_repository"):
        (SCRATCH_SCRIPT, "contract fixture database the generator builds"),
    ("scripts/generate_repository_contract_fixtures.py", "_seed_v9_rows"):
        (SCRATCH_SCRIPT, "frozen repository_v9 fixture database, rebuilt only on --rebaseline"),
    ("scripts/measure_sync_capture_cost.py", "_measure_sqlite.run_once"):
        (SCRATCH_SCRIPT, "trigger-cost measurement on a throwaway database"),
    ("scripts/measure_sync_capture_cost.py", "_measure_postgres.run_once"):
        (SCRATCH_SCRIPT, "trigger-cost measurement on a throwaway database"),
}

# Writers whose table can be ``sources`` and that could update an existing row.
_MIRRORS = "mirrors a database that holds the invariant itself; rows are copied verbatim"
REFUSED_AT_RUN_TIME = "refused at run time"
GENERIC_UPDATERS: dict[tuple, tuple[str, str]] = {
    ("backend/app/migration/sync/import_.py", "_apply_rows", "_apply_table", "table"):
        (REFUSED_AT_RUN_TIME, "_apply_table"),
    ("backend/app/migration/shadow/replicator.py", "ShadowReplicator._apply_statement"):
        (_MIRRORS, ""),
    ("backend/app/migration/shadow/replicator.py", "ShadowReplicator._park_unique_cycles"):
        (_MIRRORS, ""),
}


def _unhandled_site_message(site: Site) -> str:
    return (
        f"{site.path}::{site.qual} writes the chunks table ({site.kind}: {site.detail}) and "
        f"neither calls {HELPER}(...) before the write nor is on REVIEWED in "
        f"{Path(__file__).name}. A chunk row must never belong to a Memory source: call "
        f"{HELPER}(connection, source_id) (the ChunkStore helper, or the module-level copy "
        "in the same file) on the write transaction's own connection before the first "
        "write, or add the site to REVIEWED with the reason it needs none."
    )


def _unlisted_updater_message(site: Site) -> str:
    return (
        f"{site.path}::{site.qual} updates a row of a table that can be sources "
        f"({site.kind}: {site.detail}), which could rewrite sources.source_type: "
        "sources.source_type must never change after insert (a chunk probe cannot protect "
        "against a later change). Prove the table is never sources or list the site in "
        f"GENERIC_UPDATERS in {Path(__file__).name}."
    )


def guard_report(
    tree: Mapping[str, str],
    *,
    reviewed: Mapping[tuple, tuple[str, str]] | None = None,
    generic_updaters: Mapping[tuple, tuple[str, str]] | None = None,
) -> tuple[Report, list[str]]:
    reviewed = REVIEWED if reviewed is None else reviewed
    generic_updaters = GENERIC_UPDATERS if generic_updaters is None else generic_updaters
    analysis = Analysis(tree)
    copy_reason = copy_path_reason(tree)
    problems = list(analysis.violations)
    chunk_sites = [s for s in analysis.sites if s.target == "chunks"]
    source_sites = [s for s in analysis.sites if s.target == "sources"]
    for site in chunk_sites:
        if not site.refused and site.key not in reviewed:
            problems.append(_unhandled_site_message(site))
    for site in source_sites:
        if site.key not in generic_updaters:
            problems.append(_unlisted_updater_message(site))
    by_key = {s.key: s for s in chunk_sites}
    for key, (reason, detail) in sorted(reviewed.items()):
        where = "::".join(str(k) for k in key)
        site = by_key.get(key)
        if site is None:
            problems.append(f"{where} is on REVIEWED but is no longer a chunk write site")
            continue
        if site.refused:
            problems.append(f"{where} is on REVIEWED but refuses itself: drop the entry")
        if reason not in _PLACEMENT:
            problems.append(f"{where}: unknown reason {reason!r}")
            continue
        if not _PLACEMENT[reason].match(site.path):
            problems.append(f"{where}: reason {reason!r} is not allowed for this file")
        if reason == CHANGES and (
            not site.writes or any(w.verb != "update" for w in site.writes)
        ):
            problems.append(f"{where}: {CHANGES} but it writes chunks other than by UPDATE")
        if reason == COPY_GUARDED and copy_reason != COPY_GUARDED:
            problems.append(
                f"{where}: {COPY_GUARDED!r}, but the chunks copy statement set used when the "
                "notebook holds a Memory source does not carry the Memory predicate on both backends"
            )
        if reason == COPY_UNGUARDED and copy_reason == COPY_GUARDED:
            problems.append(
                f"{where}: {COPY_UNGUARDED!r}, but the copy snapshot now excludes Memory rows"
            )
        if reason == SEAM and not _is_seam_wiring(analysis, site):
            problems.append(f"{where}: {SEAM!r} but the reference does not fill a seat of SEAM_ALIASES")
    updater_keys = {s.key for s in source_sites}
    for key, (reason, detail) in sorted(generic_updaters.items()):
        where = "::".join(str(k) for k in key)
        if key not in updater_keys:
            problems.append(f"{where} is on GENERIC_UPDATERS but no longer updates a table that can be sources")
            continue
        if reason == _MIRRORS and not _MIRROR_PATHS.match(key[0]):
            problems.append(f"{where}: {_MIRRORS!r} is only for the migration mirrors")
        elif reason == REFUSED_AT_RUN_TIME:
            callee = _find_scope(analysis, key[0], detail)
            if callee is None or not any(
                (callee.module.scope_of.get(c) is not None)
                and callee.module.scope_of[c].outer == callee.outer
                for c in callee.module.calls_by_name.get(HELPER, [])
            ):
                problems.append(f"{where}: {detail!r} does not call {HELPER}")
        elif reason not in (_MIRRORS, REFUSED_AT_RUN_TIME):
            problems.append(f"{where}: unknown reason {reason!r}")
    report = Report(
        sites=analysis.sites,
        helpers=sorted(analysis.helpers),
        violations=analysis.violations,
        copy_reason=copy_reason,
    )
    return report, problems


def _find_scope(analysis: Analysis, path: str, qual: str) -> Scope | None:
    module = analysis.modules.get(path)
    if module is None:
        return None
    return next((s for s in module.scopes if s.qual == qual), None)


def _is_seam_wiring(analysis: Analysis, site: Site) -> bool:
    """The reference (or the call inside a lambda) fills a seat of ``SEAM_ALIASES``:
    it is the value of ``<seat>=`` or the argument of ``bind_<seat>(...)``."""
    module = analysis.modules[site.path]
    name = site.key[2]
    nodes = [*module.name_loads.get(name, []), *module.attr_loads.get(name, [])]
    nodes += module.calls_by_name.get(name, [])
    binders = {f"bind_{seat}" for seat in SEAM_ALIASES}
    for node in nodes:
        if _pos(node) != site.pos:
            continue
        current = node
        while current in module.parent:
            parent = module.parent[current]
            if isinstance(parent, ast.keyword) and parent.arg in SEAM_ALIASES:
                return True
            if isinstance(parent, ast.Call) and getattr(parent.func, "attr", "") in binders:
                return True
            if isinstance(parent, (ast.Lambda, ast.Attribute, ast.Call)) or parent is current:
                current = parent
                continue
            break
    return False


def guard_problems(
    tree: Mapping[str, str],
    *,
    reviewed: Mapping[tuple, tuple[str, str]] | None = None,
    generic_updaters: Mapping[tuple, tuple[str, str]] | None = None,
) -> list[str]:
    """THE entry point: every problem of ``tree`` (a ``{path: source}`` mapping)."""
    return guard_report(tree, reviewed=reviewed, generic_updaters=generic_updaters)[1]


@functools.lru_cache(maxsize=1)
def _repo_result() -> tuple[Report, list[str]]:
    return guard_report(repo_tree())


# ======================================================================= tests
def test_the_repository_passes_the_guard():
    problems = _repo_result()[1]
    assert not problems, "\n".join(problems)


def test_the_expected_paths_refuse_themselves():
    """The paths this task guards are write sites that refuse BEFORE the write (an
    accidental move of the call, or a list entry, would otherwise hide them)."""
    report = _repo_result()[0]
    refused = {(s.path, s.qual) for s in report.sites if s.target == "chunks" and s.refused}
    for backend in ("sqlite", "postgres"):
        base = f"backend/app/repositories/{backend}"
        for expected in (
            (f"{base}/chunk_store.py", "ChunkStore.replace_source_chunks"),
            (f"{base}/chunk_store.py", "ChunkStore.insert_rows"),
            (f"{base}/kg_build_job_store.py", "KgBuildJobStore.publish_indexing_pipeline_success"),
        ):
            assert expected in refused, expected
    # the transfer and the import write through table helpers, refused at the call site
    helpers = {(target, path, qual) for target, path, qual, _param in report.helpers}
    for backend in ("sqlite", "postgres"):
        assert ("chunks", f"backend/app/repositories/{backend}/knowhow_transfer_store.py",
                "_insert_rows") in helpers
    assert ("chunks", "backend/app/migration/sync/import_.py", "_upsert_statement") in helpers


def test_copy_path_reason_follows_the_copy_snapshot():
    tree = repo_tree()
    carries = all(
        _copy_snapshot_excludes_memory(
            backend, f"backend/app/repositories/{backend}/sharing_store.py",
            tree[f"backend/app/repositories/{backend}/sharing_store.py"],
        )
        for backend in ("sqlite", "postgres")
    )
    assert _repo_result()[0].copy_reason == (COPY_GUARDED if carries else COPY_UNGUARDED)


# ---------------------------------------------------------------- controls
def _edit(path: str, old: str | None, new: str) -> Callable[[Mapping[str, str]], dict[str, str]]:
    """Append ``new`` (``old is None``) or replace the first ``old`` by ``new``."""

    def apply(tree: Mapping[str, str]) -> dict[str, str]:
        text = tree[path]
        if old is None:
            return {path: text + "\n\n" + new + "\n"}
        assert old in text, f"control out of date: {old!r} not in {path}"
        return {path: text.replace(old, new, 1)}

    return apply


def _both(*edits: Callable) -> Callable:
    def apply(tree: Mapping[str, str]) -> dict[str, str]:
        out: dict[str, str] = {}
        for edit in edits:
            out.update(edit(overlay(tree, out)))
        return out

    return apply


_SQ_KH = f"{_SQ}/knowhow_transfer_store.py"
_PG_CS = f"{_PG}/chunk_store.py"
_SQ_CS = f"{_SQ}/chunk_store.py"
_PG_SS = f"{_PG}/source_store.py"
_SQ_MAINT = f"{_SQ}/maintenance.py"
_IMPORT = "backend/app/migration/sync/import_.py"

_PG_INSERT_ROWS_REFUSAL = "        self._refuse_memory_source(connection, source_id)\n"

# name -> (edit, a fragment the problem list must contain)
NEGATIVE_CONTROLS: dict[str, tuple[Callable, str]] = {
    # spec review
    "ma new caller of the Knowhow row helper writes chunks": (
        _edit(_SQ_KH, None, 'def restore_chunks_unrefused(db, rows):\n    _insert_rows(db, "chunks", rows)'),
        "restore_chunks_unrefused",
    ),
    "mb a CHANGES entry moves a chunk to another source": (
        _edit(_SQ_CS, '"UPDATE chunks SET question_indexed_at=? WHERE id=?"',
              '"UPDATE chunks SET question_indexed_at=?, source_id=? WHERE id=?"'),
        "UPDATE chunks SET source_id/notebook_id",
    ),
    "mb2 a CHANGES entry moves a chunk to another notebook": (
        _edit(_SQ_CS, '"UPDATE chunks SET question_indexed_at=? WHERE id=?"',
              '"UPDATE chunks SET notebook_id=?, question_indexed_at=? WHERE id=?"'),
        "UPDATE chunks SET source_id/notebook_id",
    ),
    "md side-load of chunks through the import's upsert builder": (
        _edit(_IMPORT, None,
              "def _side_load_chunks(backend, conn, columns, rows):\n"
              "    _executemany(backend, conn, _upsert_statement(\"chunks\", columns, [\"id\"], "
              "columns, seed_only=False, is_postgres=False), rows)"),
        "_side_load_chunks",
    ),
    # quality review
    "T1 another module borrows the Knowhow row helper": (
        _edit(_SQ_MAINT, None,
              "def reseed_chunks(db, rows):\n"
              "    from app.repositories.sqlite.knowhow_transfer_store import _insert_rows\n"
              "    _insert_rows(db, 'chunks', rows)"),
        "reseed_chunks",
    ),
    "T2 same module, new function, no refusal": (
        _edit(_SQ_KH, None, "def legacy_import(db, rows):\n    _insert_rows(db, 'chunks', rows)"),
        "legacy_import",
    ),
    "T3 a reviewed function is renamed": (
        _edit(_SQ_MAINT, "def apply_image_backfill(", "def apply_image_backfill_v2("),
        "apply_image_backfill is on REVIEWED but is no longer",
    ),
    "T4 f-string table name from a module constant": (
        _edit(_SQ_MAINT, None,
              "CHUNKS_TABLE = 'chunks'\ndef put(db, cid):\n"
              "    db.execute(f'INSERT INTO {CHUNKS_TABLE} (id) VALUES (?)', (cid,))"),
        "::put writes the chunks table",
    ),
    "T5a psycopg COPY chunks": (
        _edit(_PG_CS, None,
              "def bulk(conn, rows):\n"
              "    with conn.cursor().copy('COPY chunks (id,notebook_id,source_id,text) FROM STDIN') as cp:\n"
              "        for r in rows:\n            cp.write_row(r)"),
        "::bulk writes the chunks table",
    ),
    "T5b psycopg COPY public.chunks": (
        _edit(_PG_CS, None,
              "def bulk(conn, rows):\n"
              "    with conn.cursor().copy('COPY public.chunks (id,notebook_id,source_id,text) FROM STDIN') as cp:\n"
              "        for r in rows:\n            cp.write_row(r)"),
        "::bulk writes the chunks table",
    ),
    "T5c INSERT INTO public.chunks": (
        _edit(_PG_CS, None,
              "def put(conn, r):\n    conn.execute('INSERT INTO public.chunks (id) VALUES (%s)', (r,))"),
        "::put writes the chunks table",
    ),
    "T6 executemany of a fresh INSERT": (
        _edit(_SQ_MAINT, None,
              "def put_many(db, rows):\n"
              "    db.executemany('INSERT INTO chunks(id,notebook_id,source_id,text) VALUES (?,?,?,?)', rows)"),
        "::put_many writes the chunks table",
    ),
    "T7 sources upsert with a SET list composed from a column tuple": (
        _edit(_PG_SS, None,
              "_UPSERT_COLS = ('id','title','source_type')\n"
              "def upsert_source(conn, row):\n"
              "    cols = ','.join(_UPSERT_COLS)\n"
              "    sets = ','.join(f'{c}=excluded.{c}' for c in _UPSERT_COLS if c != 'id')\n"
              "    conn.execute(f'INSERT INTO sources ({cols}) VALUES (%s,%s,%s) "
              "ON CONFLICT (id) DO UPDATE SET {sets}', row)"),
        "upsert_source builds the SET list",
    ),
    "T8 UPDATE sources AS s SET source_type": (
        _edit(_PG_SS, None,
              "def retype(conn, sid, t):\n"
              "    conn.execute('UPDATE sources AS s SET source_type=%s WHERE s.id=%s', (t, sid))"),
        "::retype writes sources.source_type",
    ),
    "T9 row-constructor SET of source_type": (
        _edit(_PG_SS, None,
              "def retype(conn, sid, t):\n"
              "    conn.execute('UPDATE sources SET (title, source_type) = (%s, %s) WHERE id=%s', ('x', t, sid))"),
        "::retype writes sources.source_type",
    ),
    "T10a refusal moved after the write": (
        _both(
            _edit(_PG_CS, _PG_INSERT_ROWS_REFUSAL + "        execute_many(", "        execute_many("),
            _edit(_PG_CS, "        self._insert_chunk_element_rows(connection, notebook_id, rows)\n\n    def source_chunks",
                  "        self._insert_chunk_element_rows(connection, notebook_id, rows)\n"
                  "        self._refuse_memory_source(connection, source_id)\n\n    def source_chunks"),
        ),
        "ChunkStore.insert_rows writes the chunks table",
    ),
    "T10b refusal of an unrelated constant id": (
        _edit(_PG_CS, _PG_INSERT_ROWS_REFUSAL + "        execute_many(",
              "        self._refuse_memory_source(connection, 'unrelated-id')\n        execute_many("),
        "ChunkStore.insert_rows writes the chunks table",
    ),
    # the implementer's own static mutations, kept
    "UPDATE sources SET source_type": (
        _edit(_PG_SS, None,
              "def retype(conn, sid, t):\n    conn.execute('UPDATE sources SET source_type=%s WHERE id=%s', (t, sid))"),
        "::retype writes sources.source_type",
    ),
    "INSERT OR REPLACE INTO sources": (
        _edit(_SQ_MAINT, None,
              "def put_source(db, row):\n    db.execute('INSERT OR REPLACE INTO sources (id) VALUES (?)', row)"),
        "::put_source REPLACEs a sources row",
    ),
    "import refusal dropped": (
        _edit(_IMPORT, "        _refuse_memory_source(backend, conn, table, kept)\n", ""),
        "_apply_rows writes the chunks table",
    ),
    "KG publish refusal dropped (SQLite)": (
        _edit(f"{_SQ}/kg_build_job_store.py", "            self._refuse_memory_source(db, notebook_id, snapshot)\n", ""),
        "KgBuildJobStore.publish_indexing_pipeline_success writes the chunks table",
    ),
    "KG publish refusal dropped (PostgreSQL)": (
        _edit(f"{_PG}/kg_build_job_store.py", "            self._refuse_memory_source(connection, notebook_id, snapshot)\n", ""),
        "KgBuildJobStore.publish_indexing_pipeline_success writes the chunks table",
    ),
    "Knowhow transfer refusal dropped (PostgreSQL)": (
        _edit(f"{_PG}/knowhow_transfer_store.py", "            _refuse_memory_source(db, payload)\n", ""),
        "KnowhowTransferStore.insert_transfer writes the chunks table",
    ),
    "new copy-path caller of insert_copy_rows with chunks": (
        _edit("backend/app/services/notebook_sharing.py", None,
              "def recopy(store, rows):\n    store.insert_copy_rows('chunks', rows, chunk_size=10)"),
        "::recopy writes the chunks table",
    ),
    "a helper passed around as a callback": (
        _edit(_SQ_KH, None, "LOADER = {'rows': _insert_rows}"),
        "_insert_rows referenced, not called",
    ),
    "a pass-through wrapper whose caller writes chunks": (
        _edit(_SQ_KH, None,
              "def put(db, table, rows):\n    _insert_rows(db, table, rows)\n\n\n"
              "def seed(db, rows):\n    put(db, 'chunks', rows)"),
        "::seed writes the chunks table",
    ),
    "a new generic updater on an interpolated table": (
        _edit(_SQ_MAINT, None,
              "def touch(db, table, ident):\n    db.execute(f'UPDATE {table} SET updated_at=1 WHERE id=?', (ident,))\n\n\n"
              "def touch_sources(db, ident):\n    touch(db, 'sources', ident)"),
        "::touch_sources updates a row of a table that can be sources",
    ),
    "the copy path claims a guard it does not have": (
        lambda tree: {},
        "does not carry the Memory predicate",
    ),
    "a mirror reason on a file that is no mirror": (
        lambda tree: {},
        "reason 'moves existing rows only' is not allowed for this file",
    ),
    # second re-review
    "X1 a subclass in another module overrides the class table constant": (
        lambda tree: {
            **_edit(_SQ_MAINT, None,
                    "class _RowWriter:\n    TABLE = 'notebook_tags'\n\n"
                    "    def put(self, db, rows):\n"
                    "        db.executemany(f'INSERT INTO {self.TABLE} (id, source_id) VALUES (?, ?)', rows)")(tree),
            f"{_SQ}/chunk_seed_x1.py": (
                "from app.repositories.sqlite.maintenance import _RowWriter\n\n\n"
                "class _ChunkWriter(_RowWriter):\n    TABLE = 'chunks'\n\n\n"
                "def seed(db, rows):\n    _ChunkWriter().put(db, rows)\n"
            ),
        },
        "_RowWriter.put writes the chunks table",
    ),
    "X3b an .sql file shipped next to a module writes chunks": (
        lambda tree: {
            f"{_SQ}/seed_chunks.sql": (
                "-- a seed script\nINSERT INTO chunks (id, notebook_id, source_id, text)\n"
                "SELECT 'c', notebook_id, id, title FROM sources;\n"
            ),
        },
        "seed_chunks.sql::<sql> writes the chunks table",
    ),
    "X5 the refusal's error is swallowed": (
        _edit(_PG_CS, _PG_INSERT_ROWS_REFUSAL + "        execute_many(",
              "        try:\n            self._refuse_memory_source(connection, source_id)\n"
              "        except ValueError:\n            pass\n        execute_many("),
        "ChunkStore.insert_rows writes the chunks table",
    ),
    "X6 the refusal behind an opt-in flag": (
        _edit(_PG_CS, _PG_INSERT_ROWS_REFUSAL + "        execute_many(",
              "        if getattr(self, '_strict_memory_probe', False):\n"
              "            self._refuse_memory_source(connection, source_id)\n        execute_many("),
        "ChunkStore.insert_rows writes the chunks table",
    ),
    "X7 the refusal in a nested function that is never called": (
        _edit(_PG_CS, _PG_INSERT_ROWS_REFUSAL + "        execute_many(",
              "        def _check():\n            self._refuse_memory_source(connection, source_id)\n"
              "        execute_many("),
        "ChunkStore.insert_rows writes the chunks table",
    ),
    "X7b the refusal in a lambda that is never called": (
        _edit(_PG_CS, _PG_INSERT_ROWS_REFUSAL + "        execute_many(",
              "        _check = lambda: self._refuse_memory_source(connection, source_id)\n"
              "        execute_many("),
        "ChunkStore.insert_rows writes the chunks table",
    ),
    # third re-review
    "G1 the refusal behind a short-circuit and": (
        _edit(_PG_CS, _PG_INSERT_ROWS_REFUSAL + "        execute_many(",
              "        getattr(self, '_strict_memory_probe', False) and "
              "self._refuse_memory_source(connection, source_id)\n        execute_many("),
        "ChunkStore.insert_rows writes the chunks table",
    ),
    "G2 the refusal behind a conditional expression": (
        _edit(_PG_CS, _PG_INSERT_ROWS_REFUSAL + "        execute_many(",
              "        self._refuse_memory_source(connection, source_id) "
              "if getattr(self, '_strict', False) else None\n        execute_many("),
        "ChunkStore.insert_rows writes the chunks table",
    ),
    "G5 the refusal in a comprehension over nothing": (
        _edit(_PG_CS, _PG_INSERT_ROWS_REFUSAL + "        execute_many(",
              "        [self._refuse_memory_source(connection, s) for s in ()]\n        execute_many("),
        "ChunkStore.insert_rows writes the chunks table",
    ),
    "X10 a sources updater whose SET list comes from **kwargs": (
        _edit(_PG_SS, None,
              "def update_source_fields(conn, source_id, **changes):\n"
              "    fields = [f'{name}=%s' for name in changes]\n"
              "    conn.execute(f\"UPDATE sources SET {','.join(fields)} WHERE id=%s\", "
              "(*changes.values(), source_id))\n\n\n"
              "def retype(conn, sid):\n    update_source_fields(conn, sid, source_type='memory')"),
        "::update_source_fields updates a row of a table that can be sources",
    ),
    "X10b a sources updater whose SET list comes from a mapping's keys": (
        _edit(_PG_SS, None,
              "def update_source_row(conn, source_id, changes):\n"
              "    fields = [f'{name}=%s' for name in changes.keys()]\n"
              "    conn.execute(f\"UPDATE sources SET {','.join(fields)} WHERE id=%s\", "
              "(*changes.values(), source_id))"),
        "::update_source_row updates a row of a table that can be sources",
    ),
    "X11 UPDATE OR IGNORE chunks SET source_id": (
        _edit(_SQ_MAINT, None,
              "def move_chunk(db, cid, sid):\n"
              "    db.execute('UPDATE OR IGNORE chunks SET source_id=? WHERE id=?', (sid, cid))"),
        "moves a chunk row to another source or notebook",
    ),
    "X12 UPDATE OR REPLACE sources SET source_type": (
        _edit(_SQ_MAINT, None,
              "def retype2(db, sid):\n"
              "    db.execute(\"UPDATE OR REPLACE sources SET source_type='memory' WHERE id=?\", (sid,))"),
        "::retype2 writes sources.source_type outside an INSERT",
    ),
}

# controls that change the lists instead of the code
_LIST_OVERRIDES: dict[str, Callable[[], dict]] = {
    "the copy path claims a guard it does not have": lambda: {
        key: ((COPY_GUARDED, detail) if reason == COPY_BY_SNAPSHOT else (reason, detail))
        for key, (reason, detail) in REVIEWED.items()
    },
    "a mirror reason on a file that is no mirror": lambda: {
        key: ((MOVES, detail) if reason == CHANGES else (reason, detail))
        for key, (reason, detail) in REVIEWED.items()
    },
}

POSITIVE_CONTROLS: dict[str, Callable] = {
    "N1 the INSERT hoisted into a module constant (the function still refuses first)": _both(
        _edit(_PG_CS,
              '            "INSERT INTO chunks"\n'
              '            "(id,notebook_id,source_id,text,section_path,element_ids,created_at) "\n'
              '            "VALUES (%s,%s,%s,%s,%s,%s,%s)",\n',
              "            CHUNK_INSERT_SQL,\n"),
        _edit(_PG_CS, None,
              "CHUNK_INSERT_SQL = ('INSERT INTO chunks(id,notebook_id,source_id,text,section_path,"
              "element_ids,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)')"),
    ),
    "N2 DELETE and SELECT ... FOR UPDATE of chunks": _edit(
        _SQ_MAINT, None,
        "def purge(db, sid):\n"
        "    db.execute('SELECT id FROM chunks WHERE source_id=? FOR UPDATE', (sid,))\n"
        "    db.execute('DELETE FROM chunks WHERE source_id=?', (sid,))",
    ),
    "N3 INSERT INTO chunk_questions / chunks_fts": _edit(
        _SQ_MAINT, None,
        "def q(db, r):\n"
        "    db.execute('INSERT INTO chunk_questions(chunk_id) VALUES (?)', r)\n"
        "    db.execute('INSERT INTO chunks_fts(chunk_id) VALUES (?)', r)",
    ),
    "N4 UPDATE sources SET title WHERE source_type=": _edit(
        _PG_SS, None,
        "def retitle(conn):\n    conn.execute(\"UPDATE sources SET title=%s WHERE source_type='memory'\", ('x',))",
    ),
    "N5 an error message that mentions UPDATE chunks": _edit(
        _SQ_MAINT, None, "def err(n):\n    raise RuntimeError(f'could not UPDATE chunks for {n} rows')",
    ),
    "a pass-through wrapper whose callers name other tables": _edit(
        _SQ_KH, None,
        "def put(db, table, rows):\n    _insert_rows(db, table, rows)\n\n\n"
        "def seed(db, rows):\n    for name in ('knowhow_rows', 'knowhow_cells'):\n        put(db, name, rows)",
    ),
    "a generic updater whose table comes from a constant tuple": _edit(
        _SQ_MAINT, None,
        "_TOUCHED = ('notebooks', 'knowhow_tables')\n"
        "def touch_all(db):\n    for table in _TOUCHED:\n"
        "        db.execute(f'UPDATE {table} SET updated_at=1')",
    ),
    # second re-review
    "P1 the INSERT hoisted into a class constant used through self": _both(
        _edit(_PG_CS,
              '            "INSERT INTO chunks"\n'
              '            "(id,notebook_id,source_id,text,section_path,element_ids,created_at) "\n'
              '            "VALUES (%s,%s,%s,%s,%s,%s,%s)",\n            values,',
              "            self._INSERT_SQL,\n            values,"),
        _edit(_PG_CS, "    def insert_rows(\n",
              "    _INSERT_SQL = ('INSERT INTO chunks(id,notebook_id,source_id,text,section_path,"
              "element_ids,created_at) VALUES (%s,%s,%s,%s,%s,%s,%s)')\n\n    def insert_rows(\n"),
    ),
    "P2 the refusal with keyword arguments": _edit(
        _PG_CS, _PG_INSERT_ROWS_REFUSAL + "        execute_many(",
        "        self._refuse_memory_source(connection=connection, source_id=source_id)\n"
        "        execute_many(",
    ),
    "P3 a new Knowhow business-table insert through _TABLE_NAMES": _edit(
        _SQ_KH, None,
        "def copy_rows_only(db, payload):\n    for key in ('rows', 'cells'):\n"
        "        _insert_rows(db, _TABLE_NAMES[key], payload.get(key) or [])",
    ),
    "P4 a protective sources updater that rejects source_type": _edit(
        _PG_SS, None,
        "_MUTABLE = ('title', 'summary')\n"
        "def update_source_fields(conn, source_id, changes):\n"
        "    if 'source_type' in changes:\n"
        "        raise ValueError('source_type never changes after insert')\n"
        "    fields = [f'{k}=%s' for k in changes if k in _MUTABLE]\n"
        "    conn.execute(f\"UPDATE sources SET {','.join(fields)} WHERE id=%s\", "
        "(*changes.values(), source_id))",
    ),
}


@pytest.mark.parametrize("name", sorted(NEGATIVE_CONTROLS))
def test_negative_control_turns_the_guard_red(name):
    edit, expected = NEGATIVE_CONTROLS[name]
    tree = repo_tree()
    changed = overlay(tree, edit(tree))
    reviewed = _LIST_OVERRIDES[name]() if name in _LIST_OVERRIDES else None
    if name == "the copy path claims a guard it does not have" and copy_path_reason(tree) == COPY_GUARDED:
        # the same claim is then true; the opposite claim must be refused instead
        reviewed = {
            key: ((COPY_UNGUARDED, detail) if reason == COPY_BY_SNAPSHOT else (reason, detail))
            for key, (reason, detail) in REVIEWED.items()
        }
        expected = "the copy snapshot now excludes Memory rows"
    problems = guard_problems(changed, reviewed=reviewed)
    assert any(expected in problem for problem in problems), (expected, problems)


@pytest.mark.parametrize("name", sorted(POSITIVE_CONTROLS))
def test_positive_control_keeps_the_guard_green(name):
    tree = repo_tree()
    problems = guard_problems(overlay(tree, POSITIVE_CONTROLS[name](tree)))
    assert not problems, "\n".join(problems)


_STATEMENT_CASES: dict[str, bool | str] = {
    # statement -> is it a chunks write (or: a fragment the problem list must hold)
    # X11 / X12 of the second re-review: SQLite's conflict clause after UPDATE
    "UPDATE OR IGNORE chunks SET source_id=? WHERE id=?": "moves a chunk row to another source",
    "UPDATE OR REPLACE sources SET source_type='memory' WHERE id=?":
        "x.py::f writes sources.source_type outside an INSERT",
    "UPDATE OR ROLLBACK chunks SET text=?": True,
    "INSERT INTO chunks (id) VALUES (?)": True,
    "INSERT INTO chunks(id) VALUES (?)": True,
    "insert  into  chunks (id) values (?)": True,
    "INSERT OR IGNORE INTO chunks (id) VALUES (?)": True,
    "INSERT OR REPLACE INTO chunks (id) VALUES (?)": True,
    "REPLACE INTO chunks (id) VALUES (?)": True,
    "INSERT INTO main.chunks SELECT * FROM sec.chunks": True,
    'INSERT INTO "public"."chunks" (id) VALUES (%s)': True,
    "UPDATE chunks SET text=?": True,
    'UPDATE "chunks" SET text=?': True,
    "UPDATE chunks AS c SET text=?": True,
    "COPY chunks (id) FROM STDIN": True,
    "MERGE INTO chunks USING x ON true WHEN MATCHED THEN DO NOTHING": True,
    "COPY chunks (id) TO STDOUT": False,
    "INSERT INTO chunks_fts(chunk_id) VALUES (?)": False,
    "INSERT INTO chunk_elements (chunk_id) VALUES (?)": False,
    "UPDATE sources SET chunked_at=?": False,
    "SELECT * FROM chunks WHERE id=? FOR UPDATE": False,
    "DELETE FROM chunks WHERE id=?": False,
    "could not UPDATE chunks for 3 rows": False,
}


@pytest.mark.parametrize("statement", sorted(_STATEMENT_CASES))
def test_statement_shapes_through_the_entry_point(statement):
    """A fake tree with one function running ``statement``: the guard reports a
    chunks write exactly for the write shapes."""
    tree = {"backend/app/x.py": f"def f(db):\n    db.execute({statement!r})\n"}
    problems = guard_problems(tree, reviewed={}, generic_updaters={})
    expected = _STATEMENT_CASES[statement]
    if isinstance(expected, str):
        assert any(expected in p for p in problems), problems
        return
    flagged = any("x.py::f writes the chunks table" in p for p in problems)
    assert flagged is expected, problems


_DYNAMIC_CASES = {
    "f'INSERT INTO {t} (id) VALUES (1)'": True,
    "'INSERT INTO ' + t + ' (id) VALUES (1)'": True,
    "'INSERT INTO %s (id) VALUES (1)' % t": True,
    "sql.SQL('INSERT INTO {} (id) VALUES (1)').format(sql.Identifier(t))": True,
    "'INSERT INTO {} (id) VALUES (1)'.format(t)": True,
    "f'INSERT INTO main.{t} SELECT * FROM sec.{t}'": True,
    "f'INSERT INTO {t}_facts (id) VALUES (1)'": False,
}


@pytest.mark.parametrize("expression", sorted(_DYNAMIC_CASES))
def test_interpolated_table_names_are_followed_to_the_caller(expression):
    """``t`` is the function's parameter: the caller passing ``"chunks"`` is the
    write site; a suffix that can never spell ``chunks`` is no write at all."""
    tree = {
        "backend/app/x.py": (
            "from psycopg import sql\n\n\n"
            f"def put(db, t):\n    db.execute({expression})\n\n\n"
            "def g(db):\n    put(db, 'chunks')\n"
        )
    }
    problems = guard_problems(tree, reviewed={}, generic_updaters={})
    flagged = any("x.py::g writes the chunks table" in p for p in problems)
    assert flagged is _DYNAMIC_CASES[expression], problems


def test_a_nested_writer_is_refused_where_it_is_called():
    tree = {
        "backend/app/x.py": (
            "def outer(db, s):\n"
            "    def inner():\n        db.execute('INSERT INTO chunks (id) VALUES (1)')\n"
            "    _refuse_memory_source(db, s)\n    inner()\n\n\n"
            "def early(db, s):\n"
            "    def inner():\n        db.execute('INSERT INTO chunks (id) VALUES (1)')\n"
            "    inner()\n    _refuse_memory_source(db, s)\n"
        )
    }
    problems = guard_problems(tree, reviewed={}, generic_updaters={})
    assert not any("::outer" in p for p in problems), problems
    assert any("x.py::early.inner writes the chunks table" in p for p in problems), problems


_W = "db.execute('INSERT INTO chunks (id) VALUES (1)')"
_R = "_refuse_memory_source(db, s)"
_DOMINANCE_CASES = {
    # body of ``f(db, s, flag, rows)`` -> does the refusal count for the write
    f"{_R}\n{_W}": True,
    f"if flag:\n    {_R}\n    {_W}": True,
    f"try:\n    {_R}\n    {_W}\nexcept ValueError:\n    pass": True,
    f"{_R}\nfor r in rows:\n    {_W}": True,
    f"{_R}\nwith db:\n    if flag:\n        {_W}": True,
    f"{_W}\n{_R}": False,
    f"if flag:\n    {_R}\n{_W}": False,
    f"if flag:\n    pass\nelse:\n    {_R}\n{_W}": False,
    f"try:\n    {_R}\nexcept ValueError:\n    pass\n{_W}": False,
    f"try:\n    {_R}\nexcept ValueError:\n    {_W}": False,
    f"for r in rows:\n    {_R}\n{_W}": False,
    f"def check():\n    {_R}\n{_W}": False,
    f"check = lambda: {_R}\n{_W}": False,
    # G1 / G2 / G5 of the third re-review: a refusal inside an expression
    f"flag and {_R}\n{_W}": False,
    f"{_R} if flag else None\n{_W}": False,
    f"[_refuse_memory_source(db, x) for x in ()]\n{_W}": False,
    f"checked = {_R}\n{_W}": False,
    f"_refuse_memory_source(db, 'fixed-id')\n{_W}": False,
}


@pytest.mark.parametrize("body", sorted(_DOMINANCE_CASES))
def test_the_refusal_must_run_before_the_write_on_every_path(body):
    """The refusal runs in the same function, in a statement list that encloses the
    write, before the statement holding it: not in a ``try`` / ``if`` / loop body
    the write is outside of, not in a nested function or a lambda."""
    source = "def f(db, s, flag, rows):\n" + "".join(f"    {line}\n" for line in body.splitlines())
    problems = guard_problems({"backend/app/x.py": source}, reviewed={}, generic_updaters={})
    refused = not any("x.py::f writes the chunks table" in p for p in problems)
    assert refused is _DOMINANCE_CASES[body], problems


def test_the_copy_reason_follows_a_fake_snapshot():
    """Both backends' chunks query must carry ``NOT memory_derived_object(alias)``."""
    from app.repositories.postgres import memory_sql as pg_memory
    from app.repositories.sqlite import memory_sql as sq_memory

    def store(query: str) -> str:
        return (
            "_COPY_SNAPSHOT_QUERIES = (('notebooks', 'SELECT * FROM notebooks WHERE id = ?'), "
            f"('chunks', {query!r}))\n"
        )

    guarded_sq = "SELECT c.* FROM chunks c WHERE c.notebook_id=? AND NOT " + sq_memory.memory_derived_object("c")
    guarded_pg = "SELECT c.* FROM chunks c WHERE c.notebook_id=%s AND NOT " + pg_memory.memory_derived_object("c")
    plain = "SELECT * FROM chunks WHERE notebook_id = ?"
    sq, pg = f"{_SQ}/sharing_store.py", f"{_PG}/sharing_store.py"
    assert copy_path_reason({sq: store(guarded_sq), pg: store(guarded_pg)}) == COPY_GUARDED
    assert copy_path_reason({sq: store(guarded_sq), pg: store(plain)}) == COPY_UNGUARDED
    assert copy_path_reason({sq: store(plain), pg: store(plain)}) == COPY_UNGUARDED
    assert copy_path_reason({}) == COPY_UNGUARDED


def _two_set_store(backend: str, selector: str, *, probe_predicate: bool = True, snapshot: str = "") -> str:
    """A ``sharing_store.py`` of E5-1's shape: a plain statement set, a Memory-aware
    one, a probe, and ``selector`` (the body of ``_copy_queries``)."""
    from app.repositories.postgres import memory_sql as pg_memory
    from app.repositories.sqlite import memory_sql as sq_memory

    memory = sq_memory if backend == "sqlite" else pg_memory
    mark = "?" if backend == "sqlite" else "%s"
    guarded = f"SELECT c.* FROM chunks c WHERE c.notebook_id = {mark} AND NOT " + memory.memory_derived_object("c")
    probe = (
        f"'SELECT 1 FROM sources WHERE notebook_id = {mark} AND ' + memory_sql.memory_source_type_predicate()"
        if probe_predicate else f"'SELECT 1 FROM unified_kg_state WHERE notebook_id = {mark} AND dirty = 1'"
    )
    return (
        f"from app.repositories.{backend} import memory_sql\n"
        f"_ROOT = ('notebooks', 'SELECT * FROM notebooks WHERE id = {mark}')\n"
        f"_PLAIN = (_ROOT, ('chunks', 'SELECT * FROM chunks WHERE notebook_id = {mark}'))\n"
        f"_MEMORY = (_ROOT, ('chunks', {guarded!r}))\n"
        # validation extras: (table, WHERE fragment) pairs are not statement sets
        "_VALIDATED = (('notebooks', ''), ('chunks', ''))\n"
        f"_PROBE = {probe}\n\n\n"
        "class SharingStore:\n"
        "    @staticmethod\n"
        "    def _copy_queries(db, notebook_id):\n"
        + "".join(f"        {line}\n" for line in selector.splitlines())
        + (
            "\n    def copy_snapshot(self, db, notebook_id):\n"
            + "".join(f"        {line}\n" for line in snapshot.splitlines())
            if snapshot else ""
        )
    )


_SNAPSHOT_THROUGH_SELECTOR = (
    "root_table, root_sql = _PLAIN[0]\n"
    "queries = self._copy_queries(db, notebook_id)\n"
    "return [db.execute(sql, (notebook_id,)).fetchall() for table, sql in queries[1:]]"
)


_E5_SELECTOR = "if db.execute(_PROBE, (notebook_id,)).fetchone():\n    return _MEMORY\nreturn _PLAIN"


@pytest.mark.parametrize(
    ("selector", "probe_predicate", "expected"),
    [
        (_E5_SELECTOR, True, COPY_GUARDED),
        ("return _MEMORY if db.execute(_PROBE, (notebook_id,)).fetchone() else _PLAIN", True, COPY_GUARDED),
        ("if db.execute(_PROBE, (notebook_id,)).fetchone():\n    return _MEMORY\nelse:\n    return _PLAIN",
         True, COPY_GUARDED),
        # the set without the predicate is chosen when the probe DOES find Memory
        ("if db.execute(_PROBE, (notebook_id,)).fetchone():\n    return _PLAIN\nreturn _MEMORY",
         True, COPY_UNGUARDED),
        # the probe is not a Memory probe (a dirty flag only)
        (_E5_SELECTOR, False, COPY_UNGUARDED),
        # no selector: nothing says which set a Memory notebook gets
        ("return None", True, COPY_UNGUARDED),
        # the plain set is also returned on a path the probe does not decide
        ("if notebook_id == 'x':\n    return _PLAIN\nif db.execute(_PROBE, (notebook_id,)).fetchone():\n"
         "    return _MEMORY\nreturn _PLAIN", True, COPY_UNGUARDED),
    ],
)
def test_the_copy_reason_follows_the_set_chosen_when_memory_exists(selector, probe_predicate, expected):
    """E5-1's shape: two statement sets chosen per copy by a Memory probe. Only the
    set a notebook with a Memory source gets must exclude Memory-derived chunks."""
    tree = {
        f"backend/app/repositories/{backend}/sharing_store.py":
            _two_set_store(backend, selector, probe_predicate=probe_predicate)
        for backend in ("sqlite", "postgres")
    }
    assert copy_path_reason(tree) == expected


@pytest.mark.parametrize(
    ("snapshot", "expected"),
    [
        # the root row read by index and the rows through the selector: guarded
        (_SNAPSHOT_THROUGH_SELECTOR, COPY_GUARDED),
        # the fetch loop iterates the plain set itself (E5-1 re-review blind spot)
        (_SNAPSHOT_THROUGH_SELECTOR.replace("in queries[1:]", "in _PLAIN[1:]"), COPY_UNGUARDED),
        # the plain set's chunks entry read by index inside a function
        ("return db.execute(_PLAIN[1][1], (notebook_id,)).fetchall()", COPY_UNGUARDED),
    ],
)
def test_the_plain_set_reaches_a_copy_only_through_the_selector(snapshot, expected):
    """A set exempted because the selector returns it only when the Memory probe
    found nothing must not be fetched from on any other road inside a function."""
    tree = {
        f"backend/app/repositories/{backend}/sharing_store.py":
            _two_set_store(backend, _E5_SELECTOR, snapshot=snapshot)
        for backend in ("sqlite", "postgres")
    }
    assert copy_path_reason(tree) == expected


_PROBE_CALL = "db.execute(_PROBE, (notebook_id,)).fetchone()"


def _with_module_line(store: str, line: str) -> str:
    return store.replace("_PROBE = ", f"{line}\n_PROBE = ", 1)


@pytest.mark.parametrize(
    ("name", "build", "expected"),
    [
        ("E5-1 shape with the table list and the derived set",
         lambda b: _with_module_line(
             _two_set_store(b, _E5_SELECTOR, snapshot=_SNAPSHOT_THROUGH_SELECTOR),
             "_TABLES = frozenset(table for table, _query in _PLAIN)\n"
             # E5-1 builds its Memory set from the plain one, table for table
             "_MEMORY = tuple((table, dict(_MEMORY).get(table, query)) for table, query in _PLAIN)",
         ), COPY_GUARDED),
        # V1 / V1b: the probe's direction inverted
        ("V1 is None", lambda b: _two_set_store(
            b, f"if {_PROBE_CALL} is None:\n    return _MEMORY\nreturn _PLAIN"), COPY_UNGUARDED),
        ("V1b not", lambda b: _two_set_store(
            b, f"if not {_PROBE_CALL}:\n    return _MEMORY\nreturn _PLAIN"), COPY_UNGUARDED),
        ("V1c probe inside a boolean combination", lambda b: _two_set_store(
            b, f"if {_PROBE_CALL} or notebook_id:\n    return _MEMORY\nreturn _PLAIN"), COPY_UNGUARDED),
        # V2: a module dict built from the plain set, read by the fetch loop
        ("V2 module dict of the plain set", lambda b: _with_module_line(
            _two_set_store(b, _E5_SELECTOR, snapshot=(
                "queries = self._copy_queries(db, notebook_id)\n"
                "return [db.execute(_BY_TABLE[table], (notebook_id,)).fetchall() "
                "for table, _sql in queries[1:]]"
            )),
            "_BY_TABLE = dict(_PLAIN)",
        ), COPY_UNGUARDED),
        # V3: the predicate ORed instead of ANDed
        ("V3 predicate ORed", lambda b: _two_set_store(b, _E5_SELECTOR).replace(
            " AND NOT ", " OR NOT ", 1), COPY_UNGUARDED),
    ],
)
def test_the_copy_verdict_checks_direction_derivations_and_conjunction(name, build, expected):
    """Third re-review P3-2: the Memory set counts only when the probe FOUND a row
    (``if <conn>.execute(PROBE, ...).fetchone():``); the plain set may be used at
    module level only as a table-name projection or to build another statement
    set; the Memory predicate must be an AND-conjunct of the WHERE clause."""
    tree = {
        f"backend/app/repositories/{backend}/sharing_store.py": build(backend)
        for backend in ("sqlite", "postgres")
    }
    assert copy_path_reason(tree) == expected, name
