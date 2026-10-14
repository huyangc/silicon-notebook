"""Pins for ``tests/speedups/app_routes.py`` (create_app route templates).

The shim serves the fixed core routers' routes to every ``create_app()`` after
the first from a per-process template. These tests prove that an app built that
way is the app the real ``include_router`` would have built, that every app
resolves ``dependency_overrides`` against itself only, that the template is
dropped when a source router changes, and that per-call routers (the onboarding
router and deployment-plugin routers) are never templated.

The frozen ``api_contract`` test reads the module-level ``app.main.app``, which
is built on the miss path, so the hit path is covered only here.
"""
from __future__ import annotations

import asyncio
import inspect
import os
from pathlib import Path
import re
from types import SimpleNamespace

import fastapi
from fastapi import APIRouter
from fastapi.datastructures import DefaultPlaceholder
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
import pytest
from starlette.routing import Mount

from tests.speedups import app_routes


_REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements.txt"


@pytest.fixture
def shim():
    """The shim, installed for this test even when the run switched it off."""
    forced = not app_routes.installed()
    if forced:
        assert app_routes.install(force=True)
    try:
        yield app_routes
    finally:
        if forced:
            app_routes.uninstall()


def _main():
    from app import main

    return main


def _fixed_core_routers() -> list[APIRouter]:
    return [
        value for value in vars(_main()).values() if type(value) is APIRouter
    ]


def _delta(before: dict[str, int], after: dict[str, int]) -> dict[str, int]:
    return {name: after[name] - before[name] for name in after}


def _hit_app():
    """A ``create_app()`` whose fixed core routes all came from templates."""
    main = _main()
    main.create_app()  # ensure every template exists in this process
    before = app_routes.stats()
    app = main.create_app()
    delta = _delta(before, app_routes.stats())
    assert delta["hits"] == len(_fixed_core_routers()), delta
    assert delta["misses"] == 0 and delta["uncacheable"] == 0, delta
    return app


def _endpoint_identity(endpoint):
    # create_app()'s own `/` and `/api/ready` handlers (and FastAPI's docs
    # handlers) are closures defined per app; compare those by name.
    qualname = getattr(endpoint, "__qualname__", repr(endpoint))
    if "<locals>" in qualname:
        return (getattr(endpoint, "__module__", None), qualname)
    return endpoint


def _response_class(value):
    if isinstance(value, DefaultPlaceholder):
        return ("default", value.value)
    return ("set", value)


def _dependant_calls(dependant) -> list:
    calls = [dependant.call]
    for sub in dependant.dependencies:
        calls.extend(_dependant_calls(sub))
    return calls


def _route_view(route) -> tuple:
    if isinstance(route, Mount):
        return (type(route), route.path, route.name)
    if not isinstance(route, APIRoute):
        return (
            type(route),
            route.path,
            route.name,
            frozenset(route.methods or ()),
            _endpoint_identity(route.endpoint),
            route.include_in_schema,
        )
    return (
        type(route),
        route.path,
        route.name,
        frozenset(route.methods),
        _endpoint_identity(route.endpoint),
        route.include_in_schema,
        route.response_model,
        route.status_code,
        tuple(route.tags),
        route.unique_id,
        tuple(depends.dependency for depends in route.dependencies),
        _response_class(route.response_class),
        route.summary,
        route.description,
        tuple(_endpoint_identity(call) for call in _dependant_calls(route.dependant)),
    )


def _lifespan_chain(app) -> list:
    """The nested contexts ``include_router`` merged into the app's lifespan,
    outermost include last; fixed core routers' contexts by identity."""
    fixed = {id(router.lifespan_context) for router in _fixed_core_routers()}
    chain = []
    context = app.router.lifespan_context
    while getattr(getattr(context, "__wrapped__", None), "__qualname__", "").endswith(
        "_merge_lifespan_context.<locals>.merged_lifespan"
    ):
        cells = inspect.getclosurevars(context.__wrapped__).nonlocals
        nested = cells["nested_context"]
        chain.append(nested if id(nested) in fixed else type(nested).__qualname__)
        context = cells["original_context"]
    chain.append(_endpoint_identity(context))
    return chain


def test_the_shim_is_verified_against_the_pinned_fastapi():
    """The clone path relies on FastAPI internals read from this exact version
    (``APIRoute`` keeps all state in ``__dict__``, binds
    ``dependency_overrides_provider`` only in ``get_route_handler``, and
    ``include_router`` ends with the startup/lifespan merge). An upgrade must
    re-verify them; until then the shim refuses to install."""
    pinned = re.search(
        r"^fastapi==(\S+)", _REQUIREMENTS.read_text(encoding="utf-8"), re.MULTILINE
    )
    assert pinned is not None
    assert pinned.group(1) == app_routes.VERIFIED_FASTAPI_VERSION
    assert fastapi.__version__ == app_routes.VERIFIED_FASTAPI_VERSION
    if os.environ.get(app_routes.ENV_SWITCH, "").strip() != "0":
        assert app_routes.installed(), "conftest.pytest_configure did not install it"


def test_a_templated_app_is_the_app_a_real_build_produces(shim):
    hit_app = _hit_app()
    before = app_routes.stats()
    with app_routes.disabled():
        real_app = _main().create_app()
    delta = _delta(before, app_routes.stats())
    assert delta["hits"] == 0 and delta["misses"] == 0, delta

    assert hit_app.openapi() == real_app.openapi()
    hit_view = [_route_view(route) for route in hit_app.routes]
    real_view = [_route_view(route) for route in real_app.routes]
    assert hit_view == real_view
    paths = [view[1] for view in hit_view]
    assert {"/", "/api/ready", "/mcp"} <= set(paths)
    assert sum(isinstance(route, APIRoute) for route in hit_app.routes) > 100

    assert _lifespan_chain(hit_app) == _lifespan_chain(real_app)
    assert len(_lifespan_chain(hit_app)) > len(_fixed_core_routers())

    async def enter_and_exit(app) -> None:
        async with app.router.lifespan_context(app):
            pass

    asyncio.run(enter_and_exit(hit_app))
    asyncio.run(enter_and_exit(real_app))


def test_every_templated_route_resolves_overrides_against_its_own_app(
    shim, monkeypatch
):
    from app.api.deps import get_current_user
    from app.core.config import get_settings
    from app.models.identity import UserProfile

    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    get_settings.cache_clear()
    first = _hit_app()
    second = _hit_app()

    first_routes = [r for r in first.routes if isinstance(r, APIRoute)]
    second_routes = [r for r in second.routes if isinstance(r, APIRoute)]
    assert all(r.dependency_overrides_provider is first for r in first_routes)
    assert all(r.dependency_overrides_provider is second for r in second_routes)
    assert not {id(r) for r in first_routes} & {id(r) for r in second_routes}
    assert not {id(r.app) for r in first_routes} & {id(r.app) for r in second_routes}
    assert not {id(r.dependant) for r in first_routes} & {
        id(r.dependant) for r in second_routes
    }

    user = UserProfile(id="u-pin", email="pin@example.com", display_name="pin", role="user")
    first.dependency_overrides[get_current_user] = lambda: user

    overridden = TestClient(first).get("/api/me")
    assert overridden.status_code == 200, overridden.text
    assert overridden.json()["id"] == "u-pin"
    assert TestClient(second).get("/api/me").status_code == 401


def test_a_changed_source_router_is_rebuilt_not_served_stale(shim, monkeypatch):
    from app.api.routes import router as core_router

    _hit_app()

    def probe() -> dict:
        return {"probe": True}

    extra = APIRoute("/__route_template_probe", probe)
    monkeypatch.setattr(core_router, "routes", [*core_router.routes, extra])
    before = app_routes.stats()
    added = _main().create_app()
    assert _delta(before, app_routes.stats())["misses"] == 1
    assert "/api/__route_template_probe" in {r.path for r in added.routes}
    monkeypatch.undo()

    restored = _hit_app()
    assert "/api/__route_template_probe" not in {r.path for r in restored.routes}

    # An in-place edit of a route the template was built from is seen too.
    edited = next(r for r in core_router.routes if r.summary is None)
    monkeypatch.setattr(edited, "summary", "edited in place")
    before = app_routes.stats()
    rebuilt = _main().create_app()
    assert _delta(before, app_routes.stats())["misses"] == 1
    assert [
        r.summary
        for r in rebuilt.routes
        if getattr(r, "endpoint", None) is edited.endpoint
    ] == [
        "edited in place"
    ]


def test_per_call_routers_are_never_templated(shim, monkeypatch):
    from app.api.agent_mcp_onboarding import AGENT_MCP_ONBOARDING_PATH
    from app.domain.extension_http import PLUGIN_ROUTE_PREFIX, PluginRouterSpec

    plugin_router = APIRouter()

    @plugin_router.get("/ping")
    def ping() -> dict:
        return {"ok": True}

    # The same router object on every call: a shim that templated anything
    # outside app.main's globals would serve it from the cache the second time.
    spec = PluginRouterSpec(
        plugin_id="corp.pin",
        contribution_id="corp.pin.router",
        factory=lambda _context: plugin_router,
        settings=SimpleNamespace(),
    )
    main = _main()
    monkeypatch.setattr(main, "application_plugin_router_specs", lambda _rt: [spec])
    # Start from an empty cache so the subset check below sees only what this
    # test's own create_app() calls stored.
    app_routes.clear()

    main.create_app()
    before = app_routes.stats()
    app = main.create_app()
    delta = _delta(before, app_routes.stats())

    assert delta["hits"] == len(_fixed_core_routers()), delta
    assert delta["misses"] == 0, delta
    assert delta["bypassed"] >= 2, delta  # onboarding + plugin, at least
    paths = {route.path for route in app.routes}
    assert f"{PLUGIN_ROUTE_PREFIX}/corp.pin/ping" in paths
    assert AGENT_MCP_ONBOARDING_PATH in paths
    cached = app_routes.cached_router_ids()
    assert id(plugin_router) not in cached
    assert cached <= {id(router) for router in _fixed_core_routers()}
