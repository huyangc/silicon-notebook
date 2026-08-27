"""Deployment plugin HTTP routes: the mount seam, its gates, and its refusals.

Every fake plugin here is a real ``.py`` file imported off a real ``sys.path``
entry and loaded by real discovery — the shared helpers come from
``test_extension_discovery`` rather than being copied — and every mounted route
is exercised through a real ``create_app()`` + ``TestClient``. Monkeypatching
the mount would skip precisely the machinery under test: FastAPI's dependency
tree, the router-level session guard, and core's notebook guards.

See docs/superpowers/plans/2026-08-23-deployment-extensions-backend.md §3.3 and
主 agent 裁决 2 (``PluginRouteContext`` is nine fields, and the gate set that
``{notebook_id}`` routes are checked against includes core's *read* gate).
"""
from __future__ import annotations

import asyncio
import dataclasses
import json
import logging
import sys
import textwrap
import threading

import pytest
from fastapi import APIRouter, Depends, Request
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient

from app.api.extension_routes import (
    PluginRouteMountError,
    _PluginTaskStreamAdapter,
    _event_emitter,
    _run_plugin_router_validation,
    mount_extension_routers,
)
from app.domain.extension_http import (
    PLUGIN_ROUTE_PREFIX,
    PluginActor,
    PluginRouteContext,
)
from app.extension_sdk import (
    EXTENSION_API_VERSION,
    ContributionDeclaration,
    ContributionKind,
    ExtensionContribution,
    ExtensionManifest,
)
from app.extension_sdk.http import PLUGIN_HTTP_ROUTER_POINT
from app.extensions.discovery import ExtensionDiscoveryError
from app.extensions.http_router import collect_plugin_router_specs
from tests.test_extension_discovery import (
    _entry,
    _module_name,
    _plugin_import_isolation,  # noqa: F401 -- autouse pytest fixture, resolved by name
    _write_config,
    _write_plugin_package,
    frozen_runtime_reset,  # noqa: F401 -- pytest fixture, resolved by name
)


_PLUGIN_ID = "corp.sample"
_MOUNT = f"{PLUGIN_ROUTE_PREFIX}/{_PLUGIN_ID}"


# --------------------------------------------------------------------------
# Fake plugin packages
# --------------------------------------------------------------------------

_BODY_PREFIX = """
from dataclasses import dataclass

from fastapi import APIRouter, Depends

from app.extension_sdk import (
    EXTENSION_API_VERSION,
    ContributionDeclaration,
    ContributionKind,
    ExtensionContribution,
    ExtensionManifest,
)
from app.extension_sdk.http import PLUGIN_HTTP_ROUTER_POINT

SEEN: list = []

_DECLARATION = ContributionDeclaration(
    id=DECLARATION_ID,
    point=PLUGIN_HTTP_ROUTER_POINT,
    kind=ContributionKind.CONTRIBUTOR,
)

"""

_BODY_SUFFIX = """

@dataclass
class Bundle:
    manifest: ExtensionManifest

    def register(self, registrar) -> None:
        registrar.add_contributor(ExtensionContribution(
            declaration=_DECLARATION,
            implementation=build_router,
        ))


bundle = Bundle(ExtensionManifest(
    id=PLUGIN_ID,
    version="0.1.0",
    api_version=EXTENSION_API_VERSION,
    display_name="Sample deployment plugin",
    trust="deployment",
    contributions=(_DECLARATION,),
))
"""


def _plugin_body(
    factory_src: str, *, declaration_id: str = "corp.sample.router"
) -> str:
    """Wrap one ``build_router`` implementation in the standard bundle scaffold.

    ``declaration_id`` is injected as a module-level name (like ``PLUGIN_ID``)
    rather than formatted into the template: the factory bodies below are full
    of dict literals, so any brace-based substitution would have to escape them.
    Contribution ids are unique registry-wide, so a second plugin in the same
    config needs its own.
    """

    return (
        f"DECLARATION_ID = {declaration_id!r}\n"
        + _BODY_PREFIX
        + textwrap.dedent(factory_src).strip("\n")
        + "\n"
        + _BODY_SUFFIX
    )


_FULL_ROUTER_SRC = """
def build_router(context):
    SEEN.append(context)
    router = APIRouter()

    @router.get("/ping")
    def ping(actor=Depends(context.current_actor)):
        return {
            "actor_id": actor.id,
            "is_admin": actor.is_admin,
            "plugin_id": context.plugin_id,
        }

    @router.post(
        "/notebooks/{notebook_id}/import",
        dependencies=[Depends(context.require_notebook_capability("sources:write"))],
    )
    def import_urls(notebook_id: str, payload: dict):
        result = context.url_sources.import_urls(notebook_id, payload["urls"])
        context.emit_event({"event": "urls_imported", "count": len(result.created)})
        # Deliberately malformed: an out-of-whitelist key must be dropped by
        # core, never raised back into this handler.  If it were raised the
        # request would 500 and every import assertion below would notice.
        context.emit_event({"event": "urls_imported", "notebook_id": notebook_id})
        return {
            "created": [
                {"source_id": row.source_id, "title": row.title, "url": row.url}
                for row in result.created
            ],
            "rejected": [
                {"url": row.url, "reason": row.reason} for row in result.rejected
            ],
        }

    @router.get(
        "/notebooks/{notebook_id}/peek",
        dependencies=[Depends(context.require_notebook_read)],
    )
    def peek(notebook_id: str):
        return {"notebook_id": notebook_id}

    @router.get("/boom")
    def boom():
        raise context.user_error(409, "这项操作暂时无法完成")

    return router
"""

_LIFECYCLE_SRC = """
def build_router(context):
    router = APIRouter(on_startup=[lambda: None])

    @router.get("/ping")
    def ping():
        return {"ok": True}

    return router
"""

_NON_APIROUTE_SRC = """
def build_router(context):
    router = APIRouter()

    async def raw(request):
        from starlette.responses import JSONResponse

        return JSONResponse({"ok": True})

    router.add_route("/raw", raw)
    return router
"""

_NOT_A_ROUTER_SRC = """
def build_router(context):
    return {"not": "a router"}
"""

_UNGATED_SRC = """
def build_router(context):
    router = APIRouter()

    @router.get("/notebooks/{notebook_id}/thing")
    def thing(notebook_id: str):
        return {"notebook_id": notebook_id}

    return router
"""

_MINIMAL_GATED_SRC = """
def build_router(context):
    router = APIRouter()

    @router.get(
        "/notebooks/{notebook_id}/peek",
        dependencies=[Depends(context.require_notebook_read)],
    )
    def peek(notebook_id: str):
        return {"notebook_id": notebook_id}

    return router
"""

_TASK_STREAM_SRC = """
from fastapi import Request


def build_router(context):
    router = APIRouter()

    @router.post("/search-task")
    async def search_task(request: Request):
        def work(cancel_event):
            return {"ok": True, "cancelled": cancel_event.is_set()}

        return context.task_stream.response(
            request, work, stage="candidate_search"
        )

    @router.post("/failed-task")
    async def failed_task(request: Request):
        def work(cancel_event):
            raise RuntimeError("secret-upstream-message")

        return context.task_stream.response(
            request, work, stage="candidate_search"
        )

    return router
"""

# Three route shapes that all clear ``_validate_plugin_router`` while proving
# nothing about write access, then reach for the URL import port anyway. Each
# one is a real way a plugin could be written — none is a contrived escape —
# which is exactly why authorization has to live in the port.
_PORT_AUTHZ_SRC = """
def build_router(context):
    router = APIRouter()

    def _import(notebook_id, urls):
        result = context.url_sources.import_urls(notebook_id, urls)
        return {"created": [row.source_id for row in result.created]}

    # Shape 1: the path template check passes, but the gate it mounts is the
    # *read* gate — every reader of the notebook clears it.
    @router.post(
        "/notebooks/{notebook_id}/read-gated",
        dependencies=[Depends(context.require_notebook_read)],
    )
    def read_gated(notebook_id: str, payload: dict):
        return _import(notebook_id, payload["urls"])

    # Shape 2: same notebook-scoped route, path parameter named something else,
    # so the ``{notebook_id}`` rule never fires and no gate is required at all.
    @router.post("/n/{nb}/aliased")
    def aliased(nb: str, payload: dict):
        return _import(nb, payload["urls"])

    # Shape 3: the notebook id is not in the path in any form.
    @router.post("/from-body")
    def from_body(payload: dict):
        return _import(payload["notebook_id"], payload["urls"])

    return router
"""

# The URL import port from an ``async def`` handler, which runs on the event
# loop thread. Four routes, one per shape the offload contract has to answer:
# the offloaded call, the refused blocking call, the same blocking call from a
# sync handler (still fine), and an ungated async route so the port — not the
# route's gate — is what turns a reader away.
_ASYNC_PORT_SRC = """
import threading

HANDLER_THREADS = []


def build_router(context):
    router = APIRouter()

    @router.post(
        "/notebooks/{notebook_id}/import-async",
        dependencies=[Depends(context.require_notebook_capability("sources:write"))],
    )
    async def import_async(notebook_id: str, payload: dict):
        HANDLER_THREADS.append(threading.current_thread())
        result = await context.url_sources.import_urls_async(
            notebook_id, payload["urls"]
        )
        return {"created": [row.source_id for row in result.created]}

    @router.post(
        "/notebooks/{notebook_id}/import-sync-from-async",
        dependencies=[Depends(context.require_notebook_capability("sources:write"))],
    )
    async def import_sync_from_async(notebook_id: str, payload: dict):
        result = context.url_sources.import_urls(notebook_id, payload["urls"])
        return {"created": [row.source_id for row in result.created]}

    @router.post(
        "/notebooks/{notebook_id}/import-sync",
        dependencies=[Depends(context.require_notebook_capability("sources:write"))],
    )
    def import_sync(notebook_id: str, payload: dict):
        HANDLER_THREADS.append(threading.current_thread())
        result = context.url_sources.import_urls(notebook_id, payload["urls"])
        return {"created": [row.source_id for row in result.created]}

    @router.post("/n/{nb}/import-async-ungated")
    async def import_async_ungated(nb: str, payload: dict):
        result = await context.url_sources.import_urls_async(nb, payload["urls"])
        return {"created": [row.source_id for row in result.created]}

    return router
"""

# A core gate wrapped in the plugin's own dependency — the ordinary way a
# plugin adds its own precondition on top of core's. ``_dependant_calls`` must
# find the core gate transitively or this router cannot be mounted at all.
_WRAPPED_GATE_SRC = """
def build_router(context):
    _core_read_gate = context.require_notebook_read

    def my_gate(notebook_id: str, gated: str = Depends(_core_read_gate)):
        return gated

    router = APIRouter()

    @router.get(
        "/notebooks/{notebook_id}/wrapped", dependencies=[Depends(my_gate)]
    )
    def wrapped(notebook_id: str):
        return {"notebook_id": notebook_id}

    return router
"""

# Both handler flavours raise a bare 401, the way a plugin re-raising an
# upstream service's rejection would. Two more routes *return* a 401 instead
# of raising one — a plugin proxying an upstream service's status onto its own
# Response is a normal return, not a raise, and codex #578 R3 P2 is exactly
# that gap: the raised routes above went through ``except HTTPException``
# untouched by a returned 401, which never raises anything at all.
#
# ``starlette-401``/``starlette-403`` raise ``starlette.exceptions.HTTPException``
# directly rather than ``fastapi.HTTPException`` — the way a plugin handler (or
# a library it depends on, e.g. an upstream HTTP client wrapper) might, since
# FastAPI's is a subclass and nothing obliges a plugin author to know that.
# codex #578 R6 P1: before this fix ``_wrap_handler_unauthorized`` caught only
# the FastAPI subclass, so a Starlette-raised 401 fell straight through
# unwrapped and reached the browser as a real 401.
_UPSTREAM_401_SRC = """
from fastapi import HTTPException
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.responses import JSONResponse, Response


def build_router(context):
    router = APIRouter()

    @router.get("/sync-401")
    def sync_401():
        raise HTTPException(status_code=401, detail="upstream says no")

    @router.get("/async-401")
    async def async_401():
        raise HTTPException(status_code=401, detail="upstream says no")

    @router.get("/sync-403")
    def sync_403():
        raise HTTPException(status_code=403, detail="upstream says forbidden")

    @router.get("/starlette-401")
    def starlette_401():
        raise StarletteHTTPException(status_code=401, detail="upstream says no")

    @router.get("/starlette-403")
    def starlette_403():
        raise StarletteHTTPException(
            status_code=403, detail="upstream says forbidden"
        )

    @router.get("/returned-401-json")
    def returned_401_json():
        return JSONResponse({"detail": "upstream"}, status_code=401)

    @router.get("/returned-401-bare")
    def returned_401_bare():
        return Response(status_code=401)

    @router.get("/returned-403-json")
    def returned_403_json():
        return JSONResponse({"detail": "upstream forbidden"}, status_code=403)

    return router
"""

# The factory raises the mount seam's own exception directly — the way a
# factory might if it wants to refuse mounting on its own terms (an unmet
# runtime precondition, say). ``_call_plugin_router_factory`` must let this
# through unchanged rather than re-wrapping it: double-wrapping would replace
# the factory's own reason code with a generic one and lose it.
_FACTORY_RAISES_MOUNT_ERROR_SRC = """
from app.api.extension_routes import PluginRouteMountError


def build_router(context):
    raise PluginRouteMountError(PLUGIN_ID, "plugin_custom_denied")
"""

# ``SystemExit`` (like ``KeyboardInterrupt``) must reach the interpreter
# untouched — an operator's Ctrl-C or a ``sys.exit()`` reached from inside
# factory code has to still stop the process, not turn into an HTTP-shaped
# ``PluginRouteMountError``.
_FACTORY_RAISES_SYSTEM_EXIT_SRC = """
def build_router(context):
    raise SystemExit(1)
"""

# The same upstream 401, raised from the plugin's own ``Depends(...)`` instead
# of from its handler — codex #578 R7 P1. Checking an upstream inside a
# dependency is at least as ordinary as checking it inside the handler, and
# FastAPI solves dependencies *before* ``run_endpoint_function``, so wrapping
# only ``route.dependant.call`` left this shape leaking a real 401 to the
# browser (which clears the token and reloads).
#
# ``core-gated`` is the negative control in the same router: core's own read
# gate must keep answering 401 (no session) and 404 (not a member) untouched.
_DEPENDENCY_401_SRC = """
from fastapi import HTTPException
from starlette.exceptions import HTTPException as StarletteHTTPException


def build_router(context):
    def upstream_gate():
        raise HTTPException(status_code=401, detail="upstream says no")

    async def async_upstream_gate():
        raise HTTPException(status_code=401, detail="upstream says no")

    def starlette_upstream_gate():
        raise StarletteHTTPException(status_code=401, detail="upstream says no")

    def forbidden_gate():
        raise HTTPException(status_code=403, detail="upstream says forbidden")

    def innermost_gate():
        raise HTTPException(status_code=401, detail="upstream says no")

    def middle_gate(inner=Depends(innermost_gate)):
        return inner

    def outer_gate(middle=Depends(middle_gate)):
        return middle

    router = APIRouter()

    @router.get("/dep-401", dependencies=[Depends(upstream_gate)])
    def dep_401():
        return {"ok": True}

    @router.get("/dep-async-401", dependencies=[Depends(async_upstream_gate)])
    def dep_async_401():
        return {"ok": True}

    @router.get("/dep-starlette-401", dependencies=[Depends(starlette_upstream_gate)])
    def dep_starlette_401():
        return {"ok": True}

    @router.get("/dep-403", dependencies=[Depends(forbidden_gate)])
    def dep_403():
        return {"ok": True}

    @router.get("/dep-nested-401")
    def dep_nested_401(value=Depends(outer_gate)):
        return {"ok": True}

    @router.get(
        "/notebooks/{notebook_id}/core-gated",
        dependencies=[Depends(context.require_notebook_read)],
    )
    def core_gated(notebook_id: str):
        return {"notebook_id": notebook_id}

    return router
"""

# A settings-carrying plugin whose factory leaks its own secret into an
# exception message — exactly the shape ``_call_plugin_router_factory`` exists
# to sanitize. Deliberately *not* built through ``_plugin_body``: that helper's
# fixed ``Bundle`` never binds ``settings_model``/``configure``, so this is
# its own full bundle, mirroring
# ``test_extension_discovery._REGISTER_RAISES_BODY``.
_FACTORY_RAISES_WITH_SETTINGS_BODY = """
from dataclasses import dataclass

from pydantic import BaseModel

from app.extension_sdk import (
    ContributionDeclaration,
    ContributionKind,
    EXTENSION_API_VERSION,
    ExtensionContribution,
    ExtensionManifest,
)
from app.extension_sdk.http import PLUGIN_HTTP_ROUTER_POINT


class SampleSettings(BaseModel):
    token: str = ""


_DECLARATION = ContributionDeclaration(
    id="corp.sample.router",
    point=PLUGIN_HTTP_ROUTER_POINT,
    kind=ContributionKind.CONTRIBUTOR,
)


def build_router(context):
    raise RuntimeError(f"upstream refused {context.settings.token}")


@dataclass
class Bundle:
    manifest: ExtensionManifest
    settings_model: object = SampleSettings
    settings: object = None

    def configure(self, settings) -> None:
        self.settings = settings

    def register(self, registrar) -> None:
        registrar.add_contributor(ExtensionContribution(
            declaration=_DECLARATION,
            implementation=build_router,
        ))


bundle = Bundle(ExtensionManifest(
    id=PLUGIN_ID,
    version="0.1.0",
    api_version=EXTENSION_API_VERSION,
    display_name="Sample deployment plugin",
    trust="deployment",
    contributions=(_DECLARATION,),
))
"""


# --------------------------------------------------------------------------
# Application helpers
# --------------------------------------------------------------------------


def _clear_caches() -> None:
    from app.api import deps
    from app.core.config import get_settings
    from app.extensions.bootstrap import default_extension_runtime

    get_settings.cache_clear()
    default_extension_runtime.cache_clear()
    deps.repository.cache_clear()


def _set_test_env(tmp_path, monkeypatch, *, config_path: str, env=None) -> None:
    """The environment every test app in this module boots against.

    Split out of ``_configure`` so ``_configure_full_body`` (a plugin body
    written whole, not through the ``_plugin_body`` scaffold) can share it
    without also inheriting ``_plugin_body``'s fixed, settings-less ``Bundle``.
    """

    monkeypatch.setenv("EXTENSIONS_CONFIG", config_path)
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.delenv("MINERU_API_TOKEN", raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    _clear_caches()


def _configure(tmp_path, monkeypatch, *, factory_src: str, env=None, extra=()) -> None:
    """Write the plugin(s), point EXTENSIONS_CONFIG at them, set a fresh env.

    ``extra`` holds additional ``(plugin_id, factory_src)`` pairs written as
    their own modules and config entries. Mount order is by plugin id (see
    ``collect_plugin_router_specs``), so a caller that needs a specific plugin
    to be validated second picks an id that sorts after ``corp.sample``.
    """

    module = _write_plugin_package(tmp_path, body=_plugin_body(factory_src))
    entries = [_entry(module)]
    for plugin_id, extra_src in extra:
        extra_module = _write_plugin_package(
            tmp_path,
            plugin_id=plugin_id,
            body=_plugin_body(extra_src, declaration_id=f"{plugin_id}.router"),
        )
        entries.append(_entry(extra_module, plugin_id=plugin_id))
    _set_test_env(
        tmp_path,
        monkeypatch,
        config_path=_write_config(tmp_path, "".join(entries)),
        env=env,
    )


def _configure_full_body(
    tmp_path, monkeypatch, *, body: str, config_extra: str = ""
) -> None:
    """Like ``_configure``, but ``body`` is a complete plugin module verbatim.

    For plugins that need something the ``_plugin_body`` scaffold's fixed,
    settings-less ``Bundle`` cannot express — here, a ``settings_model`` bound
    through ``configure()`` (see ``test_extension_discovery._REGISTER_RAISES_BODY``
    for the same pattern one layer down, at registration rather than mount).
    """

    module = _write_plugin_package(tmp_path, body=body)
    config_path = _write_config(tmp_path, _entry(module, extra=config_extra))
    _set_test_env(tmp_path, monkeypatch, config_path=config_path)


def _create_app():
    from app.main import create_app

    return create_app()


def _client(
    tmp_path, monkeypatch, *, factory_src: str = _FULL_ROUTER_SRC, env=None, extra=()
):
    _configure(tmp_path, monkeypatch, factory_src=factory_src, env=env, extra=extra)
    return TestClient(_create_app())


def _auth(client: TestClient, username: str) -> dict[str, str]:
    client.post("/api/auth/register", json={"username": username, "password": "pw"})
    response = client.post(
        "/api/auth/login", json={"username": username, "password": "pw"}
    )
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _auth_admin(client: TestClient) -> dict[str, str]:
    response = client.post(
        "/api/auth/login", json={"username": "admin", "password": "admin"}
    )
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _notebook(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post("/api/notebooks", json={"name": "n"}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _seen_context(module_prefix: str = _PLUGIN_ID) -> PluginRouteContext:
    return sys.modules[_module_name(module_prefix)].SEEN[0]


_REACHABLE_MAX_DEPTH = 4


def _reachable_from(root, *, depth: int = _REACHABLE_MAX_DEPTH) -> list:
    """Objects reachable from ``root`` through instance state and closures.

    Deliberately narrow, in both directions:

    * ``__dict__`` and ``__closure__`` are followed, plus plain containers,
      because those are the ways a seam can *hold* a core object — checking only
      the eight top-level fields would miss an adapter with a repository
      attribute or a closure that captured one.
    * ``__globals__`` is **not** followed. Every function object reaches its
      defining module's globals, so walking them would report the repository via
      any core function the context legitimately exposes, and the assertion
      would be about nothing.

    Bounded by ``depth`` and by identity, so a self-referential graph (the
    logging module's registry is one) terminates.
    """

    seen: set[int] = set()
    found: list = []
    frontier = [(root, 0)]
    while frontier:
        obj, level = frontier.pop()
        if level > depth or id(obj) in seen:
            continue
        seen.add(id(obj))
        found.append(obj)
        children: list = []
        for cell in getattr(obj, "__closure__", None) or ():
            try:
                children.append(cell.cell_contents)
            except ValueError:  # pragma: no cover - empty cell
                continue
        state = getattr(obj, "__dict__", None)
        if state is not None:
            try:
                children.extend(dict(state).values())
            except Exception:  # pragma: no cover - exotic mapping
                pass
        # ``PluginRouteContext`` is ``slots=True``, so it has no ``__dict__`` at
        # all — reading only that would make this walk stop at the root and
        # report nothing, which is exactly the vacuous shape being replaced.
        for cls in type(obj).__mro__:
            slots = getattr(cls, "__slots__", ())
            if isinstance(slots, str):
                slots = (slots,)
            for name in slots:
                try:
                    children.append(getattr(obj, name))
                except AttributeError:  # pragma: no cover - unset slot
                    continue
        if isinstance(obj, (list, tuple, set, frozenset)):
            children.extend(obj)
        elif isinstance(obj, dict):
            children.extend(obj.values())
        frontier.extend((child, level + 1) for child in children)
    return found


# --------------------------------------------------------------------------
# Collector-level bundles (no file needed: these never reach a router factory)
# --------------------------------------------------------------------------


def _collector_bundle(
    *,
    plugin_id: str = _PLUGIN_ID,
    trust: str = "deployment",
    contribution_ids: tuple[str, ...] = ("corp.sample.router",),
    kind: ContributionKind = ContributionKind.CONTRIBUTOR,
    implementation=None,
):
    declarations = tuple(
        ContributionDeclaration(
            id=contribution_id, point=PLUGIN_HTTP_ROUTER_POINT, kind=kind
        )
        for contribution_id in contribution_ids
    )
    manifest = ExtensionManifest(
        id=plugin_id,
        version="0.1.0",
        api_version=EXTENSION_API_VERSION,
        display_name="Collector bundle",
        trust=trust,
        contributions=declarations,
    )
    factory = implementation if implementation is not None else (
        lambda context: APIRouter()
    )

    class Bundle:
        def __init__(self) -> None:
            self.manifest = manifest

        def register(self, registrar) -> None:
            for declaration in declarations:
                contribution = ExtensionContribution(
                    declaration=declaration, implementation=factory
                )
                # ``add`` rather than ``add_contributor``: the typed helper
                # rejects a kind mismatch itself, and the collector's own
                # kind check has to be reachable to be tested.
                registrar.add(contribution)

    return Bundle()


def _collect(bundle):
    from app.extensions.bootstrap import build_extension_registry

    return collect_plugin_router_specs(build_extension_registry([bundle]), {})


# --------------------------------------------------------------------------
# The shipped default: no plugins, no routes, frozen contract
# --------------------------------------------------------------------------


def test_no_plugins_registers_zero_routes_and_keeps_openapi_frozen(
    frozen_runtime_reset,
):
    """With no EXTENSIONS_CONFIG the mount seam is invisible in every sense.

    Both halves matter: no path under ``/api/extensions`` exists, *and* the
    OpenAPI document still equals the committed ``api_contract`` fixture — the
    same one ``test_repository_api_contract`` freezes. That equality is what
    lets this feature ship without regenerating the contract.
    """

    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[2]
    contract = json.loads(
        (
            root
            / "backend"
            / "tests"
            / "fixtures"
            / "repository_contract"
            / "api_contract.json"
        ).read_text(encoding="utf-8")
    )

    app = _create_app()
    assert [
        route.path
        for route in app.routes
        if getattr(route, "path", "").startswith(PLUGIN_ROUTE_PREFIX)
    ] == []
    assert app.openapi() == contract["openapi"]


# --------------------------------------------------------------------------
# Mounting and authentication
# --------------------------------------------------------------------------


def test_plugin_router_is_mounted_under_its_plugin_id(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    client = _client(tmp_path, monkeypatch)
    headers = _auth(client, "z00110011")

    response = client.get(f"{_MOUNT}/ping", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json()["plugin_id"] == _PLUGIN_ID

    paths = {
        route.path
        for route in client.app.routes
        if getattr(route, "path", "").startswith(PLUGIN_ROUTE_PREFIX)
    }
    assert paths == {
        f"{_MOUNT}/ping",
        f"{_MOUNT}/notebooks/{{notebook_id}}/import",
        f"{_MOUNT}/notebooks/{{notebook_id}}/peek",
        f"{_MOUNT}/boom",
    }
    # A second plugin id could not collide: every path is under its own prefix.
    assert all(path.startswith(f"{_MOUNT}/") for path in paths)


def test_plugin_routes_have_no_anonymous_surface(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    """Router-level ``Depends(get_current_user)`` covers every plugin route.

    Note the fake plugin's ``/ping`` also declares ``current_actor`` itself —
    so this must be asserted on a route that does *not*: ``/boom`` declares no
    dependency at all, and it is the one that proves the router-level guard is
    doing the work.
    """

    client = _client(tmp_path, monkeypatch)
    assert client.get(f"{_MOUNT}/boom").status_code == 401
    assert client.get(f"{_MOUNT}/ping").status_code == 401

    headers = _auth(client, "z00220022")
    assert client.get(f"{_MOUNT}/ping", headers=headers).status_code == 200


def test_plugin_routes_are_503_before_readiness(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    from app.core import readiness

    client = _client(tmp_path, monkeypatch)
    headers = _auth(client, "z00330033")
    assert client.get(f"{_MOUNT}/ping", headers=headers).status_code == 200

    readiness.reset()
    try:
        response = client.get(f"{_MOUNT}/ping", headers=headers)
        assert response.status_code == 503
        assert response.json()["ready"] is False
    finally:
        readiness.mark_ready()


# --------------------------------------------------------------------------
# Notebook gates (裁决 2: the accepted set includes core's read gate)
# --------------------------------------------------------------------------


def test_plugin_route_notebook_gate_is_the_core_guard(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    """The write-gated plugin route answers exactly as core's own would.

    Non-owner gets 404 (core never leaks existence), owner gets through to the
    handler. The plugin declared the gate; it did not implement one.
    """

    from app.services import remote_sources
    from app.services.remote_sources import PdfProbe

    client = _client(tmp_path, monkeypatch, env={"MINERU_API_TOKEN": "tok"})
    monkeypatch.setattr(
        remote_sources, "probe_pdf", lambda url, **kw: PdfProbe(True, "", 1, "d.pdf")
    )
    monkeypatch.setattr(
        __import__("app.api.source_routes", fromlist=["kg_scheduler"]).kg_scheduler,
        "submit_job",
        lambda fn, *a, **k: None,
    )

    owner = _auth(client, "z00440044")
    stranger = _auth(client, "z00550055")
    notebook_id = _notebook(client, owner)

    body = {"urls": ["https://a/d.pdf"]}
    assert (
        client.post(
            f"{_MOUNT}/notebooks/{notebook_id}/import", json=body, headers=stranger
        ).status_code
        == 404
    )
    response = client.post(
        f"{_MOUNT}/notebooks/{notebook_id}/import", json=body, headers=owner
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["created"]) == 1


def test_plugin_route_with_only_the_read_gate_mounts(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    """裁决 2: a notebook-scoped route may carry only core's read gate.

    Non-vacuous by construction — if ``require_notebook_read`` were missing
    from ``_validate_plugin_router``'s gate set, ``/peek`` would be refused and
    ``create_app()`` inside ``_client`` would raise before any assertion below.
    """

    client = _client(tmp_path, monkeypatch)
    owner = _auth(client, "z00660066")
    stranger = _auth(client, "z00770077")
    notebook_id = _notebook(client, owner)

    response = client.get(f"{_MOUNT}/notebooks/{notebook_id}/peek", headers=owner)
    assert response.status_code == 200, response.text
    assert response.json() == {"notebook_id": notebook_id}
    assert (
        client.get(
            f"{_MOUNT}/notebooks/{notebook_id}/peek", headers=stranger
        ).status_code
        == 404
    )


def test_notebook_gate_set_is_derived_from_the_capability_table():
    """The accepted gates are exactly core's three guards, derived not listed.

    ``extension_routes`` may not *name* the two bare level guards (a route-file
    guard forbids it), so it asks the capability factory for each registered
    capability instead. This test names them — a test file is not scanned — and
    pins the result, so a derivation that silently returned a smaller or wider
    set than core's own three would fail here rather than at some future
    plugin's mount.
    """

    from app.api import deps
    from app.api.extension_routes import _notebook_gates

    assert _notebook_gates() == {
        deps.require_notebook_write,
        deps.require_notebook_admin,
        deps.require_notebook_read,
    }


def test_router_missing_the_notebook_gate_fails_to_mount(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    _configure(tmp_path, monkeypatch, factory_src=_UNGATED_SRC)
    with pytest.raises(PluginRouteMountError) as excinfo:
        _create_app()
    assert excinfo.value.reason == "plugin_route_missing_notebook_gate"
    assert excinfo.value.plugin_id == _PLUGIN_ID
    assert str(excinfo.value) == f"{_PLUGIN_ID}: plugin_route_missing_notebook_gate"


def test_a_wrapped_core_gate_still_counts(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    """``Depends(my_gate)`` wrapping ``Depends(context.require_notebook_read)``.

    Adding a plugin's own precondition on top of a core gate is the ordinary
    shape, so the gate scan has to be transitive. Mounting at all is half the
    assertion (a one-level scan refuses this router and ``_create_app`` raises);
    the owner/stranger split is the other half — the wrapped gate must still be
    *doing* something, not merely be findable.
    """

    client = _client(tmp_path, monkeypatch, factory_src=_WRAPPED_GATE_SRC)
    owner = _auth(client, "z00131013")
    stranger = _auth(client, "z00141014")
    notebook_id = _notebook(client, owner)

    response = client.get(f"{_MOUNT}/notebooks/{notebook_id}/wrapped", headers=owner)
    assert response.status_code == 200, response.text
    assert response.json() == {"notebook_id": notebook_id}
    assert (
        client.get(
            f"{_MOUNT}/notebooks/{notebook_id}/wrapped", headers=stranger
        ).status_code
        == 404
    )


def test_every_plugin_router_is_validated_not_just_the_first(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    """Two plugins, and it is the *second* one whose router is ungated.

    Specs are mounted in plugin-id order, so ``corp.zzz`` is validated after
    ``corp.sample``. A validation loop that only ever looked at the first spec
    would start this application happily, with an ungated notebook-scoped route
    live on the second plugin's prefix.
    """

    _configure(
        tmp_path,
        monkeypatch,
        factory_src=_MINIMAL_GATED_SRC,
        extra=(("corp.zzz", _UNGATED_SRC),),
    )
    with pytest.raises(PluginRouteMountError) as excinfo:
        _create_app()
    assert excinfo.value.plugin_id == "corp.zzz"
    assert excinfo.value.reason == "plugin_route_missing_notebook_gate"


# --------------------------------------------------------------------------
# Port-level authorization: the gate a plugin declares is not the boundary
# --------------------------------------------------------------------------


def _grant_world(client: TestClient, letter: str) -> dict:
    """One owner's notebook plus a reader, a group admin, and a stranger.

    Both non-owner roles arrive through real grant edges rather than
    ``add_member``, because the two capability levels this exercises are
    defined on those edges: ``principal_type="group"`` + ``role="viewer"``
    gives read and nothing more; ``principal_type="group_admins"`` +
    ``role="admin"`` is what makes ``sources:write`` (an "admin"-level
    capability) resolve true for someone who is not the owner.

    ``letter`` prefixes this world's usernames so parallel cases cannot collide.
    """

    owner = _auth(client, f"{letter}00000001")
    notebook_id = _notebook(client, owner)
    reader = _auth(client, f"{letter}00000002")
    reader_id = client.get("/api/me", headers=reader).json()["id"]
    deputy = _auth(client, f"{letter}00000003")
    deputy_id = client.get("/api/me", headers=deputy).json()["id"]
    stranger = _auth(client, f"{letter}00000004")

    for user_id, group_name, member_role, principal_type, grant_role in (
        (reader_id, "读者组", "member", "group", "viewer"),
        (deputy_id, "管理组", "admin", "group_admins", "admin"),
    ):
        group_id = client.post(
            "/api/groups", json={"name": group_name}, headers=owner
        ).json()["id"]
        assert (
            client.put(
                f"/api/groups/{group_id}/members/{user_id}",
                json={"role": member_role},
                headers=owner,
            ).status_code
            == 200
        )
        granted = client.post(
            f"/api/notebooks/{notebook_id}/grants",
            json={
                "principal_type": principal_type,
                "principal_id": group_id,
                "role": grant_role,
            },
            headers=owner,
        )
        assert granted.status_code == 200, granted.text

    return {
        "notebook": notebook_id,
        "owner": owner,
        "reader": reader,
        "deputy": deputy,
        "stranger": stranger,
    }


@pytest.fixture
def port_authz(tmp_path, monkeypatch, frozen_runtime_reset):
    """One app whose plugin offers three ways to reach the URL import port."""

    from app.services import remote_sources
    from app.services.remote_sources import PdfProbe

    client = _client(
        tmp_path, monkeypatch, factory_src=_PORT_AUTHZ_SRC, env={"MINERU_API_TOKEN": "tok"}
    )
    monkeypatch.setattr(
        remote_sources, "probe_pdf", lambda url, **kw: PdfProbe(True, "", 1, "d.pdf")
    )
    monkeypatch.setattr(
        __import__("app.api.source_routes", fromlist=["kg_scheduler"]).kg_scheduler,
        "submit_job",
        lambda fn, *a, **k: None,
    )
    return client, _grant_world(client, "y")


def _source_count(client: TestClient, notebook_id: str, headers: dict) -> int:
    response = client.get(f"/api/notebooks/{notebook_id}/sources", headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["total_count"]


def test_reader_cannot_import_through_a_read_gated_plugin_route(port_authz):
    """The route's own gate lets the reader in; the port refuses them.

    404 rather than 403, matching every core notebook guard: a plugin route
    must not become the one surface that confirms a notebook id exists to
    someone who may not write to it.
    """

    client, world = port_authz
    before = _source_count(client, world["notebook"], world["owner"])
    response = client.post(
        f"{_MOUNT}/notebooks/{world['notebook']}/read-gated",
        json={"urls": ["https://a/d.pdf"]},
        headers=world["reader"],
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Notebook not found"
    assert _source_count(client, world["notebook"], world["owner"]) == before


def test_stranger_cannot_import_through_an_aliased_path_parameter(port_authz):
    """``/n/{nb}/aliased`` never trips the ``{notebook_id}`` shape rule."""

    client, world = port_authz
    before = _source_count(client, world["notebook"], world["owner"])
    response = client.post(
        f"{_MOUNT}/n/{world['notebook']}/aliased",
        json={"urls": ["https://a/d.pdf"]},
        headers=world["stranger"],
    )
    assert response.status_code == 404, response.text
    assert _source_count(client, world["notebook"], world["owner"]) == before


def test_stranger_cannot_import_when_the_notebook_id_arrives_in_the_body(port_authz):
    """No notebook id in the path at all — the shape rule cannot see this one."""

    client, world = port_authz
    before = _source_count(client, world["notebook"], world["owner"])
    response = client.post(
        f"{_MOUNT}/from-body",
        json={"notebook_id": world["notebook"], "urls": ["https://a/d.pdf"]},
        headers=world["stranger"],
    )
    assert response.status_code == 404, response.text
    assert _source_count(client, world["notebook"], world["owner"]) == before


def test_owner_imports_through_the_same_ungated_shapes(port_authz):
    """Non-vacuity for all three refusals above: the routes do work.

    Each shape is exercised by the owner and the source is checked to have
    actually landed — otherwise a port that refused *everyone* would satisfy
    the three tests above while breaking the feature.
    """

    client, world = port_authz
    notebook_id = world["notebook"]
    calls = (
        (f"{_MOUNT}/notebooks/{notebook_id}/read-gated", {"urls": ["https://a/1.pdf"]}),
        (f"{_MOUNT}/n/{notebook_id}/aliased", {"urls": ["https://a/2.pdf"]}),
        (
            f"{_MOUNT}/from-body",
            {"notebook_id": notebook_id, "urls": ["https://a/3.pdf"]},
        ),
    )
    for index, (path, body) in enumerate(calls, start=1):
        response = client.post(path, json=body, headers=world["owner"])
        assert response.status_code == 200, response.text
        assert len(response.json()["created"]) == 1
        assert _source_count(client, notebook_id, world["owner"]) == index


def test_group_admin_imports_through_the_plugin_port(port_authz):
    """``sources:write`` is an "admin"-level capability, so the port honours it.

    Pinning this direction as well as the refusals is what keeps the port from
    silently hardening into owner-only: the deputy owns nothing here, and reads
    the notebook only through a ``group_admins``/``admin`` edge.
    """

    client, world = port_authz
    response = client.post(
        f"{_MOUNT}/notebooks/{world['notebook']}/read-gated",
        json={"urls": ["https://a/d.pdf"]},
        headers=world["deputy"],
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["created"]) == 1
    assert _source_count(client, world["notebook"], world["owner"]) == 1


def test_the_import_port_reads_the_user_from_core_not_from_its_caller():
    """Outside a request there is no user, and the port refuses rather than
    falling back.

    ``get_request_user()`` returns ``None`` when unset — the seeded-admin
    fallback lives further down, in the repository's ``current_user``, and must
    never be what answers an authorization question. A plugin calling the port
    from a thread it spawned itself lands here.
    """

    from app.api.extension_routes import _UrlSourceImportAdapter
    from fastapi import HTTPException

    with pytest.raises(HTTPException) as excinfo:
        _UrlSourceImportAdapter().import_urls("nb-does-not-matter", ["https://a/d.pdf"])
    assert excinfo.value.status_code == 404
    assert excinfo.value.detail == "Notebook not found"


# --------------------------------------------------------------------------
# Error copy, actor shape, and the context's closed field set
# --------------------------------------------------------------------------


def test_user_error_header_is_visible_on_plugin_routes(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    client = _client(tmp_path, monkeypatch)
    headers = _auth(client, "z00880088")
    response = client.get(f"{_MOUNT}/boom", headers=headers)
    assert response.status_code == 409
    assert response.headers.get("X-User-Message") == "1"
    assert response.json()["detail"] == "这项操作暂时无法完成"


@pytest.mark.parametrize("path", ["sync-401", "async-401"])
def test_handler_raised_401_becomes_424(
    tmp_path, monkeypatch, frozen_runtime_reset, path
):
    """A plugin's upstream credential must not log the user out of core.

    ``frontend/app/errors.ts`` treats 401 as "your session died": it clears the
    stored token and reloads. So a 401 that a plugin handler raised — because
    *its* upstream said no — is translated to 424 with user copy. Both handler
    flavours are covered: the sync branch runs in a threadpool, the async one on
    the event loop, and they are separate wrappers.
    """

    client = _client(tmp_path, monkeypatch, factory_src=_UPSTREAM_401_SRC)
    headers = _auth(client, "z00151015")
    response = client.get(f"{_MOUNT}/{path}", headers=headers)
    assert response.status_code == 424, response.text
    assert response.headers.get("X-User-Message") == "1"
    assert response.json()["detail"] == "扩展服务的上游认证失败，请联系管理员"
    # The upstream's own wording never reaches the browser.
    assert "upstream" not in response.text


def test_handler_raised_starlette_401_becomes_424(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    """A 401 raised as ``starlette.exceptions.HTTPException`` translates too
    (codex #578 R6 P1).

    ``fastapi.HTTPException`` is a subclass of Starlette's, but nothing about
    that relationship is visible to a plugin author — a handler (or a library
    it depends on, e.g. an upstream HTTP client wrapper) can just as easily
    raise the Starlette base directly. Before this fix
    ``_wrap_handler_unauthorized``'s ``except`` clause named only the FastAPI
    subclass, so this exact request fell straight through unwrapped and the
    core client would have cleared the user's token and reloaded — the
    precise failure the mount seam exists to prevent for *any* plugin 401,
    not only the FastAPI-shaped ones.
    """

    client = _client(tmp_path, monkeypatch, factory_src=_UPSTREAM_401_SRC)
    headers = _auth(client, "z00151016")
    response = client.get(f"{_MOUNT}/starlette-401", headers=headers)
    assert response.status_code == 424, response.text
    assert response.headers.get("X-User-Message") == "1"
    assert response.json()["detail"] == "扩展服务的上游认证失败，请联系管理员"
    # The upstream's own wording never reaches the browser.
    assert "upstream" not in response.text


@pytest.mark.parametrize("path", ["returned-401-json", "returned-401-bare"])
def test_handler_returned_401_becomes_424(
    tmp_path, monkeypatch, frozen_runtime_reset, path
):
    """A *returned* 401 gets the same treatment as a raised one (codex #578 R3 P2).

    A plugin proxying an upstream service commonly copies the upstream's status
    straight onto its own ``Response``/``JSONResponse`` and returns it, rather
    than raising an ``HTTPException`` — that never enters
    ``_wrap_handler_unauthorized``'s ``except`` clause at all, so before this
    fix it reached the browser untouched and the core client would clear the
    token and reload. Both response flavours are covered: a ``JSONResponse``
    carrying an upstream-shaped body, and a bare ``Response`` with none.
    """

    client = _client(tmp_path, monkeypatch, factory_src=_UPSTREAM_401_SRC)
    headers = _auth(client, "z00151115")
    response = client.get(f"{_MOUNT}/{path}", headers=headers)
    assert response.status_code == 424, response.text
    assert response.headers.get("X-User-Message") == "1"
    assert response.json()["detail"] == "扩展服务的上游认证失败，请联系管理员"
    # The upstream's own wording never reaches the browser.
    assert "upstream" not in response.text


@pytest.mark.parametrize(
    "path", ["dep-401", "dep-async-401", "dep-starlette-401", "dep-nested-401"]
)
def test_dependency_raised_401_becomes_424(
    tmp_path, monkeypatch, frozen_runtime_reset, path
):
    """A 401 from the plugin's own ``Depends(...)`` translates too (codex #578 R7 P1).

    Checking an upstream inside a dependency is the natural way to write it —
    one guard, reused by every route in the plugin's router. FastAPI solves
    dependencies before it ever calls ``run_endpoint_function``, so wrapping
    only the endpoint's ``dependant.call`` meant this 401 never entered the
    translation at all: it reached the browser verbatim and
    ``frontend/app/errors.ts`` cleared the user's token and reloaded — the whole
    product logged out over one plugin's upstream credential.

    All four flavours are covered because they are four different code paths:
    sync and async wrappers are separate closures, the Starlette base class is a
    different ``except`` target than FastAPI's subclass, and ``dep-nested-401``
    raises two levels down (a plugin guard composed of another plugin guard),
    which only the transitive walk reaches.
    """

    client = _client(tmp_path, monkeypatch, factory_src=_DEPENDENCY_401_SRC)
    headers = _auth(client, "z00171017")
    response = client.get(f"{_MOUNT}/{path}", headers=headers)
    assert response.status_code == 424, response.text
    assert response.headers.get("X-User-Message") == "1"
    assert response.json()["detail"] == "扩展服务的上游认证失败，请联系管理员"
    # The upstream's own wording never reaches the browser.
    assert "upstream" not in response.text


def test_dependency_raised_403_passes_through_untouched(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    """Only 401 is rewritten in a dependency, exactly as in a handler.

    Non-vacuity for the parametrized test above: a wrapper that turned *every*
    ``HTTPException`` into a 424 would satisfy it.
    """

    client = _client(tmp_path, monkeypatch, factory_src=_DEPENDENCY_401_SRC)
    headers = _auth(client, "z00181018")
    response = client.get(f"{_MOUNT}/dep-403", headers=headers)
    assert response.status_code == 403, response.text
    assert response.json()["detail"] == "upstream says forbidden"
    assert "X-User-Message" not in response.headers


def test_core_gates_mounted_by_a_plugin_are_never_translated(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    """Core's own dependencies keep both of their real answers (codex #578 R7 P1).

    The dependency walk has to distinguish "the plugin's upstream said no" from
    "core said no", because they call for opposite browser behaviour: the first
    must not log the user out, the second must. Both of core's answers are
    checked on a route the *plugin* mounted the gate on — 401 for no session
    (the gate's own ``get_current_user``) and 404 for a user who cannot read the
    notebook — since that is where a too-eager exclusion rule would show up.
    """

    client = _client(tmp_path, monkeypatch, factory_src=_DEPENDENCY_401_SRC)
    owner = _auth(client, "z00191019")
    notebook = _notebook(client, owner)

    anonymous = client.get(f"{_MOUNT}/notebooks/{notebook}/core-gated")
    assert anonymous.status_code == 401, anonymous.text
    bad_token = client.get(
        f"{_MOUNT}/notebooks/{notebook}/core-gated",
        headers={"Authorization": "Bearer nope"},
    )
    assert bad_token.status_code == 401, bad_token.text

    stranger = _auth(client, "z00201020")
    refused = client.get(
        f"{_MOUNT}/notebooks/{notebook}/core-gated", headers=stranger
    )
    assert refused.status_code == 404, refused.text
    assert refused.json()["detail"] == "Notebook not found"

    # Non-vacuity: the route works for someone who may read the notebook, so
    # the two refusals above are the gate talking and not a broken route.
    allowed = client.get(f"{_MOUNT}/notebooks/{notebook}/core-gated", headers=owner)
    assert allowed.status_code == 200, allowed.text
    assert allowed.json() == {"notebook_id": notebook}


def test_core_dependencies_are_classified_out_of_the_plugin_owned_set(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    """The exclusion is asserted structurally, because HTTP cannot see it.

    Measured, not assumed (codex #578 R7): deleting the core-dependency
    exclusion does **not** change a single HTTP response today, for two
    independent and entirely incidental reasons —

    * ``get_current_user`` is an *async generator* dependency (it yields so it
      can reset the request-user ContextVar), so
      ``_install_unauthorized_translation`` already refuses it on the generator
      branch, which runs before the exclusion is ever consulted;
    * the notebook gates refuse with 404, and ``_translate_unauthorized``
      rewrites only 401.

    Both are properties of how core happens to be written this week, not of
    what the exclusion promises. Turn ``get_current_user`` into a plain
    ``async def`` — a plausible refactor, since the ContextVar reset could move
    to middleware — and the generator branch stops covering it; from that
    commit on, the exclusion is the only thing standing between an expired
    session and a 424 that never logs anybody out. So the guarantee is pinned
    here, where it is visible, rather than resting on those two coincidences.

    The route is the plugin's own ``core-gated`` one: the gate is mounted by
    plugin code, which is exactly the case a too-eager walk would swallow.

    ⚠ The assertion is **identity**, never name. Wrapping replaces
    ``Dependant.call``, and the replacement carries ``@wraps(inner)`` — so it
    reports the wrapped function's ``__name__``, ``__qualname__`` and
    ``__module__`` as its own, and even ``repr()`` renders it as
    ``<function require_notebook_read at ...>``. Anything that compares names,
    modules or reprs would pass while looking at the wrapper. Only ``is`` /
    ``in`` against the module attribute can tell "core's gate" from "a
    translation wrapper impersonating core's gate", and staying untouched is
    exactly what the exclusion promises.
    """

    from app.api import deps as core_deps
    from app.api.extension_routes import _dependant_calls

    client = _client(tmp_path, monkeypatch, factory_src=_DEPENDENCY_401_SRC)
    route = next(
        route
        for route in client.app.routes
        if isinstance(route, APIRoute) and route.path.endswith("/core-gated")
    )

    # Read *after* mounting, so this reflects the post-wrap tree: a core
    # dependency is still present as itself if and only if it was not wrapped.
    reachable = _dependant_calls(route.dependant)
    assert core_deps.get_current_user in reachable
    assert core_deps.require_notebook_read in reachable


def test_plugin_owned_dependants_splits_core_from_plugin():
    """The classifier itself, on a tree that has never been wrapped.

    The mounted-route test above can only observe the decision through its
    after-effect, and every callable in that tree is post-wrap. This one calls
    the classifier directly on a freshly solved dependant, so "core out, plugin
    in" is read straight off the return value — and both halves are asserted,
    because a rule that excluded *everything* would satisfy the exclusion on
    its own.

    The route is built here rather than in a plugin package on purpose: the
    classifier takes a dependant, not a plugin, so this needs no app, no
    fixture and no config file.
    """

    from fastapi import FastAPI

    from app.api import deps as core_deps
    from app.api.extension_routes import (
        _core_dependency_calls,
        _plugin_owned_dependants,
    )

    def innermost_gate():  # pragma: no cover - never called
        return None

    def middle_gate(inner=Depends(innermost_gate)):  # pragma: no cover
        return inner

    router = APIRouter()

    @router.get(
        "/notebooks/{notebook_id}/mixed",
        dependencies=[Depends(core_deps.require_notebook_read)],
    )
    def mixed(notebook_id: str, value=Depends(middle_gate)):  # pragma: no cover
        return {}

    app = FastAPI()
    app.include_router(
        router, dependencies=[Depends(core_deps.get_current_user)]
    )
    route = next(r for r in app.routes if isinstance(r, APIRoute))

    owned = {
        node.call
        for node in _plugin_owned_dependants(
            route.dependant, _core_dependency_calls()
        )
    }
    assert owned == {middle_gate, innermost_gate}
    assert core_deps.require_notebook_read not in owned
    assert core_deps.get_current_user not in owned


def test_declared_in_core_reads_the_defining_module():
    """The module half of the double check, unit-tested on each shape it handles.

    It exists because the identity set in :func:`_core_dependency_calls` is
    exact only for the dependencies core *hands* a plugin; a plugin composing a
    guard out of some other core helper would produce a core-owned callable
    that is not one of those objects.
    """

    from app.api import deps as core_deps
    from app.api.extension_routes import _declared_in_core, plugin_actor

    assert _declared_in_core(core_deps.require_notebook_read)
    assert _declared_in_core(core_deps.get_current_user)
    assert _declared_in_core(plugin_actor)

    def plugin_side_guard():  # pragma: no cover - never called
        return None

    plugin_side_guard.__module__ = "corp_plugin.guards"
    assert not _declared_in_core(plugin_side_guard)

    # A callable *instance* resolves to where its class was defined, not to
    # ``builtins`` — the branch that reads ``type(call).__module__``.
    class PluginGuard:
        def __call__(self):  # pragma: no cover - never called
            return None

    PluginGuard.__module__ = "corp_plugin.guards"
    assert not _declared_in_core(PluginGuard())
    CoreGuard = type("CoreGuard", (PluginGuard,), {"__module__": "app.api.deps"})
    assert _declared_in_core(CoreGuard())

    # A name that merely starts with the same letters is not the core package.
    plugin_side_guard.__module__ = "apparel.guards"
    assert not _declared_in_core(plugin_side_guard)


def _module_of(call) -> str:
    module = getattr(call, "__module__", None)
    return module if isinstance(module, str) else type(call).__module__


def test_core_dependency_exclusions_are_derived_not_hand_listed():
    """The excluded set is core's live gate table plus the two session seams.

    ``_notebook_gates`` is already derived from ``deps._CAPABILITY_LEVELS``, so
    asserting equality here keeps a future capability from quietly falling
    outside the exclusion and having its 404-shaped guard wrapped.
    """

    from app.api import deps as core_deps
    from app.api.extension_routes import (
        _core_dependency_calls,
        _notebook_gates,
        plugin_actor,
    )

    assert _core_dependency_calls() == _notebook_gates() | {
        core_deps.get_current_user,
        plugin_actor,
    }
    # Non-vacuity: the gate table is not empty, so the union above says
    # something.
    assert _notebook_gates()
    assert all(
        _module_of(call).startswith("app.") for call in _core_dependency_calls()
    )


def test_a_real_missing_session_is_still_401(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    """The translation is scoped to the handler, so core's own 401 survives.

    The router-level ``Depends(get_current_user)`` raises before the endpoint is
    ever called, so it is outside the wrapper — which is the whole reason the
    wrapper sits at ``dependant.call`` rather than around the route handler.
    Losing this distinction would mean an expired session stopped logging the
    user out.
    """

    client = _client(tmp_path, monkeypatch, factory_src=_UPSTREAM_401_SRC)
    assert client.get(f"{_MOUNT}/sync-401").status_code == 401
    assert (
        client.get(
            f"{_MOUNT}/sync-401", headers={"Authorization": "Bearer nope"}
        ).status_code
        == 401
    )


def test_other_plugin_statuses_pass_through_untouched(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    """Only 401 is rewritten; a plugin's 403 stays a 403 with its own detail.

    Covers both exception flavours: catching the wider Starlette base class
    (codex #578 R6 P1) must not turn into catching *every* status that base
    class can carry — a Starlette-raised 403 has to pass through exactly like
    a FastAPI-raised one.
    """

    client = _client(tmp_path, monkeypatch, factory_src=_UPSTREAM_401_SRC)
    headers = _auth(client, "z00161016")
    response = client.get(f"{_MOUNT}/sync-403", headers=headers)
    assert response.status_code == 403
    assert response.json()["detail"] == "upstream says forbidden"
    assert "X-User-Message" not in response.headers

    starlette_response = client.get(f"{_MOUNT}/starlette-403", headers=headers)
    assert starlette_response.status_code == 403
    assert starlette_response.json()["detail"] == "upstream says forbidden"
    assert "X-User-Message" not in starlette_response.headers

    # A *returned* non-401 status must not be mistaken for the returned-401
    # check either: only ``status_code == 401`` triggers translation.
    returned = client.get(f"{_MOUNT}/returned-403-json", headers=headers)
    assert returned.status_code == 403
    assert returned.json()["detail"] == "upstream forbidden"
    assert "X-User-Message" not in returned.headers


def test_upstream_401_is_counted_on_the_event_sink():
    """One whitelisted code, no upstream text, no notebook or user identity."""

    from fastapi import HTTPException

    from app.api.extension_routes import _translate_unauthorized

    log = _RecordingLog()
    emit = _event_emitter(_PLUGIN_ID, log)
    translated = _translate_unauthorized(
        HTTPException(status_code=401, detail="token abc123 expired"), emit
    )
    assert translated is not None and translated.status_code == 424
    assert log.records == [
        {
            "event": "plugin_upstream_unauthorized",
            "kind": "extension_plugin",
            "plugin_id": _PLUGIN_ID,
        }
    ]

    assert _translate_unauthorized(HTTPException(status_code=403), emit) is None
    assert len(log.records) == 1


def test_upstream_401_response_is_counted_on_the_event_sink():
    """Sibling of the raised-401 event-sink test, for a *returned* 401.

    Same whitelisted event, same absence of upstream text or identity — a
    plugin cannot avoid being counted by returning the status instead of
    raising it.
    """

    from starlette.responses import Response

    from app.api.extension_routes import _translate_unauthorized_response

    log = _RecordingLog()
    emit = _event_emitter(_PLUGIN_ID, log)
    translated = _translate_unauthorized_response(Response(status_code=401), emit)
    assert translated is not None and translated.status_code == 424
    assert log.records == [
        {
            "event": "plugin_upstream_unauthorized",
            "kind": "extension_plugin",
            "plugin_id": _PLUGIN_ID,
        }
    ]

    assert (
        _translate_unauthorized_response(Response(status_code=403), emit) is None
    )
    # Not a Response at all: the value most handler returns actually are
    # before FastAPI serializes them, and must never be mistaken for one.
    assert _translate_unauthorized_response({"status_code": 401}, emit) is None
    assert len(log.records) == 1


def test_plugin_actor_is_narrow(tmp_path, monkeypatch, frozen_runtime_reset):
    assert {field.name for field in dataclasses.fields(PluginActor)} == {
        "id",
        "is_admin",
    }

    client = _client(tmp_path, monkeypatch)
    body = client.get(f"{_MOUNT}/ping", headers=_auth(client, "z00990099")).json()
    assert body["actor_id"].startswith("user-")
    assert body["is_admin"] is False

    admin_headers = _auth_admin(client)
    admin_body = client.get(f"{_MOUNT}/ping", headers=admin_headers).json()
    assert admin_body["is_admin"] is True


def test_plugin_cannot_reach_repository_or_settings_through_the_context(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    """The context is exactly nine seams, and no core object hides behind one."""

    from app.api import deps
    from app.core.config import Settings
    from app.repositories.ports import NotebookRepository

    client = _client(tmp_path, monkeypatch)
    _auth(client, "z00101010")  # force a request so the repository exists
    context = _seen_context()

    assert {field.name for field in dataclasses.fields(PluginRouteContext)} == {
        "plugin_id",
        "settings",
        "require_notebook_capability",
        "require_notebook_read",
        "current_actor",
        "user_error",
        "url_sources",
        "task_stream",
        "emit_event",
    }
    # Frozen: a plugin cannot swap a seam out from under core after the fact.
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.plugin_id = "other"

    repository = deps.repository()
    reachable = _reachable_from(context)
    # Non-vacuity: the walk must actually be walking. The event sink is a
    # closure over an EventLogger, so a scan that only looked at the eight
    # top-level seams would never see one.
    from app.core.event_logging import EventLogger

    assert any(isinstance(obj, EventLogger) for obj in reachable)
    for obj in reachable:
        assert obj is not repository
        assert not isinstance(obj, (Settings, NotebookRepository)), type(obj).__name__
    # This plugin declares no settings_model, so its slot is None — not a
    # Settings object, and not core configuration of any kind.
    assert context.settings is None
    assert not hasattr(context, "repository")


# --------------------------------------------------------------------------
# The request-local task stream port
# --------------------------------------------------------------------------


def test_plugin_task_stream_uses_shared_ndjson_and_redacts_worker_failures(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    client = _client(tmp_path, monkeypatch, factory_src=_TASK_STREAM_SRC)
    headers = _auth(client, "z00101515")

    response = client.post(f"{_MOUNT}/search-task", headers=headers)
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("application/x-ndjson")
    assert response.headers["cache-control"] == "no-cache, no-transform"
    assert response.headers["x-accel-buffering"] == "no"
    events = [json.loads(line) for line in response.text.splitlines()]
    assert events == [
        {
            "event": "started",
            "stage": "extension.corp.sample.candidate_search",
            "elapsed_ms": 0,
        },
        {
            "event": "final",
            "stage": "extension.corp.sample.candidate_search",
            "result": {"ok": True, "cancelled": False},
        },
    ]

    failed = client.post(f"{_MOUNT}/failed-task", headers=headers)
    assert failed.status_code == 200
    failed_events = [json.loads(line) for line in failed.text.splitlines()]
    assert failed_events[-1] == {
        "event": "error",
        "stage": "extension.corp.sample.candidate_search",
        "error": "extension_task_failed",
    }
    assert "secret-upstream-message" not in failed.text


def test_plugin_task_stream_refuses_free_text_stage_and_async_work():
    adapter = _PluginTaskStreamAdapter(_PLUGIN_ID)
    request = Request({
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/",
        "raw_path": b"/",
        "query_string": b"",
        "headers": [],
        "client": None,
        "server": None,
    })

    with pytest.raises(TypeError, match="stable lowercase code"):
        adapter.response(request, lambda _cancel: {}, stage="用户问题 search")

    async def async_work(_cancel):
        return {}

    with pytest.raises(TypeError, match="must be synchronous"):
        adapter.response(request, async_work, stage="candidate_search")


# --------------------------------------------------------------------------
# The URL import port
# --------------------------------------------------------------------------


def test_url_import_reuses_core_capacity_and_scheduler_semantics(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    """Per-URL capacity accounting and background scheduling, unmodified.

    The limit is set to 2 and three probe-passing URLs are submitted: core's
    rule is that the first two are created and the third is *rejected* rather
    than the batch failing, and that each created source is handed to the
    background parse scheduler.
    """

    import app.api.source_routes as source_routes_module
    from app.services import remote_sources
    from app.services.remote_sources import PdfProbe

    client = _client(
        tmp_path,
        monkeypatch,
        env={"MINERU_API_TOKEN": "tok", "USER_UPLOAD_DOCUMENT_LIMIT": "2"},
    )
    monkeypatch.setattr(
        remote_sources, "probe_pdf", lambda url, **kw: PdfProbe(True, "", 1, "d.pdf")
    )
    scheduled: list[str] = []
    monkeypatch.setattr(
        source_routes_module.kg_scheduler,
        "submit_job",
        lambda fn, source_id, *a, **k: scheduled.append(source_id),
    )

    headers = _auth(client, "z00111011")
    notebook_id = _notebook(client, headers)
    response = client.post(
        f"{_MOUNT}/notebooks/{notebook_id}/import",
        json={"urls": ["https://a/1.pdf", "https://a/2.pdf", "https://a/3.pdf"]},
        headers=headers,
    )
    assert response.status_code == 200, response.text
    body = response.json()
    assert len(body["created"]) == 2
    assert len(body["rejected"]) == 1
    assert "文档数量上限" in body["rejected"][0]["reason"]
    assert [row["url"] for row in body["created"]] == [
        "https://a/1.pdf",
        "https://a/2.pdf",
    ]
    assert len(scheduled) == 2
    assert sorted(scheduled) == sorted(row["source_id"] for row in body["created"])


def test_url_import_maps_unconfigured_parser_to_400(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    """Core's own mapping, verbatim: a 400 whose detail is *not* user copy.

    ``MinerUCloudNotConfigured`` carries deployment-configuration wording, so
    core raises a plain ``HTTPException`` rather than ``user_error`` — no
    ``X-User-Message``. A plugin route must inherit that exactly, not dress the
    failure up as something the end user can act on.
    """

    client = _client(tmp_path, monkeypatch)
    headers = _auth(client, "z00122012")
    notebook_id = _notebook(client, headers)
    response = client.post(
        f"{_MOUNT}/notebooks/{notebook_id}/import",
        json={"urls": ["https://a/d.pdf"]},
        headers=headers,
    )
    assert response.status_code == 400
    assert "X-User-Message" not in response.headers


# --------------------------------------------------------------------------
# The URL import port from an async handler (codex #578 R4 P1)
# --------------------------------------------------------------------------


@pytest.fixture
def async_port(tmp_path, monkeypatch, frozen_runtime_reset):
    """One app whose plugin reaches the import port from four handler shapes.

    The spy wrapped around core's own ``import_url_sources`` is what makes the
    offload observable at all: it records, at the moment the blocking work
    actually starts, which thread is running it and whether that thread has a
    running event loop. It delegates to the real function afterwards, so the
    source still lands and the "did it work" assertions stay non-vacuous.
    """

    import app.api.source_routes as source_routes_module
    from app.services import remote_sources
    from app.services.remote_sources import PdfProbe

    client = _client(
        tmp_path,
        monkeypatch,
        factory_src=_ASYNC_PORT_SRC,
        env={"MINERU_API_TOKEN": "tok"},
    )
    monkeypatch.setattr(
        remote_sources, "probe_pdf", lambda url, **kw: PdfProbe(True, "", 1, "d.pdf")
    )
    monkeypatch.setattr(
        source_routes_module.kg_scheduler, "submit_job", lambda fn, *a, **k: None
    )

    imports: list[dict] = []
    real_import = source_routes_module.import_url_sources

    def spy(notebook_id, urls):
        try:
            asyncio.get_running_loop()
            on_loop = True
        except RuntimeError:
            on_loop = False
        imports.append({"thread": threading.current_thread(), "on_loop": on_loop})
        return real_import(notebook_id, urls)

    monkeypatch.setattr(source_routes_module, "import_url_sources", spy)
    return client, _grant_world(client, "w"), imports


def _handler_threads() -> list:
    return sys.modules[_module_name(_PLUGIN_ID)].HANDLER_THREADS


def test_an_async_handler_calling_the_sync_port_is_refused(async_port):
    """``import_urls`` from an ``async def`` handler raises, and imports nothing.

    The blocking call would otherwise run on the event loop thread — one
    unreachable URL there stalls every other in-flight request in the process.
    The refusal happens before any work, so nothing is half-done, and the
    message names the method to await instead: a plugin author reading the
    traceback gets the fix, not just a diagnosis.
    """

    client, world, imports = async_port
    with pytest.raises(RuntimeError) as excinfo:
        client.post(
            f"{_MOUNT}/notebooks/{world['notebook']}/import-sync-from-async",
            json={"urls": ["https://a/d.pdf"]},
            headers=world["owner"],
        )
    message = str(excinfo.value)
    assert "must not be called from an async handler" in message
    assert "import_urls_async" in message
    assert imports == []
    assert _source_count(client, world["notebook"], world["owner"]) == 0


def test_the_refusal_surfaces_as_a_server_error_not_as_user_copy(async_port):
    """It is a plugin wiring bug, so it must not look like something the user did.

    A plain 500 with no ``X-User-Message`` (the header core attaches to
    user-facing copy) and no notebook id echoed into the body: the actionable
    text belongs in the traceback an operator reads, not in the response an
    end user reads.
    """

    client, world, _ = async_port
    quiet = TestClient(client.app, raise_server_exceptions=False)
    response = quiet.post(
        f"{_MOUNT}/notebooks/{world['notebook']}/import-sync-from-async",
        json={"urls": ["https://a/d.pdf"]},
        headers=world["owner"],
    )
    assert response.status_code == 500
    assert "X-User-Message" not in response.headers
    assert world["notebook"] not in response.text


def test_async_import_lands_the_source_for_the_owner(async_port):
    """The awaited variant does the same work and the source actually lands.

    Non-vacuity for the two tests below: a port that refused everyone, or an
    offload that lost the request context on the way into the worker thread,
    would 404 here — the owner's own capability check reads the user out of
    core's request context, which only exists in that thread if it was copied.
    """

    client, world, imports = async_port
    response = client.post(
        f"{_MOUNT}/notebooks/{world['notebook']}/import-async",
        json={"urls": ["https://a/1.pdf"]},
        headers=world["owner"],
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["created"]) == 1
    assert _source_count(client, world["notebook"], world["owner"]) == 1
    assert len(imports) == 1


def test_the_offloaded_import_leaves_the_event_loop_thread(async_port):
    """The point of the whole change: the blocking work runs somewhere else.

    Two independent readings, because either alone can be satisfied by
    accident: the worker has no running event loop, *and* it is a different
    thread object from the one the async handler body itself ran on. Calling
    the sync implementation directly instead of hopping to the threadpool
    fails both.
    """

    client, world, imports = async_port
    _handler_threads().clear()
    response = client.post(
        f"{_MOUNT}/notebooks/{world['notebook']}/import-async",
        json={"urls": ["https://a/1.pdf"]},
        headers=world["owner"],
    )
    assert response.status_code == 200, response.text
    assert len(imports) == 1
    assert imports[0]["on_loop"] is False
    assert len(_handler_threads()) == 1
    assert imports[0]["thread"] is not _handler_threads()[0]


def test_async_import_still_authorizes_the_request_user(async_port):
    """A reader is refused by the *port*, on the offloaded path too.

    The route is ungated on purpose — the ``{notebook_id}`` shape rule never
    fires for ``/n/{nb}/…`` — so the 404 can only come from the port checking
    ``sources:write`` for the request's own user. Paired with
    ``test_async_import_lands_the_source_for_the_owner``: that one proves the
    request context reached the worker thread at all, this one proves what it
    carried is still being enforced there.
    """

    client, world, imports = async_port
    response = client.post(
        f"{_MOUNT}/n/{world['notebook']}/import-async-ungated",
        json={"urls": ["https://a/d.pdf"]},
        headers=world["reader"],
    )
    assert response.status_code == 404, response.text
    assert response.json()["detail"] == "Notebook not found"
    assert imports == []
    assert _source_count(client, world["notebook"], world["owner"]) == 0


def test_a_sync_handler_still_calls_the_sync_port(async_port):
    """The pre-existing shape is untouched: sync handler, blocking call, 200.

    FastAPI already runs ``def`` endpoints in its threadpool, so there is no
    loop on that thread and the new refusal never fires for them.
    """

    client, world, imports = async_port
    _handler_threads().clear()
    response = client.post(
        f"{_MOUNT}/notebooks/{world['notebook']}/import-sync",
        json={"urls": ["https://a/1.pdf"]},
        headers=world["owner"],
    )
    assert response.status_code == 200, response.text
    assert len(response.json()["created"]) == 1
    assert _source_count(client, world["notebook"], world["owner"]) == 1
    assert [row["on_loop"] for row in imports] == [False]
    # Same thread this time: the handler is already off the loop, so there is
    # no hop to make.
    assert imports[0]["thread"] is _handler_threads()[0]


# --------------------------------------------------------------------------
# Structural refusals
# --------------------------------------------------------------------------


def test_plugin_router_lifecycle_hooks_are_rejected(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    _configure(tmp_path, monkeypatch, factory_src=_LIFECYCLE_SRC)
    with pytest.raises(PluginRouteMountError) as excinfo:
        _create_app()
    assert excinfo.value.reason == "plugin_route_lifecycle_denied"


def test_non_apiroute_in_plugin_router_is_rejected(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    _configure(tmp_path, monkeypatch, factory_src=_NON_APIROUTE_SRC)
    with pytest.raises(PluginRouteMountError) as excinfo:
        _create_app()
    assert excinfo.value.reason == "plugin_route_unsupported_kind"


def test_factory_returning_a_non_router_is_rejected(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    _configure(tmp_path, monkeypatch, factory_src=_NOT_A_ROUTER_SRC)
    with pytest.raises(PluginRouteMountError) as excinfo:
        _create_app()
    assert excinfo.value.reason == "plugin_router_not_a_router"


# --------------------------------------------------------------------------
# Factory (and validation) failures are sanitized, not raised verbatim
# (codex #578 R1 P1)
# --------------------------------------------------------------------------


def test_factory_exception_is_sanitized_and_logged(
    tmp_path, monkeypatch, caplog, frozen_runtime_reset
):
    """A factory that leaks a settings secret into its own exception message.

    Mirrors ``test_extension_discovery.test_deployment_register_failure_is_sanitized_and_logged``
    one seam later: the same settings value, the same shape of failure, at
    router-factory time instead of registration time.
    """

    _configure_full_body(
        tmp_path,
        monkeypatch,
        body=_FACTORY_RAISES_WITH_SETTINGS_BODY,
        config_extra='[extensions."corp.sample".settings]\ntoken = "TOPSECRET"\n',
    )

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(PluginRouteMountError) as excinfo:
            _create_app()

    error = excinfo.value
    assert error.reason == "plugin_router_factory_failed"
    assert error.exception_type == "RuntimeError"
    assert error.plugin_id == _PLUGIN_ID
    message = str(error)
    assert "plugin_router_factory_failed" in message
    assert "RuntimeError" in message
    assert "TOPSECRET" not in message
    assert "TOPSECRET" not in repr(error)
    assert error.__cause__ is None
    assert error.__suppress_context__
    assert "TOPSECRET" not in caplog.text
    assert "reason=plugin_router_factory_failed" in caplog.text
    assert "exc=RuntimeError" in caplog.text


def test_factory_raising_the_mount_error_itself_passes_through_unwrapped(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    """Re-wrapping would replace the factory's own reason with a generic one."""

    _configure(tmp_path, monkeypatch, factory_src=_FACTORY_RAISES_MOUNT_ERROR_SRC)
    with pytest.raises(PluginRouteMountError) as excinfo:
        _create_app()
    assert excinfo.value.reason == "plugin_custom_denied"
    assert excinfo.value.exception_type == ""


def test_factory_raising_system_exit_is_not_caught(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    """``SystemExit`` must reach the interpreter, not become an HTTP failure."""

    _configure(tmp_path, monkeypatch, factory_src=_FACTORY_RAISES_SYSTEM_EXIT_SRC)
    with pytest.raises(SystemExit):
        _create_app()


def test_router_validation_exception_is_sanitized_and_logged(caplog):
    """A router property that raises is sanitized the same way a factory is.

    Exercised directly against ``_run_plugin_router_validation`` — a duck-typed
    stand-in for a plugin's ``APIRouter`` subclass is enough here, and it keeps
    this test independent of Starlette's actual attribute shapes for
    ``on_startup``/``on_shutdown``/``routes``.
    """

    class _ExplodingRouter:
        on_startup: tuple = ()
        on_shutdown: tuple = ()

        @property
        def routes(self):
            raise ValueError("router.routes exploded")

    with caplog.at_level(logging.DEBUG):
        with pytest.raises(PluginRouteMountError) as excinfo:
            _run_plugin_router_validation(_PLUGIN_ID, _ExplodingRouter())

    error = excinfo.value
    assert error.reason == "plugin_router_validation_failed"
    assert error.exception_type == "ValueError"
    assert error.plugin_id == _PLUGIN_ID
    assert "router.routes exploded" not in str(error)
    assert error.__cause__ is None
    assert error.__suppress_context__
    assert "router.routes exploded" not in caplog.text
    assert "reason=plugin_router_validation_failed" in caplog.text
    assert "exc=ValueError" in caplog.text


def test_router_validations_own_mount_error_passes_through_unwrapped():
    """``_validate_plugin_router``'s own refusals must not be re-wrapped."""

    with pytest.raises(PluginRouteMountError) as excinfo:
        _run_plugin_router_validation(_PLUGIN_ID, APIRouter(on_startup=[lambda: None]))
    assert excinfo.value.reason == "plugin_route_lifecycle_denied"
    assert excinfo.value.exception_type == ""


def test_two_router_contributions_from_one_plugin_are_rejected():
    with pytest.raises(ExtensionDiscoveryError) as excinfo:
        _collect(
            _collector_bundle(
                contribution_ids=("corp.sample.router", "corp.sample.router2")
            )
        )
    assert excinfo.value.reason == "plugin_router_multiple"
    assert excinfo.value.plugin_id == _PLUGIN_ID
    # A single declaration from the same bundle is accepted — otherwise the
    # assertion above would pass for a collector that rejects everything.
    specs = _collect(_collector_bundle())
    assert [spec.plugin_id for spec in specs] == [_PLUGIN_ID]
    assert specs[0].contribution_id == "corp.sample.router"
    assert specs[0].settings is None


def test_builtin_trust_may_not_contribute_an_http_router():
    with pytest.raises(ExtensionDiscoveryError) as excinfo:
        _collect(_collector_bundle(plugin_id="builtin.sample", trust="builtin"))
    assert excinfo.value.reason == "plugin_router_trust_denied"


def test_collector_rejects_a_non_contributor_declaration():
    with pytest.raises(ExtensionDiscoveryError) as excinfo:
        _collect(_collector_bundle(kind=ContributionKind.OBSERVER))
    assert excinfo.value.reason == "plugin_router_kind_invalid"


def test_collector_rejects_a_factory_that_is_not_callable():
    with pytest.raises(ExtensionDiscoveryError) as excinfo:
        _collect(_collector_bundle(implementation="not-a-factory"))
    assert excinfo.value.reason == "plugin_router_factory_invalid"


def test_collector_passes_the_plugins_validated_settings_through():
    """A ``None`` settings value means "declared no model", not "absent"."""

    from app.extensions.bootstrap import build_extension_runtime

    bundle = _collector_bundle()
    runtime = build_extension_runtime([bundle], plugin_settings={_PLUGIN_ID: None})
    from app.bootstrap import application_plugin_router_specs

    specs = application_plugin_router_specs(runtime)
    assert [spec.settings for spec in specs] == [None]

    instance = object()
    runtime = build_extension_runtime(
        [_collector_bundle()], plugin_settings={_PLUGIN_ID: instance}
    )
    assert application_plugin_router_specs(runtime)[0].settings is instance


def test_mounting_nothing_touches_no_application():
    """Empty specs return before constructing a context or an event logger."""

    mount_extension_routers(None, ())


# --------------------------------------------------------------------------
# The observability seam
# --------------------------------------------------------------------------


class _RecordingLog:
    def __init__(self) -> None:
        self.records: list[dict] = []

    def emit(self, event, **kwargs) -> None:
        self.records.append(event)


@pytest.mark.parametrize(
    "payload",
    [
        {"event": "ok", "notebook_id": "nb-1"},  # out-of-whitelist key
        {"event": "ok", "question": "谁是作者"},
        {"event": "Bad-Code"},  # not a stable code
        {"event": ""},
        {"outcome": "ok"},  # no event
        {"event": "ok", "count": -1},
        {"event": "ok", "count": 1_000_000_001},
        {"event": "ok", "count": True},  # bool is not a count
        {"event": "ok", "elapsed_ms": "12"},
        {"event": "ok", "outcome": 7},
        "not a mapping",
    ],
)
def test_plugin_event_sink_drops_out_of_whitelist_payloads(payload):
    """The whole record is dropped — never partially written, never raised."""

    log = _RecordingLog()
    emit = _event_emitter(_PLUGIN_ID, log)
    emit(payload)
    assert log.records == []


def test_plugin_event_sink_writes_the_whitelisted_shape():
    log = _RecordingLog()
    emit = _event_emitter(_PLUGIN_ID, log)
    emit({"event": "urls_imported", "outcome": "ok", "count": 2, "elapsed_ms": 12})
    assert log.records == [
        {
            "event": "urls_imported",
            "outcome": "ok",
            "count": 2,
            "elapsed_ms": 12,
            "kind": "extension_plugin",
            "plugin_id": _PLUGIN_ID,
        }
    ]


def test_plugin_event_sink_never_raises_into_the_plugin():
    class _Exploding:
        def emit(self, event, **kwargs):
            raise RuntimeError("log is broken")

    _event_emitter(_PLUGIN_ID, _Exploding())({"event": "ok"})
