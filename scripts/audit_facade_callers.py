#!/usr/bin/env python3
"""Read-only caller audit for ``RepositoryFacade``'s public surface.

This is **step 1 of the B5 structural-item plan**: it does not retire, patch
or even suggest editing any facade method — it only produces a reproducible
census of who calls each of the ~313 public members of
``backend/app/services/repository_facade.py::RepositoryFacade``, split into
production / script / test buckets, plus a fourth "ambiguous" bucket for
names we cannot confidently resolve. The output feeds
``docs/superpowers/plans/2026-08-23-facade-retirement-ledger.md``.

Usage
-----
    PYTHONPATH=backend python3 scripts/audit_facade_callers.py [--json OUT]

Run from the repo root (or anywhere — paths are resolved relative to this
file). No network, no database, no repository construction — pure AST/text
scanning of the checked-out source tree.

Methodology (read before trusting a "retire-now" row)
-------------------------------------------------------
1. **Facade surface.** We parse ``RepositoryFacade``'s own class body with
   the *same* AST helpers ``backend/tests/architecture/facade_contract.py``
   defines (``_function_contract_owner`` and friends, imported directly, not
   reimplemented), so "delegate" vs "adapter" classification here is
   definitionally identical to that module's notion of a clean one-hop
   delegate. **Caveat found while building this audit:** the module also
   defines ``facade_body_violations``/``manifest_delegate_mismatches``
   functions that would *enforce* "every public method is a one-hop
   delegate or an explicitly reasoned adapter" — but as of this writing no
   test or script actually calls them (only ``facade_delegate_evidence`` is
   consumed, by ``scripts/generate_repository_contract_fixtures.py`` to
   derive ``facade_surface.json``'s owner column). So today it is *dormant*
   validation logic, not a live gate; the currently-live surface gate is
   ``facade_surface_additions`` (``test_phase0_architecture_guard.py``),
   which only diffs the public *name set* against
   ``scripts/architecture_boundary_baseline.json``'s frozen
   ``facade_public_surface`` allowlist and does not care whether a method's
   body is a clean delegate. A method is a "delegate" here when its body is
   a clean single-hop forward to ``self._runtime.<component>.<name>`` (or
   the small set of other known one-hop targets: ``self.retrieval``,
   ``self.maintenance``, ``self.report_execution``, ``self.event_log``,
   ``self._migrator``); anything else (loops, multi-statement logic, calls
   to other facade members, branching) is an "adapter". 3 of the 312 public
   rows are adapters today: ``source_parse_busy``, ``federated_retrieve``,
   ``claim_report_generation`` (plus 3 private helpers outside this
   audit's public-only scope).

2. **Caller resolution is receiver-whitelisted, not name-only.** A plain
   ``grep -rw <method_name>`` overcounts wildly: dozens of facade method
   names (``get_notebook``, ``list_sources``, ``close``, ...) collide with
   methods on *narrower* objects that ``backend/app/api/deps.py`` hands out
   *instead of* the facade — e.g. ``notebook_catalog_repository()`` returns
   ``repository()._runtime.catalog`` (a bare ``NotebookCatalogService``), and
   ``notebook_catalog_repository().get_notebook(...)`` never touches
   ``RepositoryFacade.get_notebook`` at all, even though grep finds it. See
   deps.py's split: ``repository``, ``source_repository``,
   ``ask_stream_repository``, ``memory_service``, ``ask_state_repository``
   and ``mcp_memory_repository`` all return the facade itself; every other
   ``*_repository``/``*_port`` accessor in that file returns a
   ``._runtime.<component>`` sub-object and must NOT be treated as a facade
   receiver. This script hardcodes that split (see
   ``FACADE_ACCESSOR_CALL_NAMES`` below) rather than pattern-matching call
   targets by a generic ``*_repository`` suffix.

   For non-call receivers we use two whitelists:
     - Name nodes: ``REPO_NAME_RE`` (``repo``, ``repo1``, ``repository``,
       ``facade``, or any ``*_repo`` identifier) — the same regex
       ``backend/tests/architecture/repository_callers.py`` already uses to
       find repo-like local variables for its own (different) boundary
       guard.
     - Attribute nodes: ``self.repository`` / ``self.repo`` / ``self._repo``
       / ``self._repository`` / ``app.state.repository``-shaped chains
       (attr name in ``RECEIVER_ATTR_NAMES``).
   ``getattr(<receiver>, "<method>")`` literals are treated the same way as
   ``<receiver>.<method>(``.

3. **Three counted buckets + a mirror column.**
     - production: ``backend/app/**`` excluding ``repository_facade.py``
       itself and the two thin subclass wrappers (``sqlite_repository.py``,
       ``postgres/repository.py``), which are reported separately in the
       "wrapper镜像" column because a call landing on those files is either
       (a) an override that shadows the facade body entirely, or (b) an
       internal ``self.<method>(...)`` call from *within* the wrapper that
       is itself a legitimate caller of the inherited facade method — worth
       seeing, but not "an external production caller" in the retirement
       sense.
     - scripts: ``scripts/**`` (``backend/app/api/mcp_tools/**`` is already
       under ``backend/app/**`` and is counted as production, per the task
       brief — it is not treated as a script bucket).
     - tests: ``backend/tests/**``.

4. **Two receiver-resolution extensions, added after the first pass found
   real false negatives:**
     - *File-local propagation* (``_file_bound_repo_names``): a plain name
       assigned once from a known facade accessor (``service =
       memory_service()``) is treated as a facade receiver for the rest of
       that file. Discovered necessary because ``backend/app/api/deps.py``'s
       auth core does exactly this.
     - *Bound-method references* (``_bound_method_ref_ids``): this codebase
       systematically offloads sync facade calls with
       ``run_in_threadpool(service.list_memories, ...)`` — the method is
       passed as a bare callable, never directly invoked with parens. Both
       ``deps.py`` and most of ``memory_routes.py`` do this. Such references
       are counted exactly like a direct call once the receiver resolves.
   Both are heuristics, not scope-accurate analysis (the file-local
   propagation in particular is *file-wide*, not block/function-scoped) —
   see the ledger's risk-assessment section for the concrete trade-off.

5. **The "ambiguous" classification.** When a method has **zero** resolved
   callers in all three buckets, we separately count same-named
   ``<expr>.<method>(`` call sites (and bound-method references) whose
   receiver did *not* match the whitelist (``unresolved_same_name_calls``).
   If that count is nonzero, the method is filed as ``ambiguous`` instead of
   ``retire-now`` — we found real Python call syntax with this exact method
   name that our whitelist could not confirm or deny belongs to the facade
   (could be a different class's same-named method — most likely — or could
   be a receiver pattern this script's whitelist does not recognize — the
   dangerous case for a false "retire-now"). Only methods with zero resolved
   callers **and** zero unresolved same-name calls anywhere are filed
   ``retire-now``.

Known limitations (see the ledger's risk-assessment section for the full
discussion):
  - ``@property`` / ``@x.setter`` members are primarily used as **bare
    attribute reads/writes**, not calls, so for those rows only we fold the
    bucketed ``property_attr_hint_*`` counts (receiver-whitelisted bare
    ``<expr>.<name>`` access) into the effective per-bucket totals that
    ``classify()`` uses — otherwise every property would misfile
    ``retire-now`` regardless of real usage. For ordinary (non-property)
    methods, bare attribute reads are *not* folded into the count (an
    unusual pattern for a plain method, and the bound-method-reference
    extension above already covers the common "pass the method itself as a
    callback" case).
  - Dynamic dispatch through fully computed strings (``getattr(x, some_var)``
    where ``some_var`` is not a literal), reflection, or serialization-driven
    dispatch cannot be seen by this script at all.
  - This is a name/receiver heuristic, not a type checker; it is a *census
    for human review*, not a retirement authority.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_ROOT = REPO_ROOT / "backend"
if str(BACKEND_ROOT) not in sys.path:
    sys.path.insert(0, str(BACKEND_ROOT))

from tests.architecture.facade_contract import (  # noqa: E402
    FACADE_FILE,
    SCALAR_IDENTITY_ADAPTERS,
    _body_without_docstring,
    _decorator_names,
    _dotted,
    _facade_functions,
    _function_contract_owner,
)
from app.services.repository_facade import RepositoryFacade  # noqa: E402

FACADE_PATH = (REPO_ROOT / FACADE_FILE).resolve()
SQLITE_WRAPPER_REL = "backend/app/services/sqlite_repository.py"
POSTGRES_WRAPPER_REL = "backend/app/repositories/postgres/repository.py"
WRAPPER_RELS = {SQLITE_WRAPPER_REL, POSTGRES_WRAPPER_REL}

PROD_ROOT = REPO_ROOT / "backend" / "app"
SCRIPTS_ROOT = REPO_ROOT / "scripts"
TESTS_ROOT = REPO_ROOT / "backend" / "tests"

# The exact deps.py accessor functions (and constructor names) that hand back
# the facade instance itself, as opposed to a narrower ._runtime.<component>
# sub-object. See the module docstring point 2 for why this must be an exact
# allowlist and not a "*_repository" suffix match.
FACADE_ACCESSOR_CALL_NAMES = {
    "repository",
    "source_repository",
    "ask_stream_repository",
    "memory_service",
    "ask_state_repository",
    "mcp_memory_repository",
    "create_application_repository",
    "SQLiteRepository",
    "PostgresRepository",
    "RepositoryFacade",
}

# Local-variable/parameter name shape that conventionally holds the facade
# instance itself (mirrors REPO_NAME_RE from
# backend/tests/architecture/repository_callers.py, reused verbatim so this
# script's Name-receiver notion of "repo-like" matches the one the existing
# architecture guard already established for a related purpose).
REPO_NAME_RE = re.compile(r"^(?:repo\d*|repository|facade|[A-Za-z_]\w*_repo)$")

# Attribute names that, as the final hop of an attribute chain, indicate the
# chain's value is the facade instance (self.repository, self.repo,
# self._repo, app.state.repository, ...).
RECEIVER_ATTR_NAMES = {"repository", "repo", "_repo", "_repository", "repository_facade"}


@dataclass
class MethodInfo:
    name: str
    lineno: int
    is_property: bool
    is_setter: bool
    is_async: bool
    delegate_owner: str | None
    delegate_target: str
    delegate_kind: str  # "delegate" | "adapter"


@dataclass
class CallCounts:
    production: int = 0
    scripts: int = 0
    tests: int = 0
    production_sites: list[str] = field(default_factory=list)
    scripts_sites: list[str] = field(default_factory=list)
    tests_sites: list[str] = field(default_factory=list)
    unresolved_same_name: int = 0
    # Bare-attribute-access hint, bucketed the same way as the primary
    # counts. This is the *only* reliable signal for `@property` members
    # (see module docstring "known limitations") and is folded into the
    # effective per-bucket count for property/setter rows in `classify()`;
    # for ordinary methods it is reported but never counted.
    property_attr_hint_production: int = 0
    property_attr_hint_scripts: int = 0
    property_attr_hint_tests: int = 0
    wrapper_sqlite_override: bool = False
    wrapper_postgres_override: bool = False
    wrapper_sqlite_self_calls: int = 0
    wrapper_postgres_self_calls: int = 0

    @property
    def property_attr_hint(self) -> int:
        return (
            self.property_attr_hint_production
            + self.property_attr_hint_scripts
            + self.property_attr_hint_tests
        )


MAX_SITES_SAMPLED = 5


# --------------------------------------------------------------------------
# Facade surface enumeration
# --------------------------------------------------------------------------


def _unwrap_scalar(node: ast.AST) -> ast.AST:
    while (
        isinstance(node, ast.Call)
        and _dotted(node.func) in SCALAR_IDENTITY_ADAPTERS
        and len(node.args) == 1
        and not node.keywords
    ):
        node = node.args[0]
    return node


def _extract_target(node: ast.FunctionDef) -> str:
    """Best-effort human-readable first-hop target for the ledger's
    "委托目标" column. This is independent of (but consistent with) the
    owner classification in ``_function_contract_owner``: it just renders
    the dotted call/attribute chain a clean one-hop delegate resolves to.
    """
    body = _body_without_docstring(node)
    if not body:
        return "<empty>"
    last = body[-1]
    value: ast.AST | None = None
    if isinstance(last, ast.Return):
        value = last.value
    elif isinstance(last, ast.Expr):
        value = last.value
    if value is None:
        return "<complex>"
    value = _unwrap_scalar(value)
    if isinstance(value, ast.Call):
        dotted = _dotted(value.func)
    elif isinstance(value, ast.Attribute):
        dotted = _dotted(value)
    else:
        return "<complex>"
    if dotted.startswith("self."):
        dotted = dotted[len("self.") :]
    return dotted or "<complex>"


def facade_public_methods() -> list[MethodInfo]:
    """One row per distinct public name. A ``@property`` getter and its
    ``@x.setter`` share exactly one Python attribute name and cannot be told
    apart by receiver-based call-site scanning (a bare ``repo.x = v`` and a
    bare ``repo.x`` both surface as an ``ast.Attribute`` node with the same
    ``attr``), so they are merged into a single ledger row rather than
    listed twice with identical, confusing counts.
    """
    functions, offset = _facade_functions(RepositoryFacade)
    by_name: dict[str, MethodInfo] = {}
    order: list[str] = []
    for node in functions:
        if node.name.startswith("_"):
            continue
        decorators = _decorator_names(node)
        is_property = "property" in decorators
        is_setter = any(d.endswith(".setter") for d in decorators)
        owner = _function_contract_owner(node)
        target = _extract_target(node)
        info = MethodInfo(
            name=node.name,
            lineno=offset + node.lineno,
            is_property=is_property,
            is_setter=is_setter,
            is_async=isinstance(node, ast.AsyncFunctionDef),
            delegate_owner=owner,
            delegate_target=target,
            delegate_kind="delegate" if owner else "adapter",
        )
        existing = by_name.get(node.name)
        if existing is None:
            by_name[node.name] = info
            order.append(node.name)
        else:
            # Merge: prefer the getter's (property) delegate evidence, but
            # record that a setter also exists.
            existing.is_property = existing.is_property or is_property
            existing.is_setter = existing.is_setter or is_setter
            if is_property and not existing.delegate_owner:
                existing.delegate_owner = owner
                existing.delegate_target = target
                existing.delegate_kind = "delegate" if owner else "adapter"
    return [by_name[name] for name in order]


# --------------------------------------------------------------------------
# Caller resolution
# --------------------------------------------------------------------------


def _relpath(path: Path) -> str:
    return path.resolve().relative_to(REPO_ROOT).as_posix()


def _iter_python_files(root: Path) -> Iterable[Path]:
    for path in sorted(root.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        yield path


def _is_repo_receiver_call(call: ast.Call) -> bool:
    tail = _dotted(call.func).rsplit(".", 1)[-1]
    return tail in FACADE_ACCESSOR_CALL_NAMES


def _is_facade_accessor_ref(node: ast.AST) -> bool:
    """True for either an *invocation* of a known facade accessor
    (``memory_service()``) or a *bare reference* to one by name
    (``memory_service`` / ``deps.memory_service``) — the latter is exactly
    the shape ``Depends(memory_service)`` uses: FastAPI calls the callable
    itself, the route source never does."""
    if isinstance(node, ast.Call):
        return _is_repo_receiver_call(node)
    if isinstance(node, (ast.Name, ast.Attribute)):
        return _dotted(node).rsplit(".", 1)[-1] in FACADE_ACCESSOR_CALL_NAMES
    return False


def _is_repo_receiver(node: ast.AST, extra_names: frozenset[str] = frozenset()) -> bool:
    if isinstance(node, ast.Name):
        return bool(REPO_NAME_RE.match(node.id)) or node.id in extra_names
    if isinstance(node, ast.Attribute):
        return node.attr in RECEIVER_ATTR_NAMES
    if isinstance(node, ast.Call):
        return _is_repo_receiver_call(node)
    return False


def _file_bound_repo_names(tree: ast.AST) -> frozenset[str]:
    """Single-pass, scope-blind local-variable propagation: any plain name
    ever assigned (``x = <facade accessor>``) from something
    ``_is_repo_receiver`` already recognizes is also treated as a facade
    receiver name for the rest of *this file*.

    This is a coarse, file-wide (not block/function-scoped) heuristic —
    deliberately so, to keep this a cheap single pre-pass. It exists because
    a real, hot production path (``backend/app/api/deps.py``'s
    ``require_user_or_agent`` core) does exactly ``service =
    memory_service(); ...; service.resolve_agent_token(...)`` — a plain
    ``ast.Name`` receiver whose provenance is one assignment removed from a
    known facade accessor. Without this, such call sites fall into
    ``unresolved_same_name`` and the method is only ever filed
    ``ambiguous`` rather than ``keep``. The risk this trades in: if a name
    like ``service`` is reused for something unrelated elsewhere in the same
    (large) file, calls on that unrelated object could be miscounted as
    resolved. See the ledger's risk-assessment section.
    """
    names: set[str] = set()
    for node in ast.walk(tree):
        # FastAPI dependency-injected parameters: `service: MemoryRepository
        # = Depends(memory_service)` — very common across `backend/app/api/
        # *_routes.py`. `Depends(...)`'s own argument is exactly the same
        # accessor-name shape `_is_repo_receiver_call` already recognizes.
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for arg, default in _param_defaults(node):
                if (
                    isinstance(default, ast.Call)
                    and _dotted(default.func).rsplit(".", 1)[-1] == "Depends"
                    and len(default.args) == 1
                    and _is_facade_accessor_ref(default.args[0])
                ):
                    names.add(arg.arg)

    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if not isinstance(node, (ast.Assign, ast.AnnAssign)):
                continue
            value = node.value
            if value is None:
                continue
            if not _is_repo_receiver(value, frozenset(names)):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                if isinstance(target, ast.Name) and target.id not in names:
                    names.add(target.id)
                    changed = True
    return frozenset(names)


def _param_defaults(
    node: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[tuple[ast.arg, ast.AST]]:
    """Pair each positional/keyword-only parameter that has a default with
    that default expression (mirrors how Python itself aligns a shorter
    ``defaults`` list against the tail of ``args``)."""
    pairs: list[tuple[ast.arg, ast.AST]] = []
    positional = node.args.args
    defaults = node.args.defaults
    offset = len(positional) - len(defaults)
    for arg, default in zip(positional[offset:], defaults):
        pairs.append((arg, default))
    for arg, default in zip(node.args.kwonlyargs, node.args.kw_defaults):
        if default is not None:
            pairs.append((arg, default))
    return pairs


def _bucket_for(relpath: str) -> str | None:
    if relpath in WRAPPER_RELS:
        return "wrapper"
    if relpath == FACADE_FILE:
        return None
    if relpath.startswith("backend/tests/"):
        return "tests"
    if relpath.startswith("backend/app/"):
        return "production"
    if relpath.startswith("scripts/"):
        return "scripts"
    return None


def scan_repo(methods: list[MethodInfo]) -> dict[str, CallCounts]:
    by_name = {m.name: m for m in methods}
    property_names = {m.name for m in methods if m.is_property or m.is_setter}
    counts: dict[str, CallCounts] = {m.name: CallCounts() for m in methods}

    all_roots = (PROD_ROOT, SCRIPTS_ROOT, TESTS_ROOT)
    seen: set[Path] = set()
    for root in all_roots:
        for path in _iter_python_files(root):
            resolved = path.resolve()
            if resolved in seen:
                continue
            seen.add(resolved)
            relpath = _relpath(resolved)
            bucket = _bucket_for(relpath)
            if bucket is None:
                continue
            try:
                source = resolved.read_text(encoding="utf-8")
                tree = ast.parse(source, filename=relpath)
            except (SyntaxError, UnicodeDecodeError):
                continue
            _scan_tree(tree, relpath, bucket, by_name, property_names, counts)
    return counts


def _bound_method_ref_ids(tree: ast.AST) -> set[int]:
    """Identity set of ``ast.Attribute`` nodes passed as a bare callable
    argument to some other call — e.g. ``run_in_threadpool(service.foo,
    arg)`` — as opposed to being called directly. This codebase's async API
    routes systematically offload sync facade calls this way
    (``backend/app/api/deps.py``'s auth core, most of
    ``backend/app/api/memory_routes.py``), so without recognizing this
    shape a large, real slice of production callers would misfile as
    ``unresolved_same_name`` and their methods would misfile ``ambiguous``
    instead of ``keep``.
    """
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        for arg in node.args:
            if isinstance(arg, ast.Attribute):
                ids.add(id(arg))
        for kw in node.keywords:
            if isinstance(kw.value, ast.Attribute):
                ids.add(id(kw.value))
    return ids


def _record_site(counts_obj: CallCounts, bucket: str, relpath: str, lineno: int) -> None:
    if bucket == "production":
        counts_obj.production += 1
        if len(counts_obj.production_sites) < MAX_SITES_SAMPLED:
            counts_obj.production_sites.append(f"{relpath}:{lineno}")
    elif bucket == "scripts":
        counts_obj.scripts += 1
        if len(counts_obj.scripts_sites) < MAX_SITES_SAMPLED:
            counts_obj.scripts_sites.append(f"{relpath}:{lineno}")
    elif bucket == "tests":
        counts_obj.tests += 1
        if len(counts_obj.tests_sites) < MAX_SITES_SAMPLED:
            counts_obj.tests_sites.append(f"{relpath}:{lineno}")


def _scan_tree(
    tree: ast.AST,
    relpath: str,
    bucket: str,
    by_name: dict[str, MethodInfo],
    property_names: set[str],
    counts: dict[str, CallCounts],
) -> None:
    is_wrapper = bucket == "wrapper"
    wrapper_key = "sqlite" if relpath == SQLITE_WRAPPER_REL else "postgres" if relpath == POSTGRES_WRAPPER_REL else None
    extra_names = frozenset() if is_wrapper else _file_bound_repo_names(tree)
    bound_ref_ids = frozenset() if is_wrapper else _bound_method_ref_ids(tree)

    for node in ast.walk(tree):
        # `<expr>.<method>(` call form.
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            name = node.func.attr
            method = by_name.get(name)
            if method is None:
                continue
            receiver = node.func.value
            resolved = _is_repo_receiver(receiver, extra_names)
            if is_wrapper:
                # A call from *within* the wrapper file itself, on `self`,
                # is a legitimate caller of the inherited facade method
                # (unless the wrapper also overrides it, in which case the
                # override intercepts it — still worth surfacing as a
                # same-name self-call for human review).
                if isinstance(receiver, ast.Name) and receiver.id == "self":
                    obj = counts[name]
                    if wrapper_key == "sqlite":
                        obj.wrapper_sqlite_self_calls += 1
                    elif wrapper_key == "postgres":
                        obj.wrapper_postgres_self_calls += 1
                continue
            if resolved:
                _record_site(counts[name], bucket, relpath, node.lineno)
            else:
                counts[name].unresolved_same_name += 1
            continue

        # `getattr(<expr>, "<method>")` / `getattr(<expr>, "<method>", default)`
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "getattr"
            and len(node.args) >= 2
            and isinstance(node.args[1], ast.Constant)
            and isinstance(node.args[1].value, str)
        ):
            name = node.args[1].value
            method = by_name.get(name)
            if method is None:
                continue
            receiver = node.args[0]
            resolved = _is_repo_receiver(receiver, extra_names)
            if is_wrapper:
                continue
            if resolved:
                _record_site(counts[name], bucket, relpath, node.lineno)
            else:
                counts[name].unresolved_same_name += 1
            continue

        # Bound-method reference passed as a bare callable argument, e.g.
        # `run_in_threadpool(service.list_memories, ...)`. Counted exactly
        # like a direct call (not the property-only "hint" mechanism) since
        # this is a real, direct use of the method by name, just deferred.
        if (
            isinstance(node, ast.Attribute)
            and node.attr in by_name
            and node.attr not in property_names
            and id(node) in bound_ref_ids
        ):
            if is_wrapper:
                continue
            if _is_repo_receiver(node.value, extra_names):
                _record_site(counts[node.attr], bucket, relpath, node.lineno)
            else:
                counts[node.attr].unresolved_same_name += 1
            continue

        # Bare attribute access `<expr>.<name>` (no call) — the primary
        # usable signal for @property members (see module docstring "known
        # limitations"); bucketed the same way as call-form counts so
        # `classify()` can fold it in for property/setter rows only.
        if isinstance(node, ast.Attribute) and node.attr in property_names:
            if is_wrapper:
                continue
            if _is_repo_receiver(node.value, extra_names):
                target = counts[node.attr]
                if bucket == "production":
                    target.property_attr_hint_production += 1
                elif bucket == "scripts":
                    target.property_attr_hint_scripts += 1
                elif bucket == "tests":
                    target.property_attr_hint_tests += 1

    if is_wrapper:
        # Class-level override detection: does the wrapper class define its
        # own method/property with this exact name?
        for stmt in tree.body:
            if isinstance(stmt, ast.ClassDef):
                for child in stmt.body:
                    if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        obj = by_name.get(child.name)
                        if obj is None:
                            continue
                        target = counts[child.name]
                        if wrapper_key == "sqlite":
                            target.wrapper_sqlite_override = True
                        elif wrapper_key == "postgres":
                            target.wrapper_postgres_override = True


# --------------------------------------------------------------------------
# Classification
# --------------------------------------------------------------------------


def classify(method: MethodInfo, counts: CallCounts) -> str:
    # For @property / @x.setter members, bare attribute access (no call
    # parens) is the real-world usage form, so fold the bucketed hint into
    # the effective counts. For ordinary methods the hint is advisory-only
    # and never counted (see module docstring "known limitations").
    if method.is_property or method.is_setter:
        production = counts.production + counts.property_attr_hint_production
        scripts = counts.scripts + counts.property_attr_hint_scripts
        tests = counts.tests + counts.property_attr_hint_tests
    else:
        production, scripts, tests = counts.production, counts.scripts, counts.tests

    non_test_total = production + scripts
    resolved_total = non_test_total + tests
    if non_test_total > 0:
        return "keep"
    if resolved_total > 0:
        # only test-bucket callers
        return "test-only"
    if counts.unresolved_same_name > 0:
        return "ambiguous"
    return "retire-now"


# --------------------------------------------------------------------------
# Report assembly
# --------------------------------------------------------------------------


def build_report() -> dict[str, object]:
    methods = facade_public_methods()
    counts = scan_repo(methods)
    rows = []
    for method in methods:
        c = counts[method.name]
        classification = classify(method, c)
        wrapper_bits = []
        if c.wrapper_sqlite_override:
            wrapper_bits.append("sqlite:override")
        elif c.wrapper_sqlite_self_calls:
            wrapper_bits.append(f"sqlite:self-call x{c.wrapper_sqlite_self_calls}")
        if c.wrapper_postgres_override:
            wrapper_bits.append("postgres:override")
        elif c.wrapper_postgres_self_calls:
            wrapper_bits.append(f"postgres:self-call x{c.wrapper_postgres_self_calls}")
        rows.append(
            {
                "name": method.name,
                "lineno": method.lineno,
                "is_property": method.is_property,
                "is_setter": method.is_setter,
                "is_async": method.is_async,
                "delegate_kind": method.delegate_kind,
                "delegate_owner": method.delegate_owner,
                "delegate_target": method.delegate_target,
                "production_calls": c.production,
                "production_sites": c.production_sites,
                "script_calls": c.scripts,
                "script_sites": c.scripts_sites,
                "test_calls": c.tests,
                "test_sites": c.tests_sites,
                "unresolved_same_name_calls": c.unresolved_same_name,
                "property_attr_hint": c.property_attr_hint,
                "property_attr_hint_production": c.property_attr_hint_production,
                "property_attr_hint_scripts": c.property_attr_hint_scripts,
                "property_attr_hint_tests": c.property_attr_hint_tests,
                "wrapper_mirror": ", ".join(wrapper_bits) if wrapper_bits else "-",
                "classification": classification,
            }
        )
    summary = {
        "total_methods": len(rows),
        "retire_now": sum(1 for r in rows if r["classification"] == "retire-now"),
        "test_only": sum(1 for r in rows if r["classification"] == "test-only"),
        "ambiguous": sum(1 for r in rows if r["classification"] == "ambiguous"),
        "keep": sum(1 for r in rows if r["classification"] == "keep"),
    }
    return {"summary": summary, "methods": rows}


def _fmt_sites(sites: list[str]) -> str:
    if not sites:
        return ""
    return "<br>".join(f"`{s}`" for s in sites)


def render_markdown_table(report: dict[str, object]) -> str:
    """Render the full per-method ledger table (rows sorted alphabetically
    by method name), consumed verbatim by
    ``docs/superpowers/plans/2026-08-23-facade-retirement-ledger.md``.
    """
    rows = sorted(report["methods"], key=lambda r: r["name"])
    header = (
        "| 方法 | 行号 | 委托目标 | 分类(kind) | 生产调用数 | 脚本调用数 | 测试调用数 | "
        "撞名未解析 | wrapper镜像 | 档 |\n"
        "|---|---|---|---|---|---|---|---|---|---|\n"
    )
    lines = [header]
    for r in rows:
        name = r["name"]
        if r["is_property"] or r["is_setter"]:
            kind = "property" + ("+setter" if r["is_setter"] and r["is_property"] else "" if r["is_property"] else "setter")
            name_disp = f"`{name}` ({kind})"
        else:
            name_disp = f"`{name}`"
        lines.append(
            "| {name} | {lineno} | `{target}` | {delegate_kind} | {prod} | {script} | {test} | {unresolved} | {wrapper} | **{classification}** |\n".format(
                name=name_disp,
                lineno=r["lineno"],
                target=r["delegate_target"],
                delegate_kind=r["delegate_kind"],
                prod=r["production_calls"],
                script=r["script_calls"],
                test=r["test_calls"],
                unresolved=r["unresolved_same_name_calls"],
                wrapper=r["wrapper_mirror"],
                classification=r["classification"],
            )
        )
    return "".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", type=Path, default=None, help="optional path to write the full JSON report")
    parser.add_argument(
        "--markdown",
        type=Path,
        default=None,
        help="optional path to write the full per-method GFM ledger table",
    )
    args = parser.parse_args()

    report = build_report()
    if args.json:
        args.json.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        print(f"wrote {args.json}", file=sys.stderr)
    if args.markdown:
        args.markdown.write_text(render_markdown_table(report), encoding="utf-8")
        print(f"wrote {args.markdown}", file=sys.stderr)
    if not args.json and not args.markdown:
        print(json.dumps(report["summary"], indent=2, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
