"""Reuse the fixed core routers' built ``APIRoute`` objects across test apps.

Why: a test process calls ``app.main.create_app()`` hundreds of times, and
about 88% of each call is ``APIRouter.include_router`` re-constructing the same
~300 ``APIRoute`` objects (signature analysis, dependant trees, pydantic
response fields) from the same immutable module-level routers. Production builds
one app per process, so this lives only in the test process and the product
``create_app`` is untouched.

What is reused, and what is not. ``create_app`` still builds a new FastAPI
object, FastMCP server and streamable sub-app, middleware stack, ``app.state``
and ``EventLogger`` on every call. Only the route objects of the fixed core
routers (the ``APIRouter`` instances bound as globals of ``app.main``) come from
a template: each new app gets its own shallow copy of every template route, with
its own mutable containers and dependant tree, ``dependency_overrides_provider``
re-bound to *this* app, and a freshly built ``route.app`` handler, which is the
only place FastAPI binds that provider (verified against FastAPI
``VERIFIED_FASTAPI_VERSION``). The lifespan/startup merge tail of the real
``include_router`` is then replayed by a real ``include_router`` of an empty
router that carries the source router's own ``lifespan_context`` object, so the
merged lifespan chain is the one the real call would have produced.

A call is served from a template only when every one of these holds; anything
else takes the real ``include_router`` unchanged:

* the installed FastAPI is exactly ``VERIFIED_FASTAPI_VERSION`` (otherwise the
  shim does not install at all, and the pin test goes red);
* the target is a FastAPI app's own router (``dependency_overrides_provider``
  is a ``FastAPI`` whose ``.router`` is the target) with no router-level
  prefix, tags, dependencies, responses or callbacks of its own;
* the source router is one of the ``APIRouter`` objects bound as globals of
  ``sys.modules["app.main"]`` right now (the per-call onboarding router and the
  deployment-plugin routers never are);
* only ``prefix`` and ``dependencies`` differ from ``include_router``'s
  defaults;
* the source router has no ``on_startup``/``on_shutdown`` handlers and every
  route is exactly ``APIRoute``;
* the source routes are the very objects the template was built from, and
  every attribute ``include_router`` reads from them still compares equal to
  the snapshot taken then (so a test that adds, removes or edits a route in
  place gets a fresh real build).

Pinned by ``tests/test_app_route_templates.py``. Off switches:
``SILICON_NOTEBOOK_TEST_ROUTE_TEMPLATES=0`` (never install) and the
``disabled()`` context manager (real path for the duration).
"""
from __future__ import annotations

import copy
from contextlib import contextmanager
import inspect
import os
import sys
import threading
from typing import Any, Iterator

import fastapi
from fastapi import FastAPI
from fastapi.datastructures import DefaultPlaceholder
from fastapi.dependencies.models import Dependant
from fastapi.routing import APIRoute, APIRouter, request_response

VERIFIED_FASTAPI_VERSION = "0.135.3"
ENV_SWITCH = "SILICON_NOTEBOOK_TEST_ROUTE_TEMPLATES"

_MAIN_MODULE = "app.main"
_ALLOWED_KWARGS = frozenset({"prefix", "dependencies"})
# Every attribute ``APIRouter.include_router`` reads from a source route.
_ROUTE_INPUTS = (
    "path",
    "endpoint",
    "response_model",
    "status_code",
    "tags",
    "dependencies",
    "summary",
    "description",
    "response_description",
    "responses",
    "deprecated",
    "methods",
    "operation_id",
    "response_model_include",
    "response_model_exclude",
    "response_model_by_alias",
    "response_model_exclude_unset",
    "response_model_exclude_defaults",
    "response_model_exclude_none",
    "include_in_schema",
    "response_class",
    "name",
    "callbacks",
    "openapi_extra",
    "generate_unique_id_function",
    "strict_content_type",
)
_MAX_ENTRIES = 32

_original_include_router = None
_include_defaults: dict[str, Any] = {}
_lock = threading.Lock()
_disabled_depth = 0
_cache: dict[tuple, "_Template"] = {}
_stats = {"hits": 0, "misses": 0, "bypassed": 0, "uncacheable": 0}


class _Template:
    __slots__ = ("router", "source_routes", "inputs", "routes")

    def __init__(self, router, source_routes, inputs, routes) -> None:
        # Strong references: no id in the cache key can be reused by another
        # object while this entry lives.
        self.router = router
        self.source_routes = source_routes
        self.inputs = inputs
        self.routes = routes


def install(*, force: bool = False) -> bool:
    """Wrap ``APIRouter.include_router`` once per process. Idempotent.

    ``force`` ignores the environment switch (the pin tests use it so they
    still prove the shim when a run has it switched off); the FastAPI version
    check is never skipped.
    """
    global _original_include_router
    if not force and os.environ.get(ENV_SWITCH, "").strip() == "0":
        return False
    if fastapi.__version__ != VERIFIED_FASTAPI_VERSION:
        return False
    if _original_include_router is not None:
        return True
    original = APIRouter.include_router
    for name, parameter in inspect.signature(original).parameters.items():
        if parameter.kind is inspect.Parameter.KEYWORD_ONLY:
            _include_defaults[name] = parameter.default
    _original_include_router = original
    APIRouter.include_router = _include_router  # type: ignore[method-assign]
    return True


def uninstall() -> None:
    """Restore the real ``include_router`` and drop every template."""
    global _original_include_router
    if _original_include_router is not None:
        APIRouter.include_router = _original_include_router  # type: ignore[method-assign]
        _original_include_router = None
    clear()


def installed() -> bool:
    return (
        _original_include_router is not None
        and APIRouter.include_router is _include_router
    )


@contextmanager
def disabled() -> Iterator[None]:
    """Route every ``include_router`` through the real implementation."""
    global _disabled_depth
    with _lock:
        _disabled_depth += 1
    try:
        yield
    finally:
        with _lock:
            _disabled_depth -= 1


def stats() -> dict[str, int]:
    with _lock:
        return dict(_stats)


def cached_router_ids() -> frozenset[int]:
    with _lock:
        return frozenset(id(entry.router) for entry in _cache.values())


def clear() -> None:
    with _lock:
        _cache.clear()


def _bump(name: str) -> None:
    with _lock:
        _stats[name] += 1


def _norm(value: Any) -> tuple:
    if isinstance(value, DefaultPlaceholder):
        return ("default", value.value)
    return ("set", value)


def _fixed_core_routers() -> dict[int, APIRouter]:
    main = sys.modules.get(_MAIN_MODULE)
    if main is None:
        return {}
    return {
        id(value): value
        for value in list(vars(main).values())
        if type(value) is APIRouter
    }


def _is_default(name: str, value: Any) -> bool:
    default = _include_defaults.get(name, inspect.Parameter.empty)
    if default is inspect.Parameter.empty:
        return False
    if isinstance(default, DefaultPlaceholder):
        return isinstance(value, DefaultPlaceholder) and value.value is default.value
    return value is default or (value is None and default is None)


def _route_inputs(route: APIRoute) -> tuple:
    return tuple(getattr(route, name) for name in _ROUTE_INPUTS)


def _cache_key(target: APIRouter, router: APIRouter, kwargs: dict) -> tuple | None:
    """None when the call is not eligible for a template."""
    if _disabled_depth:
        return None
    provider = target.dependency_overrides_provider
    if not isinstance(provider, FastAPI) or provider.router is not target:
        return None
    if target.prefix or target.tags or target.dependencies:
        return None
    if target.responses or target.callbacks:
        return None
    for name, value in kwargs.items():
        if name not in _ALLOWED_KWARGS and not _is_default(name, value):
            return None
    if id(router) not in _fixed_core_routers():
        return None
    if router.on_startup or router.on_shutdown:
        return None
    if not router.routes or any(type(r) is not APIRoute for r in router.routes):
        return None
    try:
        dependency_signature = tuple(
            (
                type(depends),
                depends.dependency,
                depends.use_cache,
                depends.scope,
                tuple(getattr(depends, "scopes", None) or ()),
            )
            for depends in (kwargs.get("dependencies") or ())
        )
        key = (
            id(router),
            tuple(id(route) for route in router.routes),
            kwargs.get("prefix", ""),
            dependency_signature,
            _norm(router.default_response_class),
            _norm(router.generate_unique_id_function),
            _norm(router.strict_content_type),
            _norm(target.default_response_class),
            _norm(target.generate_unique_id_function),
            target.route_class,
            _norm(target.strict_content_type),
            target.include_in_schema,
            target.deprecated,
        )
        hash(key)
    except TypeError:
        return None
    return key


def _include_router(self: APIRouter, router: APIRouter, **kwargs: Any) -> None:
    original = _original_include_router
    key = _cache_key(self, router, kwargs)
    if key is None:
        _bump("bypassed")
        return original(self, router, **kwargs)
    with _lock:
        entry = _cache.get(key)
    if entry is not None and _template_matches(entry, router):
        _bump("hits")
        _instantiate(entry, self, router, kwargs.get("dependencies"))
        return None
    _bump("misses")
    start = len(self.routes)
    result = original(self, router, **kwargs)
    _store(key, router, self.routes[start:])
    return result


def _template_matches(entry: _Template, router: APIRouter) -> bool:
    routes = router.routes
    if len(routes) != len(entry.source_routes):
        return False
    for live, recorded in zip(routes, entry.source_routes):
        if live is not recorded:
            return False
    return [_route_inputs(route) for route in routes] == entry.inputs


def _store(key: tuple, router: APIRouter, added: list) -> None:
    source_routes = tuple(router.routes)
    if len(added) != len(source_routes) or any(
        type(route) is not APIRoute for route in added
    ):
        _bump("uncacheable")
        return
    try:
        inputs = copy.deepcopy([_route_inputs(route) for route in source_routes])
    except Exception:  # noqa: BLE001 - an uncopyable input just means no reuse
        _bump("uncacheable")
        return
    if inputs != [_route_inputs(route) for route in source_routes]:
        _bump("uncacheable")
        return
    templates = []
    for route in added:
        template = _copy_route(route)
        # Detach the template from the app it was first built for.
        template.dependency_overrides_provider = None
        template.app = None
        templates.append(template)
    entry = _Template(router, source_routes, inputs, tuple(templates))
    with _lock:
        if len(_cache) >= _MAX_ENTRIES:
            _cache.clear()
        _cache[key] = entry


def _copy_containers(clone: Any) -> None:
    for name, value in vars(clone).items():
        kind = type(value)
        if kind is list or kind is dict or kind is set:
            setattr(clone, name, kind(value))


def _copy_dependant(dependant: Dependant) -> Dependant:
    clone = copy.copy(dependant)
    _copy_containers(clone)
    clone.dependencies = [_copy_dependant(sub) for sub in dependant.dependencies]
    return clone


def _copy_route(route: APIRoute) -> APIRoute:
    clone = copy.copy(route)
    _copy_containers(clone)
    clone.dependant = _copy_dependant(route.dependant)
    clone._flat_dependant = _copy_dependant(route._flat_dependant)
    return clone


def _instantiate(
    entry: _Template, target: APIRouter, router: APIRouter, dependencies: Any
) -> None:
    provider = target.dependency_overrides_provider
    include_dependencies = list(dependencies or ())
    for template, source in zip(entry.routes, entry.source_routes):
        route = _copy_route(template)
        # The same Depends objects the real call would have recorded.
        route.dependencies = [*include_dependencies, *source.dependencies]
        route.dependency_overrides_provider = provider
        route.app = request_response(route.get_route_handler())
        target.routes.append(route)
    # Replay the real call's tail (startup/shutdown copy + lifespan merge) with
    # the source router's own lifespan object, through the real implementation.
    tail = APIRouter()
    tail.lifespan_context = router.lifespan_context
    _original_include_router(target, tail)
