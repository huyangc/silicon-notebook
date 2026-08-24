"""End-to-end: the arXiv sample plugin as a deployment would actually load it.

Everything here goes through the real machinery — a real TOML file, real
discovery, a real ``create_app()`` and a real ``TestClient`` — because that is
the only part T1–T3 could not cover.  ``test_arxiv_sample_plugin.py`` exercises
what the plugin *decides* against hand-built seams; this file exercises what
core does with it once the config file names it.  Monkeypatching the mount, the
registry, or the settings binding here would skip precisely the machinery under
test.

**Zero network, and that is asserted rather than assumed.**  The only seam that
is faked is ``client._fetch`` — the injectable transport the package ships for
exactly this purpose — and one test additionally makes ``socket.getaddrinfo``
raise for the duration of a full search round trip, so a future edit that
reintroduced a real dial would fail loudly instead of quietly reaching
arxiv.org from the test suite.  ``base_url`` in every config below points at a
``.example`` host, which is reserved and never resolves.

**Gap consultation stops at the host, on purpose.**  These tests take the
frozen ``GapConsultHost`` out of the runtime and call it with a real
``GapConsultCallContext``, rather than driving a whole reasoning Ask.  Core's
side of that wiring — when consultation is triggered, what may be sent, where
the result lands — is already pinned by ``test_gap_consult_ask_wiring.py``
(X9 PR-A).  What PR-B has to prove is the other half: that under a real load
this plugin is reachable by the host and answers honestly.  Assembling an Ask
fixture to re-prove core's half would be a slower copy of an existing test.

**Not marked slow.**  Each test boots an application, but measured serial
runtime for the whole file is ~2s — well inside G1's per-PR budget — so it
runs on every edit rather than waiting for G2's nightly pass. An earlier
revision of this file carried ``pytestmark = pytest.mark.slow`` on the
reasoning that "boots an application" implied G2-only cost; that reasoning
was never checked against a clock. T4 review measured it and moved the file
into G1 instead of adding a sixth marker (which would have meant touching
both shell lanes' ``-m`` expressions *and* the two literals pinned in
``test_test_architecture_policy.py`` for one sample). ``check_sample_plugin.sh``
(G2) still owns the separate, slower job of syncing this plugin into
``frontend/features/ui-plugins`` and exercising it end to end as a synced
package — that is a different kind of verification than "does this test file
run fast enough for G1", and is unaffected by this file's marker.
"""
from __future__ import annotations

import importlib
import json
import shutil
import socket
import sys
import textwrap
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.domain.extension_http import PLUGIN_ROUTE_PREFIX
from app.domain.gap_consult import GapConsultCallContext, GapConsultQuery
from tests.test_extension_discovery import (
    _plugin_import_isolation,  # noqa: F401 -- autouse pytest fixture, resolved by name
    frozen_runtime_reset,  # noqa: F401 -- pytest fixture, resolved by name
)

_REPO_ROOT = Path(__file__).resolve().parents[2]
_PLUGIN_ROOT = _REPO_ROOT / "examples" / "extensions" / "arxiv-search"
_PLUGIN_SRC = _PLUGIN_ROOT / "src"
_UI_MANIFEST = _PLUGIN_ROOT / "ui" / "arxiv-search" / "ui-plugin.json"
_SAMPLE_FEED = Path(__file__).parent / "fixtures" / "arxiv_atom_sample.xml"

_PACKAGE = "silicon_notebook_arxiv_search"
_PLUGIN_ID = "examples.arxiv_search"
_MOUNT = f"{PLUGIN_ROUTE_PREFIX}/{_PLUGIN_ID}"

# The endpoint every config below points at.  `.example` is reserved by
# RFC 2606 and never resolves, so a fetch that escaped the seam fails rather
# than reaching a real host.
_BASE_URL = "https://export.arxiv.example/api/query"
# Paired with the plugin's own `timeout_seconds` below: this plugin refuses a
# consultation it cannot finish inside core's deadline, and with the shipped
# defaults (3s politeness + 10s timeout + 0.25s margin against a 4s deadline)
# that refusal is permanent.  Setting both here is the end-to-end form of the
# "enabling consultation takes two settings" rule the README registers.
_GAP_CONSULT_TIMEOUT = "15.0"

if str(_PLUGIN_SRC) not in sys.path:
    sys.path.insert(0, str(_PLUGIN_SRC))


# --------------------------------------------------------------------------
# Isolation
# --------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _sample_package_isolation():
    """Restore the sample package's modules exactly as they were found.

    ``_plugin_import_isolation`` (imported above, autouse) restores ``sys.path``
    and drops the discovery tests' throwaway ``sn_test_plugin_*`` modules — it
    knows nothing about this package's name.  The out-of-repository test below
    deliberately imports a *second copy* of it from ``tmp_path``; without this
    fixture that copy would stay in ``sys.modules`` and every later test in the
    worker — including ``test_arxiv_sample_plugin.py``'s, which hold module
    references captured at import time — would be looking at a different object
    than the one they are configuring.
    """

    saved = {
        name: module
        for name, module in sys.modules.items()
        if name == _PACKAGE or name.startswith(f"{_PACKAGE}.")
    }
    yield
    for name in [
        name
        for name in sys.modules
        if name == _PACKAGE or name.startswith(f"{_PACKAGE}.")
    ]:
        sys.modules.pop(name, None)
    sys.modules.update(saved)
    importlib.invalidate_caches()


@pytest.fixture(autouse=True)
def _reset_throttle():
    """The politeness throttle is module state shared with the unit tests."""

    client = importlib.import_module(f"{_PACKAGE}.client")
    client._LAST_REQUEST_AT = None
    yield
    if client._THROTTLE.locked():
        client._THROTTLE.release()
    client._LAST_REQUEST_AT = None


# --------------------------------------------------------------------------
# Fixtures and helpers
# --------------------------------------------------------------------------


class _FetchSpy:
    """Stand in for ``client._fetch`` and record every call it receives."""

    def __init__(self, payload: bytes = b"") -> None:
        self.payload = payload
        self.calls: list[tuple[str, float, str]] = []

    def __call__(self, url: str, timeout: float, user_agent: str) -> bytes:
        self.calls.append((url, timeout, user_agent))
        return self.payload


class _ConnectionProbe:
    """The core-owned "am I holding a database lease" probe, answering no."""

    def is_connection_held(self) -> bool:
        return False


def _clear_caches() -> None:
    from app.api import deps
    from app.core.config import get_settings
    from app.extensions.bootstrap import default_extension_runtime

    get_settings.cache_clear()
    default_extension_runtime.cache_clear()
    deps.repository.cache_clear()


def _config_text(*, consult_enabled: bool) -> str:
    # `politeness_interval_seconds = 0.0` is the one production default this
    # file deviates from, and only because honouring three seconds between
    # requests would make the suite sleep for no assertion's benefit. The
    # throttle itself is pinned by the unit tests; what is under test here is
    # the wiring around it.
    return textwrap.dedent(
        f"""
        [extensions."{_PLUGIN_ID}"]
        bundle = "{_PACKAGE}.bundle:BUNDLE"
        enabled = true

        [extensions."{_PLUGIN_ID}".settings]
        base_url = "{_BASE_URL}"
        max_results = 2
        politeness_interval_seconds = 0.0
        consult_enabled = {"true" if consult_enabled else "false"}
        """
    ).lstrip()


def _configure(
    tmp_path,
    monkeypatch,
    *,
    consult_enabled: bool = True,
    src: Path | None = None,
) -> None:
    """Write a real TOML, point ``EXTENSIONS_CONFIG`` at it, clear the caches.

    ``src`` names the directory the package is imported *from*, and defaults to
    the one in this repository.  The out-of-repository test hands its own copy
    and removes this one, which is the whole content of that test.
    """

    if src is not None:
        while str(_PLUGIN_SRC) in sys.path:
            sys.path.remove(str(_PLUGIN_SRC))
        for name in [
            name
            for name in sys.modules
            if name == _PACKAGE or name.startswith(f"{_PACKAGE}.")
        ]:
            sys.modules.pop(name, None)
        sys.path.insert(0, str(src))
        importlib.invalidate_caches()

    config = tmp_path / "extensions.toml"
    config.write_text(_config_text(consult_enabled=consult_enabled), encoding="utf-8")
    monkeypatch.setenv("EXTENSIONS_CONFIG", str(config))
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/t.db")
    monkeypatch.setenv("SILICON_NOTEBOOK_STORAGE_DIR", str(tmp_path / "storage"))
    monkeypatch.setenv("SILICON_NOTEBOOK_AUTH_OPTIONAL", "false")
    monkeypatch.setenv("EVENT_LOG_ENABLED", "false")
    monkeypatch.setenv("LLM_LOG_ENABLED", "false")
    monkeypatch.setenv("ASK_GAP_CONSULT_TIMEOUT_SECONDS", _GAP_CONSULT_TIMEOUT)
    monkeypatch.setenv("MINERU_API_TOKEN", "")
    _clear_caches()


def _client(tmp_path, monkeypatch, **kwargs) -> TestClient:
    _configure(tmp_path, monkeypatch, **kwargs)
    from app.main import create_app

    return TestClient(create_app())


def _auth(client: TestClient, username: str) -> dict[str, str]:
    client.post("/api/auth/register", json={"username": username, "password": "pw"})
    response = client.post(
        "/api/auth/login", json={"username": username, "password": "pw"}
    )
    return {"Authorization": f"Bearer {response.json()['token']}"}


def _notebook(client: TestClient, headers: dict[str, str]) -> str:
    response = client.post("/api/notebooks", json={"name": "n"}, headers=headers)
    assert response.status_code == 200, response.text
    return response.json()["id"]


def _install_fetch_spy(monkeypatch, payload: bytes = b"") -> _FetchSpy:
    """Replace the loaded package's transport, whichever copy that is.

    Resolved through ``sys.modules`` rather than a module-level import: the
    out-of-repository test swaps in a second copy of the package, and patching
    the import this file happened to bind first would leave the copy that is
    actually serving the request dialling for real.
    """

    spy = _FetchSpy(payload)
    monkeypatch.setattr(sys.modules[f"{_PACKAGE}.client"], "_fetch", spy)
    return spy


def _gap_context(*, question: str, gaps: tuple[str, ...] = ()) -> GapConsultCallContext:
    """A real call context with the deadline core would actually give.

    The deadline is derived from the live setting rather than a literal, so
    this file's ``ASK_GAP_CONSULT_TIMEOUT_SECONDS`` and the plugin's own
    ``timeout_seconds`` are pinned together: drop the former back to its 4.0s
    default and the contributor refuses (``arxiv_budget_too_small``) and these
    tests fail, which is the "two settings, not one" rule made mechanical.
    """

    from app.core.config import get_settings

    return GapConsultCallContext(
        query=GapConsultQuery(question=question, gaps=gaps, max_suggestions=3),
        cancellation=threading.Event(),
        connection_probe=_ConnectionProbe(),
        deadline_monotonic=(
            time.monotonic() + get_settings().ask_gap_consult_timeout_seconds
        ),
    )


def _gap_host():
    from app.extensions.bootstrap import default_extension_runtime

    return default_extension_runtime().gap_consult


def _stub_ingestion(monkeypatch) -> None:
    """The two seams core's own URL-import tests stub, for the same reasons.

    ``probe_pdf`` would make a real request, and ``submit_job`` would run a
    parse (and a model call) in a background thread.  Everything between the
    plugin's route and these two — the capability gate, the port's own
    authorization, the source row — stays real.
    """

    from app.api import source_routes
    from app.services import remote_sources
    from app.services.remote_sources import PdfProbe

    monkeypatch.setattr(
        remote_sources,
        "probe_pdf",
        lambda url, **kwargs: PdfProbe(True, "", 1, "paper.pdf"),
    )
    monkeypatch.setattr(
        source_routes.kg_scheduler, "submit_job", lambda fn, *a, **k: None
    )


# --------------------------------------------------------------------------
# The search route, over the mounted wire
# --------------------------------------------------------------------------


def test_search_chain_returns_fixture_papers_over_the_real_wire(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    client = _client(tmp_path, monkeypatch)
    spy = _install_fetch_spy(monkeypatch, _SAMPLE_FEED.read_bytes())
    headers = _auth(client, "x00110011")
    notebook_id = _notebook(client, headers)

    response = client.get(
        f"{_MOUNT}/notebooks/{notebook_id}/search",
        params={"q": "retrieval augmented generation"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    body = response.json()
    titles = [row["title"] for row in body["items"]]
    assert titles == [
        "Retrieval-Augmented Generation for Long Documents",
        "Graph Reasoning Without a Graph",
    ]
    # The entry with no id is dropped by the parser, so it never reaches here.
    assert body["start"] == 0
    assert body["has_more"] is False
    assert all(
        row["pdf_url"].startswith("https://arxiv.org/pdf/") for row in body["items"]
    )

    # Exactly one round trip, carrying this deployment's own configured
    # endpoint and user agent — the settings binding is real, not defaulted.
    assert len(spy.calls) == 1
    url, _timeout, user_agent = spy.calls[0]
    assert url.startswith(_BASE_URL + "?")
    assert user_agent.startswith("silicon-notebook-arxiv-sample/")


def test_search_route_is_behind_the_core_read_gate(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    """A stranger gets core's 404, and the plugin never dials for them."""

    client = _client(tmp_path, monkeypatch)
    spy = _install_fetch_spy(monkeypatch, _SAMPLE_FEED.read_bytes())
    owner = _auth(client, "x00220022")
    stranger = _auth(client, "x00330033")
    notebook_id = _notebook(client, owner)

    response = client.get(
        f"{_MOUNT}/notebooks/{notebook_id}/search",
        params={"q": "retrieval"},
        headers=stranger,
    )

    assert response.status_code == 404
    assert spy.calls == []


# --------------------------------------------------------------------------
# The import route, through core's own port
# --------------------------------------------------------------------------


def test_import_chain_creates_a_source_through_the_core_port(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    client = _client(tmp_path, monkeypatch)
    monkeypatch.setenv("MINERU_API_TOKEN", "tok")
    _clear_caches()
    _stub_ingestion(monkeypatch)
    headers = _auth(client, "x00440044")
    notebook_id = _notebook(client, headers)

    response = client.post(
        f"{_MOUNT}/notebooks/{notebook_id}/import",
        json={"urls": ["https://arxiv.org/pdf/2401.00001v1"]},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    created = response.json()["created"]
    assert len(created) == 1
    assert created[0]["url"] == "https://arxiv.org/pdf/2401.00001v1"
    assert created[0]["source_id"]

    # The row core made is visible through core's own endpoint: the plugin
    # asked the port, it did not write anything itself.
    listed = client.get(f"/api/notebooks/{notebook_id}/sources", headers=headers)
    assert listed.status_code == 200, listed.text
    assert len(listed.json()["items"]) == 1


def test_import_chain_refuses_a_foreign_host_end_to_end(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    """The allow-list runs before core's importer is reached, on the real wire.

    The spy on ``probe_pdf`` is what makes the ordering observable: it is the
    first thing core's URL import does with a URL, so a zero call count is
    proof the plugin refused before delegating rather than after.
    """

    from app.services import remote_sources
    from app.services.remote_sources import PdfProbe

    probes: list[str] = []

    def _probe(url: str, **kwargs):
        probes.append(url)
        return PdfProbe(True, "", 1, "paper.pdf")

    client = _client(tmp_path, monkeypatch)
    monkeypatch.setenv("MINERU_API_TOKEN", "tok")
    _clear_caches()
    monkeypatch.setattr(remote_sources, "probe_pdf", _probe)
    headers = _auth(client, "x00550055")
    notebook_id = _notebook(client, headers)

    response = client.post(
        f"{_MOUNT}/notebooks/{notebook_id}/import",
        json={"urls": ["https://arxiv.org.evil.example/pdf/2401.00001v1"]},
        headers=headers,
    )

    assert response.status_code == 400
    assert response.headers.get("X-User-Message")
    assert probes == []
    listed = client.get(f"/api/notebooks/{notebook_id}/sources", headers=headers)
    assert listed.json()["items"] == []


# --------------------------------------------------------------------------
# Gap consultation, through the frozen host
# --------------------------------------------------------------------------


def test_gap_consult_chain_yields_suggestions_from_the_frozen_host(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    _configure(tmp_path, monkeypatch, consult_enabled=True)
    host = _gap_host()
    spy = _install_fetch_spy(monkeypatch, _SAMPLE_FEED.read_bytes())
    events: list[dict[str, object]] = []

    suggestions = host.consult(
        _gap_context(
            question="检索增强生成在长文档上的效果如何？",
            gaps=("retrieval augmented generation",),
        ),
        event_sink=events.append,
    )

    assert [item.title for item in suggestions] == [
        "Retrieval-Augmented Generation for Long Documents",
        "Graph Reasoning Without a Graph",
    ]
    assert {item.source_label for item in suggestions} == {"arXiv"}
    assert all(item.url.startswith("https://arxiv.org/pdf/") for item in suggestions)
    assert len(spy.calls) == 1

    assert len(events) == 1
    assert events[0]["plugin_id"] == _PLUGIN_ID
    assert events[0]["contribution_id"] == f"{_PLUGIN_ID}.gap_consult"
    assert events[0]["status"] == "available"
    assert events[0]["count"] == 2


def test_gap_consult_is_silent_when_consult_is_disabled(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    """``consult_enabled = false`` is enforced by the contribution's own probe.

    The router is still mounted and the panel capability still resolves — that
    separation is the whole reason this plugin gates consultation on a
    per-contribution ``availability`` probe rather than on ``manifest.requires``
    — so this test also proves the plugin is loaded, not merely absent. "Still
    mounted" is checked mechanically, not just inferred from registry state: a
    second ``TestClient`` calls the router's own ``GET /health`` over the wire
    and gets a real 200 back.
    """

    _configure(tmp_path, monkeypatch, consult_enabled=False)
    host = _gap_host()
    spy = _install_fetch_spy(monkeypatch, _SAMPLE_FEED.read_bytes())
    events: list[dict[str, object]] = []

    suggestions = host.consult(
        _gap_context(
            question="检索增强生成在长文档上的效果如何？",
            gaps=("retrieval augmented generation",),
        ),
        event_sink=events.append,
    )

    assert suggestions == ()
    assert spy.calls == []
    assert len(events) == 1
    assert events[0]["status"] == "unavailable"
    assert events[0]["reason_code"] == "consult_disabled"
    assert events[0]["count"] == 0

    # Loaded, not absent: the same runtime still carries this plugin's router
    # contribution and its panel capability.
    from app.extensions.bootstrap import default_extension_runtime

    runtime = default_extension_runtime()
    assert _PLUGIN_ID in runtime.plugin_settings
    assert runtime.gap_consult.has_contributions() is True

    # Mechanically, not just by inspecting registry state: the router really
    # answers a real request over the wire.
    from app.main import create_app

    router_client = TestClient(create_app())
    headers = _auth(router_client, "x00880088")
    response = router_client.get(f"{_MOUNT}/health", headers=headers)
    assert response.status_code == 200, response.text
    assert response.json() == {"plugin_id": _PLUGIN_ID, "configured": True}


# --------------------------------------------------------------------------
# Cross-language manifest reconciliation
# --------------------------------------------------------------------------


def test_ui_manifest_matches_the_backend_manifest(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    """The browser looks the contribution up by (plugin_id, version, id).

    A single character of drift between the two files does not raise anywhere:
    the entry simply never renders, in every deployment, forever.  ``permission``
    is deliberately excluded — it is a browser-side field with no backend
    counterpart (see the UI package's own comment for why it reads
    ``notebook:write``).
    """

    _configure(tmp_path, monkeypatch)
    bundle = importlib.import_module(f"{_PACKAGE}.bundle")
    manifest = json.loads(_UI_MANIFEST.read_text(encoding="utf-8"))

    rows = manifest["contributions"]
    assert len(rows) == 1
    row = rows[0]
    panel = bundle.BUNDLE.manifest.ui_contributions[0]

    assert row["plugin_id"] == bundle.BUNDLE.manifest.id == _PLUGIN_ID
    assert row["version"] == bundle.BUNDLE.manifest.version
    assert row["id"] == panel.id
    assert row["capability"] == panel.capability
    assert row["slot"] == panel.slot
    # The capability the panel is gated on must be one the bundle can answer
    # for; a name the bundle does not supply a probe for is a startup failure.
    assert panel.capability in bundle.BUNDLE.capability_decisions


# --------------------------------------------------------------------------
# Zero-patch acceptance (machine half)
# --------------------------------------------------------------------------


def test_the_package_runs_from_outside_the_repository(
    tmp_path, monkeypatch, frozen_runtime_reset
):
    """Copy the whole package out, load it from there, and use it.

    This is the machine-checkable half of the zero-patch claim: nothing about
    loading or running this plugin depends on it sitting inside the checkout.
    The other half — a clean checkout, three environment variables, a green
    ``npm run build`` and a working panel — needs a second checkout and is
    transcribed in the pull request instead.

    The ``__file__`` assertions are load-bearing.  Without them this test
    passes just as happily while still importing the in-repository copy, which
    would make it a test of nothing at all.
    """

    outside = tmp_path / "outside"
    shutil.copytree(
        _PLUGIN_ROOT,
        outside,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )
    _configure(tmp_path, monkeypatch, src=outside / "src")
    from app.main import create_app

    client = TestClient(create_app())
    spy = _install_fetch_spy(monkeypatch, _SAMPLE_FEED.read_bytes())
    headers = _auth(client, "x00660066")
    notebook_id = _notebook(client, headers)

    response = client.get(
        f"{_MOUNT}/notebooks/{notebook_id}/search",
        params={"q": "retrieval"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert len(response.json()["items"]) == 2
    assert len(spy.calls) == 1

    # What was actually imported, and from where.
    copy_root = str(outside)
    for module_name in (_PACKAGE, f"{_PACKAGE}.bundle", f"{_PACKAGE}.client"):
        loaded = sys.modules[module_name]
        assert loaded.__file__ is not None
        assert loaded.__file__.startswith(copy_root), (
            f"{module_name} was imported from {loaded.__file__}, "
            "not from the copy outside the repository"
        )
    assert str(_PLUGIN_SRC) not in sys.path


# --------------------------------------------------------------------------
# No network
# --------------------------------------------------------------------------


def test_no_network_is_dialled(tmp_path, monkeypatch, frozen_runtime_reset):
    """Boot, sign in and search with name resolution disabled.

    ``socket.getaddrinfo`` is the narrowest chokepoint that every real outbound
    HTTP call in this path has to pass: ``TestClient`` speaks ASGI in-process,
    and SQLite is a file, so nothing legitimate in this chain resolves a name.
    Making it raise turns "we believe the seam covers it" into "the suite
    cannot reach the network even if the seam is removed".
    """

    def _refuse(*args, **kwargs):
        raise AssertionError("the test suite must not resolve a hostname")

    monkeypatch.setattr(socket, "getaddrinfo", _refuse)

    client = _client(tmp_path, monkeypatch)
    spy = _install_fetch_spy(monkeypatch, _SAMPLE_FEED.read_bytes())
    headers = _auth(client, "x00770077")
    notebook_id = _notebook(client, headers)

    response = client.get(
        f"{_MOUNT}/notebooks/{notebook_id}/search",
        params={"q": "retrieval"},
        headers=headers,
    )

    assert response.status_code == 200, response.text
    assert len(spy.calls) == 1
