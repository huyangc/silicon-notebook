"""A1 static guard: the ``notebook_id`` carried by ``Citation`` /
``AnswerAnchor`` evidence must come from the one registered normalisation
helper.

The invariant (``docs/superpowers/specs/2026-07-19-multi-domain-bases-followups.md``
§A1): ``notebook_id`` is non-empty ONLY for cross-notebook evidence.  A value
equal to the notebook the ask/report runs against must be normalised to ``""``,
because the frontend resolves a non-empty id through a library-name map that
includes the active notebook — echoing the active id back badges the user's own
notes as 「来自「当前笔记本自己的名字」」.

The rule was missed three separate times during the multi-domain base rollout
(Task 14 implementation, codex round 2, codex round 4), each time caught only by
a human reviewer, because the fix was a one-off inline expression re-typed at
every new producer.  This guard removes the "new construction point, no
reminder" failure mode.  Two shapes are covered:

*Constructor keywords.*  A ``Citation(...)`` / ``AnswerAnchor(...)`` call (under
the name it is imported as — ``from app.models.ask import Citation as C`` is
resolved per module) is compliant only when it

  (a) passes ``notebook_id=`` a call to ``foreign_notebook_id`` (the shared rule
      in ``app/domain/citation_origin.py``), or
  (b) supplies no ``notebook_id`` at all — neither the keyword nor a ``**``
      splat, which could smuggle one in (the model field then defaults to
      ``""``, which trivially satisfies the invariant), or
  (c) is listed in ``REGISTERED_SITES`` below with a one-line reason.

*id_map dict writes.*  Anchors are not always built by a constructor: the
``EvidenceContextService`` builders stash the id under a ``"notebook_id"`` key
in an id_map entry, and ``parse_anchors`` copies it into the ``AnswerAnchor``
later.  Every ``"notebook_id": ...`` dict write inside a ``BUILDER_SITES``
function is therefore held to the same rule, so reverting one of those to an
inline expression is not invisible to the guard.

*Helper identity.*  Both shapes above recognise the helper by *name*, which on
its own a module could satisfy with a two-line stand-in of the same name.  So
every module that calls ``foreign_notebook_id`` must additionally import it from
``app.domain.citation_origin`` under that exact name, bind that name to nothing
else (a local ``def``, a parameter, a loop variable, an ``import ... as``, an
injected ``self.foreign_notebook_id = ...``), and never reach it through an
attribute.  The defining module is not caught by this: the check only looks at
modules that *call* the name.

Both registries are ratchets.  Each entry pins the *exact* set of argument
source texts expected at that site, so an entry can neither go stale (the
function was renamed or the site deleted) nor act as a blanket (a second,
differently-spelled construction point added to an already-registered function
trips the guard).  Registries are keyed by (file, enclosing qualname) rather
than by line, per the repository's test architecture policy
(``tests/architecture/policy.py`` forbids ``path:line`` identity, which rots on
any edit above the site).

What the guard does NOT see: a ``notebook_id`` assembled outside these shapes —
a dict built by ``dict(...)`` or ``update()``, a write in a function that is not
a registered builder, or a value laundered through an attribute assignment.
Those paths are covered by the behavioural tests (``test_evidence_context_service``,
``test_cross_tier_reasoning``, ``test_knowhow_graph_anchor``), not here.
"""
from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from app.domain.citation_origin import foreign_notebook_id
from app.services.kg.graph_reason import render_subgraph_context


ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "backend" / "app"
HELPER_NAME = "foreign_notebook_id"
CONSTRUCTED_MODELS = ("Citation", "AnswerAnchor")
ID_MAP_KEY = "notebook_id"

HELPER_MODULE = "app.domain.citation_origin"

ASK_SERVICE = "backend/app/services/ask_service.py"
EVIDENCE_CONTEXT = "backend/app/services/evidence_context.py"
FOLLOW_CHAIN = "backend/app/services/kg/follow_chain.py"
GRAPH_REASON = "backend/app/services/kg/graph_reason.py"
SPREADSHEET = "backend/app/services/spreadsheet_analysis.py"

Site = tuple[str, str]
# site -> (exact expected argument sources at that site, why they are allowed)
Registry = dict[Site, tuple[tuple[str, ...], str]]

# Construction points whose ``notebook_id`` is not spelled as a direct
# ``foreign_notebook_id`` call.  The source-text tuple is exhaustive: a second
# or re-spelled construction point in the same function is an offender.
REGISTERED_SITES: Registry = {
    (EVIDENCE_CONTEXT, "EvidenceContextService.parse_anchors"): (
        ("str(context.get('notebook_id', ''))",),
        "Copies the value out of the id_map; the five builders that can carry "
        "cross-notebook evidence (chunk_context / knowledge_context / "
        "render_follow_chain_context / render_subgraph_context / "
        "spreadsheet_prompt_block) normalise through the helper when they "
        "WRITE that key — pinned below in BUILDER_SITES — and element_context "
        "is single-notebook by construction and always writes an empty "
        "string.",
    ),
    (EVIDENCE_CONTEXT, "EvidenceContextService.citations_from"): (
        ("hit_notebook_id",),
        "Normalises once per hit into `hit_notebook_id` (helper call a few "
        "lines above) and reuses that local for every evidence row of the hit.",
    ),
    (ASK_SERVICE, "AskService._draft_reasoning_response"): (
        ("''",),
        "Element citations are hydrated from `element_context`, which is "
        "single-notebook by construction (it tier-maps exactly the active "
        "notebook and writes an empty id_map notebook_id), so the constant "
        "empty string is structurally the active notebook.",
    ),
}

# id_map builders: functions that WRITE the ``"notebook_id"`` key an anchor is
# later hydrated from.  Same exhaustive-source rule as REGISTERED_SITES.
BUILDER_SITES: Registry = {
    (GRAPH_REASON, "render_subgraph_context"): (
        ("foreign_notebook_id(node.get('notebook_id', ''), active_notebook_id)",),
        "Direct helper call on the federated node payload (A2).",
    ),
    (FOLLOW_CHAIN, "render_follow_chain_context"): (
        ("foreign_notebook_id(hop.notebook_id, active_notebook_id)",),
        "Direct helper call on the hop's owning notebook id.",
    ),
    (EVIDENCE_CONTEXT, "EvidenceContextService.chunk_context"): (
        ("raw_origin",),
        "`raw_origin` is assigned from the helper a few lines above and used "
        "for exactly this write.",
    ),
    (EVIDENCE_CONTEXT, "EvidenceContextService.knowledge_context._admit"): (
        ("raw_origin",),
        "`raw_origin` is assigned from the helper in the enclosing loop and "
        "used for exactly this write.",
    ),
    (SPREADSHEET, "spreadsheet_prompt_block"): (
        ("citation.notebook_id if citation else ''",),
        "Workbook receipts DO cross notebooks (the ask lane loads one manifest "
        "per participant `(notebook_id, source_id)` ref), but this renderer is "
        "handed only the results — no active notebook id — so it copies the "
        "value `SpreadsheetAnalysisService._citation` already normalised "
        "through the helper for the very same result, rather than "
        "re-implementing the rule a seventh time.  The `else ''` arm is the "
        "degenerate no-delivered-rows case, where there is no citation to "
        "copy and the model field's own default is the answer.",
    ),
    (EVIDENCE_CONTEXT, "EvidenceContextService.element_context"): (
        ("''",),
        "Single-notebook by construction: it tier-maps exactly the active "
        "notebook, so the constant empty string IS the normalised value.  "
        "Pinned because parse_anchors' registration leans on this claim.",
    ),
}

BUILDER_FILES = frozenset(site[0] for site in BUILDER_SITES)

# Construction points that deliberately omit ``notebook_id`` altogether.  These
# are compliant under rule (b); they are pinned positively so the reason stays
# recorded (§A1 lists the memory path explicitly) and so a future edit that
# starts filling the field has to justify itself here.
DELIBERATELY_ABSENT: dict[Site, str] = {
    (
        ASK_SERVICE,
        "AskService._memory_citations",
    ): "Memories belong to the asking user in the active notebook only.",
}


@dataclass
class _Scan:
    """What one pass over the sources found.

    ``offenders`` carry a ``file:line`` diagnostic; they are messages, never
    identity (every set/dict below is keyed by qualname), so the guard survives
    any edit that shifts line numbers.
    """

    offenders: list[str] = field(default_factory=list)
    helper_offenders: list[str] = field(default_factory=list)
    provides: set[Site] = field(default_factory=set)
    absent: set[Site] = field(default_factory=set)
    arg_sources: dict[Site, list[str]] = field(default_factory=dict)
    builder_sources: dict[Site, list[str]] = field(default_factory=dict)

    def merge(self, other: "_Scan") -> None:
        self.offenders.extend(other.offenders)
        self.helper_offenders.extend(other.helper_offenders)
        self.provides |= other.provides
        self.absent |= other.absent
        for target, source in (
            (self.arg_sources, other.arg_sources),
            (self.builder_sources, other.builder_sources),
        ):
            for site, values in source.items():
                target.setdefault(site, []).extend(values)


def _called_name(func: ast.expr) -> str:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return ""


def _model_aliases(tree: ast.AST) -> dict[str, str]:
    """Local name -> model name, for ``from ... import Citation as C``."""
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ImportFrom):
            continue
        for item in node.names:
            if item.name in CONSTRUCTED_MODELS:
                aliases[item.asname or item.name] = item.name
    return aliases


class _SiteCollector(ast.NodeVisitor):
    """Collect construction points and ``"notebook_id"`` dict writes."""

    def __init__(self, aliases: dict[str, str]) -> None:
        self.aliases = aliases
        self.scope: list[str] = []
        # (qualname, model name, [(source text, value node or None)], node)
        self.sites: list[
            tuple[str, str, list[tuple[str, ast.expr | None]], ast.Call]
        ] = []
        # (qualname, source text, value node, lineno)
        self.writes: list[tuple[str, str, ast.expr, int]] = []

    def _scoped(self, node: ast.AST) -> None:
        self.scope.append(node.name)
        self.generic_visit(node)
        self.scope.pop()

    visit_ClassDef = _scoped
    visit_FunctionDef = _scoped
    visit_AsyncFunctionDef = _scoped

    def _model_name(self, func: ast.expr) -> str:
        name = _called_name(func)
        if isinstance(func, ast.Name):
            name = self.aliases.get(func.id, func.id)
        return name if name in CONSTRUCTED_MODELS else ""

    def visit_Call(self, node: ast.Call) -> None:
        model = self._model_name(node.func)
        if model:
            supplies: list[tuple[str, ast.expr | None]] = []
            for keyword in node.keywords:
                if keyword.arg == ID_MAP_KEY:
                    supplies.append((ast.unparse(keyword.value), keyword.value))
                elif keyword.arg is None:
                    # ``Citation(**payload)`` can carry notebook_id invisibly.
                    supplies.append((f"**{ast.unparse(keyword.value)}", None))
            self.sites.append((".".join(self.scope), model, supplies, node))
        self.generic_visit(node)

    def visit_Dict(self, node: ast.Dict) -> None:
        qualname = ".".join(self.scope)
        for key, value in zip(node.keys, node.values):
            if isinstance(key, ast.Constant) and key.value == ID_MAP_KEY:
                self.writes.append(
                    (qualname, ast.unparse(value), value, value.lineno)
                )
        self.generic_visit(node)


def _is_helper_call(value: ast.expr | None) -> bool:
    return (
        isinstance(value, ast.Call) and _called_name(value.func) == HELPER_NAME
    )


def _rival_bindings(tree: ast.AST) -> list[str]:
    """Every binding of the helper's *name* other than the canonical import.

    ``_is_helper_call`` resolves the helper by name, so any of these makes the
    guard accept a call that never reaches ``app.domain.citation_origin``: a
    module-local ``def foreign_notebook_id``, a parameter or loop variable of
    that name, an injected ``self.foreign_notebook_id = ...``, or an
    ``import ... as foreign_notebook_id`` from anywhere else.  ``Store``/``Del``
    ``Name`` contexts cover assignment, ``for``/``with ... as``, walrus,
    comprehension and ``except ... as`` in one rule.
    """
    bindings: list[str] = []
    for node in ast.walk(tree):
        if (
            isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
            and node.name == HELPER_NAME
        ):
            bindings.append(f"line {node.lineno}: a local def/class {HELPER_NAME}")
        elif isinstance(node, ast.arg) and node.arg == HELPER_NAME:
            bindings.append(f"line {node.lineno}: a parameter named {HELPER_NAME}")
        elif (
            isinstance(node, ast.Name)
            and node.id == HELPER_NAME
            and isinstance(node.ctx, (ast.Store, ast.Del))
        ):
            bindings.append(f"line {node.lineno}: a rebinding of {HELPER_NAME}")
        elif (
            isinstance(node, ast.Attribute)
            and node.attr == HELPER_NAME
            and isinstance(node.ctx, (ast.Store, ast.Del))
        ):
            bindings.append(
                f"line {node.lineno}: an assignment to .{HELPER_NAME}"
            )
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            canonical = (
                isinstance(node, ast.ImportFrom) and node.module == HELPER_MODULE
            )
            for item in node.names:
                bound = item.asname or item.name.split(".")[0]
                if bound == HELPER_NAME and not (canonical and item.asname is None):
                    bindings.append(
                        f"line {node.lineno}: an import binding {HELPER_NAME} "
                        f"to something other than {HELPER_MODULE}"
                    )
    return bindings


def _helper_usage_offenders(tree: ast.AST, relative: str) -> list[str]:
    """Hold every module that *calls* the helper to the real helper.

    Compliance above is "the argument is a call to something spelled
    ``foreign_notebook_id``".  That is name resolution, not identity: without
    this check a module could satisfy the whole guard with its own two-line
    stand-in.  So a module that calls the name must import it from
    ``app.domain.citation_origin`` under that exact name, must not bind the
    name to anything else, and must not reach it through an attribute (a
    ``self.foreign_notebook_id`` seam is an injection point the guard cannot
    follow).
    """
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and _called_name(node.func) == HELPER_NAME
    ]
    if not calls:
        return []

    offenders = [
        f"{relative}:{call.lineno}: {ast.unparse(call.func)}() reaches "
        f"{HELPER_NAME} through an attribute; call the imported function"
        for call in calls
        if isinstance(call.func, ast.Attribute)
    ]
    imported = any(
        isinstance(node, ast.ImportFrom)
        and node.module == HELPER_MODULE
        and any(
            item.name == HELPER_NAME and item.asname is None for item in node.names
        )
        for node in ast.walk(tree)
    )
    if not imported:
        offenders.append(
            f"{relative}: calls {HELPER_NAME}() without "
            f"`from {HELPER_MODULE} import {HELPER_NAME}`"
        )
    offenders.extend(
        f"{relative}:{binding} shadows the imported helper"
        for binding in _rival_bindings(tree)
    )
    return offenders


def _collect(
    source: str,
    relative: str,
    *,
    registered: Registry | None = None,
    builders: Registry | None = None,
) -> _Scan:
    registered = REGISTERED_SITES if registered is None else registered
    builders = BUILDER_SITES if builders is None else builders

    tree = ast.parse(source, filename=relative)
    collector = _SiteCollector(_model_aliases(tree))
    collector.visit(tree)

    scan = _Scan(helper_offenders=_helper_usage_offenders(tree, relative))
    for qualname, model, supplies, node in collector.sites:
        site = (relative, qualname)
        if not supplies:
            scan.absent.add(site)
            continue
        scan.provides.add(site)
        allowed = registered.get(site, ((), ""))[0]
        for text, value in supplies:
            scan.arg_sources.setdefault(site, []).append(text)
            if _is_helper_call(value) or text in allowed:
                continue
            detail = text if value is None else f"{ID_MAP_KEY}={text}"
            scan.offenders.append(
                f"{relative}:{node.lineno}: {model}({detail}) in "
                f"{qualname or '<module>'} neither calls {HELPER_NAME}() nor "
                "is registered in REGISTERED_SITES"
            )

    for qualname, text, value, lineno in collector.writes:
        site = (relative, qualname)
        if site not in builders:
            continue
        scan.builder_sources.setdefault(site, []).append(text)
        if _is_helper_call(value) or text in builders[site][0]:
            continue
        scan.offenders.append(
            f'{relative}:{lineno}: id_map write "{ID_MAP_KEY}": {text} in '
            f"{qualname or '<module>'} neither calls {HELPER_NAME}() nor "
            "matches the source pinned in BUILDER_SITES"
        )
    return scan


def _scan_app() -> _Scan:
    scan = _Scan()
    for path in sorted(APP.rglob("*.py")):
        if "__pycache__" in path.parts:
            continue
        relative = path.relative_to(ROOT).as_posix()
        source = path.read_text(encoding="utf-8")
        # Cheap prefilter only: a module that constructs the model under an
        # alias still names it on its import line, so this cannot hide an
        # aliased construction point the way a ``f"{name}("`` prefilter did.
        # ``HELPER_NAME`` widens it past the constructors so the helper-binding
        # check below sees every caller, including one that constructs nothing.
        interesting = (
            relative in BUILDER_FILES
            or HELPER_NAME in source
            or any(name in source for name in CONSTRUCTED_MODELS)
        )
        if not interesting:
            continue
        scan.merge(_collect(source, relative))
    return scan


def test_every_construction_point_normalises_notebook_id() -> None:
    scan = _scan_app()

    assert not scan.offenders, (
        "Citation/AnswerAnchor notebook_id must come from "
        f"app.domain.citation_origin.{HELPER_NAME}() — see this module's "
        "docstring:\n  " + "\n  ".join(sorted(scan.offenders))
    )


def test_every_helper_call_reaches_the_registered_helper() -> None:
    """Compliance above is by *name*; this test is what makes it identity."""
    scan = _scan_app()

    assert not scan.helper_offenders, (
        f"{HELPER_NAME}() must be the one in {HELPER_MODULE}:\n  "
        + "\n  ".join(sorted(scan.helper_offenders))
    )


def test_guard_flags_a_module_local_helper_shadow() -> None:
    """The mutation case for the identity check: a two-line stand-in that
    satisfies every other assertion in this module."""
    scan = _collect(
        f"def {HELPER_NAME}(origin, active):\n"
        "    return str(origin or '')\n"
        "\n"
        "def build(origin, active):\n"
        f"    return Citation(notebook_id={HELPER_NAME}(origin, active))\n",
        "backend/app/services/not_a_real_module.py",
    )

    assert scan.offenders == []  # the by-name rule is happy — that is the point
    assert len(scan.helper_offenders) == 2, scan.helper_offenders
    assert any("without `from " in item for item in scan.helper_offenders)
    assert any("local def/class" in item for item in scan.helper_offenders)


def test_guard_flags_an_attribute_spelled_helper_call() -> None:
    """``self.foreign_notebook_id`` is an injection seam, not the helper."""
    scan = _collect(
        f"from {HELPER_MODULE} import {HELPER_NAME}\n"
        "\n"
        "class Builder:\n"
        "    def build(self, origin, active):\n"
        f"        return Citation(notebook_id=self.{HELPER_NAME}(origin, active))\n",
        "backend/app/services/not_a_real_module.py",
    )

    assert scan.offenders == []
    assert len(scan.helper_offenders) == 1, scan.helper_offenders
    assert "through an attribute" in scan.helper_offenders[0]


def test_guard_accepts_the_canonical_helper_import() -> None:
    scan = _collect(
        f"from {HELPER_MODULE} import {HELPER_NAME}\n"
        "\n"
        "def build(origin, active):\n"
        f"    return Citation(notebook_id={HELPER_NAME}(origin, active))\n",
        "backend/app/services/not_a_real_module.py",
    )

    assert scan.helper_offenders == []


@pytest.mark.parametrize(
    "shadow",
    [
        f"{HELPER_NAME} = lambda origin, active: str(origin or '')",
        f"from app.somewhere_else import normalise as {HELPER_NAME}",
        f"for {HELPER_NAME} in ():\n    pass",
    ],
)
def test_guard_flags_every_other_rebinding_of_the_name(shadow: str) -> None:
    scan = _collect(
        f"from {HELPER_MODULE} import {HELPER_NAME}\n"
        f"{shadow}\n"
        "\n"
        "def build(origin, active):\n"
        f"    return Citation(notebook_id={HELPER_NAME}(origin, active))\n",
        "backend/app/services/not_a_real_module.py",
    )

    assert len(scan.helper_offenders) == 1, scan.helper_offenders
    assert "shadows the imported helper" in scan.helper_offenders[0]


def test_guard_ignores_the_helper_name_in_a_module_that_never_calls_it() -> None:
    """``app/domain/citation_origin.py`` itself defines the name and must not
    be read as its own shadow."""
    scan = _collect(
        f"def {HELPER_NAME}(origin, active):\n    return str(origin or '')\n",
        "backend/app/domain/citation_origin.py",
    )

    assert scan.helper_offenders == []


def test_registered_sites_have_not_gone_stale() -> None:
    """The allowlist may only shrink: every entry must still name a real site."""
    scan = _scan_app()

    stale = sorted(set(REGISTERED_SITES) - scan.provides)
    assert not stale, (
        "REGISTERED_SITES entries no longer match a Citation/AnswerAnchor "
        "construction point that supplies notebook_id; delete them: "
        f"{stale}"
    )


def test_registered_sites_are_not_blanket_exemptions() -> None:
    """An entry covers the argument sources it names — nothing else.

    Without this, adding a second, differently-spelled construction point to an
    already-registered function would inherit the exemption silently.
    """
    scan = _scan_app()

    for site, (expected, _reason) in REGISTERED_SITES.items():
        observed = tuple(sorted(scan.arg_sources.get(site, [])))
        assert observed == tuple(sorted(expected)), (
            f"{site} now supplies notebook_id as {observed}, but "
            f"REGISTERED_SITES pins {tuple(sorted(expected))} "
            f"({len(observed)} vs {len(expected)} construction points). "
            "Route the new one through the helper, or update the entry with a "
            "reason."
        )


def test_builder_sites_pin_their_id_map_writes() -> None:
    """The id_map builders' ``"notebook_id"`` writes are pinned exactly.

    Reverting one to an inline expression (the A2 ``tier == "base"`` test, say)
    is otherwise invisible: no constructor call is involved.
    """
    scan = _scan_app()

    for site, (expected, _reason) in BUILDER_SITES.items():
        observed = tuple(sorted(scan.builder_sources.get(site, [])))
        assert observed == tuple(sorted(expected)), (
            f'{site} now writes "notebook_id" as {observed}, but BUILDER_SITES '
            f"pins {tuple(sorted(expected))}. Route the write through "
            f"{HELPER_NAME}(), or update the entry with a reason."
        )


def test_deliberately_absent_sites_still_omit_the_field() -> None:
    scan = _scan_app()

    for site, reason in DELIBERATELY_ABSENT.items():
        assert site in scan.absent, (
            f"{site} no longer omits notebook_id (recorded reason: {reason}); "
            "either restore the omission or move it to REGISTERED_SITES."
        )
        assert site not in scan.provides, site


def test_guard_flags_an_unregistered_arbitrary_expression() -> None:
    """The guard's own detection, pinned so a refactor cannot silently blind it
    (this is the mutation case: a fresh site with a bare variable)."""
    scan = _collect(
        "def build(origin):\n"
        "    return Citation(label='x', notebook_id=origin)\n",
        "backend/app/services/not_a_real_module.py",
    )

    assert len(scan.offenders) == 1, scan.offenders
    assert "not_a_real_module.py:2" in scan.offenders[0]
    assert "notebook_id=origin" in scan.offenders[0]


def test_guard_accepts_the_helper_and_a_missing_keyword() -> None:
    scan = _collect(
        "def build(origin, active):\n"
        "    a = Citation(notebook_id=foreign_notebook_id(origin, active))\n"
        "    b = AnswerAnchor(key='k1')\n"
        "    return a, b\n",
        "backend/app/services/not_a_real_module.py",
    )

    assert scan.offenders == []
    assert scan.provides == {("backend/app/services/not_a_real_module.py", "build")}
    assert scan.absent == {("backend/app/services/not_a_real_module.py", "build")}


def test_guard_flags_a_splat_that_could_smuggle_notebook_id() -> None:
    """``Citation(**payload)`` used to read as "no keyword" — i.e. compliant."""
    scan = _collect(
        "def build(payload):\n    return Citation(**payload)\n",
        "backend/app/services/not_a_real_module.py",
    )

    site = ("backend/app/services/not_a_real_module.py", "build")
    assert len(scan.offenders) == 1, scan.offenders
    assert "Citation(**payload)" in scan.offenders[0]
    assert scan.provides == {site}
    assert scan.absent == set()


def test_guard_accepts_a_registered_splat() -> None:
    site = ("backend/app/services/not_a_real_module.py", "build")
    scan = _collect(
        "def build(payload):\n    return Citation(**payload)\n",
        site[0],
        registered={site: (("**payload",), "pinned in this test")},
    )

    assert scan.offenders == []
    assert scan.arg_sources[site] == ["**payload"]


def test_guard_resolves_an_import_alias() -> None:
    """``from app.models.ask import Citation as C`` must not evade the scan."""
    scan = _collect(
        "from app.models.ask import Citation as C\n"
        "\n"
        "def build(origin):\n"
        "    return C(notebook_id=origin)\n",
        "backend/app/services/not_a_real_module.py",
    )

    assert len(scan.offenders) == 1, scan.offenders
    assert "Citation(notebook_id=origin)" in scan.offenders[0]


def test_guard_flags_an_unregistered_builder_dict_write() -> None:
    site = ("backend/app/services/not_a_real_module.py", "build")
    scan = _collect(
        "def build(node, active):\n"
        "    return {'name': node['name'], 'notebook_id': node['notebook_id']}\n",
        site[0],
        builders={site: (("foreign_notebook_id(node, active)",), "test pin")},
    )

    assert len(scan.offenders) == 1, scan.offenders
    assert 'id_map write "notebook_id": node[\'notebook_id\']' in scan.offenders[0]
    assert "BUILDER_SITES" in scan.offenders[0]


def test_guard_ignores_notebook_id_writes_outside_builder_sites() -> None:
    """Only the registered id_map builders are held to the rule; the app writes
    that key in dozens of unrelated payloads."""
    scan = _collect(
        "def unrelated(row):\n    return {'notebook_id': row['notebook_id']}\n",
        "backend/app/services/not_a_real_module.py",
    )

    assert scan.offenders == []
    assert scan.builder_sources == {}


def test_render_subgraph_context_requires_active_notebook_id() -> None:
    """A2's "required, no default" claim, enforced rather than only documented.

    A default would let a caller silently fall back to comparing against ``""``,
    which normalises nothing.
    """
    parameter = inspect.signature(render_subgraph_context).parameters[
        "active_notebook_id"
    ]

    assert parameter.kind is inspect.Parameter.KEYWORD_ONLY
    assert parameter.default is inspect.Parameter.empty
    with pytest.raises(TypeError):
        render_subgraph_context([])


@pytest.mark.parametrize(
    "origin,active,expected",
    [
        ("nb-base", "nb-active", "nb-base"),
        ("nb-active", "nb-active", ""),
        ("", "nb-active", ""),
        (None, "nb-active", ""),
        ("nb-base", "", "nb-base"),
        ("nb-base", None, "nb-base"),
    ],
)
def test_helper_returns_only_genuinely_foreign_ids(origin, active, expected):
    assert foreign_notebook_id(origin, active) == expected


def test_helper_is_idempotent() -> None:
    once = foreign_notebook_id("nb-active", "nb-active")
    assert foreign_notebook_id(once, "nb-active") == once
    other = foreign_notebook_id("nb-base", "nb-active")
    assert foreign_notebook_id(other, "nb-active") == other
