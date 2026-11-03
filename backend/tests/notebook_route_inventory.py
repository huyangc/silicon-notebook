"""The one census of notebook write routes (share-edit P1-T3).

Every check that has to cover "all routes that change a notebook" starts from
this set and asserts set *equality* against its own table: the per-route
access levels (``notebook_route_levels.ROUTE_LEVELS``, P1-T3), the editor
destructive-action registry (P1-T4) and the pulse classification (P2-T5). A
route added without registering it, or a stale entry left behind, fails all
of them.

The set is enumerated by **path**, not by dependency: a route whose
capability guard was deleted is still in it (it still carries
``{notebook_id}``), so the HTTP refusal sweep that walks it notices the guard
is gone. Two halves:

* every route of the app whose path carries ``{notebook_id}`` and whose method
  is not safe (anything but GET / HEAD / OPTIONS);
* ``BODY_LOOKUP_WRITE_ROUTES``: write routes without ``{notebook_id}`` that
  resolve the notebook inside the handler (from a source id, a Knowhow row
  id) and judge a notebook capability there. Every one of them must still
  exist (``unsafe_notebook_routes`` asserts it), so a renamed route cannot
  silently drop out.

Plugin routes (``/api/extensions/{plugin_id}/...``) are mounted only when a
plugin is installed and are not part of the default app this census reads; a
plugin route carrying ``{notebook_id}`` would enter the first half
automatically. The plugin URL-import port judges ``sources:write`` in its own
body (``extension_routes._authorized_notebook``) and is pinned by
``test_extension_plugin_routes``.
"""
from __future__ import annotations

from typing import Iterable

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

#: Write routes without ``{notebook_id}`` that look the notebook up in the body
#: and judge a capability on it (method, path template).
BODY_LOOKUP_WRITE_ROUTES: frozenset[tuple[str, str]] = frozenset({
    # source_routes: notebook resolved from the source, sources:write in the body
    ("DELETE", "/api/sources/{source_id}"),
    ("POST", "/api/sources/{source_id}/parse"),
    # knowhow_agent_routes: notebook resolved from the row, knowhow:write (D6)
    ("PUT", "/api/agent/knowhow/rows/{row_id}/cells/{column_id}/code"),
    ("DELETE", "/api/agent/knowhow/rows/{row_id}/cells/{column_id}/code"),
})


def _route_pairs(app) -> Iterable[tuple[str, str]]:
    for route in app.routes:
        path = getattr(route, "path", None)
        methods = getattr(route, "methods", None) or set()
        if path is None:
            continue
        for method in methods:
            yield method.upper(), path


def unsafe_notebook_routes(app) -> frozenset[tuple[str, str]]:
    """``(method, path)`` of every unsafe route carrying ``{notebook_id}``,
    plus ``BODY_LOOKUP_WRITE_ROUTES`` (each of which must exist in ``app``)."""
    pairs = set(_route_pairs(app))
    missing = BODY_LOOKUP_WRITE_ROUTES - pairs
    assert not missing, f"BODY_LOOKUP_WRITE_ROUTES names routes the app no longer has: {sorted(missing)}"
    by_path = {
        (method, path) for method, path in pairs
        if method not in SAFE_METHODS and "{notebook_id}" in path
    }
    return frozenset(by_path | BODY_LOOKUP_WRITE_ROUTES)


def route_for(app, method: str, path: str):
    """The APIRoute serving ``(method, path)``."""
    for route in app.routes:
        if getattr(route, "path", None) == path and method in (
            getattr(route, "methods", None) or set()
        ):
            return route
    raise KeyError((method, path))
