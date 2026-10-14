"""Pins for ``tests/speedups/app_routes.py`` (create_app route templates).

The shim serves the fixed core routers' routes to every ``create_app()`` after
the first from a per-process template. These tests prove that an app built that
way is the app the real ``include_router`` would have built, that every app
resolves ``dependency_overrides`` against itself only, that the template is
dropped when a source router changes, that a router outside ``app.main``'s
globals (the onboarding router, deployment-plugin routers) is never templated,
and that every call outside the hit conditions builds exactly what the real
``include_router`` builds. A source guard keeps tests from patching the names
string annotations on ``app.api`` modules resolve to, the one input templates
cannot see.

The frozen ``api_contract`` test reads the module-level ``app.main.app``, which
is built on the miss path, so the hit path is covered only here.
"""
from __future__ import annotations

import ast
import asyncio
import inspect
import os
from pathlib import Path
import re
from types import SimpleNamespace

import fastapi
from fastapi import APIRouter, Depends, FastAPI
from fastapi.datastructures import DefaultPlaceholder
from fastapi.responses import PlainTextResponse
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
import pytest
from starlette.routing import Mount

from tests.speedups import app_routes


_REQUIREMENTS = Path(__file__).resolve().parents[1] / "requirements.txt"
_TESTS_ROOT = Path(__file__).resolve().parent


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


def _owned_container_ids(routes) -> set[int]:
    """Ids of every object a route must own per app: its own list/dict/set
    attributes, and every Dependant (plus its list/dict/set attributes) in
    both ``dependant`` and ``_flat_dependant``, walked recursively."""
    owned: set[int] = set()

    def containers(obj) -> None:
        for value in vars(obj).values():
            if type(value) in (list, dict, set):
                owned.add(id(value))

    def walk(dependant) -> None:
        owned.add(id(dependant))
        containers(dependant)
        for sub in dependant.dependencies:
            walk(sub)

    for route in routes:
        containers(route)
        walk(route.dependant)
        walk(route._flat_dependant)
    return owned


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
    shared = _owned_container_ids(first_routes) & _owned_container_ids(second_routes)
    assert not shared, f"{len(shared)} per-route containers are shared between apps"

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


def test_routers_outside_app_main_globals_are_never_templated(shim, monkeypatch):
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


# --- Calls outside the hit conditions -------------------------------------


def _boundary_dependency() -> None:
    return None


def _boundary_unique_id(route: APIRoute) -> str:
    return f"boundary-{route.name}"


def _boundary_startup() -> None:
    return None


def _include(target, router, **kwargs):
    target.include_router(router, **kwargs)
    return target


# Each case is one boundary of the hit conditions or the cache key, built on a
# fresh target after the default configuration (``FastAPI()`` + prefix="/api",
# what create_app does) has a template. The guard that case exercises:
_BOUNDARY_CASES = {
    # cache key: prefix
    "other_prefix": lambda r: _include(FastAPI(), r, prefix="/other"),
    # cache key: include-level dependency signature
    "include_dependencies": lambda r: _include(
        FastAPI(), r, prefix="/api", dependencies=[Depends(_boundary_dependency)]
    ),
    # eligibility: only prefix/dependencies may differ from the defaults
    "include_tags": lambda r: _include(FastAPI(), r, prefix="/api", tags=["edge"]),
    "include_responses": lambda r: _include(
        FastAPI(), r, prefix="/api", responses={418: {"description": "teapot"}}
    ),
    # eligibility: the target router carries no prefix/tags/deps/responses/callbacks
    "app_dependencies": lambda r: _include(
        FastAPI(dependencies=[Depends(_boundary_dependency)]), r, prefix="/api"
    ),
    # cache key: target router configuration
    "app_unique_id": lambda r: _include(
        FastAPI(generate_unique_id_function=_boundary_unique_id), r, prefix="/api"
    ),
    "app_response_class": lambda r: _include(
        FastAPI(default_response_class=PlainTextResponse), r, prefix="/api"
    ),
    # eligibility: the source router has no startup/shutdown handlers
    "source_on_startup": lambda r: _include(FastAPI(), r, prefix="/api"),
    # eligibility: the target is the app's own router, not another router
    # that merely names the app as its overrides provider
    "router_naming_an_app": lambda r: _include(
        APIRouter(dependency_overrides_provider=FastAPI(), strict_content_type=True),
        r,
        prefix="/api",
    ),
    "plain_router": lambda r: _include(APIRouter(), r, prefix="/api"),
}


def _target_view(target) -> tuple:
    router = target.router if isinstance(target, FastAPI) else target
    return (
        [_route_view(route) for route in router.routes],
        target.openapi() if isinstance(target, FastAPI) else None,
        list(router.on_startup),
        list(router.on_shutdown),
    )


@pytest.mark.parametrize("case", sorted(_BOUNDARY_CASES))
def test_calls_outside_the_hit_conditions_build_what_the_real_call_builds(
    shim, monkeypatch, case
):
    source = _main().debug_logs_router
    app_routes.clear()
    _include(FastAPI(), source, prefix="/api")
    before = app_routes.stats()
    _include(FastAPI(), source, prefix="/api")
    assert _delta(before, app_routes.stats())["hits"] == 1  # the template serves

    if case == "source_on_startup":
        monkeypatch.setattr(source, "on_startup", [_boundary_startup])
    before = app_routes.stats()
    built = _BOUNDARY_CASES[case](source)
    delta = _delta(before, app_routes.stats())
    with app_routes.disabled():
        real = _BOUNDARY_CASES[case](source)

    assert delta["hits"] == 0, delta
    assert delta["misses"] + delta["bypassed"] == 1, delta
    assert _target_view(built) == _target_view(real)


# --- Annotation names: the one input a template cannot see ------------------
#
# ``include_router`` re-resolves string annotations (19 ``app.api`` modules use
# ``from __future__ import annotations``) against the endpoint module's
# globals on every real build; a template keeps the first resolution. A test
# that patched such a name on an ``app.api`` module before ``create_app()``
# would see the patch on the real path only, so tests may not do it.

_CANDIDATE = re.compile(
    r"setattr\(\s*(?:[\w.]+\s*,\s*[\"'][A-Z]|[\"']app\.api\.)"
)
_API_STRING_TARGET = re.compile(r"^(app\.api(?:\.\w+)+)\.([A-Z]\w*)$")
_ENDPOINT_DECORATOR = re.compile(
    r"@\w+\.(?:get|post|put|patch|delete|head|options|api_route)\("
)
_APP_API = Path(__file__).resolve().parents[1] / "app" / "api"


def endpoint_modules() -> frozenset[str]:
    """``app.api`` modules that define route endpoints: the modules whose
    globals the endpoints' string annotations resolve against."""
    found = set()
    for path in _APP_API.rglob("*.py"):
        if _ENDPOINT_DECORATOR.search(path.read_text(encoding="utf-8")):
            parts = path.relative_to(_APP_API.parents[1]).with_suffix("").parts
            found.add(".".join(parts))
    return frozenset(found)


def _is_type_like(name: str) -> bool:
    return name[:1].isupper() and name != name.upper()


def _dotted(node) -> str | None:
    parts = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if not isinstance(node, ast.Name):
        return None
    parts.append(node.id)
    return ".".join(reversed(parts))


def annotation_patch_sites(
    source: str, modules: frozenset[str]
) -> list[tuple[int, str]]:
    """``(line, patched name)`` for every setattr of a type-like name on one of
    ``modules``, by module alias, dotted path or string target."""
    if "app.api" not in source or not _CANDIDATE.search(source):
        return []
    tree = ast.parse(source)
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("app.api.") and alias.asname:
                    aliases[alias.asname] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module:
            for alias in node.names:
                full = f"{node.module}.{alias.name}"
                if node.module == "app.api" or node.module.startswith("app.api."):
                    aliases[alias.asname or alias.name] = full
    sites = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        if not (
            (isinstance(func, ast.Attribute) and func.attr == "setattr")
            or (isinstance(func, ast.Name) and func.id == "setattr")
        ):
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            match = _API_STRING_TARGET.match(first.value)
            if match and match.group(1) in modules and _is_type_like(match.group(2)):
                sites.append((node.lineno, match.group(2)))
            continue
        if len(node.args) < 2:
            continue
        name = node.args[1]
        if not (isinstance(name, ast.Constant) and isinstance(name.value, str)):
            continue
        dotted = _dotted(first)
        if dotted is None:
            continue
        head, _, rest = dotted.partition(".")
        module = aliases.get(head, head) + (f".{rest}" if rest else "")
        if module in modules and _is_type_like(name.value):
            sites.append((node.lineno, name.value))
    return sites


_GUARD_FIXTURE_MODULES = frozenset(
    {
        "app.api.report_routes",
        "app.api.system_routes",
        "app.api.ask_routes",
        "app.api.kg_routes",
        "app.api.notebook_routes",
    }
)


def test_annotation_patch_guard_flags_every_spelling():
    flagged = annotation_patch_sites(
        "from app.api import report_routes\n"
        "from app.api import system_routes as sr\n"
        "import app.api.ask_routes as ar\n"
        "import app.api.kg_routes\n"
        "def t(monkeypatch):\n"
        "    monkeypatch.setattr(report_routes, 'ReportRequest', object)\n"
        "    monkeypatch.setattr(sr, \"UserProfile\", object)\n"
        "    setattr(ar, 'AskRequest', object)\n"
        "    monkeypatch.setattr(app.api.kg_routes, 'KgQuery', object)\n"
        "    monkeypatch.setattr('app.api.notebook_routes.NotebookCreate', object)\n",
        _GUARD_FIXTURE_MODULES,
    )
    assert [name for _, name in flagged] == [
        "ReportRequest", "UserProfile", "AskRequest", "KgQuery", "NotebookCreate"
    ]
    assert annotation_patch_sites(
        "from app.api import report_routes\n"
        "from app.services import thing\n"
        "def t(monkeypatch):\n"
        "    monkeypatch.setattr(report_routes, '_launch_plan_job', None)\n"
        "    monkeypatch.setattr(report_routes, 'MAX_TURNS', 3)\n"
        "    monkeypatch.setattr(thing, 'Model', object)\n"
        "    monkeypatch.setattr('app.services.thing.Model', object)\n"
        "from app.api import mcp_server\n"
        "monkeypatch.setattr(mcp_server, 'TransportSecuritySettings', object)\n"
        "monkeypatch.setattr('app.api.mcp_server.TransportSecuritySettings', 1)\n",
        _GUARD_FIXTURE_MODULES,
    ) == []


def test_endpoint_module_discovery_sees_the_core_route_modules():
    modules = endpoint_modules()
    assert {"app.api.system_routes", "app.api.auth_routes", "app.api.debug_logs"} <= modules
    assert "app.api.mcp_server" not in modules  # builds the MCP app, no endpoints


def test_no_test_patches_an_annotation_name_on_an_endpoint_module():
    modules = endpoint_modules()
    offenders = []
    for path in sorted(_TESTS_ROOT.rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        for line, name in annotation_patch_sites(source, modules):
            offenders.append(f"{path.relative_to(_TESTS_ROOT)}:{line} {name}")
    assert offenders == [], (
        "patching a type-like name on an app.api module diverges between a "
        "templated and a real create_app() (see tests/speedups/app_routes.py); "
        "patch the behaviour behind it instead: " + ", ".join(offenders)
    )
