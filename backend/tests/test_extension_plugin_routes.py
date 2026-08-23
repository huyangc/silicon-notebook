"""Deployment plugin HTTP routes: the mount seam, its gates, and its refusals.

Every fake plugin here is a real ``.py`` file imported off a real ``sys.path``
entry and loaded by real discovery — the shared helpers come from
``test_extension_discovery`` rather than being copied — and every mounted route
is exercised through a real ``create_app()`` + ``TestClient``. Monkeypatching
the mount would skip precisely the machinery under test: FastAPI's dependency
tree, the router-level session guard, and core's notebook guards.

See docs/superpowers/plans/2026-08-23-deployment-extensions-backend.md §3.3 and
主 agent 裁决 2 (``PluginRouteContext`` is eight fields, and the gate set that
``{notebook_id}`` routes are checked against includes core's *read* gate).
"""
from __future__ import annotations

import dataclasses
import sys
import textwrap

import pytest
from fastapi import APIRouter
from fastapi.testclient import TestClient

from app.api.extension_routes import (
    PluginRouteMountError,
    _event_emitter,
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
    id="corp.sample.router",
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


def _plugin_body(factory_src: str) -> str:
    """Wrap one ``build_router`` implementation in the standard bundle scaffold."""

    return _BODY_PREFIX + textwrap.dedent(factory_src).strip("\n") + "\n" + _BODY_SUFFIX


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


def _configure(tmp_path, monkeypatch, *, factory_src: str, env=None) -> None:
    """Write the plugin, point EXTENSIONS_CONFIG at it, and set a fresh env."""

    module = _write_plugin_package(tmp_path, body=_plugin_body(factory_src))
    monkeypatch.setenv("EXTENSIONS_CONFIG", _write_config(tmp_path, _entry(module)))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.delenv("MINERU_API_TOKEN", raising=False)
    for key, value in (env or {}).items():
        monkeypatch.setenv(key, value)
    _clear_caches()


def _create_app():
    from app.main import create_app

    return create_app()


def _client(tmp_path, monkeypatch, *, factory_src: str = _FULL_ROUTER_SRC, env=None):
    _configure(tmp_path, monkeypatch, factory_src=factory_src, env=env)
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
    """The context is exactly eight seams, and none of them is a core object."""

    from app.api import deps
    from app.core.config import Settings

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
        "emit_event",
    }
    # Frozen: a plugin cannot swap a seam out from under core after the fact.
    with pytest.raises(dataclasses.FrozenInstanceError):
        context.plugin_id = "other"

    repository = deps.repository()
    for field in dataclasses.fields(PluginRouteContext):
        value = getattr(context, field.name)
        assert value is not repository
        assert not isinstance(value, Settings)
        # No seam smuggles the repository or the settings out as an attribute.
        assert getattr(value, "_runtime", None) is None
    # This plugin declares no settings_model, so its slot is None — not a
    # Settings object, and not core configuration of any kind.
    assert context.settings is None
    assert not hasattr(context, "repository")


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
