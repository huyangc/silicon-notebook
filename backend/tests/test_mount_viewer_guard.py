"""E6-3 -- every service-layer participant read names its viewer (M3).

A private library mounted on a shared notebook counts only for a viewer who
may read it (or for its mounter).  The store methods that resolve a mount
(``NotebookStore.participant_*`` / ``resolve_participants``,
``QueryStore.notebook_has_usable_base_kg`` / ``mounted_bases_row``,
``UnifiedKgStore.mounted_base_ids``, ``KnowledgeStore.any_mounted_has_kg*`` /
``follow_start_row``, and the catalog's ``mounted_bases``) take a REQUIRED
``viewer_id`` keyword, so forgetting it is a ``TypeError``.  What a keyword
cannot stop is the wrong VALUE: ``viewer_id=""`` / ``None`` quietly turns a
mounter's own private library off, and a hard-coded or stale identity turns
it ON for someone else.  This guard pins the value at every CALL under
``backend/app`` outside the two store packages, one registry entry per call
site with its call COUNT:

* ``current`` -- ``viewer_id=current_viewer_id()``: the retrieval run's actor,
  else the requesting user, else nobody (``retrieval_run.current_viewer_id``);
* ``explicit`` -- any other non-literal viewer, at a site whose registration
  names who that viewer is: the anonymous public pages (the share's creator),
  MCP (the token's owner), the source proxy routes (the authenticated user),
  the ceiling (its owner), the catalog (the summary's user), the facade
  pass-throughs, the graph builders' once-bound memo viewer;
* ``seam`` -- no ``viewer_id`` at all: a call through an injected
  one-argument seam (``Callable[[str], ...]``) or a service method that
  resolves its own viewer; the registration names where that viewer is bound;
* never a literal (``""``, ``None``, a string) -- anywhere, any site.

The registry is compared with the observed calls as a MULTISET of
``(kind, path, function, method)``: a second call added to an already
registered function -- with any viewer -- changes a count and goes red, as
does a removed one or a call that changed kind.
"""
from __future__ import annotations

import ast
from collections import Counter
from pathlib import Path


_BACKEND = Path(__file__).resolve().parents[1]
_APP = _BACKEND / "app"
_EXCLUDED = (
    _APP / "repositories" / "postgres",
    _APP / "repositories" / "sqlite",
    _APP / "repositories" / "ports.py",
)

#: Store / catalog methods whose answer depends on the viewer.
_VIEWER_METHODS = frozenset({
    "participant_notebook_ids",
    "participant_ids",
    "participant_rows",
    "participant_tiers",
    "resolve_participants",
    "notebook_has_usable_base_kg",
    "mounted_bases_row",
    "mounted_base_ids",
    "any_mounted_has_kg_on",
    "any_mounted_has_kg",
    "any_mounted_has_kg_compat",
    "follow_start_row",
    "mounted_bases",
})

_CURRENT_VIEWER = "current_viewer_id"

_CURRENT = "current"
_EXPLICIT = "explicit"
_SEAM = "seam"

_RUN_VIEWER = "current_viewer_id(): the run's actor, else the request user"

#: ``(path, function, method) -> (kind, calls, who the viewer is)``.
_SITES = {
    # -- current_viewer_id(): the retrieval layer, resolved at call time.
    ("app/services/collection_catalog.py",
     "CollectionCatalogService.collection_map.<lambda>", "participant_ids"):
        (_CURRENT, 1, _RUN_VIEWER),
    ("app/services/collection_enumeration.py",
     "CollectionEnumerationService._closing_participants.<lambda>",
     "participant_ids"):
        (_CURRENT, 1, _RUN_VIEWER),
    ("app/services/collection_enumeration.py",
     "CollectionEnumerationService._mount_participant_pairs", "participant_tiers"):
        (_CURRENT, 1, _RUN_VIEWER),
    ("app/services/communities.py",
     "CommunityQueryService.mounted_base_ids.<lambda>", "mounted_base_ids"):
        (_CURRENT, 1, _RUN_VIEWER),
    ("app/services/evidence_context.py",
     "EvidenceContextService.collection_item_citations", "participant_notebook_ids"):
        (_CURRENT, 1, _RUN_VIEWER),
    ("app/services/evidence_context.py",
     "EvidenceContextService.knowledge_context.<lambda>", "participant_notebook_ids"):
        (_CURRENT, 1, _RUN_VIEWER),
    ("app/services/graph_retrieval.py",
     "GraphRetrievalService._follow_chain", "follow_start_row"):
        (_CURRENT, 1, _RUN_VIEWER),
    ("app/services/repository_runtime.py",
     "RepositoryRuntime._participant_notebook_ids", "participant_notebook_ids"):
        (_CURRENT, 1, _RUN_VIEWER + " (knowledge_query, lifecycle, plugin seams)"),
    ("app/services/retrieval_candidates.py",
     "_RetrievalState._any_base_notebook_has_kg", "any_mounted_has_kg"):
        (_CURRENT, 1, _RUN_VIEWER),
    ("app/services/retrieval_candidates.py",
     "_RetrievalState._any_base_notebook_has_kg", "any_mounted_has_kg_on"):
        (_CURRENT, 1, _RUN_VIEWER),
    ("app/services/retrieval_candidates.py",
     "_RetrievalState._federated_graph_is_large", "participant_notebook_ids"):
        (_CURRENT, 1, _RUN_VIEWER),
    ("app/services/retrieval_candidates.py",
     "_RetrievalState._mount_participants", "participant_tiers"):
        (_CURRENT, 1, _RUN_VIEWER),
    # -- explicit viewers.
    ("app/api/source_routes.py", "_participant_ids_for.participant_ids",
     "participant_notebook_ids"):
        (_EXPLICIT, 1, "the source proxy / asset routes' authenticated user"),
    ("app/api/mcp_tools/citations.py",
     "register_citation_tools.get_cited_element.load.<lambda>",
     "participant_notebook_ids"):
        (_EXPLICIT, 1, "MCP: the token's owner"),
    ("app/services/public_share_recheck.py", "mounts_still_effective",
     "participant_notebook_ids"):
        (_EXPLICIT, 1, "anonymous public pages (conversation page + images, "
                       "share preflight, public report): the share's creator"),
    ("app/services/repository_runtime.py",
     "RepositoryRuntime._viewer_participant_notebook_ids",
     "participant_notebook_ids"):
        (_EXPLICIT, 1, "the ceiling's participant reader: the ceiling's owner"),
    ("app/services/graph_retrieval.py",
     "GraphRetrievalService._viewer_graph_participants.read", "participant_rows"):
        (_EXPLICIT, 1, "current_viewer_id() bound once: the same value keys "
                       "the per-run memo and the read"),
    ("app/services/notebook_catalog.py", "NotebookSummaryQuery.mounted_bases",
     "mounted_bases_row"):
        (_EXPLICIT, 2, "the summary's user (N-6); one call per connection branch"),
    ("app/services/notebook_catalog.py", "NotebookSummaryQuery.from_row",
     "mounted_bases"):
        (_EXPLICIT, 1, "the summary's user (N-6)"),
    ("app/services/notebook_catalog.py", "NotebookSummaryQuery.get",
     "notebook_has_usable_base_kg"):
        (_EXPLICIT, 1, "the summary's user (N-6)"),
    ("app/services/repository_facade.py",
     "RepositoryFacade._any_base_notebook_has_kg", "any_mounted_has_kg_compat"):
        (_EXPLICIT, 1, "facade pass-through of its caller's viewer"),
    ("app/services/repository_facade.py", "RepositoryFacade._mounted_bases",
     "mounted_bases"):
        (_EXPLICIT, 1, "facade pass-through of its caller's viewer"),
    ("app/services/repository_facade.py",
     "RepositoryFacade.participant_notebook_ids", "participant_notebook_ids"):
        (_EXPLICIT, 1, "facade pass-through of its caller's viewer"),
    # -- seams: no viewer at the call, bound where the registration says.
    ("app/api/source_routes.py", "in_participant_scope",
     "participant_notebook_ids"):
        (_SEAM, 1, "injected ``_participant_ids_for(user.id)``"),
    ("app/services/knowledge_query.py",
     "KnowledgeQueryService._participant_source", "participant_notebook_ids"):
        (_SEAM, 1, "injected ``RepositoryRuntime._participant_notebook_ids``"),
    ("app/services/knowledge_lifecycle.py",
     "KnowledgeLifecycleService._participant_source_notebook",
     "participant_notebook_ids"):
        (_SEAM, 1, "injected ``RepositoryRuntime._participant_notebook_ids``"),
    ("app/services/plugin_ask_engine.py",
     "PluginRetrievalAccess.__init__", "participant_notebook_ids"):
        (_SEAM, 1, "injected ``AskService.ask_engine_participant_notebooks`` = "
                   "``RepositoryRuntime._participant_notebook_ids``"),
    ("app/services/ask_service.py", "AskService.ask_chunk", "mounted_base_ids"):
        (_SEAM, 1, "``CommunityQueryService.mounted_base_ids`` "
                   "(current_viewer_id inside)"),
    ("app/services/reasoning_retrieval.py",
     "ReasoningRetriever._action_expand_community", "mounted_base_ids"):
        (_SEAM, 1, "``CommunityQueryService.mounted_base_ids`` "
                   "(current_viewer_id inside)"),
}


def _qualified_calls(path: Path):
    """``(qualname, method, viewer keyword node or None)`` for every call of a
    viewer method in ``path``; ``qualname`` names the enclosing function(s),
    ``<lambda>`` included."""
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    found = []

    def visit(node, scope):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope = scope + (node.name,)
        elif isinstance(node, ast.Lambda):
            scope = scope + ("<lambda>",)
        if isinstance(node, ast.Call):
            func = node.func
            name = (
                func.attr if isinstance(func, ast.Attribute)
                else func.id if isinstance(func, ast.Name) else ""
            )
            if name in _VIEWER_METHODS:
                viewer = next(
                    (kw.value for kw in node.keywords if kw.arg == "viewer_id"),
                    None,
                )
                found.append((".".join(scope), name, viewer))
        for child in ast.iter_child_nodes(node):
            visit(child, scope)

    visit(tree, ())
    return found


def _production_sites():
    for path in sorted(_APP.rglob("*.py")):
        if any(path == ex or ex in path.parents for ex in _EXCLUDED):
            continue
        relative = str(path.relative_to(_BACKEND))
        for qualname, name, viewer in _qualified_calls(path):
            yield relative, qualname, name, viewer


def _is_current_viewer(node) -> bool:
    return (
        isinstance(node, ast.Call) and not node.args and not node.keywords
        and (
            (isinstance(node.func, ast.Name) and node.func.id == _CURRENT_VIEWER)
            or (isinstance(node.func, ast.Attribute)
                and node.func.attr == _CURRENT_VIEWER)
        )
    )


def _classify(sites) -> tuple[list[str], Counter]:
    """``(literal viewers, Counter of (kind, path, function, method))``."""
    literals: list[str] = []
    observed: Counter = Counter()
    for path, qualname, name, viewer in sites:
        if viewer is None:
            kind = _SEAM
        elif any(isinstance(n, ast.Constant) for n in ast.walk(viewer)):
            literals.append(f"{path}::{qualname} {name}(viewer_id={ast.unparse(viewer)})")
            continue
        elif _is_current_viewer(viewer):
            kind = _CURRENT
        else:
            kind = _EXPLICIT
        observed[(kind, path, qualname, name)] += 1
    return literals, observed


def _expected(registry) -> Counter:
    return Counter({
        (kind, *key): calls for key, (kind, calls, _reason) in registry.items()
    })


def _registry_problems(observed: Counter, registry) -> list[str]:
    expected = _expected(registry)
    problems = []
    for site in sorted(set(observed) | set(expected)):
        if observed[site] != expected[site]:
            problems.append(
                f"{site}: observed {observed[site]} call(s), registered "
                f"{expected[site]}"
            )
    return problems


def test_no_participant_read_passes_a_literal_viewer():
    literals, _observed = _classify(_production_sites())
    assert not literals, (
        "participant reads with a literal viewer -- an empty viewer turns the "
        "mounter's own private library off, a fixed one turns it on for "
        "someone else; use current_viewer_id() or a registered explicit "
        "viewer:\n  " + "\n  ".join(literals)
    )


def test_every_participant_read_is_registered_with_its_viewer_and_count():
    _literals, observed = _classify(_production_sites())
    problems = _registry_problems(observed, _SITES)
    assert not problems, (
        "participant reads differ from the registry (kind, path, function, "
        "method -> call count); a new call names its viewer and is registered "
        "with who that viewer is:\n  " + "\n  ".join(problems)
    )


def test_every_registration_names_who_the_viewer_is():
    blank = [key for key, (_kind, calls, reason) in _SITES.items()
             if not reason.strip() or calls < 1]
    assert not blank, blank


def _probe(tmp_path, source: str):
    path = tmp_path / "probe.py"
    path.write_text(source, encoding="utf-8")
    return [("probe.py", q, n, v) for q, n, v in _qualified_calls(path)]


def test_the_guard_sees_literals_seams_and_a_second_call(tmp_path):
    """Positive control on a synthetic module: a literal ``""`` / ``None`` is
    reported, ``current_viewer_id()`` is ``current``, a bare call is a seam --
    and a SECOND call in an already registered function, even with a
    non-literal viewer, breaks the count."""
    sites = _probe(tmp_path, (
        "def f(store, db, nb, current_viewer_id, someone):\n"
        "    store.participant_ids(db, nb, viewer_id='')\n"
        "    store.participant_rows(db, nb, viewer_id=None)\n"
        "    store.participant_tiers(db, nb, viewer_id=current_viewer_id())\n"
        "    store.mounted_base_ids(nb)\n"
        "    store.participant_notebook_ids(nb, viewer_id=someone)\n"
    ))
    literals, observed = _classify(sites)
    assert len(literals) == 2
    registry = {
        ("probe.py", "f", "participant_tiers"): (_CURRENT, 1, "run"),
        ("probe.py", "f", "mounted_base_ids"): (_SEAM, 1, "seam"),
        ("probe.py", "f", "participant_notebook_ids"): (_EXPLICIT, 1, "who"),
    }
    assert _registry_problems(observed, registry) == []

    doubled = _probe(tmp_path, (
        "def f(store, db, nb, current_viewer_id, someone, other):\n"
        "    store.participant_tiers(db, nb, viewer_id=current_viewer_id())\n"
        "    store.mounted_base_ids(nb)\n"
        "    store.participant_notebook_ids(nb, viewer_id=someone)\n"
        "    store.participant_notebook_ids(nb, viewer_id=other.id)\n"
    ))
    _literals, observed = _classify(doubled)
    assert _registry_problems(observed, registry) == [
        "('explicit', 'probe.py', 'f', 'participant_notebook_ids'): "
        "observed 2 call(s), registered 1"
    ]
