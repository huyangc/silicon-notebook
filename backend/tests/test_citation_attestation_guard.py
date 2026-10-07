"""D9 -- every place that can put an element locator in front of a reader is
REGISTERED, with how its evidence gets a retrieval-time snapshot.

A global Ask judges each citation and anchor against the run's evidence table
(``global_citation_check``). A card whose element no producer registered is
reported "无法核对" on a run where nothing changed, so a new construction site
that forgets to register is a user-visible regression the behavioural tests
only catch if someone thought to write one. This guard makes the registry
equal to reality:

* it scans ``backend/app`` for every ``Citation(...)`` / ``AnswerAnchor(...)``
  construction (under any import alias), every id_map / evidence_by_id entry
  (a dict literal carrying ``"element_id"`` beside ``"object_id"`` or
  ``"object_type"``), every ``x["element_id"] = ...`` store, every
  ``model_copy(update={"element_id": ...})`` and every ``{**x, "element_id":
  ...}`` rewrite;
* each site -- file + enclosing function + callee, never a line number -- must
  appear in ``REGISTRY`` with exactly one classification, and every registry
  entry must still exist (a site that disappears fails too);
* each classification carries a proof the guard re-checks:

  ``federated``            the passage is fingerprinted by ``chunk_federation``
                           (the fan-out, or ``attest_selected_passages`` for the
                           mix branch's KG-overlay leg);
  ``read-registered``      a producer hashes the text it read (``attest_read``);
  ``pointer-registered``   a producer attests the pointer (``attest_pointers``),
                           including the J2 clears that drop a dead locator;
  ``inherited``            copies an entry of an id_map every writer of which
                           is registered here. Allowed ONLY for the sites in
                           ``INHERITED_SITES`` (``parse_anchors``), and the
                           proof re-checks that the locator it writes is read
                           from the named id_map parameter and nothing else;
  ``not-a-locator``        a telemetry dict that never reaches a reader: every
                           such dict literal in the function must go straight
                           (or through one local name) into a telemetry sink
                           call (``TELEMETRY_SINKS``);
  ``no-element``           no element exists by construction (document rows,
                           external URL material, Memory): the site must pass
                           the empty-string LITERAL -- a producer cannot dodge
                           the fingerprint check by leaving a real element out;
  ``global-unreachable``   never runs inside a global Ask; the named GATE is
                           re-checked (a code gate, or an import gate: none of
                           the global engine's modules import the module, even
                           one import away);
  ``single-notebook-only`` runs only outside a global Ask, behind the named gate.

  A "calls" proof asserts that function X contains a LIVE call to Y (a call
  under ``if False:`` or after an unconditional ``return`` does not count). An
  "if-calls" proof asserts an ``if`` whose test mentions Y() with the right
  POLARITY: either the test is true whenever Y reports a global run and the
  branch exits (``if federated_ask_active(): return []``), or the test is false
  whenever Y reports one and the branch holds the named guarded call
  (``if ... and current_federated_run_plan() is None: drop(...)``).

What this guard is, and is not: a REGISTRY in which every construction site is
accounted for, with its proofs re-checked syntactically. It does not prove
behaviour -- a registration call can still be moved to a point where it
registers the wrong rows, and the guard stays green. Behaviour is proven by the
end-to-end tests (``test_global_citation_producers_e2e``: every producer and
reasoning action, a healthy run is clean and attributed; the per-producer race
tests in ``test_producer_attestation_races`` / ``test_kg_attestation_races`` /
``test_global_ask_citation_check`` and their PostgreSQL twins).
"""
from __future__ import annotations

import ast
import functools
from collections import Counter
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = ROOT / "backend" / "app"

EC = "services/evidence_context.py"
AS = "services/ask_service.py"
SA = "services/spreadsheet_analysis.py"
RR = "services/reasoning_retrieval.py"
RS = "services/retrieval_service.py"
CF = "services/chunk_federation.py"
RC = "services/retrieval_candidates.py"
GA = "services/global_ask.py"
DSO = "services/document_source_overview.py"

#: Modules a global Ask executes (the engine and everything it imports for
#: citations). An "import gate" says none of them imports the gated module.
GLOBAL_ENGINE_MODULES = (
    GA, AS, EC, RR, RS, CF, RC, SA, DSO,
    "services/graph_retrieval.py", "services/collection_enumeration.py",
    "services/collection_enumeration_answer.py", "services/document_catalog_overview.py",
    "services/kg/follow_chain.py", "services/kg/graph_reason.py",
    "services/memory_retrieval.py", "services/evidence_attestation.py",
    "services/global_citation_check.py", "services/reference_liveness.py",
)

_FEDERATED_PROOF = (
    ("calls", CF, "_report_evidence", "on_evidence"),
    ("calls", CF, "attest_selected_passages", "_report_evidence"),
    ("calls", RC, "_attest_overlay_passages", "attest_selected_passages"),
    ("calls", RC, "CandidateRetrievalService._mix_retrieve", "_attest_overlay_passages"),
)
_KG_POINTER_PROOF = (
    ("calls", EC, "EvidenceContextService.knowledge_context", "_attest_kg_anchor_evidence"),
    ("calls", EC, "_attest_kg_anchor_evidence", "attest_pointers"),
)
_COLLECTION_PROOF = (
    ("calls", EC, "EvidenceContextService.collection_item_citations", "_attest_collection_citations"),
    ("calls", EC, "_attest_collection_citations", "attest_read"),
)
_TABLE_PROOF = (
    ("calls", SA, "SpreadsheetAnalysisService._execute", "_attested_row_citation"),
    ("calls", SA, "_attested_row_citation", "attest_pointers"),
)
_CHAIN_PROOF = (
    ("calls", RR, "ReasoningRetriever.follow_chain", "attest_chain_evidence"),
    ("calls", RS, "attest_chain_evidence", "attest_pointers"),
)
_ELEMENT_ARM_CLOSED = (
    ("if-calls", RC, "CandidateRetrievalService.retrieve_elements", "federated_ask_active"),
    ("if-calls", RR, "ReasoningRetriever._element_search_skip", "subjectless_run_active"),
    ("if-calls", RR, "ReasoningRetriever._first_round_empty_fallback", "subjectless_run_active",
     "search_elements"),
)
_PLUGIN_ENGINE_REFUSED = (
    ("compares", GA, "GlobalAskService._resolve_mode", "ask_plugin_engine"),
    ("raises-name", GA, "GlobalAskService._resolve_mode", "_PLUGIN_ENGINE_COPY"),
)


def _import_gate(module_path: str):
    return (("not-imported", module_path),)


#: (file, enclosing function, callee) -> (classification, proofs, reason, count)
REGISTRY = {
    # --- evidence_context -------------------------------------------------
    (EC, "EvidenceContextService.chunk_citations", "Citation"): (
        "federated", _FEDERATED_PROOF, "chunk cards name element_ids[0] of a fingerprinted passage", 1),
    (EC, "EvidenceContextService.chunk_context", 'dict:"element_id"'): (
        "federated", _FEDERATED_PROOF, "chunk id_map entry, same passage", 1),
    (EC, "EvidenceContextService.chunk_context", "splat:element_id"): (
        "federated", _FEDERATED_PROOF, "single-element chunk locator rewrite", 1),
    (EC, "EvidenceContextService.citations_from", "Citation"): (
        "pointer-registered",
        (("calls", EC, "EvidenceContextService.citations_from", "_live_kg_evidence"),
         ("calls", EC, "_live_kg_evidence", "attest_pointers")),
        "KG evidence cards; a pointer dead before the question is not minted (J2)", 1),
    (EC, "EvidenceContextService.knowledge_context._admit", 'dict:"element_id"'): (
        "pointer-registered", _KG_POINTER_PROOF, "KG object anchor locator (occurrences[0])", 1),
    (EC, "_attest_kg_anchor_evidence", 'store:["element_id"]'): (
        "pointer-registered", _KG_POINTER_PROOF, "J2 clear of a dead KG anchor locator", 1),
    (EC, "_snapshot_foreign_entry", 'store:["element_id"]'): (
        "no-element", (),
        "B-11: a KG anchor whose source belongs to another library keeps no locator", 1),
    ("services/graph_retrieval.py", "GraphRetrievalService._snapshot_foreign_relation_evidence",
     "splat:element_id"): (
        "no-element", (),
        "B-11: a chain hop's evidence item from another library keeps no locator", 1),
    ("services/knowledge_query.py", "KnowledgeQueryService._viewer_resolved_evidence",
     "splat:element_id"): (
        "no-element", (),
        "B-11: a concept-detail raw evidence item naming another library's element "
        "keeps no locator", 1),
    ("repositories/sqlite/knowledge_store.py", "KnowledgeStore._enrich_evidence",
     'store:["element_id"]'): (
        "no-element", (),
        "B-11: an occurrence naming another library's element keeps no locator", 1),
    ("repositories/postgres/knowledge_store.py", "KnowledgeStore._enrich_evidence",
     'store:["element_id"]'): (
        "no-element", (),
        "B-11: an occurrence naming another library's element keeps no locator", 1),
    (EC, "EvidenceContextService.collection_item_citations", "Citation"): (
        "read-registered", _COLLECTION_PROOF,
        "enumerated element/KG rows (hydrated full text); document rows pass element_id='' (J3)", 2),
    (EC, "EvidenceContextService.parse_anchors", "AnswerAnchor"): (
        "inherited",
        (("reads-param", EC, "EvidenceContextService.parse_anchors", "evidence_by_id"),),
        "copies an id_map entry; every id_map writer is registered here", 1),
    (EC, "EvidenceContextService.element_citations", "Citation"): (
        "global-unreachable", _ELEMENT_ARM_CLOSED, "single-library element search is closed in peer mode", 1),
    (EC, "EvidenceContextService.element_context", 'dict:"element_id"'): (
        "global-unreachable", _ELEMENT_ARM_CLOSED, "same element arm", 1),
    (EC, "EvidenceContextService.element_context", "splat:element_id"): (
        "global-unreachable", _ELEMENT_ARM_CLOSED, "same element arm", 1),
    (EC, "EvidenceContextService.external_citations", "Citation"): (
        "no-element", (), "URL-backed material from outside every library", 1),
    (EC, "EvidenceContextService.external_context", 'dict:"element_id"'): (
        "no-element", (), "URL-backed material from outside every library", 1),
    # --- other producers --------------------------------------------------
    (DSO, "prepare_source_overview", "Citation"): (
        "read-registered",
        (("calls", DSO, "prepare_source_overview", "_attest_cited"),
         ("calls", DSO, "_attest_cited", "attest_read")),
        "document overview / read_document excerpts, full element text in hand", 1),
    (DSO, "prepare_source_overview", 'dict:"element_id"'): (
        "read-registered",
        (("calls", DSO, "prepare_source_overview", "_attest_cited"),),
        "same excerpts as id_map entries", 1),
    ("services/collection_enumeration_answer.py", "_preview_evidence", 'dict:"element_id"'): (
        "read-registered", _COLLECTION_PROOF,
        "copies the element of the citation collection_item_citations minted", 1),
    ("services/kg/follow_chain.py", "render_follow_chain_context", 'dict:"element_id"'): (
        "pointer-registered", _CHAIN_PROOF, "chain hop primary evidence", 1),
    (RS, "_hop_without_dead_primary", "splat:element_id"): (
        "pointer-registered",
        (("calls", RS, "attest_chain_evidence", "_hop_without_dead_primary"),),
        "J2 clear of a dead chain-hop locator", 1),
    (SA, "SpreadsheetAnalysisService._citation", "Citation"): (
        "pointer-registered", _TABLE_PROOF, "workbook row receipt", 1),
    (SA, "spreadsheet_prompt_block", 'dict:"element_id"'): (
        "pointer-registered", _TABLE_PROOF, "copies the receipt's (attested) element", 1),
    (SA, "_attested_row_citation", "model_copy:element_id"): (
        "pointer-registered", _TABLE_PROOF, "J2 clear of a dead row locator", 1),
    (AS, "AskService._memory_citations", "Citation"): (
        "no-element", (), "Memory rows are not source elements (and memory is off in peer mode)", 1),
    (AS, "AskService.ask_plugin_engine", "Citation"): (
        "global-unreachable", _PLUGIN_ENGINE_REFUSED, "global Ask refuses plugin engines", 1),
    (AS, "AskService.ask_plugin_engine", "AnswerAnchor"): (
        "global-unreachable", _PLUGIN_ENGINE_REFUSED, "global Ask refuses plugin engines", 1),
    ("services/reference_liveness.py", "drop_dangling_references", "model_copy:element_id"): (
        "single-notebook-only",
        (("if-calls", AS, "AskService._drop_dangling_references", "current_federated_run_plan",
          "drop_dangling_references"),),
        "J2 single-notebook pass; skipped whenever a run plan is installed", 1),
    ("services/reference_liveness.py", "prune_dead_report_elements", "splat:element_id"): (
        "single-notebook-only",
        (("if-calls", "services/report_engine.py",
          "ReportEngine._sections_without_dead_elements", "current_federated_run_plan"),
         ("calls", "services/report_engine.py", "ReportEngine.run_final_audit_stage",
          "_sections_without_dead_elements")),
        "J2 for deep reports: clears a dead locator; skipped whenever a run plan is installed", 1),
    # --- never inside a global Ask (import gates) --------------------------
    ("services/report_engine.py", "ReportEngine._assemble._sub", 'dict:"element_id"'): (
        "global-unreachable", _import_gate("services/report_engine.py"),
        "deep reports run per notebook, never under a global run plan", 1),
    # The same stored-citation literal: its splat adds the citation's library
    # fields (``reference_library_fields``), never a locator.
    ("services/report_engine.py", "ReportEngine._assemble._sub", "splat:element_id"): (
        "global-unreachable", _import_gate("services/report_engine.py"),
        "deep reports run per notebook, never under a global run plan", 1),
    ("services/notebook_sharing.py", "NotebookCopyService.copy_notebook", 'dict:"element_id"'): (
        "global-unreachable", _import_gate("services/notebook_sharing.py"),
        "notebook copy job remaps stored rows", 1),
    ("services/notebook_sharing.py", "NotebookCopyService.copy_notebook", 'store:["element_id"]'): (
        "global-unreachable", _import_gate("services/notebook_sharing.py"),
        "notebook copy job remaps stored rows", 2),
    ("services/knowledge_lifecycle.py", "KnowledgeLifecycleService.complete_relations_for_source",
     "splat:element_id"): (
        "global-unreachable", _import_gate("services/knowledge_lifecycle.py"),
        "relation completion at ingestion", 1),
    ("repositories/sqlite/memory_store.py", "MemoryStore._validate_evidence_ref_on", "splat:element_id"): (
        "global-unreachable", _import_gate("repositories/sqlite/memory_store.py"),
        "Memory evidence validation on write", 1),
    ("repositories/postgres/memory_store.py", "MemoryStore._validate_evidence_ref_on", "splat:element_id"): (
        "global-unreachable", _import_gate("repositories/postgres/memory_store.py"),
        "Memory evidence validation on write", 1),
    ("api/mcp_tools/memory_context.py", "register_memory_context_tools.ask_notebook", 'dict:"element_id"'): (
        "single-notebook-only", _import_gate("api/mcp_tools/memory_context.py"),
        "MCP ask_notebook output: a notebook ask, never a global run", 1),
}

CLASSIFICATIONS = {
    "federated", "read-registered", "pointer-registered", "inherited", "not-a-locator",
    "no-element", "global-unreachable", "single-notebook-only",
}

#: The only sites allowed to claim ``inherited`` (P3-3: it must not become the
#: catch-all for a new producer that forgot to register).
INHERITED_SITES = {(EC, "EvidenceContextService.parse_anchors", "AnswerAnchor")}

#: Calls a ``not-a-locator`` dict may flow into: event and log sinks only.
TELEMETRY_SINKS = {
    "emit", "_emit", "debug", "info", "warning", "error", "exception",
    "append_ask_trace", "emit_stage_timing",
}


# ---------------------------------------------------------------------------
# Scanner
# ---------------------------------------------------------------------------

def _aliases(tree, classes=("Citation", "AnswerAnchor")) -> dict:
    """Every local name bound to one of ``classes``: the class name itself, an
    ``import ... as X`` and a plain ``X = Citation`` rebinding."""
    names = {name: name for name in classes}
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            for alias in node.names:
                if alias.name in classes and alias.asname:
                    names[alias.asname] = alias.name
    changed = True
    while changed:
        changed = False
        for node in ast.walk(tree):
            if (isinstance(node, ast.Assign) and isinstance(node.value, (ast.Name, ast.Attribute))):
                value = node.value.id if isinstance(node.value, ast.Name) else node.value.attr
                for target in node.targets:
                    if (isinstance(target, ast.Name) and value in names
                            and target.id not in names):
                        names[target.id] = names[value]
                        changed = True
    return names


def _keys(node: ast.Dict) -> list:
    return [key.value if isinstance(key, ast.Constant) else key for key in node.keys]


@functools.lru_cache(maxsize=None)
def parse(source: str):
    """``ast.parse`` memoised by source text (the trees are never mutated)."""
    return ast.parse(source)


def parse_file(path) -> ast.Module:
    return parse(Path(path).read_text(encoding="utf-8"))


def scan_source(relative: str, source: str) -> list:
    """Every site in one module as ``(file, qualname, callee)`` (with repeats)."""
    tree = parse(source)
    aliases = _aliases(tree)
    sites: list = []

    def visit(node, stack):
        for child in ast.iter_child_nodes(node):
            scope = stack
            if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                scope = stack + [child.name]
            qualname = ".".join(stack)
            if isinstance(child, ast.Call):
                func = child.func
                name = func.id if isinstance(func, ast.Name) else (
                    func.attr if isinstance(func, ast.Attribute) else "")
                if name in aliases:
                    sites.append((relative, qualname, aliases[name]))
                if (name in ("model_validate", "model_construct")
                        and isinstance(func, ast.Attribute)
                        and isinstance(func.value, ast.Name) and func.value.id in aliases):
                    sites.append((relative, qualname, f"unclassifiable:{aliases[func.value.id]}.{name}"))
                if name == "dict" and any(k.arg == "element_id" for k in child.keywords):
                    sites.append((relative, qualname, "unclassifiable:dict(element_id=)"))
                for keyword in child.keywords:
                    if (keyword.arg == "update" and isinstance(keyword.value, ast.Dict)
                            and "element_id" in _keys(keyword.value)):
                        sites.append((relative, qualname, "model_copy:element_id"))
                    if keyword.arg == "update" and not isinstance(keyword.value, ast.Dict):
                        sites.append((relative, qualname, "unclassifiable:model_copy(update=<non-literal>)"))
                    if (keyword.arg in ("citations", "anchors")
                            and isinstance(keyword.value, (ast.List, ast.ListComp, ast.Tuple))
                            and any(isinstance(item, ast.Dict) for item in ast.walk(keyword.value))):
                        sites.append((relative, qualname, f"unclassifiable:{keyword.arg}=[dict]"))
            if isinstance(child, ast.Dict):
                keys = _keys(child)
                if "element_id" in keys and ("object_id" in keys or "object_type" in keys):
                    sites.append((relative, qualname, 'dict:"element_id"'))
                if "element_id" in keys and None in keys:
                    sites.append((relative, qualname, "splat:element_id"))
            if isinstance(child, ast.Assign):
                for target in child.targets:
                    if (isinstance(target, ast.Subscript)
                            and isinstance(target.slice, ast.Constant)
                            and target.slice.value == "element_id"):
                        sites.append((relative, qualname, 'store:["element_id"]'))
            if isinstance(child, (ast.Assign, ast.AugAssign, ast.AnnAssign)):
                targets = child.targets if isinstance(child, ast.Assign) else [child.target]
                for target in targets:
                    if isinstance(target, ast.Attribute) and target.attr == "element_id":
                        sites.append((relative, qualname, "unclassifiable:.element_id="))
            visit(child, scope)

    visit(tree, [])
    return sites


@functools.lru_cache(maxsize=None)
def _scan_source_cached(relative: str, source: str) -> tuple:
    return tuple(scan_source(relative, source))


def scan() -> Counter:
    """Every site under ``APP``. No text pre-filter: every module is parsed, so
    an alias, a single-quoted key or any spelling the AST normalises is seen."""
    found: Counter = Counter()
    for path in sorted(APP.rglob("*.py")):
        found.update(_scan_source_cached(str(path.relative_to(APP)), path.read_text(encoding="utf-8")))
    return found


# ---------------------------------------------------------------------------
# Proofs
# ---------------------------------------------------------------------------

def _function(relative: str, qualname: str):
    tree = parse_file(APP / relative)
    node = tree
    for part in qualname.split("."):
        node = next(
            (child for child in ast.iter_child_nodes(node)
             if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))
             and child.name == part),
            None,
        )
        if node is None:
            return None
    return node


def _constant_truth(test) -> "bool | None":
    if isinstance(test, ast.Constant):
        return bool(test.value)
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        inner = _constant_truth(test.operand)
        return None if inner is None else not inner
    return None


_TERMINAL = (ast.Return, ast.Raise, ast.Continue, ast.Break)


def live_walk(node):
    """``ast.walk`` without dead code: the body of ``if <false constant>:`` /
    ``while <false constant>:``, the ``else`` of ``if <true constant>:``, either
    arm of a constant conditional expression, and every statement after an
    unconditional return/raise/continue/break in the same block."""
    stack = [node]
    while stack:
        current = stack.pop()
        yield current
        if isinstance(current, (ast.If, ast.While, ast.IfExp)):
            truth = _constant_truth(current.test)
            stack.append(current.test)
            if isinstance(current, ast.IfExp):
                branches = [current.body, current.orelse]
                if truth is True:
                    branches = [current.body]
                elif truth is False:
                    branches = [current.orelse]
                stack.extend(branches)
                continue
            if truth is not False:
                stack.extend(_live_block(current.body))
            if truth is not True:
                stack.extend(_live_block(current.orelse))
            continue
        for field, value in ast.iter_fields(current):
            if isinstance(value, list) and value and isinstance(value[0], ast.stmt):
                stack.extend(_live_block(value))
            elif isinstance(value, list):
                stack.extend(item for item in value if isinstance(item, ast.AST))
            elif isinstance(value, ast.AST):
                stack.append(value)


def _live_block(statements) -> list:
    live = []
    for statement in statements:
        live.append(statement)
        if isinstance(statement, _TERMINAL):
            break
    return live


def _call_name(call) -> str:
    func = call.func
    return func.id if isinstance(func, ast.Name) else getattr(func, "attr", "")


def _called_names(node) -> set:
    """Names of every LIVE call under ``node`` (see ``live_walk``)."""
    return {_call_name(call) for call in live_walk(node) if isinstance(call, ast.Call)}


def _global_implies(test, name) -> "bool | None":
    """What ``test`` evaluates to whenever ``name()`` reports a global run
    (returns a truthy, non-None value): True, False, or None (undetermined)."""
    if isinstance(test, ast.Call) and _call_name(test) == name:
        return True
    if isinstance(test, ast.UnaryOp) and isinstance(test.op, ast.Not):
        inner = _global_implies(test.operand, name)
        return None if inner is None else not inner
    if (isinstance(test, ast.Compare) and len(test.ops) == 1
            and isinstance(test.left, ast.Call) and _call_name(test.left) == name
            and isinstance(test.comparators[0], ast.Constant)
            and test.comparators[0].value is None):
        if isinstance(test.ops[0], ast.Is):
            return False
        if isinstance(test.ops[0], ast.IsNot):
            return True
        return None
    if isinstance(test, ast.BoolOp):
        values = [_global_implies(value, name) for value in test.values]
        if isinstance(test.op, ast.And):
            if False in values:
                return False
            return True if all(value is True for value in values) else None
        if True in values:
            return True
        return False if all(value is False for value in values) else None
    return None


def _gate_holds(function, name, guarded=None) -> bool:
    """An ``if`` on ``name()`` that keeps the global run out: an EXIT gate (the
    test is true in a global run and the branch returns/raises) or an ENCLOSING
    gate (the test is false in a global run and the branch holds ``guarded``)."""
    for branch in live_walk(function):
        if not isinstance(branch, ast.If):
            continue
        implied = _global_implies(branch.test, name)
        body = _live_block(branch.body)
        if implied is True and body and isinstance(body[-1], (ast.Return, ast.Raise)):
            return True
        if implied is False and guarded is not None and any(
            guarded in _called_names(statement) for statement in body
        ):
            return True
    return False


def _imported_modules(relative: str) -> set:
    tree = parse_file(APP / relative)
    modules = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
            modules.update(f"{node.module}.{alias.name}" for alias in node.names)
        elif isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
    return modules


def _module_name(relative: str) -> str:
    return "app." + relative[:-3].replace("/", ".")


def _module_path(module: str) -> "str | None":
    """``app.x.y`` -> ``x/y.py`` (or ``x/y/__init__.py``) when it exists."""
    if not module.startswith("app."):
        return None
    base = module[4:].replace(".", "/")
    for candidate in (f"{base}.py", f"{base}/__init__.py"):
        if (APP / candidate).is_file():
            return candidate
    return None


def _reachable_imports(importer: str) -> set:
    """What ``importer`` imports, and what each of those imports: one level of
    indirection, so a gated module re-exported through a helper is caught."""
    direct = _imported_modules(importer)
    reachable = set(direct)
    for module in direct:
        path = _module_path(module)
        if path is not None:
            reachable |= _imported_modules(path)
    return reachable


def proof_problems(proof) -> list:
    kind = proof[0]
    if kind == "not-imported":
        module = _module_name(proof[1])
        return [
            f"{importer} reaches {module} (directly or one import away): the import gate is gone"
            for importer in GLOBAL_ENGINE_MODULES
            if module in _reachable_imports(importer)
        ]
    _kind, relative, qualname, name, *extra = proof
    node = _function(relative, qualname)
    if node is None:
        return [f"{relative}::{qualname} no longer exists (proof {proof})"]
    if kind == "calls" and name not in _called_names(node):
        return [f"{relative}::{qualname} no longer calls {name} (on a live path)"]
    if kind == "if-calls" and not _gate_holds(node, name, extra[0] if extra else None):
        return [f"{relative}::{qualname} lost its `if {name}()` gate (or its polarity/guarded call)"]
    if kind == "reads-param":
        return _reads_param_problems(node, relative, qualname, name)
    if kind == "compares" and not any(
        isinstance(compare, ast.Compare)
        and any(isinstance(item, ast.Constant) and item.value == name
                for item in [compare.left, *compare.comparators])
        for compare in ast.walk(node)
    ):
        return [f"{relative}::{qualname} no longer compares against {name!r}"]
    if kind == "raises-name" and not any(
        isinstance(raised, ast.Raise)
        and any(isinstance(item, ast.Name) and item.id == name for item in ast.walk(raised))
        for raised in ast.walk(node)
    ):
        return [f"{relative}::{qualname} no longer raises with {name}"]
    return []


def _reads_param_problems(function, relative, qualname, param) -> list:
    """Every Citation/AnswerAnchor ``element_id=`` in ``function`` is derived
    only from names bound to ``param[...]`` (or ``param.get(...)``): the site
    copies an id_map entry and adds no locator of its own."""
    if param not in [arg.arg for arg in function.args.args]:
        return [f"{relative}::{qualname} has no parameter {param!r}"]
    derived = {param}
    for assign in ast.walk(function):
        if (isinstance(assign, ast.Assign) and len(assign.targets) == 1
                and isinstance(assign.targets[0], ast.Name)):
            names = {n.id for n in ast.walk(assign.value) if isinstance(n, ast.Name)}
            if isinstance(assign.value, ast.Subscript) and names & derived:
                derived.add(assign.targets[0].id)
    problems = []
    seen = False
    for call in ast.walk(function):
        if isinstance(call, ast.Call) and _call_name(call) in ("Citation", "AnswerAnchor"):
            for keyword in call.keywords:
                if keyword.arg != "element_id":
                    continue
                seen = True
                names = {n.id for n in ast.walk(keyword.value) if isinstance(n, ast.Name)}
                sources = names - {"str"}
                if not sources or not sources <= derived:
                    problems.append(
                        f"{relative}::{qualname} writes element_id from {sorted(sources)}, "
                        f"not only from {param!r}")
    return problems if seen else [f"{relative}::{qualname} writes no element_id to inherit"]


def not_a_locator_problems(site) -> list:
    """Every element_id dict literal in the function goes straight into a
    telemetry sink call, or into one local name used only as such an argument."""
    relative, qualname, _callee = site
    function = _function(relative, qualname)
    if function is None:
        return [f"{relative}::{qualname} no longer exists"]
    sink_args: set = set()
    sink_names: set = set()
    for call in ast.walk(function):
        if isinstance(call, ast.Call) and _call_name(call) in TELEMETRY_SINKS:
            for arg in [*call.args, *(k.value for k in call.keywords)]:
                sink_args.add(id(arg))
                if isinstance(arg, ast.Name):
                    sink_names.add(arg.id)
    bound: dict = {}
    for assign in ast.walk(function):
        if (isinstance(assign, ast.Assign) and len(assign.targets) == 1
                and isinstance(assign.targets[0], ast.Name) and isinstance(assign.value, ast.Dict)):
            bound[id(assign.value)] = assign.targets[0].id
    loads: Counter = Counter()
    sunk: Counter = Counter()
    for node in ast.walk(function):
        if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load):
            loads[node.id] += 1
            if id(node) in sink_args:
                sunk[node.id] += 1
    problems = []
    for node in ast.walk(function):
        if isinstance(node, ast.Dict) and "element_id" in _keys(node):
            if id(node) in sink_args:
                continue
            name = bound.get(id(node))
            if name is not None and loads[name] and loads[name] == sunk[name]:
                continue
            problems.append(f"{relative}::{qualname} has an element_id dict that reaches "
                            f"something other than a telemetry sink")
    return problems


def no_element_problems(site) -> list:
    """A "no element" site must pass the empty-string LITERAL for element_id."""
    relative, qualname, callee = site
    node = _function(relative, qualname)
    if node is None:
        return [f"{relative}::{qualname} no longer exists"]
    literal_empty = False
    for child in ast.walk(node):
        if isinstance(child, ast.Call) and callee in ("Citation", "AnswerAnchor"):
            for keyword in child.keywords:
                if keyword.arg == "element_id":
                    if isinstance(keyword.value, ast.Constant) and keyword.value.value == "":
                        literal_empty = True
                    else:
                        return [f"{relative}::{qualname} passes a non-literal element_id"]
        if isinstance(child, ast.Dict) and callee.startswith(("dict", "splat")):
            for key, value in zip(child.keys, child.values):
                if isinstance(key, ast.Constant) and key.value == "element_id":
                    if isinstance(value, ast.Constant) and value.value == "":
                        literal_empty = True
                    else:
                        return [f"{relative}::{qualname} writes a non-literal element_id"]
        if isinstance(child, ast.Assign) and callee.startswith("store"):
            for target in child.targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.slice, ast.Constant)
                    and target.slice.value == "element_id"
                ):
                    if isinstance(child.value, ast.Constant) and child.value.value == "":
                        literal_empty = True
                    else:
                        return [f"{relative}::{qualname} stores a non-literal element_id"]
    return [] if literal_empty else [f"{relative}::{qualname} never writes element_id=''"]


#: Construction forms the guard cannot classify (pydantic ``model_validate``,
#: a dict turned into a model inside ``citations=[{...}]``, ``dict(element_id=)``,
#: ``x.element_id = ...``, ``model_copy(update=<variable>)``). A site using one
#: fails unless listed here with the reason it builds no citation; new code
#: must use a form the guard can see.
UNCLASSIFIABLE_ALLOWED = {
    ("api/ask_routes.py", "_apply_resolved_scopes", "unclassifiable:model_copy(update=<non-literal>)"):
        "copies an AskRequest with frozen scope ceilings; builds no citation or locator",
}


def registry_problems(found: Counter, registry=REGISTRY) -> list:
    problems = []
    unclassifiable = {site: count for site, count in found.items()
                      if site[2].startswith("unclassifiable:")}
    for site in sorted(set(unclassifiable) - set(UNCLASSIFIABLE_ALLOWED)):
        problems.append(f"construction form the guard cannot classify: {site}")
    for site in sorted(set(UNCLASSIFIABLE_ALLOWED) - set(unclassifiable)):
        problems.append(f"allowed unclassifiable site {site} no longer exists; remove it")
    found = Counter({site: count for site, count in found.items() if site not in unclassifiable})
    for site, count in sorted(found.items()):
        entry = registry.get(site)
        if entry is None:
            problems.append(f"UNREGISTERED citation/locator site {site} x{count}")
        elif entry[3] != count:
            problems.append(f"{site} occurs {count}x, registry says {entry[3]}x")
    for site in sorted(set(registry) - set(found)):
        problems.append(f"registered site {site} no longer exists; remove or re-point it")
    return problems


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_the_registry_equals_reality():
    assert registry_problems(scan()) == []


def classification_problems(site, entry) -> list:
    """What each classification must carry, before its proofs are re-checked."""
    classification, proofs, reason, count = entry
    problems = []
    if classification not in CLASSIFICATIONS:
        problems.append(f"{site}: unknown classification {classification!r}")
    if not reason or count < 1:
        problems.append(f"{site}: a reason and a count are required")
    if classification in {"global-unreachable", "single-notebook-only"} and not proofs:
        problems.append(f"{site}: a gate must be named")
    if classification in {"read-registered", "pointer-registered", "federated"} and not proofs:
        problems.append(f"{site}: a registration proof must be named")
    if classification == "inherited":
        if site not in INHERITED_SITES:
            problems.append(f"{site}: `inherited` is reserved for {sorted(INHERITED_SITES)}; "
                            "a new producer must register its evidence")
        if not any(proof[0] == "reads-param" for proof in proofs):
            problems.append(f"{site}: `inherited` must name the id_map parameter it copies")
    if classification == "not-a-locator" and not str(site[2]).startswith("dict:"):
        problems.append(f"{site}: `not-a-locator` covers telemetry dict literals only")
    return problems


def test_every_entry_has_one_known_classification_and_a_reason():
    assert [p for site, entry in REGISTRY.items() for p in classification_problems(site, entry)] == []


def site_problems(site, registry=REGISTRY) -> list:
    classification, proofs, _reason, _count = registry[site]
    problems = [problem for proof in proofs for problem in proof_problems(proof)]
    if classification == "no-element":
        problems += no_element_problems(site)
    if classification == "not-a-locator":
        problems += not_a_locator_problems(site)
    return problems


@pytest.mark.parametrize("site", sorted(REGISTRY), ids=lambda site: f"{site[1]}:{site[2]}")
def test_every_proof_and_gate_still_holds(site):
    assert site_problems(site) == []


# --- the guard's own teeth ------------------------------------------------
# Every tooth goes through ``scan()`` over a fake ``APP`` directory -- the same
# path the real check takes -- never through ``scan_source`` alone.

def _fake_app(tmp_path, monkeypatch, relative: str, source: str) -> None:
    """Point the guard at a directory holding ``relative`` (and nothing else
    that was not written into it)."""
    import sys

    fake = tmp_path / "app"
    target = fake / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(source, encoding="utf-8")
    monkeypatch.setattr(sys.modules[__name__], "APP", fake)


def _scanned_problems(tmp_path, monkeypatch, relative: str, source: str) -> list:
    _fake_app(tmp_path, monkeypatch, relative, source)
    return [problem for problem in registry_problems(scan()) if relative in problem]


@pytest.mark.parametrize("source", [
    # an import alias: no "Citation(" text anywhere in the module
    "from app.models.ask import Citation as C\n"
    "def mint(row):\n"
    "    return C(label='x', source_id=row.s, element_id=row.e, location_label='', quoted_span='')\n",
    # a rebinding alias
    "from app.models import ask\n"
    "Card = ask.Citation\n"
    "def mint(row):\n"
    "    return Card(label='x', source_id=row.s, element_id=row.e, location_label='', quoted_span='')\n",
    # a single-quoted id_map entry
    "def entry(row):\n"
    "    return {'object_type': 'chunk', 'object_id': row.o, 'element_id': row.e}\n",
], ids=["import-alias", "rebinding-alias", "single-quoted-dict"])
def test_a_new_construction_site_without_an_entry_fails(tmp_path, monkeypatch, source):
    problems = _scanned_problems(tmp_path, monkeypatch, "services/new_producer.py", source)
    assert any("UNREGISTERED" in problem for problem in problems), problems


def test_a_new_site_cannot_hide_behind_inherited():
    site = ("services/new_producer.py", "mint", "Citation")
    entry = ("inherited", (), "copies something", 1)
    assert classification_problems(site, entry)
    # Even with a well-formed id_map proof, `inherited` is reserved for
    # parse_anchors: a new producer must register its evidence instead.
    proven = ("inherited", (("reads-param", "services/new_producer.py", "mint", "id_map"),),
              "copies an id_map entry", 1)
    assert any("reserved" in problem for problem in classification_problems(site, proven))


def test_inherited_must_copy_its_locator_from_the_id_map(tmp_path, monkeypatch):
    source = (APP / EC).read_text(encoding="utf-8")
    forged = source.replace(
        '                    element_id=str(context.get("element_id", "")),\n',
        '                    element_id=str(self.pick_element(key)),\n', 1)
    assert forged != source
    _fake_app(tmp_path, monkeypatch, EC, forged)
    assert site_problems((EC, "EvidenceContextService.parse_anchors", "AnswerAnchor"))


def test_a_telemetry_dict_passes_and_a_returned_one_fails(tmp_path, monkeypatch):
    registry = {("services/probe.py", "note", 'dict:"element_id"'):
                ("not-a-locator", (), "telemetry", 1)}
    sunk = (
        "def note(log, row):\n"
        "    event = {'object_type': 'chunk', 'element_id': row.e}\n"
        "    log.emit(event)\n"
    )
    _fake_app(tmp_path, monkeypatch, "services/probe.py", sunk)
    assert site_problems(("services/probe.py", "note", 'dict:"element_id"'), registry) == []
    returned = sunk + "    return event\n"
    _fake_app(tmp_path, monkeypatch, "services/probe.py", returned)
    assert site_problems(("services/probe.py", "note", 'dict:"element_id"'), registry)


def test_a_registered_site_that_disappears_fails():
    found = scan()
    del found[(EC, "EvidenceContextService.chunk_citations", "Citation")]
    assert any("no longer exists" in problem for problem in registry_problems(found))


def test_a_removed_gate_fails(tmp_path, monkeypatch):
    source = (APP / RC).read_text(encoding="utf-8")
    gated = source.replace("        if federated_ask_active():\n            return []\n", "", 1)
    assert gated != source
    _fake_app(tmp_path, monkeypatch, RC, gated)
    assert proof_problems(_ELEMENT_ARM_CLOSED[0])


def test_a_removed_registration_call_fails(tmp_path, monkeypatch):
    source = (APP / EC).read_text(encoding="utf-8")
    unregistered = source.replace("        filtered = _live_kg_evidence(filtered)\n", "", 1)
    assert unregistered != source
    _fake_app(tmp_path, monkeypatch, EC, unregistered)
    assert proof_problems(
        ("calls", EC, "EvidenceContextService.citations_from", "_live_kg_evidence")
    )


def test_a_no_element_site_cannot_dodge_with_a_real_element(tmp_path, monkeypatch):
    source = (
        "from app.models.ask import Citation\n"
        "class EvidenceContextService:\n"
        "    def external_citations(self, items):\n"
        "        return [Citation(label='', source_id='', element_id=item.e,\n"
        "                         location_label='', quoted_span='') for item in items]\n"
    )
    _fake_app(tmp_path, monkeypatch, EC, source)
    problems = no_element_problems((EC, "EvidenceContextService.external_citations", "Citation"))
    assert problems and "non-literal" in problems[0]



@pytest.mark.parametrize("form,source", [
    ("model_validate", "from app.models.ask import Citation\ndef make(d):\n    return Citation.model_validate(d)\n"),
    ("citations=[dict]", "def make(r):\n    return AskResponse(conclusion='', citations=[{'element_id': r.e}])\n"),
    ("dict(element_id=)", "def make(r):\n    return dict(object_id=r.o, element_id=r.e)\n"),
    ("attribute store", "def fix(c, e):\n    c.element_id = e\n"),
    ("model_copy(update=var)", "def fix(c, u):\n    return c.model_copy(update=u)\n"),
], ids=lambda value: value if isinstance(value, str) and "\n" not in value else "")
def test_a_construction_form_the_guard_cannot_classify_fails(tmp_path, monkeypatch, form, source):
    problems = _scanned_problems(tmp_path, monkeypatch, "services/new_producer.py", source)
    assert any("cannot classify" in problem for problem in problems), form


_J2_GATE = REGISTRY[("services/reference_liveness.py", "drop_dangling_references",
                     "model_copy:element_id")][1][0]
_J2_GATE_TEXT = "        if read is not None and current_federated_run_plan() is None:\n"


@pytest.mark.parametrize("replacement", [
    # a bare call beside an ungated action
    "        current_federated_run_plan()\n        if read is not None:\n",
    # the same `if`, inverted: the pass would run ONLY inside a global run
    "        if read is not None and current_federated_run_plan() is not None:\n",
], ids=["bare-call", "inverted-polarity"])
def test_a_single_notebook_gate_needs_an_if_with_the_right_polarity(
    tmp_path, monkeypatch, replacement,
):
    source = (APP / AS).read_text(encoding="utf-8")
    assert source.count(_J2_GATE_TEXT) == 1
    _fake_app(tmp_path, monkeypatch, AS, source.replace(_J2_GATE_TEXT, replacement, 1))
    assert proof_problems(_J2_GATE)


def test_an_exit_gate_with_inverted_polarity_fails(tmp_path, monkeypatch):
    source = (APP / RC).read_text(encoding="utf-8")
    inverted = source.replace(
        "        if federated_ask_active():\n            return []\n",
        "        if not federated_ask_active():\n            return []\n", 1)
    assert inverted != source
    _fake_app(tmp_path, monkeypatch, RC, inverted)
    assert proof_problems(_ELEMENT_ARM_CLOSED[0])


def test_a_registration_call_under_if_false_does_not_count(tmp_path, monkeypatch):
    source = (APP / EC).read_text(encoding="utf-8")
    line = "        filtered = _live_kg_evidence(filtered)\n"
    assert source.count(line) == 1
    _fake_app(tmp_path, monkeypatch, EC, source.replace(
        line, "        if False:\n    " + line, 1))
    assert proof_problems(
        ("calls", EC, "EvidenceContextService.citations_from", "_live_kg_evidence")
    )


def test_a_registration_call_after_a_return_does_not_count():
    tree = ast.parse("def f(x):\n    return x\n    register(x)\n")
    assert "register" not in _called_names(tree)
    tree = ast.parse("def f(x):\n    if True:\n        return x\n    else:\n        register(x)\n")
    assert "register" not in _called_names(tree)
    tree = ast.parse("def f(x):\n    if x:\n        return x\n    register(x)\n")
    assert "register" in _called_names(tree)


def test_an_import_gate_sees_one_level_of_indirection(tmp_path, monkeypatch):
    """A global-engine module importing a helper that imports the gated module
    breaks the gate even though the gated module is never imported directly."""
    import shutil
    import sys

    fake = tmp_path / "app"
    shutil.copytree(APP, fake)
    (fake / "services" / "bridge_helper.py").write_text(
        "from app.services import report_engine  # noqa: F401\n", encoding="utf-8",
    )
    engine = fake / "services" / "evidence_attestation.py"
    engine.write_text(
        engine.read_text(encoding="utf-8")
        + "\nfrom app.services import bridge_helper  # noqa: E402,F401\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(sys.modules[__name__], "APP", fake)
    assert proof_problems(("not-imported", "services/report_engine.py"))
