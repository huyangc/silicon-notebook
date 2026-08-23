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
so no plugin route can ever serve an anonymous request.

Authorization is **per port, not per route shape**. Every core port that can
change a notebook checks the calling user's capability itself, against the
request-scoped user core resolved — never against anything the plugin passed
in. The ``{notebook_id}`` path check below is defence in depth on top of that,
not the thing that makes the port safe: a plugin can name its path parameter
``{nb}``, take the notebook id from a request body, or mount only core's *read*
gate, and none of those shapes would be caught by a path-template rule.

Two response-shape rules the mount also enforces, both about not letting a
plugin's failure look like a core failure:

* a 401 that a plugin handler either *raises* or *returns* (a plugin proxying
  an upstream service's own 401 response is a normal return, not a raise)
  becomes a 424 (see ``_wrap_handler_unauthorized``), because the browser
  treats 401 as "your session died" and logs the user out;
* everything else the plugin raises or returns is its own business.

Failures here raise :class:`PluginRouteMountError` out of ``create_app()``, so
the process refuses to start. A plugin whose routes cannot be mounted safely
must never come up half-wired.
"""
from __future__ import annotations

import asyncio
from collections.abc import Callable, Mapping, Sequence
from functools import wraps
import logging
import re
from typing import Any

from fastapi import APIRouter, Depends, FastAPI, HTTPException
from fastapi.routing import APIRoute
from starlette.concurrency import run_in_threadpool
from starlette.responses import Response

from app.api import deps as core_deps
from app.api import source_routes
from app.api.deps import (
    get_current_user,
    notebook_capability_allowed,
    require_notebook_capability,
    require_notebook_read,
    user_error,
)
from app.core.config import get_settings
from app.core.event_logging import EventLogger
from app.core.request_context import get_request_user
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

    The message is ``"{plugin_id}: {reason_code}"``, optionally followed by
    ``" ({exception_type})"`` — the offending exception's *class name*, never
    ``str(exc)`` — and nothing else: no module path, no route handler name, no
    settings value, no traceback. An operator gets the plugin to disable, a
    stable code to look up, and (when the reason names a caught exception) the
    class it was. This mirrors ``ExtensionDiscoveryError`` in
    ``app.extensions.discovery`` on purpose — same shape, same redaction.
    """

    def __init__(
        self, plugin_id: str, reason: str, *, exception_type: str = ""
    ) -> None:
        self.plugin_id = plugin_id
        self.reason = reason
        self.exception_type = exception_type
        message = f"{plugin_id}: {reason}"
        if exception_type:
            message = f"{message} ({exception_type})"
        super().__init__(message)


# Deliberately the same logger *name* ``app.extensions.discovery`` logs
# rejections under (``DISCOVERY_LOGGER`` there), gotten by name rather than by
# importing that module — this file may not import ``app.extensions.*`` (see
# the module docstring). One logger name means an operator filters exactly one
# thing for every deployment-plugin rejection, whether it happened during
# discovery or here at mount time.
_PLUGIN_ROUTE_LOGGER = logging.getLogger("silicon_notebook.extensions")


async def plugin_actor(user=Depends(get_current_user)) -> PluginActor:
    """Narrow the authenticated session down to what a plugin may see.

    ``async`` on purpose: ``get_current_user`` is an async dependency, and a
    sync wrapper would cost every plugin request a threadpool hop to copy two
    fields. The parameter is deliberately unannotated — annotating it would
    mean importing ``app.models.identity`` into a module whose import list is
    itself part of the contract.
    """

    return PluginActor(id=user.id, is_admin=getattr(user, "role", "") == "admin")


# The capability core's own ``POST /notebooks/{id}/sources/url`` declares.
# Naming it once and resolving it through ``notebook_capability_allowed`` means
# the port and the browser endpoint read the *same* row of
# ``deps._CAPABILITY_LEVELS``: a future level flip moves both together, and
# neither can drift into a second hand-written predicate.
_URL_IMPORT_CAPABILITY = "sources:write"


def _authorized_notebook(capability: str, notebook_id: str) -> str:
    """Prove the *request's own* user holds ``capability`` on ``notebook_id``.

    The user comes from ``app.core.request_context`` — the ContextVar
    ``get_current_user`` sets while resolving the router-level session
    dependency — and from nowhere else. In particular it is never taken from an
    argument: :class:`PluginActor` is an ordinary dataclass a plugin can
    construct with any id it likes, so a port that accepted an actor would be
    accepting the caller's own claim about who is calling.

    ``get_request_user()`` has **no fallback**: unset means ``None``, not the
    seeded admin (that fallback lives one layer down, in the repository's
    ``current_user``, and must never be what answers an authorization question).
    Unset cannot happen through a mounted plugin route — the router-level
    ``Depends(get_current_user)`` runs first — so reaching it means the port was
    called from outside a request, e.g. a thread the plugin spawned itself. That
    is refused rather than trusted.

    The refusal is 404 with core's own wording, exactly like
    ``require_notebook_capability``: a plugin route must not become the one
    surface that tells a stranger which notebook ids exist. 404 rather than 401
    for the unset case too, deliberately — 401 out of a handler is translated to
    424 downstream (``_wrap_handler_unauthorized``), which would relabel a core
    invariant violation as an upstream service problem.
    """

    user = get_request_user()
    if user is None:
        raise HTTPException(status_code=404, detail="Notebook not found")
    if not notebook_capability_allowed(capability, notebook_id, user.id):
        raise HTTPException(status_code=404, detail="Notebook not found")
    return notebook_id


# Developer-facing on purpose, and therefore English and *not* routed through
# ``user_error()``: no end user can act on it, and dressing it up as UI copy
# would put a plugin's wiring bug in front of the person who typed a URL. It
# reaches an operator as an ordinary 500 plus a traceback; what makes it worth
# writing carefully is that it names the exact fix.
_EVENT_LOOP_MISUSE_MESSAGE = (
    "url_sources.import_urls must not be called from an async handler; "
    "await url_sources.import_urls_async(...) instead"
)


def _refuse_event_loop_thread() -> None:
    """Refuse to run blocking work on the thread the event loop is using.

    ``asyncio.get_running_loop()`` *succeeding* is the signal: a loop is
    running on **this** thread, so this call is inside an ``async def``
    handler (or anything else awaited by the loop). Blocking there stalls
    every other in-flight request in the process, and the URL import is a
    database write plus one serial remote probe per URL — seconds, not
    milliseconds, and bounded by somebody else's HTTP server.

    The check is deliberately positive rather than a thread-identity
    comparison: FastAPI's threadpool workers, ``run_in_threadpool`` and a
    thread a plugin spawned itself all have no running loop and are all fine,
    while "the loop thread" is not a stable object this module could name.

    A raised ``RuntimeError`` — not an ``HTTPException``, not a ``user_error``
    — because this is a bug in the plugin's code rather than a condition of
    the request: it must surface as a 500 with a traceback pointing at the
    call site, every time, and never look like something the end user did.
    """

    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return
    raise RuntimeError(_EVENT_LOOP_MISUSE_MESSAGE)


class _UrlSourceImportAdapter:
    """``PluginUrlSourceImportPort`` over core's single URL-import implementation.

    The port authorizes itself. ``import_url_sources`` only does capacity
    accounting and scheduling — it takes a notebook id and trusts its caller —
    so this adapter checks ``sources:write`` for the request's own user before
    delegating. It cannot be delegated to the route's declared gate: a plugin
    chooses its own path template and its own dependencies, so "this route ran
    some core notebook gate" is neither necessary (the id can arrive in a body)
    nor sufficient (the read gate is a core gate too).

    Past that check, every rule that governs the browser endpoint governs a
    plugin too, because it is literally the same function: per-URL capacity
    accounting, the admin exemption, the unconfigured-parser 400, and the
    background parse scheduler. The adapter only re-shapes the pydantic result
    into the domain dataclasses so a plugin never has to import
    ``app.models.sources``.
    """

    def import_urls(
        self, notebook_id: str, urls: Sequence[str]
    ) -> PluginUrlImportResult:
        """Blocking import, for a sync handler (or a worker thread).

        See :func:`_refuse_event_loop_thread` for why an ``async def`` handler
        is turned away here instead of being quietly served.
        """

        _refuse_event_loop_thread()
        _authorized_notebook(_URL_IMPORT_CAPABILITY, notebook_id)
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

    async def import_urls_async(
        self, notebook_id: str, urls: Sequence[str]
    ) -> PluginUrlImportResult:
        """The same import, offloaded so an ``async def`` handler may await it.

        It delegates to :meth:`import_urls` **in the worker thread** rather
        than duplicating the body: one implementation means the authorization
        check, the capacity accounting and the result shaping cannot drift
        apart between the two entry points, and the loop-thread refusal above
        is exercised on this path too (it passes, because a worker thread has
        no running loop — which is precisely the property being bought).

        ``run_in_threadpool`` is core's existing spelling for this hop (see
        ``app.api.deps``), and it carries the current ``contextvars`` context
        into the worker. That is load-bearing, not incidental: the port reads
        the request's authenticated user out of ``app.core.request_context``,
        so without the copied context ``get_request_user()`` would come back
        ``None`` in the worker and *every* async import — the owner's included
        — would 404. ``test_async_import_still_authorizes_the_request_user``
        and its owner-side twin fail together if that ever stops holding.
        """

        return await run_in_threadpool(self.import_urls, notebook_id, list(urls))


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

    The traversal is transitive rather than one level deep because wrapping a
    core gate in the plugin's own dependency is a normal shape —
    ``Depends(my_gate)`` where ``my_gate`` itself takes
    ``Depends(context.require_notebook_read)``. A one-level scan would refuse
    that router; ``test_a_wrapped_core_gate_still_counts`` keeps the recursion
    honest.
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

        ⚠ This is **defence in depth, not the authorization boundary**, and the
        limits are structural, not incidental: it only looks at the literal
        ``{notebook_id}`` substring (a route declaring ``{nb}``, or reading the
        id out of a request body, is not covered), and the accepted set
        includes the *read* gate (so clearing it does not imply write access).
        What actually keeps a plugin from writing into someone else's notebook
        is that each core port authorizes the request's own user itself — see
        ``_authorized_notebook``. Removing this check would not open a hole;
        removing that one would.
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


# A plugin talking to an internal service upstream will sooner or later get a
# 401 back and re-raise it. In core that status has exactly one meaning to the
# browser — ``frontend/app/errors.ts`` clears the stored token and reloads —
# so an upstream credential expiring inside one plugin route would log the user
# out of the whole product. 424 Failed Dependency says the same thing without
# that side effect, and the copy names the actionable party.
_UPSTREAM_UNAUTHORIZED_EVENT = "plugin_upstream_unauthorized"


def _translate_unauthorized(
    exc: HTTPException, emit: Callable[[Mapping[str, object]], None]
) -> "HTTPException | None":
    """The 424 to raise instead of ``exc``, or ``None`` to let ``exc`` stand.

    The copy is written inline rather than hoisted to a module constant on
    purpose: both ``scripts/check_ui_vocabulary.py`` and
    ``test_user_error.py::test_static_user_error_messages_are_chinese`` read the
    ``user_error()`` argument as an AST literal, and a constant reference would
    slip past the first and land this site in the second's dynamic-site ledger.
    """

    if exc.status_code != 401:
        return None
    emit({"event": _UPSTREAM_UNAUTHORIZED_EVENT})
    return user_error(424, "扩展服务的上游认证失败，请联系管理员")


def _translate_unauthorized_response(
    result: Any, emit: Callable[[Mapping[str, object]], None]
) -> "HTTPException | None":
    """The 424 to raise instead of a handler's *returned* 401 ``Response``.

    Sibling of :func:`_translate_unauthorized`, for the other way a plugin
    hands core a 401: a handler that catches its own upstream call, copies the
    status straight onto a ``Response``/``JSONResponse`` it builds itself (a
    perfectly ordinary "proxy the upstream status" shape), and returns it —
    never raising anything, so :func:`_translate_unauthorized`'s ``except``
    clause never runs. To the browser this is indistinguishable from a raised
    401: ``frontend/app/errors.ts`` reads the status line, not how the backend
    produced it. Same event, same copy, same header, so a plugin cannot get a
    different user-facing outcome by returning the status instead of raising
    it — see ``_wrap_handler_unauthorized`` for where this is called.

    ``result`` is deliberately typed ``Any`` rather than ``Response``: this
    runs on *every* handler return value, most of which are plain dicts/models
    FastAPI has not yet serialized, and ``isinstance`` below is what separates
    "an actual Response object" from "a 401 field somewhere in a payload" —
    the latter is the plugin's own business, not core's.
    """

    if not isinstance(result, Response) or result.status_code != 401:
        return None
    emit({"event": _UPSTREAM_UNAUTHORIZED_EVENT})
    return user_error(424, "扩展服务的上游认证失败，请联系管理员")


def _wrap_handler_unauthorized(
    route: APIRoute, emit: Callable[[Mapping[str, object]], None]
) -> None:
    """Translate 401s raised *inside* this route's handler; leave gates alone.

    The scope is the whole point, and it is what rules out the two obvious
    implementations:

    * a ``route_class`` override of ``get_route_handler`` wraps dependency
      solving *and* the endpoint, so core's own router-level 401 ("no session")
      would be rewritten into a 424 — the user would stop being logged out when
      they genuinely should be. (``include_router`` also forces
      ``route_class_override=type(route)``, so the class could not be injected
      from here anyway.)
    * wrapping ``route.endpoint`` before ``include_router`` makes FastAPI
      re-solve the dependency tree from the *wrapper*'s signature. With
      ``functools.wraps`` the signature survives, but ``get_typed_signature``
      resolves string annotations against ``call.__globals__`` — this module's
      globals, not the plugin's — so any plugin using
      ``from __future__ import annotations`` with its own request model would
      fail to mount. Correct-looking, and broken for the common case.

    So the swap happens one level in: ``Dependant.call`` is what
    ``run_endpoint_function`` invokes *after* every dependency has already been
    solved, and it is read at call time. The dependant itself was built from the
    pristine endpoint, so no signature, annotation, or ``__globals__`` question
    arises. This is the same semi-public ``route.dependant`` surface the gate
    check above already depends on (fastapi pinned in
    ``backend/requirements.txt:1``); the 401-translation tests fail loudly if a
    future version changes it.

    Generator endpoints are skipped: FastAPI calls ``dependant.call`` directly
    for them and iterates the result inside the response body, so a 401 raised
    there happens after the status line is already committed and there is
    nothing left to translate. Touching the three ``is_*_callable`` properties
    before the swap also warms their ``cached_property`` slots off the original
    callable rather than the wrapper.

    A handler can also hand core a 401 without raising it — proxying an
    upstream service by returning a ``Response``/``JSONResponse`` built from
    the upstream's own status code is the normal shape for that, not an edge
    case. So after ``inner`` returns normally (no exception at all), the
    return value is checked the same way the exception is:
    :func:`_translate_unauthorized_response` is the ``except`` branch's sibling
    for that path, same event, same copy, same header. This is why the swap
    still has to happen at ``dependant.call`` and not, say, only around the
    ``except``: the check has to see every ordinary return, not just the ones
    that unwound through an exception.
    """

    dependant = route.dependant
    inner = dependant.call
    if inner is None:  # pragma: no cover - FastAPI always sets it
        return
    if dependant.is_gen_callable or dependant.is_async_gen_callable:
        return
    if dependant.is_coroutine_callable:

        @wraps(inner)
        async def call(**values: Any) -> Any:
            try:
                result = await inner(**values)
            except HTTPException as exc:
                translated = _translate_unauthorized(exc, emit)
                if translated is None:
                    raise
                raise translated from None
            translated = _translate_unauthorized_response(result, emit)
            if translated is not None:
                raise translated
            return result

    else:

        @wraps(inner)
        def call(**values: Any) -> Any:
            try:
                result = inner(**values)
            except HTTPException as exc:
                translated = _translate_unauthorized(exc, emit)
                if translated is None:
                    raise
                raise translated from None
            translated = _translate_unauthorized_response(result, emit)
            if translated is not None:
                raise translated
            return result

    dependant.call = call


def _call_plugin_router_factory(
    spec: PluginRouterSpec, context: PluginRouteContext
) -> Any:
    """Call ``spec.factory(context)``, sanitizing whatever it raises.

    A plugin's ``build_router`` is arbitrary code handed the plugin's own
    validated settings — see ``_UrlSourceImportAdapter`` and
    :class:`PluginRouteContext` above for what that settings object may hold,
    up to and including secrets a deployment operator configured for it. If
    the factory raises with that value interpolated into its message (a
    perfectly ordinary thing to do — "upstream refused {token}" — from the
    factory's own point of view), letting the exception propagate untouched
    out of ``create_app()`` would put that secret in a startup traceback: on
    stderr, in a process supervisor's log, potentially in an error-tracking
    service. So every exception this call can raise is caught here and
    replaced with :class:`PluginRouteMountError` carrying only the plugin id,
    a stable reason code, and the exception's *class name* — never
    ``str(exc)``, never the traceback (``from None`` clears ``__cause__`` and
    sets ``__suppress_context__`` so nothing upstream re-attaches it).

    Two things are deliberately let through untouched:

    * a :class:`PluginRouteMountError` the factory itself raises (or a
      preceding call already wrapped) — re-wrapping it would double-encode
      the reason and lose the original one;
    * ``KeyboardInterrupt``/``SystemExit`` — an operator's Ctrl-C or a
      ``sys.exit()`` reached from inside factory code must still stop the
      process, exactly as ``app.extensions.discovery`` treats them.
    """

    try:
        return spec.factory(context)
    except PluginRouteMountError:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        _PLUGIN_ROUTE_LOGGER.error(
            "plugin router factory failed: plugin=%s reason=%s exc=%s",
            spec.plugin_id,
            "plugin_router_factory_failed",
            type(exc).__name__,
        )
        raise PluginRouteMountError(
            spec.plugin_id,
            "plugin_router_factory_failed",
            exception_type=type(exc).__name__,
        ) from None


def _run_plugin_router_validation(plugin_id: str, router: APIRouter) -> None:
    """Run :func:`_validate_plugin_router`, sanitizing whatever it raises.

    ``_validate_plugin_router`` itself only ever raises
    :class:`PluginRouteMountError` for a condition it checks for on purpose —
    that passes through unchanged. What it does *not* control is the shape of
    ``router``: a plugin factory can return an ``APIRouter`` subclass whose
    ``on_startup``/``on_shutdown``/``routes`` are overridden properties, and a
    property that raises on read would otherwise blow up validation with a
    raw, unsanitized exception before this module has had a chance to look at
    it — the same secret-leak risk the factory call above guards against, one
    step later. ``KeyboardInterrupt``/``SystemExit`` still pass through
    untouched.
    """

    try:
        _validate_plugin_router(plugin_id, router)
    except PluginRouteMountError:
        raise
    except (KeyboardInterrupt, SystemExit):
        raise
    except Exception as exc:
        _PLUGIN_ROUTE_LOGGER.error(
            "plugin router validation failed: plugin=%s reason=%s exc=%s",
            plugin_id,
            "plugin_router_validation_failed",
            type(exc).__name__,
        )
        raise PluginRouteMountError(
            plugin_id,
            "plugin_router_validation_failed",
            exception_type=type(exc).__name__,
        ) from None


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
        emit = _event_emitter(spec.plugin_id, event_log)
        context = PluginRouteContext(
            plugin_id=spec.plugin_id,
            settings=spec.settings,
            require_notebook_capability=require_notebook_capability,
            require_notebook_read=require_notebook_read,
            current_actor=plugin_actor,
            user_error=user_error,
            url_sources=_UrlSourceImportAdapter(),
            emit_event=emit,
        )
        router = _call_plugin_router_factory(spec, context)
        if not isinstance(router, APIRouter):
            raise PluginRouteMountError(spec.plugin_id, "plugin_router_not_a_router")
        _run_plugin_router_validation(spec.plugin_id, router)
        # ``include_router`` copies each APIRoute into a fresh instance with its
        # own freshly solved dependant, so the translation has to be applied to
        # what landed on the app — not to the plugin's own router objects.
        mounted_from = len(app.routes)
        app.include_router(
            router,
            prefix=f"{PLUGIN_ROUTE_PREFIX}/{spec.plugin_id}",
            dependencies=[Depends(get_current_user)],
        )
        for route in app.routes[mounted_from:]:
            if isinstance(route, APIRoute):
                _wrap_handler_unauthorized(route, emit)
