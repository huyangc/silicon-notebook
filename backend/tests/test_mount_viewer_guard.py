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
it ON for someone else.  This guard pins the value at every call site under
``backend/app`` outside the two store packages:

* ``viewer_id=current_viewer_id()`` -- the retrieval run's actor, else the
  requesting user, else nobody (``retrieval_run.current_viewer_id``); or
* an explicit viewer at a REGISTERED site (``_EXPLICIT_VIEWER_SITES``): the
  anonymous public pages (the share's creator), MCP (the token's owner), the
  source proxy routes (the authenticated user), the ceiling (its owner), the
  catalog (the summary's user), the facade pass-throughs;
* never a literal (``""``, ``None``, a string) -- anywhere.

A call WITHOUT ``viewer_id`` is a call through an injected one-argument seam
(``Callable[[str], ...]``) or a service method that resolves its own viewer;
each such site is registered in ``_SEAM_SITES`` with the place its viewer is
bound.  Both registries are compared by EQUALITY, so a removed site is as loud
as an added one.
"""
from __future__ import annotations

import ast
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

#: ``(path, qualname, method)`` -> who the explicit viewer is.
_EXPLICIT_VIEWER_SITES = {
    ("app/api/source_routes.py", "_participant_ids_for.participant_ids",
     "participant_notebook_ids"):
        "the source proxy / asset routes' authenticated user",
    ("app/api/mcp_tools/citations.py",
     "register_citation_tools.get_cited_element.load.<lambda>",
     "participant_notebook_ids"):
        "MCP: the token's owner",
    ("app/services/public_share_recheck.py", "mounts_still_effective",
     "participant_notebook_ids"):
        "anonymous public pages (conversation page + images, share "
        "preflight, public report): the share's creator",
    ("app/services/repository_runtime.py",
     "RepositoryRuntime._viewer_participant_notebook_ids",
     "participant_notebook_ids"):
        "the ceiling's participant reader: the ceiling's owner",
    ("app/services/notebook_catalog.py", "NotebookSummaryQuery.mounted_bases",
     "mounted_bases_row"):
        "the summary's user (N-6)",
    ("app/services/notebook_catalog.py", "NotebookSummaryQuery.from_row",
     "mounted_bases"):
        "the summary's user (N-6)",
    ("app/services/notebook_catalog.py", "NotebookSummaryQuery.get",
     "notebook_has_usable_base_kg"):
        "the summary's user (N-6)",
    ("app/services/repository_facade.py",
     "RepositoryFacade._any_base_notebook_has_kg", "any_mounted_has_kg_compat"):
        "facade pass-through of its caller's viewer",
    ("app/services/repository_facade.py", "RepositoryFacade._mounted_bases",
     "mounted_bases"):
        "facade pass-through of its caller's viewer",
    ("app/services/repository_facade.py",
     "RepositoryFacade.participant_notebook_ids", "participant_notebook_ids"):
        "facade pass-through of its caller's viewer",
}

#: ``(path, qualname, method)`` called WITHOUT ``viewer_id`` -> where the
#: viewer of that seam is bound.
_SEAM_SITES = {
    ("app/api/source_routes.py", "in_participant_scope",
     "participant_notebook_ids"):
        "injected ``_participant_ids_for(user.id)``",
    ("app/services/knowledge_query.py",
     "KnowledgeQueryService._participant_source", "participant_notebook_ids"):
        "injected ``RepositoryRuntime._participant_notebook_ids`` "
        "(current_viewer_id at call time)",
    ("app/services/knowledge_lifecycle.py",
     "KnowledgeLifecycleService._participant_source_notebook",
     "participant_notebook_ids"):
        "injected ``RepositoryRuntime._participant_notebook_ids``",
    ("app/services/plugin_ask_engine.py",
     "PluginRetrievalAccess.__init__", "participant_notebook_ids"):
        "injected ``AskService.ask_engine_participant_notebooks`` = "
        "``RepositoryRuntime._participant_notebook_ids``",
    ("app/services/ask_service.py",
     "AskService.ask_chunk", "mounted_base_ids"):
        "``CommunityQueryService.mounted_base_ids`` (current_viewer_id inside)",
    ("app/services/reasoning_retrieval.py",
     "ReasoningRetriever._action_expand_community", "mounted_base_ids"):
        "``CommunityQueryService.mounted_base_ids`` (current_viewer_id inside)",
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


def _classify(sites):
    literals, explicit, seams, current = [], set(), set(), set()
    for path, qualname, name, viewer in sites:
        key = (path, qualname, name)
        if viewer is None:
            seams.add(key)
        elif any(isinstance(n, ast.Constant) for n in ast.walk(viewer)):
            literals.append(f"{path}::{qualname} {name}(viewer_id={ast.unparse(viewer)})")
        elif _is_current_viewer(viewer):
            current.add(key)
        else:
            explicit.add(key)
    return literals, explicit, seams, current


def test_no_participant_read_passes_a_literal_viewer():
    literals, _explicit, _seams, _current = _classify(_production_sites())
    assert not literals, (
        "participant reads with a literal viewer -- an empty viewer turns the "
        "mounter's own private library off, a fixed one turns it on for "
        "someone else; use current_viewer_id() or a registered explicit "
        "viewer:\n  " + "\n  ".join(literals)
    )


def test_explicit_viewers_are_exactly_the_registered_sites():
    _literals, explicit, _seams, _current = _classify(_production_sites())
    assert explicit == set(_EXPLICIT_VIEWER_SITES), (
        "unregistered explicit viewers: "
        f"{sorted(explicit - set(_EXPLICIT_VIEWER_SITES))}; stale registrations: "
        f"{sorted(set(_EXPLICIT_VIEWER_SITES) - explicit)}"
    )


def test_viewerless_calls_are_exactly_the_registered_seams():
    _literals, _explicit, seams, _current = _classify(_production_sites())
    registered = set(_SEAM_SITES)
    assert seams == registered, (
        "a viewer-dependent read without viewer_id must be an injected seam "
        f"registered with where its viewer is bound; unregistered: "
        f"{sorted(seams - registered)}; stale: {sorted(registered - seams)}"
    )


def test_the_retrieval_layer_resolves_the_viewer_at_call_time():
    """The run-scoped retrieval consumers (candidates, graph, enumeration,
    catalog, evidence, communities, the runtime's one-argument reader) all
    read ``current_viewer_id()`` -- the positive half: a site rerouted to a
    registered explicit value would leave these files without one."""
    _literals, _explicit, _seams, current = _classify(_production_sites())
    files = {path for path, _qualname, _name in current}
    assert files == {
        "app/services/retrieval_candidates.py",
        "app/services/graph_retrieval.py",
        "app/services/collection_enumeration.py",
        "app/services/collection_catalog.py",
        "app/services/evidence_context.py",
        "app/services/communities.py",
        "app/services/repository_runtime.py",
    }, sorted(files)


def test_the_guard_sees_literals_and_seams():
    """Positive control on a synthetic module: a literal ``""`` / ``None`` is
    reported, ``current_viewer_id()`` is not, a bare call is a seam."""
    source = (
        "def f(store, db, nb, current_viewer_id):\n"
        "    store.participant_ids(db, nb, viewer_id='')\n"
        "    store.participant_rows(db, nb, viewer_id=None)\n"
        "    store.participant_tiers(db, nb, viewer_id=current_viewer_id())\n"
        "    store.mounted_base_ids(nb)\n"
    )
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "probe.py"
        path.write_text(source, encoding="utf-8")
        sites = [("probe.py", q, n, v) for q, n, v in _qualified_calls(path)]
    literals, explicit, seams, current = _classify(sites)
    assert len(literals) == 2 and not explicit
    assert current == {("probe.py", "f", "participant_tiers")}
    assert seams == {("probe.py", "f", "mounted_base_ids")}
