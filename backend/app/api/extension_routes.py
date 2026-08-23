"""The one place a deployment plugin's HTTP router is built and mounted.

Import boundary (enforced by ``scripts/check_architecture_boundaries.py``):
this module may not import ``app.extensions.*`` or ``app.extension_sdk.*``.
It therefore names only ``app.domain.extension_http`` shapes and receives
already-frozen ``PluginRouterSpec`` values from ``app.bootstrap``. The mirror
rule holds on the other side — ``app.extensions.http_router`` may not import
``app.api.*`` — so neither package can reach the other and the two halves meet
in ``app.domain``.

What a plugin gets is the whole of :class:`PluginRouteContext`: eight seams,
none of which is the repository, global ``Settings``, a model client, the
FastMCP host, or a raw bearer token. What it gets *around* those seams is
nothing: the router is mounted with a router-level ``Depends(get_current_user)``
so no plugin route can ever serve an anonymous request, and any route whose
path carries ``{notebook_id}`` must additionally carry one of core's own
notebook gates or the mount is refused outright.

Failures here raise :class:`PluginRouteMountError` out of ``create_app()``, so
the process refuses to start. A plugin whose routes cannot be mounted safely
must never come up half-wired.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
import re
from typing import Any

from fastapi import APIRouter, Depends, FastAPI
from fastapi.routing import APIRoute

from app.api import deps as core_deps
from app.api import source_routes
from app.api.deps import (
    get_current_user,
    require_notebook_capability,
    require_notebook_read,
    user_error,
)
from app.core.config import get_settings
from app.core.event_logging import EventLogger
from app.domain.extension_http import (
    PLUGIN_ROUTE_PREFIX,
    PluginActor,
    PluginImportedSource,
    PluginRejectedUrl,
    PluginRouteContext,
    PluginRouterSpec,
    PluginUrlImportResult,
)


class PluginRouteMountError(RuntimeError):
    """A plugin router was refused; ``create_app()`` — and the process — stops.

    The message is ``"{plugin_id}: {reason_code}"`` and nothing else: no
    module path, no route handler name, no settings value. An operator gets
    the plugin to disable and a stable code to look up.
    """

    def __init__(self, plugin_id: str, reason: str) -> None:
        self.plugin_id = plugin_id
        self.reason = reason
        super().__init__(f"{plugin_id}: {reason}")


async def plugin_actor(user=Depends(get_current_user)) -> PluginActor:
    """Narrow the authenticated session down to what a plugin may see.

    ``async`` on purpose: ``get_current_user`` is an async dependency, and a
    sync wrapper would cost every plugin request a threadpool hop to copy two
    fields. The parameter is deliberately unannotated — annotating it would
    mean importing ``app.models.identity`` into a module whose import list is
    itself part of the contract.
    """

    return PluginActor(id=user.id, is_admin=getattr(user, "role", "") == "admin")


class _UrlSourceImportAdapter:
    """``PluginUrlSourceImportPort`` over core's single URL-import implementation.

    Every rule that governs the browser endpoint governs a plugin too, because
    it is literally the same function: per-URL capacity accounting, the admin
    exemption, the unconfigured-parser 400, and the background parse scheduler.
    The adapter only re-shapes the pydantic result into the domain dataclasses
    so a plugin never has to import ``app.models.sources``.
    """

    def import_urls(
        self, notebook_id: str, urls: Sequence[str]
    ) -> PluginUrlImportResult:
        result = source_routes.import_url_sources(notebook_id, list(urls))
        return PluginUrlImportResult(
            created=tuple(
                PluginImportedSource(
                    source_id=row.id, title=row.title, url=row.source_url
                )
                for row in result.created
            ),
            rejected=tuple(
                PluginRejectedUrl(url=row.url, reason=row.reason)
                for row in result.rejected
            ),
        )


# One whitelisted observability payload: two stable codes and two counters.
# Anything else — a question, a notebook id, a source title, an exception
# string — is not "extra data to ignore", it is a reason to drop the whole
# record: a plugin that put a secret in an unexpected key must not have the
# rest of its event persisted as if the payload had been understood.
_EVENT_FIELDS = frozenset({"event", "outcome", "count", "elapsed_ms"})
_EVENT_CODE = re.compile(r"^[a-z][a-z0-9_]{0,63}$")
_EVENT_NUMBER_MAX = 1_000_000_000


def _sanitized_event(payload: object) -> "dict[str, object] | None":
    """Return the record to write, or ``None`` to drop this payload entirely."""

    if not isinstance(payload, Mapping):
        return None
    if set(payload) - _EVENT_FIELDS:
        return None
    if "event" not in payload:
        return None
    record: dict[str, object] = {}
    for key in ("event", "outcome"):
        if key in payload:
            value = payload[key]
            if not isinstance(value, str) or not _EVENT_CODE.match(value):
                return None
            record[key] = value
    for key in ("count", "elapsed_ms"):
        if key in payload:
            value = payload[key]
            # ``type(...) is int`` and not ``isinstance``: bool subclasses int,
            # and ``{"count": True}`` is a malformed payload, not the number 1.
            if type(value) is not int or not (0 <= value <= _EVENT_NUMBER_MAX):
                return None
            record[key] = value
    return record


def _event_emitter(plugin_id: str, event_log: EventLogger):
    """Build the ``emit_event`` seam for one plugin.

    Two properties, both load-bearing: the whitelist above is applied *before*
    anything is written, and nothing this function does can ever raise back
    into plugin code — an observability call is not allowed to become a plugin
    route's failure mode.
    """

    def emit(payload: Mapping[str, object]) -> None:
        try:
            record = _sanitized_event(payload)
            if record is None:
                return
            record["kind"] = "extension_plugin"
            record["plugin_id"] = plugin_id
            event_log.emit(record)
        except Exception:  # pragma: no cover - defence in depth
            return

    return emit


def _dependant_calls(dependant: Any) -> set[Any]:
    """Every dependency callable reachable from ``dependant``, transitively.

    ``APIRoute.dependant`` is FastAPI's semi-public solved-dependency tree
    (pinned by ``backend/requirements.txt:1`` — ``fastapi==0.135.3``). There is
    no public API that answers "does this route run core's notebook gate?", and
    the alternative — trusting a plugin's own declaration — is exactly what the
    check below exists to avoid. If a FastAPI upgrade changes this shape, the
    two ``{notebook_id}`` gate tests (one positive, one negative) fail together
    rather than silently letting ungated routes through.
    """

    calls: set[Any] = set()
    stack = list(getattr(dependant, "dependencies", ()) or ())
    while stack:
        node = stack.pop()
        call = getattr(node, "call", None)
        if call is not None:
            calls.add(call)
        stack.extend(getattr(node, "dependencies", ()) or ())
    return calls


def _notebook_gates() -> set[Any]:
    """Every core dependency that proves a request cleared a notebook gate.

    Derived, never enumerated. Two reasons, and the second one is a hard rule:

    1. ``require_notebook_capability`` is the *only* way a core route may
       declare a write gate, so asking the factory for each registered
       capability yields exactly the guard objects core itself mounts — and a
       future third capability level would land here on its own rather than
       silently falling outside the accepted set. The factory is ``lru_cache``d,
       so these are the same objects FastAPI records in ``route.dependant``.
    2. ``backend/tests/test_notebook_capability_guard.py`` forbids every
       ``app/api/*.py`` file except ``deps.py`` from naming the bare level
       guards in any form — Name, Attribute, or import alias. Reading the
       capability table instead of importing those names keeps that guard
       intact rather than carving out an exemption for this module.

    ``require_notebook_read`` is added explicitly (decision 2): it is not a
    capability, so the factory cannot produce it, and without it a plugin could
    not mount a read-only notebook-scoped route at all.
    """

    gates: set[Any] = {
        require_notebook_capability(capability)
        for capability in core_deps._CAPABILITY_LEVELS
    }
    gates.add(require_notebook_read)
    return gates


def _validate_plugin_router(plugin_id: str, router: APIRouter) -> None:
    """Structural admission for one plugin router, before it is mounted.

    ``plugin_route_lifecycle_denied``
        Startup/shutdown hooks would run inside the application's lifespan,
        next to migrations and warm-up, with no budget and no failure
        containment. A plugin does not get to extend the process lifecycle.
    ``plugin_route_unsupported_kind``
        Anything that is not an ``APIRoute`` (a mounted sub-application, a raw
        websocket route, a bare Starlette route) escapes the dependency
        inspection below, so its notebook gate cannot be proven.
    ``plugin_route_missing_notebook_gate``
        A path carrying ``{notebook_id}`` addresses a specific notebook, so it
        must run one of core's own gates. Router-level ``get_current_user``
        proves *who* is calling; it says nothing about whether that user may
        touch this notebook. The accepted set is exactly core's three guards —
        including the read gate (decision 2), so a plugin can mount a read-only
        notebook-scoped route without pretending to need write access.
    """

    if router.on_startup or router.on_shutdown:
        raise PluginRouteMountError(plugin_id, "plugin_route_lifecycle_denied")
    gates = _notebook_gates()
    for route in router.routes:
        if not isinstance(route, APIRoute):
            raise PluginRouteMountError(plugin_id, "plugin_route_unsupported_kind")
        if "{notebook_id}" not in route.path:
            continue
        if not (_dependant_calls(route.dependant) & gates):
            raise PluginRouteMountError(
                plugin_id, "plugin_route_missing_notebook_gate"
            )


def mount_extension_routers(
    app: FastAPI, specs: Sequence[PluginRouterSpec]
) -> None:
    """Build and mount every deployment plugin router. The only such place.

    With zero deployment plugins this returns before constructing anything:
    no context, no event logger, no route. That is what keeps the frozen
    ``api_contract`` fixture valid for the shipped default — the OpenAPI
    document of a plugin-less deployment is byte-identical to one built
    before this seam existed.
    """

    if not specs:
        return
    # One logger for all plugins; the record's ``plugin_id`` is what separates
    # them. ``per_user=True`` matches core's own ``events`` channel so a
    # plugin's records land in the calling user's log directory like everything
    # else that happens inside their request.
    event_log = EventLogger(get_settings(), channel="events", per_user=True)
    for spec in specs:
        context = PluginRouteContext(
            plugin_id=spec.plugin_id,
            settings=spec.settings,
            require_notebook_capability=require_notebook_capability,
            require_notebook_read=require_notebook_read,
            current_actor=plugin_actor,
            user_error=user_error,
            url_sources=_UrlSourceImportAdapter(),
            emit_event=_event_emitter(spec.plugin_id, event_log),
        )
        router = spec.factory(context)
        if not isinstance(router, APIRouter):
            raise PluginRouteMountError(spec.plugin_id, "plugin_router_not_a_router")
        _validate_plugin_router(spec.plugin_id, router)
        app.include_router(
            router,
            prefix=f"{PLUGIN_ROUTE_PREFIX}/{spec.plugin_id}",
            dependencies=[Depends(get_current_user)],
        )
